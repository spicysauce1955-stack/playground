"""Post-provision egress isolation for local-libvirt labs (``isolate`` / ``connect``).

Enforcement is a libvirt nwfilter (``playground-isolate-<lab>``)
referenced from every lab VM interface that sits on a lab network.
libvirtd applies it on the host (ebtables layer, direction ``out`` =
traffic sent by the guest), so a VM cannot undo it, and because the
``<filterref>`` is written to the persistent domain config it survives
VM and host reboots. No sudo is needed: it is plain ``virsh`` against
``qemu:///system``.

Behaviour verified live against libvirt 10.0.0 (see CONTRACTS.md):

* ``nwfilter-define`` on an existing name fails with ``filter ... already
  exists with uuid ...`` unless the XML carries that UUID; we reuse it, or
  skip the call when the rules are unchanged. Redefining a filter that
  running domains reference updates them in place.
* ``update-device --live`` / ``--config`` adds or removes ``<filterref>``
  on a running domain; the binding shows in ``nwfilter-binding-list``.
  The live XML (target dev, portid) is used for ``--live`` and
  ``dumpxml --inactive`` for ``--config``.
* ``nwfilter-undefine`` of a referenced filter is refused ("nwfilter is
  in use"), so ``connect`` detaches first.

Limits worth knowing: ebtables-level rules can only DROP, so outside
connections time out instead of failing fast; DNS queries to the lab
gateway are dropped too (dnsmasq forwards upstream), so lab-internal names
stop resolving while isolated.

The host itself listens on the lab gateway IPs (swarm, rabbitmq, ...), so
guest->gateway traffic is cut statelessly down to DHCP renewal (udp dport
67), ICMP and SSH replies (tcp sport 22). Consequence: host->VM
connections other than SSH (e.g. curling a VM port directly from the
host) do not work while isolated; use an SSH tunnel. Exec, cp, ansible
and adb-over-ssh all ride sshd and keep working.

Scope: the subnet used in the rules is the one libvirt actually runs
(``net-dumpxml``); if it differs from the lab config the run fails with
``network_drift``. EVERY domain with an interface on a lab network is
isolated, declared in the lab or not (undeclared ones get a warning), and
the run fails (``non_lab_interface``) if such a domain also has an
interface elsewhere (another network, bridge, direct, ...): that would be
an unfiltered egress path. Subnet network/broadcast addresses are dropped
so a directed broadcast cannot reach host sockets bound to 0.0.0.0.

Threat model: a NON-ADVERSARIAL guest. The filter keeps well-behaved
software (and mistakes) off the internet and the host's services; it is
not a sandbox for a hostile root user. In particular the gateway accept on
tcp source port 22 is stateless, so a root guest could abuse it (e.g.
SNAT outbound traffic to source port 22 towards the gateway). Do not rely
on it to contain malicious code.

A pre-existing, non-playground ``<filterref>`` on a lab interface is
replaced (with a ``filter_replaced`` warning) because an interface takes
one filterref, and ``connect`` does NOT restore it. Lab VMs created by
tofu never carry one.

Every function returns instead of raising.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from playground.backend.local_libvirt.domains import VIRSH_URI
from playground.backend.local_libvirt.isolation_xml import (
    DomainInterface,
    binding_filter,
    filter_name,
    filter_uuid,
    filters_equivalent,
    foreign_attachments,
    interface_xml_with_filter,
    interface_xml_without_filter,
    interfaces_on,
    network_cidr_ipv4,
    network_gateway_ipv4,
    network_interfaces,
    parse_ipv4_network,
    render_filter_xml,
)
from playground.events import EventBus, JsonlWriter
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.runs import Operation, OperationRun, RunStatus, StepResult, finish_run, start_run

VirshRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
"""Runs ``virsh <args>`` (connection already bound) and returns the result.

