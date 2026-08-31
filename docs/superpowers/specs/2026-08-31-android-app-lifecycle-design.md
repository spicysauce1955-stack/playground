# Android app lifecycle on Redroid VMs

**Date:** 2026-08-31
**Status:** Approved design, not yet implemented
**Builds on:** `docs/superpowers/specs/2026-08-31-cloud-redroid-design.md`

## Problem

`redroid-host` labs give you a booted Android instance reachable over ADB,
and nothing above that. Installing an APK, launching it, or looking at what
it did means dropping to `playground exec -- ...` and hand-writing adb
invocations against `127.0.0.1:5555`.

`docs/product/mvp_scope.md` lists "ADB automation" and "APK installation"
under "Deferred But Designed For". This is that layer.

## Scope

**In:** install, uninstall, launch, force-stop, list packages, grant
permissions, screenshots, logcat, key/text/tap input, file push/pull, and a
raw `adb shell` escape hatch.

**Out:** uiautomator / Appium-style element finding and scripted UI flows.
That is a separate project, not a section of this one.

## Decisions

1. **`adb` runs on the VM, driven over SSH.** Not on the controller through
   tunnels. The guest already publishes 5555 from the Redroid container, so
   `adb -s 127.0.0.1:5555` works locally on the VM. This is backend-neutral
   (libvirt, vbox NAT, DigitalOcean public IP all reach it through the same
   SSH path), needs nothing installed on the operator's machine, needs no
   tunnel lifecycle, and makes fleet fan-out ordinary parallel SSH.
2. **Two layers, one foundation.** A declarative `android_app` workload for
   reproducible labs (principle 2), and a `playground app` verb group for
   interactive work. Both drive the same on-VM adb.
3. **APKs are local files only.** `source: ./apks/foo.apk`, staged and
   pushed exactly as compose files already are. No new `ArtifactSources`
   category, no fetch step, no checksum handling — air-gap-safe by
   construction. A URL/cache category can be added later if a real need
   appears; nothing here forecloses it.
4. **Fan-out from the start.** `--on VM` / `--role ROLE` / `--all`, because
   the existing labs are fleet-shaped.
5. **Idempotency is a design constraint, not a nice-to-have** (principle 7).
   See "Idempotency" below — it is the subtlest part of this design.

## Design

### 1. Foundation: adb on the guest

The `redroid` role installs the `adb` package, behind a new default
`redroid_install_adb: true` (opt-out mirrors the existing
`redroid_install_kernel_modules`).

Every adb operation, from both layers, is:

```sh
adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true
adb -s 127.0.0.1:5555 <command>
```

The unconditional `connect` is cheap and makes each invocation independent
of whether an adb server is already running.

**Verify at implementation time** that `adb` is installable from the
`universe` pocket on the DigitalOcean `ubuntu-24-04-x64` image without
enabling extra repositories. If it is not, the fallback is
`android-sdk-platform-tools-common` or fetching platform-tools; do not
silently skip the install.

### 2. Declarative: `type: android_app`

`LabWorkload.type` gains `"android_app"` (it is a closed
`Literal["container", "compose", "swarm"]` today). Android-specific fields
go in an optional sub-model rather than widening the shared schema:

```python
class AndroidAppOptions(StrictModel):
    package: str            # required; drives idempotency and verify
    launch: bool = False    # "ensure running", not "start every apply"
    permissions: list[str] = Field(default_factory=list)
    reinstall: bool = False # force `adb install -r` even when present
```

`LabWorkload` and `ResolvedWorkload` both gain
`android: AndroidAppOptions | None = None`.

Lab syntax:

```yaml
workloads:
  - name: my-app
    type: android_app
    source: ./apks/my-app.apk
    placement:
      target_role: redroid-host
    android:
      package: com.example.app
      launch: true
      permissions: [android.permission.CAMERA]
```

**Validator additions:**

- `config.workload.android_options_missing` (error): `type: android_app`
  without an `android:` block. `package` cannot be inferred from the APK
  without parsing it, and every idempotency and verify check needs it.
