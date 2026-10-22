"""Post-provision egress isolation for cloud-digitalocean labs (``isolate`` / ``connect``).

Enforcement is the lab's Cloud Firewall (``<lab>-fw``): its outbound rules
switch from ``0.0.0.0/0, ::/0`` to the lab's own Droplets BY ID (not the
VPC CIDR), so Droplets can still reach each other on any port but nothing
else.  The CIDR is deliberately not used: DigitalOcean gives VPC Droplets a
resolver at an address inside the VPC range (e.g. ``10.97.0.254``) that
forwards outside lookups, and droplet-id destinations do not cover it.  The
firewall lives
outside the guests, so a VM cannot undo it, and it persists across reboots.
DigitalOcean Cloud Firewalls are stateful, so replies to inbound SSH from
the operator still flow while isolated.

``tofu`` stays the single source of truth: the toggle is the
``egress_isolated`` variable.  We edit that one key in the per-lab tfvars
JSON written by the last apply (never re-render from config, never re-copy
the ``.tf`` sources, so an unrelated config edit cannot ride along), run
``tofu plan -out`` in the per-lab directory, refuse unless the only change is
an update of ``digitalocean_firewall.lab`` (so isolate never recreates a
hand-deleted Droplet or applies drift), ``tofu apply`` that exact plan, then
confirm through the DigitalOcean API, BY FIREWALL ID taken from the tfstate
(names are not unique) and after DO reports the firewall settled, that the
firewall covers the whole lab and has the intended outbound destinations.

``connect`` has one fallback: when the scoped plan has unexpected changes (a
drifted lab), it does not run tofu but PUTs our firewall BY ID with outbound
reopened, waits for it to settle, verifies it, and warns the operator to run
`playground apply` (step ``api-reopen``; never a ``tofu-apply`` step).
Opening is the safe direction; ``isolate`` still refuses on drift and egress
is never closed outside tofu.

NOT blocked (verified live): the link-local metadata service
(169.254.169.254) is served by the hypervisor, outside Cloud Firewall's
reach, so it stays reachable while isolated.

The API token is read from the environment only; it never appears in a
file, argv, log line, or Diagnostic.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from playground.backend.cloud_digitalocean.do import (
    get_firewall,
    list_droplets_by_tag,
    list_firewalls,
    put_firewall,
    read_token,
    token_env_name,
)
from playground.backend.cloud_digitalocean.runner import (
    _finalize_failure,
    _iso_now,
    _step,
    tfstate_instance_ids,
    tfstate_resources,
)
from playground.backend.local_libvirt.apply import _run_step, run_tofu_init
from playground.events import EventBus, JsonlWriter
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.runs import OperationRun, StepResult, finish_run, start_run

_OPEN_DESTINATIONS = ("0.0.0.0/0", "::/0")
_OUTBOUND_PROTOCOLS = frozenset({"tcp", "udp", "icmp"})

_SETTLE_TIMEOUT = 60.0
"""Seconds to wait for DigitalOcean to finish applying firewall changes."""
_SETTLE_INTERVAL = 3.0
_sleep = time.sleep
_clock = time.monotonic
"""Indirections so tests can drive the settle loop without real waiting."""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def execute_isolate(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Narrow the lab firewall's outbound rules to the lab's own Droplets. Never raises."""
    return _set_egress(operation="isolate", isolated=True,
                       resolved=resolved, state_dir=state_dir, bus=bus)