Must not raise: a missing binary is exit 127, a timeout exit 124.
"""

_VIRSH_TIMEOUT = 30.0
_FILTER_NOT_FOUND = ("network filter not found", "failed to get nwfilter")
_DOMAIN_NOT_FOUND = ("domain not found", "failed to get domain")
_NETWORK_NOT_FOUND = ("network not found", "failed to get network")


def default_virsh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """``virsh --quiet -c qemu:///system <args>``; never raises."""
    cmd = ["virsh", "--quiet", "-c", VIRSH_URI, *args]
    if shutil.which("virsh") is None:
        return subprocess.CompletedProcess(cmd, 127, "", "virsh: command not found")
    try:
        return subprocess.run(  # noqa: S603 - explicit argv, no shell
            cmd, capture_output=True, text=True, check=False, timeout=_VIRSH_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"virsh timed out after {_VIRSH_TIMEOUT}s")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def execute_isolate(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
    virsh: VirshRunner | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Cut every lab VM's egress outside the lab's own networks. Never raises.

    Idempotent: an already-isolated lab succeeds without changing anything,
    and a half-isolated lab (crash between steps) is completed.
    """
    return _execute("isolate", resolved, state_dir, bus, virsh, _isolate)


def execute_connect(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
    virsh: VirshRunner | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Undo :func:`execute_isolate`. Never raises. Succeeds on a never-isolated lab."""
    return _execute("connect", resolved, state_dir, bus, virsh, _connect)


def remove_isolation_filter(lab_name: str, *, virsh: VirshRunner | None = None) -> list[Diagnostic]:
    """Best-effort: undefine the lab's nwfilter after its domains are gone.

    No-op when the filter is absent. Failures come back as warnings.
    """
    run = virsh or default_virsh
    name = filter_name(lab_name)
    result = run(["nwfilter-undefine", name])
    if result.returncode == 0 or _has(result, _FILTER_NOT_FOUND):
        return []
    return [
        Diagnostic(
            id="runtime.isolate.filter_cleanup_failed",
            severity="warning",
            message=(
                f"could not undefine nwfilter {name!r} (exit {result.returncode}): {_err(result)}"
            ),
            source=SourceLocation(path="host"),
            suggestion=f"remove it manually: `virsh -c {VIRSH_URI} nwfilter-undefine {name}`",
        )
    ]


# ---------------------------------------------------------------------------
# Run / step plumbing
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    op: Operation
    resolved: ResolvedLab
    bus: EventBus
    run_id: str
    logs_dir: Path
    xml_dir: Path
    virsh: VirshRunner
    steps: list[StepResult] = field(default_factory=list)
    diags: list[Diagnostic] = field(default_factory=list)
    # Filled by inspect-lab.
    present: list[str] = field(default_factory=list)
    running: set[str] = field(default_factory=set)
    cidrs: list[str] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)

    @property
    def lab(self) -> str:
        return self.resolved.lab_name

    @property
    def filter(self) -> str:
        return filter_name(self.lab)

    @property
    def net_names(self) -> set[str]:
        return {n.name for n in self.resolved.networks}

    def error(self, suffix: str, message: str, suggestion: str | None = None) -> None:
        self.diags.append(
            Diagnostic(
                id=f"runtime.{self.op}.{suffix}",
                severity="error",
                message=message,
                source=SourceLocation(path="host"),
                suggestion=suggestion,
            )
        )

    def warn(self, suffix: str, message: str, suggestion: str | None = None) -> None:
        self.diags.append(
            Diagnostic(
                id=f"runtime.{self.op}.{suffix}",
                severity="warning",
                message=message,
                source=SourceLocation(path="host"),
                suggestion=suggestion,
            )
        )

    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diags)


