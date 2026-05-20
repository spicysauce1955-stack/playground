# Roadmap

This is the current sequential task queue.

Source documents:

- `docs/product/requirements.md`
- `docs/product/user_stories.md`
- `docs/product/mvp_scope.md`
- `docs/system_design.md`
- `docs/config_design.md`
- `docs/engineering_principles.md`
- `docs/architecture_decisions.md`

## 1. Baseline Cleanup

Status: done.

Goal: make the repository ready for the next implementation slice.

Acceptance:

- extra local branches are gone
- root `main.tf` stub is retired
- workflow files are committed together
- old parallel planning tree is removed
- durable design constraints live under `docs/`
- product intent has been rehomed under `docs/product/`

## 2. Read-Only CLI

Status: done.

Goal: prove the Python config layer without touching real infrastructure.

Commands:

```text
playground validate
playground lab list
playground lab show <name>
```

Acceptance:

- `validate` reports diagnostics and exits nonzero on errors
- `lab list` shows configured labs
- `lab show <name>` emits a resolved lab as JSON
- unit tests cover command wiring and invalid config behavior

## 3. Validation Hardening

Status: done.

Goal: make `ResolvedLab` safe as a future backend input.

Acceptance:

- missing defaults are diagnostics, not late resolver exceptions
  (`config.required.defaults_missing`)
- workload placement targets are validated against the full
  `spec.extends` chain (`config.reference.unknown_workload_target`)
- routing intent survives resolution (`ResolvedVm.routing`)
- budget checks exist for VM totals (`config.budget.exceeded`,
  strict→error / permissive→warning, falls back to `Defaults.spec.budget`)
- source paths in diagnostics are accurate enough for CLI use
  (`LoadedConfig.sources[(kind, name)]` from the loader)
- offline labs flag missing VM-image artifacts before apply
  (`config.artifact.offline_missing`). Other artifact classes from
  `requirements.md` §5.13 are tracked separately; see
  `docs/config_design.md` "Validation Rules".

Note: `playground validate` now exits with code 1 when `Defaults` is
absent — previously the resolver crashed later with a less actionable
error.

## 4. OpenTofu / Ansible Bridge

Status: done.

Goal: reduce manual handoff without changing runtime behavior.

Slice 4a (done):

- `playground inventory render <lab>` writes
  `.playground/state/inventory/<lab>.ini` from a `ResolvedLab` plus
  `tofu output -json`
- new backend adapter layer under `src/playground/backend/local_libvirt/`
- `config.inventory.*` diagnostics for the failure modes
- `ansible/site.yml` and `ansible/roles/*` unchanged

Slice 4b (done):

- `tofu/outputs.tf` emits `vm_ips` as a **name-keyed map**
  (`{domain -> ip}`) instead of a positional tuple
- new `var.vm_names` in `tofu/variables.tf` lets the operator name
  libvirt domains after their lab VMs (`lab.spec.vms[*].name`); default
  falls back to `pg-node-N` for backward compatibility
- renderer matches by name; mismatches surface as
  `config.inventory.vm_ip_not_found`
- legacy positional `vm_ips` payloads from pre-4b state are explicitly
  rejected so silent index drift can't return

Slice 4d (done):

- `playground tofu render <lab>` writes
  `.playground/state/tofu/<lab>.tfvars.json` from a `ResolvedLab` so
  `var.vm_names` stays in sync with the lab. Closes the last manual
  handoff: operator runs `playground tofu render … && tofu -chdir=tofu
  apply -var-file=…`.
- new validator check `config.backend.per_vm_resources_unsupported`
  (warning) fires whenever a lab declares heterogeneous per-VM resources
  that the local-libvirt backend cannot honor today. Surfaces under
  `playground validate` and every command that depends on it.
- `_resolve_lab_or_exit` helper extracted from the three CLI commands
  that resolve a lab (`lab show`, `inventory render`, `tofu render`).

Slice 4c (done):

- Inventory now emits one `[role_group]` per distinct VM role in
  addition to `[playground]`. Group names normalize kebab→snake
  (`docker-host` → `docker_host`) so they're valid Ansible
  identifiers. Future playbooks can target `hosts: docker_host` etc.
  without scanning host vars.

Carried forward to future work:

- Per-VM `resources` from the lab still don't reach tofu. Today's
  `tofu/main.tf` applies global `var.vm_memory` / `var.vm_vcpu`
  uniformly; the `config.backend.per_vm_resources_unsupported`
  warning documents the gap. Future slice can enrich tofu to accept
  per-VM resources as a list of objects.