- `config.workload.android_options_ignored` (warning): an `android:` block
  on a workload of another type.
- `config.workload.android_target_not_capable` (warning): placement
  resolves to a VM whose role capabilities lack `redroid: true`. Warning,
  not error, per principle 10.

**Scheduling.** `_pick_target_vm`'s `auto` branch hardcodes
`capabilities.get("docker")`. It becomes capability-by-type: `android_app`
auto-places on a VM with `capabilities['redroid']`, everything else keeps
today's docker behavior. This reuses the capability vocabulary made
load-bearing by the cloud-redroid work.

**Staging.** `stage_workload_files` currently skips `type == "container"`
and stages the rest. `android_app` stages like compose — the `.apk` is
copied into the per-lab state dir and its path travels to the role as
`staged_source`. APKs are larger than compose files but this is a local
copy, not a transfer.

**New role `ansible/roles/workload_android_app`**, added to `site.yml`
after the `redroid` play and alongside the other `workload_*` plays. It
follows the established skeleton exactly: parse `pg_workloads`, `end_host`
when empty, loop filtered on `item.type == 'android_app'`.

Per workload:

1. `ansible.builtin.copy` the staged APK to
   `/opt/playground/apks/<name>.apk`.
2. `adb connect`, then query installed packages once.
3. Install when the package is absent, or when `reinstall: true`:
   `adb install -r <apk>`.
4. Grant each listed permission (`adb shell pm grant <pkg> <perm>`).
5. When `launch: true`, start the app only if it is not already running.

### 3. Imperative: `playground app`

A Typer sub-app (`app.add_typer(app_app, name="app")`), matching the
existing `lab` / `inventory` / `tofu` / `runs` groups.

| Subcommand | Behavior |
|---|---|
| `install APK [APK...]` | copy to VM, `adb install -r` |
| `uninstall PACKAGE` | `adb uninstall` |
| `launch PACKAGE` | `monkey -p PKG -c android.intent.category.LAUNCHER 1` |
| `stop PACKAGE` | `am force-stop` |
| `list [--pattern P]` | `pm list packages` |
| `screenshot [--out DIR]` | `screencap -p`, pulled to the controller |
| `logcat [--follow] [--lines N]` | `logcat -d` or streaming |
| `input text\|tap\|key ARGS` | `input <subcommand>` |
| `push SRC DEST` / `pull SRC DEST` | `adb push` / `adb pull` |
| `shell -- CMD` | raw `adb shell`, escape hatch |

**Targeting**, shared by every subcommand: `--lab`, plus exactly one of
`--on VM`, `--role ROLE`, `--all`. `--all` selects every VM whose resolved
capabilities include `redroid: true` — not every VM in the lab. When no
target flag is given and the lab has exactly one Android VM, use it;
otherwise require an explicit flag, mirroring how `exec` defaults `--lab`.

**Fan-out** runs targets in parallel with a `ThreadPoolExecutor`, the same
pattern `verify.py` uses for per-VM checks. Output is prefixed per device.
The exit code is 0 only if every device succeeded; otherwise 1, with a
per-device summary naming which failed.

Two deliberate limits:

- `logcat --follow` requires a single device. Multiplexing live streams is
  not worth the complexity; the error names `--on`.
- `screenshot` under fan-out writes `<out>/<vm>-<timestamp>.png` so
  concurrent devices cannot collide on one filename.

**Diagnostics** follow the existing namespaces: `config.app.*` for input
and targeting faults (`no_android_vms`, `unknown_vm`, `target_required`),
`runtime.app.*` for execution faults (`adb_failed`, `device_unreachable`).

### 4. One shared SSH argv helper

`exec`, `cp`, `verify.py`, and `adb` each hand-build an ssh command line
carrying the same `StrictHostKeyChecking=accept-new`,
`UserKnownHostsFile=/dev/null`, `LogLevel=ERROR`, and the same
"add `-p` only when the port is not 22" rule. `app` would be the fifth
copy.