class _Step:
    def __init__(self, ctx: _Ctx, name: str) -> None:
        self.ctx = ctx
        self.name = name
        self.lines: list[str] = []
        self.exit_code = 0

    def log(self, line: str) -> None:
        self.lines.append(line)
        self.ctx.bus.publish(self.ctx.run_id, "log_line", {"step": self.name, "line": line})

    def virsh(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = self.ctx.virsh(args)
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        self.log(f"[{stamp}] virsh {' '.join(args)}  (exit {result.returncode})")
        if result.stderr.strip():
            self.log(f"  stderr: {result.stderr.strip()}")
        return result

    def fail(self) -> None:
        self.exit_code = 1


@contextmanager
def _step(ctx: _Ctx, name: str) -> Iterator[_Step]:
    step = _Step(ctx, name)
    started = _now()
    ctx.bus.publish(ctx.run_id, "step_started", {"step": name})
    try:
        yield step
    except Exception as exc:  # noqa: BLE001 - never raise out of a runner
        step.log(f"INTERNAL ERROR: {type(exc).__name__}: {exc}")
        step.fail()
        ctx.error("internal_error", f"step {name!r} crashed: {type(exc).__name__}: {exc}")
    finally:
        log_path = ctx.logs_dir / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join([f"# {name} for lab {ctx.lab!r}", *step.lines]) + "\n")
        ctx.steps.append(
            StepResult(
                name=name,
                command=["virsh", "-c", VIRSH_URI, name],
                exit_code=step.exit_code,
                log_path=str(log_path),
                started_at=started,
                finished_at=_now(),
            )
        )
        ctx.bus.publish(ctx.run_id, "step_finished", {"step": name, "exit_code": step.exit_code})


def _execute(
    op: Operation,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
    virsh: VirshRunner | None,
    body: Callable[[_Ctx], str],
) -> tuple[OperationRun, list[Diagnostic]]:
    run, run_dir = start_run(state_dir / "runs", op, resolved.lab_name)
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": op, "lab": resolved.lab_name})
    ctx = _Ctx(
        op=op,
        resolved=resolved,
        bus=bus,
        run_id=run.run_id,
        logs_dir=run_dir / "logs",
        xml_dir=run_dir / "xml",
        virsh=virsh or default_virsh,
    )
    ctx.xml_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = body(ctx)
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        ctx.error("internal_error", f"{op} crashed: {type(exc).__name__}: {exc}")
        summary = f"{op} crashed"
    status: RunStatus = (
        "failed" if ctx.has_errors() or any(s.exit_code for s in ctx.steps) else "succeeded"
    )
    finished = finish_run(run, run_dir, status=status, steps=ctx.steps, summary=summary)
    bus.publish(run.run_id, "operation_finished", {"status": status})
    return finished, ctx.diags


# ---------------------------------------------------------------------------
# isolate
# ---------------------------------------------------------------------------


def _isolate(ctx: _Ctx) -> str:
    lab = ctx.lab
    if not _inspect(ctx, require_all=True):
        return f"could not isolate lab {lab!r}"
    if not _define_filter(ctx):
        return f"could not define nwfilter for lab {lab!r}"
    if not _attach(ctx):
        return f"could not attach nwfilter to every interface of lab {lab!r}"
    if not _verify_isolated(ctx):
        return f"isolation of lab {lab!r} could not be verified"
    return f"isolated lab {lab!r} ({len(ctx.present)} VMs, filter {ctx.filter})"