def execute_connect(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Undo :func:`execute_isolate`: reopen outbound to the internet. Never raises."""
    return _set_egress(operation="connect", isolated=False,
                       resolved=resolved, state_dir=state_dir, bus=bus)


# ---------------------------------------------------------------------------
# Shared path
# ---------------------------------------------------------------------------


def _set_egress(
    *,
    operation: str,
    isolated: bool,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    lab = resolved.lab_name
    run, run_dir = start_run(state_dir / "runs", operation, lab)  # type: ignore[arg-type]
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": operation, "lab": lab})

    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    var_file = (per_lab_dir / f"{lab}.tfvars.json").resolve()
    steps: list[StepResult] = []

    def fail(diags: list[Diagnostic], summary: str) -> tuple[OperationRun, list[Diagnostic]]:
        return _finalize_failure(run, run_dir, steps, bus, diags, summary)

    # ---- Step 1: preflight (local state + token; no network) ----
    bus.publish(run.run_id, "step_started", {"step": "preflight"})
    pre_log = logs_dir / "preflight.log"
    pre_log.parent.mkdir(parents=True, exist_ok=True)
    started = _iso_now()
    tfvars, pre_diags = _preflight(
        operation=operation, resolved=resolved, per_lab_dir=per_lab_dir, var_file=var_file,
    )
    # connect on a lab that was never provisioned (or predates isolation) has
    # nothing to reopen: it is trivially connected, so report that, don't fail.
    if operation == "connect" and pre_diags and all(
        d.id.endswith((".not_provisioned", ".needs_reapply")) for d in pre_diags
    ):
        pre_log.write_text("# preflight: nothing to reopen (lab never isolated)\n")
        steps.append(_step("preflight", exit_code=0, log_path=pre_log, started=started))
        bus.publish(run.run_id, "step_finished", {"step": "preflight", "exit_code": 0})
        finished = finish_run(
            run, run_dir, status="succeeded", steps=steps,
            summary=f"connected lab {lab!r}: nothing to reopen (not isolated)",
        )
        bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
        return finished, [d.model_copy(update={"severity": "info"}) for d in pre_diags]
    pre_log.write_text(
        "\n".join(f"{d.id}: {d.message}" for d in pre_diags)
        or "# preflight: ok\n"
    )
    steps.append(_step("preflight", exit_code=1 if pre_diags else 0,
                       log_path=pre_log, started=started))
    bus.publish(run.run_id, "step_finished",
                {"step": "preflight", "exit_code": 1 if pre_diags else 0})
    if pre_diags or tfvars is None:
        return fail(pre_diags, f"{operation} lab {lab!r} failed preflight")

    token = read_token(resolved) or ""  # _preflight guarantees it is set

    # ---- Step 2: set-egress-isolated (edit the one tfvars key) ----
    bus.publish(run.run_id, "step_started", {"step": "set-egress-isolated"})
    set_log = logs_dir / "set-egress-isolated.log"
    started = _iso_now()
    previous = tfvars.get("egress_isolated")
    try:
        _write_tfvars_atomic(var_file, {**tfvars, "egress_isolated": isolated})
    except OSError as exc:
        set_log.write_text(f"FAILED to write {var_file}: {exc}\n")
        steps.append(_step("set-egress-isolated", exit_code=1,
                           log_path=set_log, started=started))
        bus.publish(run.run_id, "step_finished",
                    {"step": "set-egress-isolated", "exit_code": 1})
        return fail(
            [Diagnostic(
                id=f"runtime.{operation}.state_unwritable",
                severity="error",
                message=f"could not update {var_file}: {exc}",
                source=SourceLocation(path=str(var_file)),
            )],
            f"{operation} lab {lab!r}: could not update the per-lab tfvars",
        )
    set_log.write_text(f"egress_isolated: {previous!r} -> {isolated!r}\n")
    steps.append(_step("set-egress-isolated", exit_code=0,
                       log_path=set_log, started=started))
    bus.publish(run.run_id, "step_finished",
                {"step": "set-egress-isolated", "exit_code": 0})

    def revert() -> None:
        # Keep the tfvars file equal to the last state we know was applied.
        try:
            _write_tfvars_atomic(var_file, tfvars)
        except OSError:
            pass

    # ---- Step 3: tofu-init (only when the provider dir is missing) ----
    if not (per_lab_dir / ".terraform").is_dir():
        bus.publish(run.run_id, "step_started", {"step": "tofu-init"})
        init_step, init_diags = run_tofu_init(
            per_lab_dir, logs_dir / "tofu-init.log", bus=bus, run_id=run.run_id,
        )
        steps.append(init_step)
        bus.publish(run.run_id, "step_finished",
                    {"step": "tofu-init", "exit_code": init_step.exit_code})
        if init_step.exit_code != 0:
            revert()
            return fail(init_diags, f"{operation} lab {lab!r}: tofu-init failed")

    # ---- Step 4: tofu-plan, then refuse anything but a firewall update ----
    plan_file = (logs_dir / "egress.tfplan").resolve()
    bus.publish(run.run_id, "step_started", {"step": "tofu-plan"})
    plan_step, plan_diags = _tofu_plan(
        per_lab_dir, var_file, plan_file, logs_dir / "tofu-plan.log",
        bus=bus, run_id=run.run_id,
    )
    steps.append(plan_step)
    bus.publish(run.run_id, "step_finished",
                {"step": "tofu-plan", "exit_code": plan_step.exit_code})
    if plan_step.exit_code != 0:
        revert()
        return fail(
            plan_diags or [Diagnostic(
                id=f"runtime.{operation}.plan_failed",
                severity="error",
                message=f"tofu plan failed for lab {lab!r}; nothing was changed",
                source=SourceLocation(path=str(per_lab_dir)),
                suggestion=f"see {plan_step.log_path}",
            )],
            f"{operation} lab {lab!r}: tofu-plan failed; nothing was changed",
        )
    plan_json = _tofu_show_plan(per_lab_dir, plan_file)
    unexpected = (
        ["<plan could not be read>"] if plan_json is None
        else _unexpected_changes(plan_json)
    )
    if unexpected and operation == "connect":
        # Drifted lab: tofu would refuse to apply (and apply is refused while
        # isolated), leaving the operator stuck.  Opening egress is the safe
        # direction, so reopen our firewall BY ID outside tofu.
        firewall_id, _ = _read_state_ids(per_lab_dir / "terraform.tfstate")
        bus.publish(run.run_id, "step_started", {"step": "api-reopen"})
        reopen_log = logs_dir / "api-reopen.log"
        started = _iso_now()
        if firewall_id is None:
            reopen_log.write_text("no readable firewall id in the OpenTofu state\n")
            reopen_diags = [Diagnostic(
                id="runtime.connect.state_unreadable",
                severity="error",
                message=f"the OpenTofu state for lab {lab!r} has no readable firewall id",
                source=SourceLocation(path=str(per_lab_dir / "terraform.tfstate")),
                suggestion=f"re-run `playground apply {lab}`",
            )]
        else:
            reopen_diags = _api_reopen(
                lab=lab, token=token, firewall_id=firewall_id, log_path=reopen_log,
            )
        steps.append(_step("api-reopen", exit_code=1 if reopen_diags else 0,
                           log_path=reopen_log, started=started))
        bus.publish(run.run_id, "step_finished",
                    {"step": "api-reopen", "exit_code": 1 if reopen_diags else 0})
        if reopen_diags:
            revert()
            return fail(reopen_diags, f"connect lab {lab!r}: out-of-band reopen failed")
        finished = finish_run(
            run, run_dir, status="succeeded", steps=steps,
            summary=(
                f"connected lab {lab!r}: outbound reopened through the API "
                "(lab has drifted from its config)"
            ),
        )
        bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
        return finished, [Diagnostic(
            id="runtime.connect.reopened_out_of_band",
            severity="warning",
            message=(
                f"lab {lab!r} has drifted from its config ({', '.join(unexpected)}), "
                "so egress was reopened directly through the DigitalOcean API "
                "instead of OpenTofu"
            ),
            source=SourceLocation(path=str(per_lab_dir)),
            suggestion=f"run `playground apply {lab}` to reconcile the lab",
        )]
    if unexpected:
        revert()
        return fail(
            [Diagnostic(
                id=f"runtime.{operation}.unexpected_changes",
                severity="error",
                message=(
                    f"the plan for lab {lab!r} changes more than the firewall's "
                    f"outbound rules: {', '.join(unexpected)}; refusing to apply it"
                ),
                source=SourceLocation(path=str(per_lab_dir)),
                suggestion=(
                    "the lab has drifted from its config (e.g. a Droplet was deleted "
                    f"by hand); run `playground apply {lab}` (after `connect` if "
                    "isolated) to reconcile it first"
                ),
            )],
            f"{operation} lab {lab!r}: plan has unexpected changes; nothing was applied",
        )

    # ---- Step 4b: tofu-apply of exactly that saved plan ----
    bus.publish(run.run_id, "step_started", {"step": "tofu-apply"})
    apply_step, apply_diags = _tofu_apply_plan(
        per_lab_dir, plan_file, logs_dir / "tofu-apply.log",
        bus=bus, run_id=run.run_id,
    )
    steps.append(apply_step)
    bus.publish(run.run_id, "step_finished",
                {"step": "tofu-apply", "exit_code": apply_step.exit_code})
    if apply_step.exit_code != 0:
        revert()
        return fail(
            apply_diags or [Diagnostic(
                id=f"runtime.{operation}.apply_failed",
                severity="error",
                message=(
                    f"tofu apply failed while changing egress for lab {lab!r}; "
                    "the firewall's actual rules are unknown"
                ),
                source=SourceLocation(path=str(per_lab_dir)),
                suggestion=(
                    f"see {apply_step.log_path}; re-run `playground {operation} "
                    f"{lab}` or inspect the {lab}-fw firewall in the console"
                ),
            )],
            f"{operation} lab {lab!r}: tofu-apply failed; egress state is not confirmed",
        )

    # ---- Step 5: verify-firewall (ground truth from the DO API) ----
    bus.publish(run.run_id, "step_started", {"step": "verify-firewall"})
    verify_log = logs_dir / "verify-firewall.log"
    started = _iso_now()
    firewall_id, state_ids = _read_state_ids(per_lab_dir / "terraform.tfstate")
    if firewall_id is None:
        verify_diags = [Diagnostic(
            id=f"runtime.{operation}.state_unreadable",
            severity="error",
            message=f"the OpenTofu state for lab {lab!r} has no readable firewall id",
            source=SourceLocation(path=str(per_lab_dir / "terraform.tfstate")),
            suggestion=f"re-run `playground apply {lab}`",
        )]
    else:
        verify_diags = _verify_firewall(
            operation=operation, isolated=isolated, token=token,
            firewall_id=firewall_id, state_droplet_ids=state_ids,
            log_path=verify_log, lab=lab,
        )
    steps.append(_step("verify-firewall", exit_code=1 if verify_diags else 0,
                       log_path=verify_log, started=started))
    bus.publish(run.run_id, "step_finished",
                {"step": "verify-firewall", "exit_code": 1 if verify_diags else 0})
    if verify_diags:
        return fail(verify_diags, f"{operation} lab {lab!r}: firewall verification failed")

    summary = (
        f"isolated lab {lab!r}: outbound limited to the lab's own Droplets"
        if isolated
        else f"connected lab {lab!r}: outbound open to the internet"
    )
    finished = finish_run(run, run_dir, status="succeeded", steps=steps, summary=summary)
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _preflight(
    *,
    operation: str,
    resolved: ResolvedLab,
    per_lab_dir: Path,
    var_file: Path,
) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Check local state and the token.  Returns ``(tfvars, [])`` when usable."""
    lab = resolved.lab_name

    def not_provisioned(why: str) -> list[Diagnostic]:
        return [Diagnostic(
            id=f"runtime.{operation}.not_provisioned",
            severity="error",
            message=f"lab {lab!r} is not provisioned on cloud-digitalocean ({why})",
            source=SourceLocation(path=str(per_lab_dir)),
            suggestion=f"run `playground apply {lab}` first",
        )]

    if not var_file.is_file():
        return None, not_provisioned("no per-lab tfvars from a previous apply")
    if not _state_has_firewall(per_lab_dir / "terraform.tfstate"):
        # After destroy/suspend the state file survives but is empty;
        # applying then would CREATE a fresh lab, so refuse.
        return None, not_provisioned("no firewall in the OpenTofu state")

    try:
        loaded = json.loads(var_file.read_text())
    except (OSError, ValueError) as exc:
        return None, [Diagnostic(
            id=f"runtime.{operation}.state_unreadable",
            severity="error",
            message=f"could not read {var_file}: {exc}",
            source=SourceLocation(path=str(var_file)),
            suggestion=f"re-run `playground apply {lab}`",
        )]
    if not isinstance(loaded, dict):
        return None, [Diagnostic(
            id=f"runtime.{operation}.state_unreadable",
            severity="error",
            message=f"{var_file} is not a JSON object",
            source=SourceLocation(path=str(var_file)),
            suggestion=f"re-run `playground apply {lab}`",
        )]

    variables_tf = per_lab_dir / "variables.tf"
    declared = (
        variables_tf.is_file()
        and 'variable "egress_isolated"' in variables_tf.read_text()
    )
    if not declared or not loaded.get("vpc_cidr"):
        return None, [Diagnostic(
            id=f"runtime.{operation}.needs_reapply",
            severity="error",
            message=(
                f"lab {lab!r} was provisioned before egress isolation existed "
                "(its OpenTofu root has no VPC / `egress_isolated` variable)"
            ),
            source=SourceLocation(path=str(variables_tf)),
            suggestion=(
                f"re-run `playground apply {lab}` to adopt the VPC-enabled "
                "root; note this replaces the Droplets (new VPC attachment)"
            ),
        )]

    if not read_token(resolved):
        env_name = token_env_name(resolved)
        return None, [Diagnostic(
            id="runtime.cloud.token_missing",
            severity="error",
            message=f"set ${env_name} before changing a cloud lab's egress",
            source=SourceLocation(path="environment"),
            suggestion=f"export {env_name}=<your-token>",
        )]
    return loaded, []


def _state_has_firewall(state_path: Path) -> bool:
    """True when the local tfstate records a ``digitalocean_firewall``."""
    return bool(tfstate_resources(state_path, "digitalocean_firewall"))


def _read_state_ids(state_path: Path) -> tuple[str | None, set[str]]:
    """``(firewall_id, droplet_ids)`` of the lab as recorded in the tfstate.

    Ids come back as strings; ``firewall_id`` is None when unreadable.
    """
    fw_ids = tfstate_instance_ids(state_path, "digitalocean_firewall", name="lab")
    return (
        fw_ids[-1] if fw_ids else None,
        set(tfstate_instance_ids(state_path, "digitalocean_droplet")),
    )


def _open_outbound_body(firewall: dict[str, Any]) -> dict[str, Any]:
    """Full PUT body for ``firewall`` with outbound reopened to the internet.

    Keeps name, inbound rules and attached Droplets; ``tags`` stays empty
    (the lab firewall is attached by Droplet id only).
    """
    return {
        "name": firewall.get("name"),
        "inbound_rules": firewall.get("inbound_rules") or [],
        "outbound_rules": [
            {
                "protocol": proto,
                **({"ports": "all"} if proto != "icmp" else {}),
                "destinations": {"addresses": list(_OPEN_DESTINATIONS)},
            }
            for proto in ("tcp", "udp", "icmp")
        ],
        "droplet_ids": firewall.get("droplet_ids") or [],
        "tags": [],
    }


def _api_reopen(
    *,
    lab: str,
    token: str,
    firewall_id: str,
    log_path: Path,
) -> list[Diagnostic]:
    """Reopen OUR firewall's outbound through the API, BY ID; ``[]`` means verified open.

    Connect-only fallback for a drifted lab: opening egress is the safe
    direction, so it may bypass tofu; closing never does.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fw, get_diags, ok = get_firewall(token, firewall_id)
    if not ok or fw is None:
        log_path.write_text(f"could not read firewall id={firewall_id}\n")
        return get_diags or [_verify_failed(
            "connect",
            f"firewall id={firewall_id} was not found in the DigitalOcean account",
            f"re-run `playground apply {lab}` to recreate it",
        )]
    put_ok, put_diags = put_firewall(token, firewall_id, _open_outbound_body(fw))
    if not put_ok:
        log_path.write_text(f"PUT firewall id={firewall_id} failed\n")
        return [Diagnostic(
            id="runtime.connect.apply_failed",
            severity="error",
            message=(
                f"could not reopen outbound on firewall id={firewall_id} through the "
                "DigitalOcean API; its actual rules are unknown"
            ),
            source=SourceLocation(path=f"firewall/{firewall_id}"),
            suggestion=(
                f"re-run `playground connect {lab}` or open the {lab}-fw firewall's "
                "outbound rules in the console"
            ),
        ), *put_diags]
    return _verify_firewall(
        operation="connect", isolated=False, token=token, firewall_id=firewall_id,
        state_droplet_ids=set(), log_path=log_path, lab=lab,
    )


def _tofu_plan(
    per_lab_dir: Path, var_file: Path, plan_file: Path, log_path: Path,
    *, bus: EventBus, run_id: str,
) -> tuple[StepResult, list[Diagnostic]]:
    """``tofu plan -out=<plan_file>`` in the per-lab dir (token via the environment)."""
    return _run_step(
        name="tofu-plan",
        command=[
            "tofu", "plan", "-input=false", f"-out={plan_file}", f"-var-file={var_file}",
        ],
        cwd=per_lab_dir, log_path=log_path,
        missing_binary_id="runtime.apply.tofu_binary_missing",
        bus=bus, run_id=run_id,
    )


def _tofu_show_plan(per_lab_dir: Path, plan_file: Path) -> dict[str, Any] | None:
    """``tofu show -json <plan_file>`` parsed, or None if it cannot be read."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["tofu", "show", "-json", str(plan_file)],
            cwd=per_lab_dir, capture_output=True, text=True, timeout=120, check=False,
        )
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return data if isinstance(data, dict) else None