- CLI imports the concrete `playground.backend.local_libvirt` adapter
  directly. Introduce a small adapter protocol / registry only when a
  second backend appears.

## 5. Plan Rendering

Status: in progress (first slice done; state-observation slice queued).

Slice 5a (done):

- `playground plan <lab>` renders a backend-neutral `Plan` from a
  `ResolvedLab`. Today every action verb is `create`; future verbs
  (`update` / `delete` / `no_op`) are reserved in `ActionVerb` and
  unlock when state observation lands.
- `Plan` carries: per-resource actions (network/vm/workload),
  aggregate budget (totals vs limits + `fits` flag), and validator
  warnings carried forward as a snapshot.
- New module `src/playground/planner/` — peer of `validation/`,
  `config/`, `backend/`. Pure function `render_plan(resolved,
  warnings=None) -> Plan`.
- Human and JSON output modes.

Slice 5b (queued):

- State observation: read `.playground/state/observed/` and backend
  reports (e.g. `tofu state list -json`, libvirt domain query).
- Emit `update` / `delete` / `no_op` actions and `before`/`after`
  details where applicable.
- Promote `plan` to a subapp (`plan render`, `plan show <run-id>`,
  `plan diff`) once operation runs land.

## 6. Apply / Status / Destroy

Status: done.

Slice 6a (done):

- `playground apply <lab>` chains render tfvars → tofu apply →
  fetch_vm_ips → render inventory → ansible-playbook, wrapped in an
  operation run record.
- New module `src/playground/runs/operation.py`: `OperationRun`,
  `StepResult`, `allocate_run_id`, `start_run`, `finish_run`. Writes
  `.playground/runs/<id>/run.json` plus captured subprocess logs.
- New module `src/playground/backend/local_libvirt/apply.py`: thin
  subprocess wrappers for `tofu apply` and `ansible-playbook` with
  combined-stream log capture.
