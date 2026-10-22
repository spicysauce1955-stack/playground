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
``tofu apply`` in the per-lab directory, then confirm through the DigitalOcean
API that the firewall really has the intended outbound destinations.

NOT blocked (verified live): the link-local metadata service
(169.254.169.254) is served by the hypervisor, outside Cloud Firewall's
reach, so it stays reachable while isolated.

The API token is read from the environment only; it never appears in a
file, argv, log line, or Diagnostic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playground.backend.cloud_digitalocean.do import (
    find_firewall,
    list_droplets_by_tag,
    list_firewalls,
    read_token,
    token_env_name,
)
from playground.backend.cloud_digitalocean.runner import (
    _finalize_failure,
    _iso_now,
    _step,
)
from playground.backend.local_libvirt.apply import run_tofu_apply, run_tofu_init
from playground.events import EventBus, JsonlWriter
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.runs import OperationRun, StepResult, finish_run, start_run

_OPEN_DESTINATIONS = ("0.0.0.0/0", "::/0")
_OUTBOUND_PROTOCOLS = frozenset({"tcp", "udp", "icmp"})


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

    # ---- Step 4: tofu-apply (changes only the firewall's outbound rules) ----
    bus.publish(run.run_id, "step_started", {"step": "tofu-apply"})
    apply_step, apply_diags = run_tofu_apply(
        per_lab_dir, var_file, logs_dir / "tofu-apply.log",
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
    verify_diags = _verify_firewall(
        operation=operation, isolated=isolated, token=token,
        firewall_name=f"{lab}-fw", log_path=verify_log, lab=lab,
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
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return False
    resources = state.get("resources") if isinstance(state, dict) else None
    if not isinstance(resources, list):
        return False
    return any(
        isinstance(r, dict) and r.get("type") == "digitalocean_firewall"
        for r in resources
    )


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
    own_name: str,
    droplets: list[dict[str, Any]],
    log_path: Path,
) -> list[Diagnostic]:
    """Fail if ANY other firewall applying to a lab Droplet grants wider egress.

    DigitalOcean unions allow rules across the firewalls attached to a
    Droplet, so one foreign firewall with open egress silently defeats
    isolation.  A firewall applies to a Droplet by ``droplet_ids`` or by a
    ``tags`` overlap with the Droplet's tags.
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
    lab_ids = {str(d.get("id")) for d in droplets}
    lab_tags = {t for d in droplets for t in (d.get("tags") or [])}
    diags: list[Diagnostic] = []
    for fw in firewalls:
        if fw.get("name") == own_name:
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


def _verify_firewall(
    *,
    operation: str,
    isolated: bool,
    token: str,
    firewall_name: str,
    log_path: Path,
    lab: str,
) -> list[Diagnostic]:
    """Read the firewall back from the DO API; ``[]`` means the state holds."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    wanted = "outbound limited to the lab's own Droplets" if isolated else "outbound open"
    lab_droplets: list[dict[str, Any]] = []
    if isolated:
        lab_droplets, _, d_ok = list_droplets_by_tag(token, f"lab:{lab}")
        if not d_ok:
            log_path.write_text("INCONCLUSIVE: could not list the lab's Droplets\n")
            return [Diagnostic(
                id=f"runtime.{operation}.verify_failed",
                severity="error",
                message=(
                    "tofu apply succeeded but the DigitalOcean API could not list "
                    f"lab {lab!r} Droplets to confirm isolation; do not rely on it"
                ),
                source=SourceLocation(path="DigitalOcean API"),
                suggestion=f"re-run `playground {operation}` once the API is reachable",
            )]
    lab_ids = {str(d.get("id")) for d in lab_droplets}
    fw, api_diags, ok = find_firewall(token, firewall_name)
    if not ok:
        log_path.write_text(f"INCONCLUSIVE: could not list firewalls ({wanted} unconfirmed)\n")
        return [Diagnostic(
            id=f"runtime.{operation}.verify_failed",
            severity="error",
            message=(
                f"tofu apply succeeded but the DigitalOcean API could not confirm "
                f"firewall {firewall_name!r} has {wanted}; do not rely on it"
            ),
            source=SourceLocation(path="DigitalOcean API"),
            suggestion=f"re-run `playground {operation}` once the API is reachable",
        ), *api_diags]
    if fw is None:
        log_path.write_text(f"firewall {firewall_name!r} not found\n")
        return [Diagnostic(
            id=f"runtime.{operation}.verify_failed",
            severity="error",
            message=f"firewall {firewall_name!r} was not found in the DigitalOcean account",
            source=SourceLocation(path="DigitalOcean API"),
            suggestion="re-run `playground apply` to recreate it",
        )]
    holds = outbound_is_droplets_only(fw, lab_ids) if isolated else outbound_is_open(fw)
    log_path.write_text(
        f"firewall {firewall_name!r}: {wanted}: {'confirmed' if holds else 'NOT confirmed'}\n"
    )
    if holds:
        if isolated:
            return _check_foreign_firewalls(
                operation=operation, token=token, lab=lab,
                own_name=firewall_name, droplets=lab_droplets, log_path=log_path,
            )
        return []
    return [Diagnostic(
        id=f"runtime.{operation}.verify_failed",
        severity="error",
        message=(
            f"firewall {firewall_name!r} does not have the expected state "
            f"({wanted}) after tofu apply"
        ),
        source=SourceLocation(path="DigitalOcean API"),
        suggestion=(
            f"inspect the firewall at https://cloud.digitalocean.com/networking/firewalls "
            f"and re-run `playground {operation}`"
        ),
    )]


__all__ = [
    "execute_connect",
    "execute_isolate",
    "grants_egress_beyond",
    "outbound_is_open",
    "outbound_is_droplets_only",
]