def _tofu_apply_plan(
    per_lab_dir: Path, plan_file: Path, log_path: Path,
    *, bus: EventBus, run_id: str,
) -> tuple[StepResult, list[Diagnostic]]:
    """``tofu apply <plan_file>``: applies exactly the plan that was inspected."""
    return _run_step(
        name="tofu-apply",
        command=["tofu", "apply", "-input=false", str(plan_file)],
        cwd=per_lab_dir, log_path=log_path,
        missing_binary_id="runtime.apply.tofu_binary_missing",
        bus=bus, run_id=run_id,
    )


def _unexpected_changes(plan: dict[str, Any]) -> list[str]:
    """Addresses of resource changes other than an update of the lab firewall."""
    bad: list[str] = []
    for rc in plan.get("resource_changes") or []:
        if not isinstance(rc, dict):
            continue
        change = rc.get("change")
        actions = list((change or {}).get("actions") or []) if isinstance(change, dict) else []
        if set(actions) <= {"no-op", "read"}:
            continue
        if rc.get("address") == "digitalocean_firewall.lab" and actions == ["update"]:
            continue
        bad.append(f"{rc.get('address')} ({'+'.join(actions) or 'unknown'})")
    return bad


def _write_tfvars_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _destinations(rule: dict[str, Any]) -> dict[str, Any]:
    dest = rule.get("destinations")
    return dest if isinstance(dest, dict) else {}