def _inspect(ctx: _Ctx, *, require_all: bool) -> bool:
    """Read-only discovery: domains in scope, plus lab gateways and CIDRs.

    isolate: scope is EVERY libvirt domain with an interface on a lab
    network (declared in the lab or not); rules use the subnet libvirt
    actually runs. connect: scope is every defined domain, because the
    detach step only touches interfaces carrying our filter.
    """
    with _step(ctx, "inspect-lab") as st:
        listed = _list_domains(ctx, st)
        if listed is None:
            return False
        existing, running = listed
        wanted = [vm.name for vm in ctx.resolved.vms]
        missing = [n for n in wanted if n not in existing]
        ctx.running = set(running)

        if not require_all:
            ctx.present = _wanted_first(wanted, existing)
            st.log(f"domains in scope: {ctx.present}; running: {sorted(ctx.running)}")
            if missing:
                st.log(f"lab domains not defined in libvirt (skipped): {missing}")
            return True

        failed = False
        if missing:
            st.fail()
            failed = True
            ctx.error(
                "not_provisioned",
                f"lab {ctx.lab!r} is not provisioned: libvirt has no domain "
                f"{', '.join(repr(m) for m in missing)}",
                f"run `playground apply {ctx.lab}` first",
            )
        if not ctx.resolved.networks:
            st.fail()
            ctx.error("no_networks", f"lab {ctx.lab!r} declares no networks to isolate to")
            return False

        gateways: list[str] = []
        cidrs: list[str] = []
        missing_nets: list[str] = []
        for net in ctx.resolved.networks:
            try:
                configured = parse_ipv4_network(net.cidr)
            except ValueError as exc:
                st.fail()
                failed = True
                ctx.error(
                    "unsupported_cidr",
                    f"network {net.name!r} cidr {net.cidr!r} is not usable: {exc}",
                    "local-libvirt isolation supports IPv4 lab networks only",
                )
                continue
            result = st.virsh(["net-dumpxml", net.name])
            if result.returncode != 0:
                if _has(result, _NETWORK_NOT_FOUND):
                    missing_nets.append(net.name)
                else:
                    _virsh_failed(ctx, st, result, f"net-dumpxml {net.name}")
                    failed = True
                continue
            gw = network_gateway_ipv4(result.stdout)
            actual = network_cidr_ipv4(result.stdout)
            if gw is None or actual is None:
                st.fail()
                failed = True
                ctx.error(
                    "network_gateway_missing",
                    f"libvirt network {net.name!r} has no usable IPv4 <ip address=... "
                    "prefix=.../>; cannot tell its subnet or gateway",
                    f"inspect `virsh -c {VIRSH_URI} net-dumpxml {net.name}`",
                )
                continue
            if actual != configured:
                st.fail()
                failed = True
                ctx.error(
                    "network_drift",
                    f"libvirt network {net.name!r} runs {actual} but the lab config says "
                    f"{configured}; refusing to isolate against a stale config",
                    f"re-apply the lab (`playground apply {ctx.lab}`) so libvirt matches the "
                    "config, then isolate again",
                )
                continue
            gateways.append(gw)
            cidrs.append(str(actual))
            st.log(f"network {net.name}: cidr {actual} (from libvirt), gateway {gw}")
        if missing_nets:
            st.fail()
            failed = True
            ctx.error(
                "not_provisioned",
                f"lab {ctx.lab!r} is not provisioned: libvirt has no network "
                f"{', '.join(repr(m) for m in missing_nets)}",
                f"run `playground apply {ctx.lab}` first",
            )
        ctx.cidrs, ctx.gateways = cidrs, gateways
        if not _scan_scope(ctx, st, _wanted_first(wanted, existing)):
            failed = True
        return not failed


def _wanted_first(wanted: list[str], existing: set[str]) -> list[str]:
    """Declared lab VMs that exist, then the other domains (sorted)."""
    return [n for n in wanted if n in existing] + sorted(existing - set(wanted))