Extract `build_ssh_argv(host, *, user, port, remote_command=None,
local_forward=None, extra_opts=())` into one module and move all five onto
it. This is behavior-preserving: the existing `exec`, `cp`, and `adb` tests
assert the resulting argv and must pass unchanged. Scoped to this
extraction — no other refactoring rides along.

### 5. verify-lab

When a lab declares `android_app` workloads, `verify-lab` asserts each
declared package is present in `pm list packages` on its target VM, as a
fourth per-VM sub-check gated the way `has_docker` gates the docker check.

This needs plumbing that does not exist: `verify.py` never calls
`schedule_workloads`, so `verify_lab` must receive the schedule (or compute
it) to know which packages belong on which VM. Keep the existing
warning-only severity model — a failed app check must not fail the run.

## Idempotency

The subtlest part of the design, and where a careless implementation breaks
principle 7.

- **Install** is naturally convergent: query `pm list packages` first, skip
  when present unless `reinstall: true`.
- **Launch is not.** "Start the app" would report `changed` on every single
  apply. So `launch: true` means *ensure running*: check whether the process
  exists (`pidof <package>`) and start it only when it does not. A lab that
  declares `launch: true` and is applied twice must report `changed=0` on
  the second run.
- **Permissions** are convergent: `pm grant` on an already-granted
  permission is a no-op, but the role should still check first so the task
  does not report `changed` every run.

The live validation must include a second apply asserting `changed=0`,
exactly as the cloud-redroid work did — that is what caught real bugs there.

## Testing

- **Unit:** the `AndroidAppOptions` model; the three new validator
  diagnostics; capability-aware `auto` placement; `.apk` staging.
- **Ansible:** a structural test in the style of
  `tests/unit/ansible/test_redroid_binder_remediation.py`, asserting the
  role queries installed packages before installing and checks `pidof`
  before launching — the two guards that make it idempotent.
- **CLI:** ssh-shim tests (`_write_ssh_shim`) asserting the adb argv each
  subcommand builds, that `--all` selects only `redroid: true` VMs, that
  fan-out exits non-zero when one device fails, and that `logcat --follow`
  rejects multi-device targeting.
- **Live, on DigitalOcean:** apply a lab with an `android_app` workload,
  verify the package installs and launches, re-apply for `changed=0`, and
  exercise the imperative verbs including a screenshot.

### The test-APK problem

Live validation needs a real APK. Committing one drags in licensing
questions and repo bloat, and downloading one breaks air-gap testing.

**Round-trip an app already on the device instead:** `adb shell pm path
<existing package>` gives the APK path on the device, `adb pull` fetches it
to the controller, and that file becomes the fixture for `install -r`. The
Redroid 11 image carries 142 packages, so a suitable one is guaranteed
present. No external download, works air-gapped, and it exercises pull,
push, and install in one flow.

Pick a package whose reinstall cannot brick the device — a leaf app, never
a system service. Verify the chosen package is a normal app
(`pm list packages -3` or a non-`/system` path) before relying on it.

## Risks

**Idempotent launch is the most likely thing to get wrong**, and it fails
quietly: the lab still works, the second apply just reports `changed`. The
test suite must assert it rather than trusting review.

**APK size in state.** Staging copies APKs into the per-lab state dir; a
large APK is copied on every apply. Acceptable for now — note it, and
revisit with a checksum guard if it becomes annoying.

**`adb connect` races a still-booting Android.** Immediately after apply,
`sys.boot_completed` may not be 1 yet, so an install can fail on a device
that is merely slow. The role and the `install` verb should wait for
`sys.boot_completed=1` with a bounded timeout before acting, and say so
clearly on timeout rather than emitting a raw adb error.

**Fan-out error reporting.** With N devices, one failure among many must
not be lost in interleaved output. Per-device prefixes plus an explicit
end-of-run summary of failures.

## Open questions for implementation

1. Is `adb` installable from `universe` on the DO Noble image without
   extra repositories? Determines whether the foundation task is one apt
   line or something larger.
2. Which on-device package is the safest round-trip fixture? Decide by
   inspection on a live device, not by guessing.