def outbound_is_droplets_only(firewall: dict[str, Any], droplet_ids: set[str]) -> bool:
    """True when every outbound rule's only destination is exactly ``droplet_ids``.

    All three protocols must be present; no rule may carry an address, tag,
    load balancer or Kubernetes destination (an address inside the VPC would
    reach the VPC resolver, which forwards outside lookups).  The firewall
    itself must also be attached to exactly those Droplets.
    """
    if not droplet_ids:
        return False
    if {str(i) for i in (firewall.get("droplet_ids") or [])} != droplet_ids:
        return False
    rules = [r for r in (firewall.get("outbound_rules") or []) if isinstance(r, dict)]
    if {str(r.get("protocol")) for r in rules} != _OUTBOUND_PROTOCOLS:
        return False
    for rule in rules:
        dest = _destinations(rule)
        if {str(i) for i in (dest.get("droplet_ids") or [])} != droplet_ids:
            return False
        for extra in ("addresses", "tags", "load_balancer_uids", "kubernetes_ids"):
            if dest.get(extra):
                return False
    return True


def outbound_is_open(firewall: dict[str, Any]) -> bool:
    """True when every outbound rule allows ``0.0.0.0/0`` for tcp, udp and icmp."""
    rules = [r for r in (firewall.get("outbound_rules") or []) if isinstance(r, dict)]
    if {str(r.get("protocol")) for r in rules} != _OUTBOUND_PROTOCOLS:
        return False
    return all(
        _OPEN_DESTINATIONS[0] in (_destinations(r).get("addresses") or [])
        for r in rules
    )