def _scan_scope(ctx: _Ctx, st: _Step, candidates: list[str]) -> bool:
    """Pick every domain with a lab-network interface; reject unfiltered paths."""
    ok = True
    declared = {vm.name for vm in ctx.resolved.vms}
    nets = ctx.net_names
    scope: list[str] = []
    for vm in candidates:
        on_lab = False
        foreign: list[str] = []
        modes = ([False] if vm in ctx.running else []) + [True]
        for inactive in modes:
            args = ["dumpxml", *(["--inactive"] if inactive else []), vm]
            result = st.virsh(args)
            if result.returncode != 0:
                if _has(result, _DOMAIN_NOT_FOUND):
                    st.log(f"{vm}: vanished while scanning; skipped")
                    break
                _virsh_failed(ctx, st, result, " ".join(args))
                ok = False
                break
            if interfaces_on(result.stdout, nets):
                on_lab = True
            foreign.extend(foreign_attachments(result.stdout, nets))
        if not on_lab:
            continue
        scope.append(vm)
        if vm not in declared:
            st.log(f"{vm}: NOT declared in lab {ctx.lab!r} but attached to a lab network")
            ctx.warn(
                "undeclared_domain",
                f"domain {vm!r} is attached to a lab network but is not declared in lab "
                f"{ctx.lab!r}; it will be isolated too",
            )
        if foreign:
            st.fail()
            ok = False
            ctx.error(
                "non_lab_interface",
                f"domain {vm!r} also has interface(s) outside the lab network(s) "
                f"{sorted(nets)}: {', '.join(sorted(set(foreign)))}; that is an unfiltered "
                "egress path",
                "detach the extra interface(s) (or re-apply the lab), then isolate again",
            )
    # Declared VMs that exist but sit on no lab network stay in scope so
    # attach-filter reports no_lab_interface for them.
    for vm in candidates:
        if vm in declared and vm not in scope:
            scope.append(vm)
    ctx.present = scope
    st.log(f"domains in scope: {scope}; running: {sorted(ctx.running & set(scope))}")
    return ok


def _list_domains(ctx: _Ctx, st: _Step) -> tuple[set[str], set[str]] | None:
    all_res = st.virsh(["list", "--all", "--name"])
    if all_res.returncode != 0:
        _virsh_failed(ctx, st, all_res, "list --all --name")
        return None
    run_res = st.virsh(["list", "--name"])
    if run_res.returncode != 0:
        _virsh_failed(ctx, st, run_res, "list --name")
        return None
    return _names(all_res.stdout), _names(run_res.stdout)


def _define_filter(ctx: _Ctx) -> bool:
    name = ctx.filter
    with _step(ctx, "define-nwfilter") as st:
        desired = render_filter_xml(name, cidrs=ctx.cidrs, gateways=ctx.gateways)
        existing = st.virsh(["nwfilter-dumpxml", name])
        uuid: str | None = None
        if existing.returncode == 0:
            if filters_equivalent(existing.stdout, desired):
                st.log(f"nwfilter {name} already defined with identical rules; unchanged")
                return True
            uuid = filter_uuid(existing.stdout)
            st.log(f"nwfilter {name} exists with different rules; redefining (uuid {uuid})")
        elif _has(existing, _FILTER_NOT_FOUND):
            st.log(f"nwfilter {name} not defined yet")
        else:
            _virsh_failed(ctx, st, existing, f"nwfilter-dumpxml {name}")
            return False

        path = ctx.xml_dir / "nwfilter.xml"
        path.write_text(render_filter_xml(name, cidrs=ctx.cidrs, gateways=ctx.gateways, uuid=uuid))
        result = st.virsh(["nwfilter-define", str(path)])
        if result.returncode != 0:
            st.fail()
            ctx.error(
                "nwfilter_define_failed",
                f"`virsh nwfilter-define` failed for {name!r} (exit {result.returncode}): "
                f"{_err(result)}",
                f"inspect {path}",
            )
            return False
        return True


def _dump(ctx: _Ctx, st: _Step, vm: str, *, inactive: bool) -> str | None:
    args = ["dumpxml", *(["--inactive"] if inactive else []), vm]
    result = st.virsh(args)
    if result.returncode != 0:
        _virsh_failed(ctx, st, result, " ".join(args))
        return None
    return result.stdout


def _modes(ctx: _Ctx, vm: str) -> list[tuple[str, bool]]:
    """(update-device flag, dump --inactive?) pairs. Live first: it bites soonest."""
    modes = [("--live", False)] if vm in ctx.running else []
    modes.append(("--config", True))
    return modes