- Failure protocol: any step's nonzero exit (or missing-binary
  diagnostic) finalizes the run as `failed` with a summary tailored
  to what state the lab is now in (e.g. "VMs were provisioned but
  Ansible configuration failed — re-run apply or destroy via tofu").
- Two new diagnostic IDs: `runtime.apply.tofu_binary_missing`,
  `runtime.apply.ansible_binary_missing`. New `runtime.*` namespace
  separates execution-time concerns from config-side `config.*`
  diagnostics.

Slice 6b (done):

- `playground destroy <lab>` re-renders the same tfvars apply uses,
  then runs `tofu destroy -auto-approve -var-file=...`. Wrapped in
  an OperationRun with `operation: destroy`. Same failure protocol
  as apply: nonzero tofu exit finalizes the run as `failed` with a
  summary telling the operator what to inspect.
- Symmetric with apply: re-rendering the tfvars guarantees tofu
  sees the same `var.vm_names` as the apply did, so destroy
  targets the right resources.

Slice 6c (done):

- `playground status <lab>` — read-only snapshot. Pairs
  `ResolvedLab.vms` with `tofu output -json` to report
  `provisioned` / `missing` per VM. No run record (read-only per
  §5.10). Ansible reachability + docker readiness are reserved as
  states (`running` / `failed` / `degraded` in `VmState`) and land
  alongside §8 (Docker workloads).
- New backend-neutral model `playground.models.status` (`LabStatus`,
  `VmStatus`). Adapter `playground.backend.local_libvirt.status`
  composes `fetch_vm_ips` with the model and treats `tofu_no_state`
  as the steady "nothing applied yet" status rather than an error.
- `TOFU_NO_STATE_DIAGNOSTIC_ID` exported from `inventory.py` so the
  status adapter doesn't depend on a magic string.

## 7. Operation Runs + Events

Status: in progress (first slice done).

Slice 7a (done):

- New module `src/playground/events/` with `OperationEvent`,
  in-process `EventBus`, and a `JsonlWriter` subscriber that appends
  one event per line to `.playground/runs/<id>/events.jsonl`.
- `playground apply` and `playground destroy` now publish
  `operation_started`, `step_started`, `step_finished`, and
  `operation_finished` events around their tofu/ansible steps. The
  `operation_finished` event fires even on failure so an event log
  is always reconstructable.
- New CLI: `playground runs list` (newest-first, with status + start/
  end timestamps) and `playground runs show <run-id>` (renders the
  recorded `run.json`, step results, events path, log dir).

Carried forward to future work:

- Live subprocess streaming as events (`log_line`-style). Today we
  still capture combined stdout/stderr to per-step log files.
- Retention enforcement (the `RetentionPolicy` model exists; the
  cleanup pass doesn't).
- Additional event consumers (TUI views, status caches) when those
  arrive in §9.

## 8. Docker Workloads

Status: done.

Slice 8a (done):

- New planner module `playground.planner.scheduling`: pure-function
  `schedule_workloads(resolved) -> ({vm: [workloads]}, diagnostics)`.
  Resolves `target_vm` / `target_role` / `target_tag` / `auto`, with
  role matching that walks the full `spec.extends` ancestry so the
  scheduler agrees with the validator.
- `ResolvedVm.roles: list[str]` carries the full role ancestry
  (leaf → root). The scheduler matches against this; the validator
  was already doing the same walk via `_role_ancestors`.
- Inventory renderer adds a `pg_workloads='<json>'` host var on each
  VM that has scheduled workloads. Embedded single quotes are
  shell-escaped (`'\''`).
- `playground apply` runs `schedule_workloads` as a pre-flight before
  `start_run` / `tofu apply`, so a no-target workload fails fast
  without provisioning anything.
- New Ansible role `workload_container` reads the JSON payload and
  deploys items with `type: container` via
  `community.docker.docker_container`. Idempotent. Compose / Swarm
  items are skipped by the `when: item.type == 'container'` guard.
- New diagnostic ID `config.workload.no_target`.

Slice 8b (done): Docker Compose

- `stage_workload_files()` copies each scheduled compose source from
  `<config_dir>/../<workload.source>` into
  `.playground/state/workloads/<lab>/<vm>/<workload>.yml`. Missing
  sources emit `config.workload.source_missing` and abort apply
  before tofu touches infrastructure.
- New ansible role `workload_compose` reads the per-VM
  `pg_workloads` JSON, filters to `type == compose`,
  `ansible.builtin.copy`s the staged file onto the target as
  `/opt/playground/compose/<workload>/docker-compose.yml`, and runs
  `community.docker.docker_compose_v2`. Idempotent.
- Example `compose/demo.yaml` next to `config/` gives the committed
  `generic-infra` lab a real compose file to stage.

Slice 8c (done): Docker Swarm

- `assign_swarm_membership()` decides each VM's role in the lab's
  swarm. Auto-pick: first docker-capable VM (lab declaration order)
  becomes manager, other docker-capable VMs become workers.
  Non-docker VMs are `"none"`. Hybrid explicit assignment lands in
  a follow-up when `LabVm.swarm_role` or workload-level pins exist.
- New diagnostic `config.workload.swarm_needs_docker_host` fires
  when a swarm workload exists but no VM is docker-capable.
- Inventory renderer emits `[swarm_manager]` and `[swarm_worker]`
  groups when applicable, and adds a `pg_swarm_role` host var on
  participating VMs.
- New ansible role `workload_swarm` split across three task files
  (`init` / `join` / `deploy`) because Ansible can't reorder tasks
  across hosts within a single play. `site.yml` includes the role
  three times with explicit `tasks_from` against the matching host
  group. The manager's `docker swarm init` exposes the worker join
  token via host facts; workers pick it up through
  `hostvars[manager]`. Stacks deploy via
  `community.docker.docker_stack`.

Carried forward:

- Workload `networks` field (lab-level network names) still doesn't
  reach the workload_* roles. Mapping lab networks to docker
  networks is the next follow-up.
- Explicit swarm-role assignment via lab YAML.

Carried forward:

- Workload `networks` field (lab-level network names) doesn't reach
  the docker_container role yet — mapping lab networks to docker
  networks is a follow-up. `workload_to_ansible_payload` deliberately
  omits the field with a comment.

## 9. TUI

Status: in progress (first slice done).

Slice 9a (done):

- Textual-based read-only TUI in `src/playground/tui/`. Two-pane
  layout: lab list (left) + lab detail (right). Detail renders
  resolved metadata, observed status (delegates to
  `query_status`), planned actions (delegates to `render_plan`),
  budget totals, and validation diagnostics — every panel reuses
  the same primitives the CLI uses per requirements §5.8.
- New CLI entry point `playground tui` lazily imports Textual so
  the rest of the CLI still works without the `[tui]` extra.
  Missing dependency surfaces as
  `runtime.tui.missing_dependency`.
- `textual` moved from optional dependency-only to dev-deps so the
  test suite can import it; Textual `App.run_test()` Pilot drives
  the two new tests.

Slice 9b (done): mutating actions

- ``a`` / ``d`` keybindings run apply / destroy from the TUI through
  the shared service layer
  (:func:`playground.backend.local_libvirt.runner.execute_apply` /
  ``execute_destroy``). A modal confirm guards each mutating action.
- Each operation runs in a background daemon thread; the
  :class:`EventBus` ``log_line`` subscriber bridges to the foreground
  via :meth:`textual.app.App.call_from_thread`, appending to a live
  log pane bounded at ~1000 lines. The detail pane refreshes when
  the operation completes so observed status reflects the new VMs.
- Both the TUI and the CLI go through the same runner, so the
  failure protocols (run record persisted as ``failed``,
  ``operation_finished`` event with ``status=failed``) are
  identical no matter how the operator triggered the work.

Slice 9c (done): runs viewer

- ``v`` keybinding opens :class:`RunsScreen` listing recorded runs
  (newest first), rendering id / operation / status / start / end
  per row. Selecting one opens :class:`RunDetailScreen` which
  renders the persisted run record plus the full ``events.jsonl``
  timeline (one line per event, ``log_line`` events show
  ``step: line``).

## 10. Cross-VM lab support (barak-deploy) — RETIRED

Status: done, then retired. The barak-deploy-cross-vm lab and
its supporting roles (`deployment-source`, `deployment-target`,
`docker_tunneler`, `ssh_keypair_*`, `barak_deploy_*`) were
removed from the playground once barak-deploy moved its own
integration test in-tree. The lab YAML now lives in
barak-deploy's `examples/cross-vm/` and gets dropped into the
playground's `config/labs/` only when barak-deploy's pytest
harness needs to run. The only remaining coupling is the
playground CLI surface (`apply` / `status` / `destroy` /
`reset`) — exactly the integration contract a lab-platform
should expose.

The historical record below is preserved for the lessons it
encoded; the implementation is gone.

Built to host the cross-VM ship-and-deploy test described in
the original `playground-requirements.md` spec (since deleted
along with the lab). Six small slices on top of the existing
§1-§9 platform. Each slice is reviewer-pass-ready independently.

Slice 10a (done): Lab YAML schema for per-VM IPs + extra_hosts.

- `LabVm.networks` accepts both legacy `list[str]` and new
  `list[{name, ip?}]` via a before-validator. Existing labs need no
  change; new labs can pin static IPs per attachment.
- `LabVm.extra_hosts: list[str]` carries literal `/etc/hosts` lines
  (workaround for missing lab-scoped DNS, backlog item).
- `ResolvedVm` gains `network_ips: dict[net_name, ip]` and
  `extra_hosts: list[str]`.
- Two new validator diagnostics: `config.network.ip_not_in_cidr`,
  `config.network.duplicate_ip`.

Slice 10b (done): Tofu multi-network + DHCP-pinned IPs.

- `tofu/main.tf` consumes `var.networks`, `var.vm_networks`, and
  `var.vm_network_ips`. `libvirt_network` is now a `for_each` over
  the lab's `spec.networks`. Each `libvirt_domain` builds its
  `network_interface` blocks dynamically with pinned `addresses`
  when the lab requested.
- `render_tfvars` emits the new variables when the lab is
  non-trivial; legacy single-network labs still get a clean
  var-file with just `vm_names`.

Slice 10c (done): Inventory `extra_hosts` + ansible role.

- Renderer adds `pg_extra_hosts='<json>'` host var when set.
- New `extra_hosts` ansible role runs first in `site.yml` so all
  later plays can resolve peer VMs by name.

Slice 10d (done): SSH keypair distribution + docker_tunneler.

- Three new platform-generic ansible roles: `docker_tunneler`,
  `ssh_keypair_generator`, `ssh_keypair_receiver`.
- `site.yml` ordering: source bootstrap (generator) early, then
  the standard plays, then target receiver. Future multi-host
  tests reuse the same roles + groups.

Slice 10e (done): barak-deploy-specific roles + lab + Makefile.

- `barak_deploy_staging` + `barak_deploy_agent` ansible roles.
- Two new VmRoles (`deployment-source`, `deployment-target`) and
  a lab (`barak-deploy-cross-vm`) declaring two VMs on
  10.20.40.0/24 with pinned IPs and reciprocal `extra_hosts`.
- Root `Makefile` with `sync-from-barak-deploy` target.
- `ansible/files/` directory; the wheel is gitignored, the
  cross-vm config set is committed.

Slice 10f (done): Multi-VM pytest harness + docs.

- `tests/integration/multi_vm/test_cross_vm_deploy.py` skipped by
  default; runs against real libvirt when
  `PLAYGROUND_LIVE_INFRA=1` is set. Asserts every pass/fail
  criterion from the original cross-VM spec.
- `docs/developer_guide.md` gains a "Multi-VM integration tests"
  section.

Slice 10g (done): `playground exec --on <vm> <cmd>`

- New CLI subcommand resolves a lab, looks up the VM's IP via
  `fetch_vm_ips`, SSHes in with `-o StrictHostKeyChecking=accept-new
  -o LogLevel=ERROR`, streams stdout/stderr, propagates the remote
  exit code unchanged. Defaults `--lab` to the only configured lab
  when exactly one exists.
- New diagnostic IDs: `config.exec.no_command`,
  `config.exec.lab_required`, `config.exec.unknown_vm`,
  `config.exec.vm_ip_not_found`, `runtime.exec.ssh_binary_missing`.
- Closes the spec's "no multi-VM orchestration primitive" gap.

Slice 10h (done): Harness hardening against pass/fail criteria.

- `tests/integration/multi_vm/test_cross_vm_deploy.py` now asserts
  image-ID parity between central and target (criterion 1 cross-
  check) and sha256 parity between the archived tar.gz and the
  manifest's `tar_sha256` (criteria 4 + 5 cross-check). A comment
  explains why criterion 4's "source on central" check is
  unreachable after ship-deploy.sh's `trap` cleans up its tmpdir.

## 11. Baseline `common` role + lab-scoped DNS (follow-ups)

Status: done.

Two small follow-ups that close residual gaps in the cross-VM
slice. Each is independently reviewable.

Slice 11a (done): `common` baseline ansible role.

- New `ansible/roles/common/` with two tasks: UTC timezone via
  `community.general.timezone` and a minimal package install
  (`jq curl ca-certificates`). Idempotent on re-apply; uses
  `cache_valid_time: 3600` so apt skips redundant fetches.
- `site.yml` runs `common` on `[playground]` between the
  `extra_hosts` play and `Configure Playground Guests`. Spec'd
  VmRoles can now list `common` as their first ansible role and
  inherit a consistent baseline regardless of higher-level roles.

Slice 11b (done): Lab-scoped DNS — retires the `extra_hosts`
per-lab workaround.

- `LabSpec.dns_domain: str | None`; resolver defaults to
  `<lab.metadata.name>.lab` so every `ResolvedLab` has a
  populated `dns_domain` regardless of whether the YAML sets one.
- New validator diagnostic `config.network.dns_domain_invalid`
  rejects malformed overrides (uppercase, leading dot, spaces,
  labels > 63 chars, total > 253 chars).
- `render_tfvars` emits two new top-level keys: `dns_domain`
  (always) and `vm_dns_hosts` (one record per (vm, network) pin).
- `tofu/variables.tf` gains `var.dns_domain` and
  `var.vm_dns_hosts`; `tofu/main.tf` consumes both: every
  `libvirt_network` sets `domain = var.dns_domain` and renders a
  `dns { hosts { hostname, ip } }` block per registered VM.
- `tofu/cloud_init.cfg` sets `hostname: ${vm_name}` and
  `fqdn: ${vm_name}.${dns_domain}` with `preserve_hostname:
  false`, so each VM advertises the right name via DHCP and
  `hostname` returns the short name locally.
- `config/labs/barak-deploy-cross-vm.yaml` dropped its
  `extra_hosts` entries — DNS now resolves `central` / `target`
  end-to-end. The live cross-VM test asserts hostname-based ssh
  and a `getent hosts <vm>.<dns_domain>` lookup as a closing
  cross-check.

Slice 11b follow-up (done): added `enabled = true` to the
dynamic `dns` block on `libvirt_network.lab`. Without it the
dmacvicar/libvirt provider's `getDNSEnableFromResource` returns
`"no"`, libvirtxml emits `<dns enable='no'>`, and libvirt
disables dnsmasq DNS for the network entirely — silently
ignoring the `<host>` records the block populates. Cloud-init's
self-registered hostname path was still working, which masked
the defect outside the live `getent hosts <vm>.<lab>.lab`
assertion.

Carried forward to a follow-up:

- Pre-existing minor validator quirk: the `unknown_image` check
  runs once per lab, so an orphaned role surfaces N times when N
  labs are loaded. Move the check outside the per-lab loop when
  someone touches the validator next.

## 12. `playground doctor` — host prerequisite probes

Status: done.

A read-only diagnostic command that bundles the host-side checks
new operators forget. Same diagnostic shape as `playground
validate`; identical `--output {human,json}` story; exits 1 on
any error, 0 on warnings-only.

Implemented checks (`runtime.doctor.*`):

- `iso_tool_missing` — `genisoimage` or `mkisofs` on PATH (one
  of them is required to build cloud-init ISOs).
- `virsh_missing` / `virsh_unreachable` — `virsh` on PATH and
  `qemu:///system` reachable. Gates the pool checks.
- `libvirt_group_missing` / `libvirt_group_inactive` — current
  user is in the `libvirt` group (and the current session has
  picked up the membership, not just `/etc/group`).
- `default_pool_missing` / `default_pool_inactive` /
  `default_pool_no_autostart` — `default` storage pool defined,
  state == running, autostart == yes. Last one is a warning.
- `pool_path_unreadable` — every ancestor of the pool target
  path is world-traversable so libvirt-qemu can reach the
  disks. Warning; the common breakage is a pool inside `$HOME`.
- `ssh_public_key_missing` — `var.ssh_public_key_path` (default
  `~/.ssh/id_rsa.pub`) exists. Overrideable with `--ssh-key`.
- `apparmor_libvirt_unconfigured` — when AppArmor is active,
  either `security_driver = "none"` is set in
  `/etc/libvirt/qemu.conf` OR the per-VM profile machinery is
  in place (`apparmor_parser` + `/etc/apparmor.d/libvirt/`).
  Skipped silently when AppArmor isn't loaded.
- `ansible_missing` / `ansible_collection_missing` —
  `ansible-playbook` on PATH plus `ansible.posix`,
  `community.crypto`, `community.docker` collections installed.

Module: `src/playground/preflight/doctor.py`. Each check is a
pure function returning `list[Diagnostic]`. `run_all_checks` is
the orchestrator. Pure read-only; doctor never auto-remediates
— each diagnostic carries a one-line `suggestion` instead.

Follow-up (intentionally not yet shipped): wire doctor as a
pre-apply hook so `playground apply` runs the doctor checks
first and refuses to proceed on errors. Keep the manual command
either way for "what's wrong before I even try?".

Slice 12 follow-up (done): tighten the AppArmor check. The
original check verified that `/etc/apparmor.d/libvirt/` exists
and `apparmor_parser` is on PATH — both true on every stock
libvirt install, so the check returned green even when
`virt-aa-helper` was silently broken (libvirt creates the
`libvirt-<uuid>` profile but no `.files` companion, qemu then
fails to read disk images at runtime). Replaced with a direct
scan for orphan `libvirt-<uuid>` profiles without matching
`.files` companions. New diagnostic
`runtime.doctor.apparmor_orphan_profiles` (error). The
machinery-missing warning (`apparmor_libvirt_unconfigured`)
now fires only when `/etc/apparmor.d/libvirt/` doesn't exist
at all, which is rare. `security_driver = "none"` still
silences both.

## 13. `playground reset` — scrub-by-name cleanup

Status: done.

The cleanup path of last resort. ``playground destroy`` is
tofu-state-driven and refuses to make progress when state is
corrupt or out of sync with reality. ``playground reset`` skips
that dependency: it reads the lab YAML, enumerates expected
libvirt resources by name, and force-removes whatever's there.

Steps:

1. **scrub-libvirt** — for every VM in the resolved lab, run
   `virsh destroy` then
   `virsh undefine --nvram --managed-save --snapshots-metadata`;
   delete `<vm>.qcow2` and `commoninit-<vm>.iso` from the
   `default` pool. For every network, run `virsh net-destroy`
   then `virsh net-undefine`. Resources are only touched when a
   pre-flight `virsh ... --all --name` listing shows they exist,
   so the step is idempotent (re-running is a no-op). Fatal on
   missing `virsh` or unreachable libvirtd; tolerant of
   "domain not running", "network not active", etc.
2. **tofu-destroy** — best-effort. Step 1 already cleaned
   reality, so a tofu failure here just means state may hold
   stale entries; emit `runtime.reset.tofu_destroy_warning` and
   continue.
3. **clean-state-files** — remove the lab's per-lab artifacts
   under `.playground/state/{tofu,inventory,workloads}/`. Never
   touches the shared `tofu/terraform.tfstate`, the
   `ubuntu-noble.qcow2` base image, other labs' state, or any
   `OperationRun` records under `.playground/runs/`.

Diagnostic IDs: `runtime.reset.*`. Lifecycle is the standard
OperationRun (`operation: "reset"`) so reset runs show up in
`playground runs list` and the TUI alongside apply/destroy.

Module: `src/playground/backend/local_libvirt/scrub.py`
(pure scrub logic; subprocess-shimmed in tests). Orchestrator:
`execute_reset` in `runner.py`. CLI: `reset_command` in
`cli/main.py`. Tests: 7 unit tests for `scrub_lab` (every
tolerance + every fatal path) + 5 CLI tests (full pipeline,
tofu failure, mocked execute_reset, failure surface, JSON
shape).

## 14. `wait-for-vms-ready` — gate apply's tofu→ansible handoff

Status: done.

Closes the well-known timing race in `playground apply`: tofu
returns successfully (VMs created, network up, IPs pinned) and
the CLI immediately fires `ansible-playbook` — but on Ubuntu
Noble cloud-init takes 30-90 s between "VM boots" and "sshd
accepts connections", and another 1-3 min before
`package_upgrade` releases the apt lock. Ansible's gather-facts
gets "Connection refused" on a port that just isn't listening
yet, or the first `apt install` races cloud-init's own
`apt update`. Both fail the apply as if a real provisioning
step had failed.

The new `wait-for-vms-ready` step in `execute_apply` (after
inventory render, before `ansible-playbook`) gates the handoff
on per-VM readiness, in parallel via `ThreadPoolExecutor`:

1. **TCP probe** — `socket.create_connection((ip, 22))` with
   exponential backoff up to 300 s. Fast, cheap signal of
   "sshd is listening".
2. **cloud-init wait** — `ssh user@ip "cloud-init status --wait"`
   with a 600 s subprocess timeout. The remote command blocks
   on the VM side until every cloud-init stage (including
   `package_upgrade`) is finished, so the apt-lock race is
   eliminated.

Diagnostic IDs (`runtime.apply.*`): `ssh_binary_missing`,
`wait_ssh_timeout`, `wait_cloud_init_timeout`,
`wait_cloud_init_failed`, `wait_unexpected`. Each diagnostic
names the failing VM and includes a console-into-the-VM
suggestion since the operator's next step is typically
`virsh console <vm>` for cloud-init logs.

Module: `src/playground/backend/local_libvirt/wait.py`. Tests:
11 unit tests (TCP backoff, cloud-init timeout/error/done,
mixed-success VM fleet, log-order stability, SSH option hardening)
+ a new CLI test that proves the gate blocks `ansible-playbook`
from running when a VM never comes up. Existing apply CLI tests
auto-stub the wait via an `autouse` fixture so they don't probe
fake IPs.

## 15. Ship an `ansible.cfg` + wire `ANSIBLE_CONFIG`

Status: done.

Second fresh-vs-warm gap (after SSH-readiness): `playground apply`
shelled out to `ansible-playbook` without a project-level
`ansible.cfg`. On a warmed-up dev box this mostly worked because
known_hosts was populated, ControlMaster sockets lingered in
`/tmp` from earlier runs, and pipelining bugs had been worked
around manually. On a fresh box, ansible's defaults
(`host_key_checking=True`, no ControlMaster, no pipelining) made
the first apply hang on the "Are you sure you want to continue
connecting" prompt — looking like a stuck step rather than a
config gap.

Three things shipped together:

1. **`ansible/ansible.cfg`** with the canonical settings: 
   `host_key_checking = False`, `interpreter_python = auto_silent`,
   `pipelining = True`, plus `ssh_args = -o ControlMaster=auto -o
   ControlPersist=60s -o UserKnownHostsFile=/dev/null -o
   StrictHostKeyChecking=accept-new`.
2. **`ANSIBLE_CONFIG` env wiring** in `run_ansible_playbook`.
   Without this, Ansible's auto-discovery looks for
   `./ansible.cfg` relative to cwd — and the runner's cwd is the
   repo root, not `ansible/`. The file we ship was about to be
   silently ignored. The wiring is opt-in: when `ansible_cfg=` is
   not passed, the subprocess inherits the parent env as before,
   so test shims and ad-hoc `cd ansible && ansible-playbook`
   invocations behave unchanged.
3. **New doctor probe `check_ansible_config`** so the next gap
   is loud. Two diagnostics:
   - `runtime.doctor.ansible_cfg_missing` (warning) — file
     absent entirely.
   - `runtime.doctor.ansible_cfg_misconfigured` (warning) —
     file exists but missing one of the load-bearing knobs
     (`host_key_checking=False`, `pipelining=True`, or
     `ControlMaster=auto` in ssh_args). Names the missing
     setting so the fix is obvious.

Tests: 6 unit tests for the doctor probe (missing, complete,
each load-bearing knob removed in turn, flexible spacing) + 2
unit tests for the env wiring (set when ansible_cfg passed,
inherited otherwise). Total suite 310 passing, 3 skipped.

## 16. Provisioner-driven dispatch in site.yml

Status: done.

`ansible/site.yml`'s "Configure Playground Guests" play
hardcoded `roles: [docker, redroid]` against
`hosts: playground` — every VM in every lab got both roles, no
matter what its VmRole's `provisioners` list said. The bug had
two practical consequences:

1. **redroid ran on every host.** Most Linux dev-box kernels
   don't ship binder_linux / ashmem_linux, so the redroid role's
   `grep -qw binder /proc/filesystems` check failed and the
   whole apply errored out. Hard blocker for any lab whose host
   isn't on an Android-capable kernel.
2. **docker installation was implicitly load-bearing.** The
   cross-VM VmRoles (`deployment-source`, `deployment-target`)
   list-replace the parent's `[docker]` provisioner with their
   own `[docker_tunneler, ...]`. They depended on the hardcoded
   site.yml play to get docker installed at all — bug + workaround
   coupled, no role-YAML reflected the truth.

Fix is architectural: inventory and site.yml drive off the per-VM
provisioner list, not VmRole name.

- **`render_inventory`** now emits `[needs_<ansible_role>]`
  groups derived from every VM's `provisioners`. A VM whose
  VmRole provisions `[docker, docker_tunneler]` lands in both
  `[needs_docker]` and `[needs_docker_tunneler]`.
- **`site.yml`** dispatches each platform role via its
  `needs_<role>` group: `hosts: needs_docker` runs docker,
  `hosts: needs_redroid` runs redroid, etc. The three platform
  plays (`extra_hosts`, `common`, `workload_*`) stay on
  `hosts: playground` since they're truly universal.
- **`deployment-source` / `deployment-target` VmRoles** now
  list `docker` explicitly in their `provisioners` — was
  implicit through the hardcoded play, now reflected in the
  YAML.
- **New `redroid-host` VmRole** for explicit opt-in. Operators
  who want Redroid set `role: redroid-host` on the relevant
  lab VM. Generic-infra deliberately doesn't use it.
- **Redroid role's binder check** now fails with a clear
  message naming the VmRole opt-out path
  (`role: docker-host` instead of `redroid-host`, or remove
  `ansible_role: redroid` from a custom role) rather than a
  bare `failed_when: rc != 0`.

Tests: 2 new inventory tests (`needs_<role>` groups for
generic-infra and the cross-VM lab) + updated loader test
covering the new role. Suite now 313 passing, 3 skipped.
Ansible syntax-check clean.

## Backlog (acknowledged, not sequenced)

Items confirmed as real product needs but explicitly not urgent —
captured here so they aren't lost.

- `TargetSelector.network` field — requirements §5.9 calls for
  selectors keyed on **network** in addition to name / role / tag.
  Today's `TargetSelector` has `role / vm / tag / any` only.
- ~~Lab-scoped DNS — Story 5.2 / §5.6 require DNS names scoped per
  lab.~~ **Shipped.** `LabSpec.dns_domain` (resolver defaults to
  `<lab>.lab`) flows through `render_tfvars` into
  `libvirt_network.domain` plus authoritative `dns { hosts { ... } }`
  records, and cloud-init sets `hostname` / `fqdn` on each VM. The
  cross-VM lab dropped its `extra_hosts` workaround as a result.
- Runtime overrides + promote — Story 2.3 / §5.2 require temporary
  CLI/TUI overrides on top of YAML, with an explicit "promote back
  to YAML" path. Schema slot `ResolvedLab.runtime_overrides:
  list[Any]` is reserved and unused; needs a real type, a state
  store under `.playground/state/overrides/`, and CLI commands to
  set / clear / promote.