def grants_egress_beyond(firewall: dict[str, Any], droplet_ids: set[str]) -> bool:
    """True when any outbound rule allows a destination other than lab Droplets.

    ANY address (even inside the VPC CIDR, which reaches the VPC resolver),
    tag, load balancer or Kubernetes destination counts, as does a Droplet id
    outside the lab.
    """
    for rule in firewall.get("outbound_rules") or []:
        if not isinstance(rule, dict):
            continue
        dest = _destinations(rule)
        for extra in ("addresses", "tags", "load_balancer_uids", "kubernetes_ids"):
            if dest.get(extra):
                return True
        if {str(i) for i in (dest.get("droplet_ids") or [])} - droplet_ids:
            return True
    return False


def _check_foreign_firewalls(
    *,
    operation: str,
    token: str,
    lab: str,
    own_id: str,
    lab_ids: set[str],
    droplets: list[dict[str, Any]],
    log_path: Path,
) -> list[Diagnostic]:
    """Fail if ANY other firewall applying to a lab Droplet grants wider egress.

    DigitalOcean unions allow rules across the firewalls attached to a
    Droplet, so one foreign firewall with open egress silently defeats
    isolation.  A firewall applies to a Droplet by ``droplet_ids`` or by a
    ``tags`` overlap with the Droplet's tags.  Only OUR firewall, identified
    by id (names are not unique), is exempt.
    """
    firewalls, _, f_ok = list_firewalls(token)
    if not f_ok:
        with log_path.open("a") as fh:
            fh.write("INCONCLUSIVE: could not enumerate firewalls\n")
        return [Diagnostic(
            id=f"runtime.{operation}.verify_failed",
            severity="error",
            message=(
                "could not enumerate the account's firewalls "
                "to rule out another firewall granting outside egress; do not rely "
                "on isolation"
            ),
            source=SourceLocation(path="DigitalOcean API"),
            suggestion=f"re-run `playground {operation}` once the API is reachable",
        )]
    lab_tags = {t for d in droplets for t in (d.get("tags") or [])}
    diags: list[Diagnostic] = []
    for fw in firewalls:
        if str(fw.get("id")) == own_id:
            continue
        applies = bool(lab_ids & {str(i) for i in (fw.get("droplet_ids") or [])}) or bool(
            lab_tags & set(fw.get("tags") or [])
        )
        if not applies or not grants_egress_beyond(fw, lab_ids):
            continue
        fw_name, fw_id = fw.get("name"), fw.get("id")
        with log_path.open("a") as fh:
            fh.write(f"FOREIGN firewall {fw_name!r} (id={fw_id}) grants wider egress\n")
        diags.append(Diagnostic(
            id=f"runtime.{operation}.foreign_firewall",
            severity="error",
            message=(
                f"firewall {fw_name!r} (id={fw_id}) also applies to lab {lab!r} "
                "Droplets and allows outbound beyond the lab's own Droplets (any "
                "address, even inside the VPC, reaches the VPC resolver); "
                "DigitalOcean unions allow rules, so the lab is NOT isolated"
            ),
            source=SourceLocation(path=f"firewall/{fw_id}"),
            suggestion=(
                "remove or narrow that firewall in "
                "https://cloud.digitalocean.com/networking/firewalls, or re-apply "
                "the lab that owns it with current code (older code tagged firewalls "
                "with the account-wide `playground` tag)"
            ),
        ))
    return diags