def _attach(ctx: _Ctx) -> bool:
    name = ctx.filter
    ok = True
    declared = {vm.name for vm in ctx.resolved.vms}
    with _step(ctx, "attach-filter") as st:
        for vm in ctx.present:
            for flag, inactive in _modes(ctx, vm):
                xml = _dump(ctx, st, vm, inactive=inactive)
                if xml is None:
                    ok = False
                    continue
                targets = interfaces_on(xml, ctx.net_names)
                if not targets and vm not in declared:
                    st.log(f"{vm} {flag}: no lab-network interface in this XML; skip")
                    continue
                if not targets:
                    st.fail()
                    ok = False
                    ctx.error(
                        "no_lab_interface",
                        f"domain {vm!r} has no interface on lab network(s) "
                        f"{sorted(ctx.net_names)} ({flag}); cannot isolate it",
                        "check the lab's network attachments, then re-run",
                    )
                    continue
                for n, iface in enumerate(targets):
                    if iface.filter == name:
                        st.log(f"{vm} {flag}: {_iface_label(iface)} already filtered; skip")
                        continue
                    if iface.filter:
                        ctx.warn(
                            "filter_replaced",
                            f"domain {vm!r} {_iface_label(iface)} carried filter "
                            f"{iface.filter!r}; replacing it with {name!r} "
                            "(an interface takes one filterref)",
                        )
                    path = ctx.xml_dir / f"attach-{vm}{flag}-{n}.xml"
                    path.write_text(interface_xml_with_filter(iface, name))
                    result = st.virsh(["update-device", vm, str(path), flag])
                    if result.returncode != 0:
                        st.fail()
                        ok = False
                        ctx.error(
                            "attach_failed",
                            f"`virsh update-device {vm} {flag}` failed (exit "
                            f"{result.returncode}): {_err(result)}",
                            f"{vm} is NOT isolated ({flag}); fix and re-run "
                            f"`playground isolate {ctx.lab}`",
                        )
                    else:
                        st.log(f"{vm} {flag}: attached {name} to {_iface_label(iface)}")
    return ok


def _verify_isolated(ctx: _Ctx) -> bool:
    name = ctx.filter
    ok = True
    with _step(ctx, "verify-isolation") as st:
        # Re-list: a domain started after attach must show a live binding.
        fresh = st.virsh(["list", "--name"])
        if fresh.returncode != 0:
            _virsh_failed(ctx, st, fresh, "list --name")
            return False
        ctx.running = _names(fresh.stdout)
        st.log(f"running now: {sorted(ctx.running & set(ctx.present))}")
        for vm in ctx.present:
            for flag, inactive in _modes(ctx, vm):
                xml = _dump(ctx, st, vm, inactive=inactive)
                if xml is None:
                    ok = False
                    continue
                for iface in interfaces_on(xml, ctx.net_names):
                    if iface.filter != name:
                        ok = False
                        _unverified(ctx, st, vm, flag, iface, "has no filterref in the XML")
                        continue
                    if inactive:
                        st.log(f"{vm} config: {_iface_label(iface)} carries {name}")
                        continue
                    bound = _live_binding(st, iface)
                    if bound == name:
                        st.log(f"{vm} live: {iface.target_dev} bound to {name}")
                    else:
                        ok = False
                        _unverified(
                            ctx, st, vm, flag, iface, f"live binding is {bound!r}, not {name!r}"
                        )
    return ok


def _live_binding(st: _Step, iface: DomainInterface) -> str | None:
    if not iface.target_dev:
        return None
    result = st.virsh(["nwfilter-binding-dumpxml", iface.target_dev])
    if result.returncode != 0:
        return None
    return binding_filter(result.stdout)


def _unverified(ctx: _Ctx, st: _Step, vm: str, flag: str, iface: DomainInterface, why: str) -> None:
    st.fail()
    ctx.error(
        "verify_failed",
        f"domain {vm!r} {_iface_label(iface)} ({flag}) is not isolated: {why}",
        f"re-run `playground isolate {ctx.lab}`",
    )


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def _connect(ctx: _Ctx) -> str:
    lab = ctx.lab
    if not _inspect(ctx, require_all=False):
        return f"could not connect lab {lab!r}"
    if not _detach(ctx):
        return f"could not detach nwfilter from every interface of lab {lab!r}"
    if not _undefine_filter(ctx):
        return f"could not undefine nwfilter for lab {lab!r}"
    return f"connected lab {lab!r} (filter {ctx.filter} removed)"


def _detach(ctx: _Ctx) -> bool:
    """Strip our filterref wherever it is (any network), live and persistent."""
    name = ctx.filter
    ok = True
    with _step(ctx, "detach-filter") as st:
        for vm in ctx.present:
            for flag, inactive in _modes(ctx, vm):
                xml = _dump(ctx, st, vm, inactive=inactive)
                if xml is None:
                    ok = False
                    continue
                carrying = [i for i in network_interfaces(xml) if i.filter == name]
                if not carrying:
                    st.log(f"{vm} {flag}: no interface carries {name}; skip")
                    continue
                for n, iface in enumerate(carrying):
                    path = ctx.xml_dir / f"detach-{vm}{flag}-{n}.xml"
                    path.write_text(interface_xml_without_filter(iface))
                    result = st.virsh(["update-device", vm, str(path), flag])
                    if result.returncode != 0:
                        st.fail()
                        ok = False
                        ctx.error(
                            "detach_failed",
                            f"`virsh update-device {vm} {flag}` failed (exit "
                            f"{result.returncode}): {_err(result)}",
                            f"inspect `virsh -c {VIRSH_URI} dumpxml {vm}`",
                        )
                    else:
                        st.log(f"{vm} {flag}: detached {name} from {_iface_label(iface)}")
    return ok


def _undefine_filter(ctx: _Ctx) -> bool:
    name = ctx.filter
    with _step(ctx, "undefine-nwfilter") as st:
        result = st.virsh(["nwfilter-undefine", name])
        if result.returncode == 0:
            return True
        if _has(result, _FILTER_NOT_FOUND):
            st.log(f"nwfilter {name} not defined; nothing to remove")
            return True
        st.fail()
        if "in use" in result.stderr.lower():
            ctx.error(
                "filter_in_use",
                f"nwfilter {name!r} is still referenced by a domain: {_err(result)}",
                f"find it with `virsh -c {VIRSH_URI} nwfilter-binding-list`, detach, re-run",
            )
        else:
            _virsh_failed(ctx, st, result, f"nwfilter-undefine {name}", already_failed=True)
        return False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _virsh_failed(
    ctx: _Ctx,
    st: _Step,
    result: subprocess.CompletedProcess[str],
    what: str,
    *,
    already_failed: bool = False,
) -> None:
    if not already_failed:
        st.fail()
    if result.returncode == 127:
        ctx.error(
            "virsh_missing",
            "`virsh` is not on PATH; cannot manage libvirt nwfilters",
            "sudo apt install -y libvirt-clients",
        )
        return
    ctx.error(
        "virsh_failed",
        f"`virsh {what}` failed (exit {result.returncode}): {_err(result)}",
        "verify libvirtd is running and your user is in the libvirt group: "
        "`sudo systemctl status libvirtd && groups`",
    )


def _has(result: subprocess.CompletedProcess[str], needles: tuple[str, ...]) -> bool:
    text = result.stderr.lower()
    return any(n in text for n in needles)


def _err(result: subprocess.CompletedProcess[str]) -> str:
    return " ".join(result.stderr.split()) or "(no stderr)"


def _names(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip()}


def _iface_label(iface: DomainInterface) -> str:
    return f"interface {iface.mac or iface.target_dev or '?'} on {iface.network!r}"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "VirshRunner",
    "default_virsh",
    "execute_connect",
    "execute_isolate",
    "remove_isolation_filter",
]