def _verify_failed(operation: str, message: str, suggestion: str | None = None) -> Diagnostic:
    return Diagnostic(
        id=f"runtime.{operation}.verify_failed",
        severity="error",
        message=message,
        source=SourceLocation(path="DigitalOcean API"),
        suggestion=suggestion or f"re-run `playground {operation}` once the API is reachable",
    )


def _wait_settled(
    *, operation: str, token: str, firewall_id: str, log_path: Path,
) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Poll the firewall by id until ``status == succeeded`` with no pending changes.

    DigitalOcean applies firewall changes asynchronously, so reading the
    rules right after ``tofu apply`` can show the old or a half-applied
    state.  Bounded by ``_SETTLE_TIMEOUT``; not settling in time is a failure
    (inconclusive), never a pass.
    """
    deadline = _clock() + _SETTLE_TIMEOUT
    last = "no response"
    while True:
        fw, _, ok = get_firewall(token, firewall_id)
        if ok and fw is None:
            log_path.write_text(f"firewall id={firewall_id} not found\n")
            return None, [_verify_failed(
                operation,
                f"firewall id={firewall_id} was not found in the DigitalOcean account",
                "re-run `playground apply` to recreate it",
            )]
        if ok and fw is not None:
            pending = fw.get("pending_changes") or []
            if fw.get("status") == "succeeded" and not pending:
                return fw, []
            last = f"status={fw.get('status')!r}, {len(pending)} pending change(s)"
        else:
            last = "API request failed"
        if _clock() >= deadline:
            break
        _sleep(_SETTLE_INTERVAL)
    log_path.write_text(f"INCONCLUSIVE: firewall id={firewall_id} did not settle ({last})\n")
    return None, [_verify_failed(
        operation,
        f"firewall id={firewall_id} did not finish applying within "
        f"{int(_SETTLE_TIMEOUT)}s ({last}); its effective rules are unconfirmed",
    )]


def _verify_firewall(
    *,
    operation: str,
    isolated: bool,
    token: str,
    firewall_id: str,
    state_droplet_ids: set[str],
    log_path: Path,
    lab: str,
) -> list[Diagnostic]:
    """Read our firewall back BY ID from the DO API; ``[]`` means the state holds.

    Always waits for the firewall to settle first.  Connect then needs only
    "open again".  Isolate additionally requires the lab Droplet set (tofu
    state union the Droplets tagged ``lab:<lab>``) to be fully covered by our
    firewall, the outbound rules to name exactly that set, and no foreign
    firewall to widen egress.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fw, diags = _wait_settled(
        operation=operation, token=token, firewall_id=firewall_id, log_path=log_path,
    )
    if fw is None:
        return diags

    if not isolated:
        holds = outbound_is_open(fw)
        log_path.write_text(
            f"firewall id={firewall_id}: outbound open: "
            f"{'confirmed' if holds else 'NOT confirmed'}\n"
        )
        if holds:
            return []
        return [_verify_failed(
            operation,
            f"firewall id={firewall_id} does not have outbound open after tofu apply",
            f"inspect https://cloud.digitalocean.com/networking/firewalls and re-run "
            f"`playground {operation}`",
        )]

    tagged, _, d_ok = list_droplets_by_tag(token, f"lab:{lab}")
    if not d_ok:
        log_path.write_text("INCONCLUSIVE: could not list the lab's Droplets\n")
        return [_verify_failed(
            operation,
            f"the DigitalOcean API could not list lab {lab!r} Droplets to confirm "
            "isolation; do not rely on it",
        )]
    tag_ids = {str(d.get("id")) for d in tagged}
    names = {str(d.get("id")): str(d.get("name")) for d in tagged}
    if not state_droplet_ids:
        log_path.write_text("no Droplets recorded in the OpenTofu state\n")
        return [_verify_failed(
            operation,
            f"the OpenTofu state for lab {lab!r} records no Droplets, so isolation "
            "cannot be confirmed",
            f"re-run `playground apply {lab}`",
        )]
    if not tag_ids or state_droplet_ids - tag_ids:
        missing = sorted(state_droplet_ids - tag_ids)
        log_path.write_text(
            f"tag listing lab:{lab} {'is empty' if not tag_ids else 'misses ' + str(missing)}\n"
        )
        return [_verify_failed(
            operation,
            f"the Droplets tagged lab:{lab} "
            f"{'came back empty' if not tag_ids else 'are missing ' + ', '.join(missing)} "
            "although the OpenTofu state has them; isolation cannot be confirmed",
            "check the tags in the DigitalOcean console and retry",
        )]

    lab_ids = state_droplet_ids | tag_ids
    attached = {str(i) for i in (fw.get("droplet_ids") or [])}
    uncovered = sorted(lab_ids - attached)
    if uncovered:
        log_path.write_text(f"uncovered Droplets: {uncovered}\n")
        return [
            Diagnostic(
                id=f"runtime.{operation}.uncovered_droplet",
                severity="error",
                message=(
                    f"Droplet {names.get(i, '<not in tag listing>')!r} (id={i}) belongs "
                    f"to lab {lab!r} but is not covered by firewall id={firewall_id}; "
                    "it keeps whatever egress it had"
                ),
                source=SourceLocation(path=f"droplet/{i}"),
                suggestion=(
                    f"re-run `playground apply {lab}` to attach it, or delete the "
                    "orphaned Droplet in the DigitalOcean console"
                ),
            )
            for i in uncovered
        ]

    holds = outbound_is_droplets_only(fw, lab_ids)
    log_path.write_text(
        f"firewall id={firewall_id}: outbound limited to the lab's own Droplets: "
        f"{'confirmed' if holds else 'NOT confirmed'}\n"
    )
    if not holds:
        return [_verify_failed(
            operation,
            f"firewall id={firewall_id} does not have outbound limited to the lab's "
            "own Droplets after tofu apply",
            f"inspect https://cloud.digitalocean.com/networking/firewalls and re-run "
            f"`playground {operation}`",
        )]
    return _check_foreign_firewalls(
        operation=operation, token=token, lab=lab, own_id=firewall_id,
        lab_ids=lab_ids, droplets=tagged, log_path=log_path,
    )


__all__ = [
    "execute_connect",
    "execute_isolate",
    "grants_egress_beyond",
    "outbound_is_open",
    "outbound_is_droplets_only",
]
