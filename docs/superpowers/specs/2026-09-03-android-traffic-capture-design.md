# Android device traffic capture

**Date:** 2026-09-03
**Status:** Approved design, not yet implemented
**Builds on:** `docs/superpowers/specs/2026-08-31-android-app-lifecycle-design.md`
**Research:** `docs/research/android-emulation/network-proxy-tunneling.md`

## Problem

A `redroid-host` lab gives a booted Android instance you can install apps
on and drive over ADB. What it cannot tell you is what that instance talks
to. Answering "where did this app send my data" today means hand-rolling a
`tcpdump` over `playground exec` and guessing which interface belongs to
which container.

The app-lifecycle spec deferred this explicitly ("Network proxying, TLS
interception, and packet capture -- these get their own spec"). This is
that spec, for the packet-capture half.

`docs/product/user_stories.md` Story 12.2 reserved the seam: "the model can
later attach capture points to host, network, VM, container, or device
scope" and "artifacts can later store packet captures". `mvp_scope.md` lists
`.pcap` artifacts under deferred-but-designed-for. Nothing here widens the
model beyond what those two lines already promised.

## Scope

**In:** passive, full-fidelity packet capture of one Redroid device's own
network namespace, started and stopped by an explicit CLI verb, landing as
a `.pcap` run artifact on the operator's machine.

**Out:**

- **TLS interception.** Deliberately a second slice, with the seam designed
  in here (see "Phase 2 seam"). Decrypting HTTPS needs a MITM proxy, a CA in
  Android's *system* trust store, and an answer for certificate pinning --
  each an independent source of failure. Shipping passive capture first
  means the plumbing (role, unit, state, artifacts, CLI group) is proven
  before any of that lands.
- **`local-vbox`.** Its provider config declares `redroid: false`; Redroid
  is unverified on that backend, so there is no device to capture.
- **Capturing non-Android workloads.** A `compose` workload on a
  `docker-host` is a legitimate future capture target and the schema does
  not preclude it, but nothing asks for it today. YAGNI.
- **Certificate-pinning bypass** (Frida / objection / APK patching). The
  research doc is right that this must not be default product behavior.
- **In-device capture** (PCAPdroid / VPNService). Rejected below.

## Decisions

1. **Capture from the host VM, in the device's network namespace.** Not
   inside Android, not on a gateway VM. The Redroid container is the only
   thing in its netns, so entering it makes `-i any` mean exactly "this
   device's traffic" -- no veth-to-container mapping, no filtering out
   bystander containers, no packets missed because they were not IP.

   Rejected: **in-device PCAPdroid**, because VPNService changes the network
   path apps see and some apps misbehave under it -- capture that perturbs
   the thing being measured. Rejected: **a dedicated gateway VM**, because
   `config/roles/router.yaml` has no `ansible/roles/router` behind it yet,
   so it is a prerequisite project rather than a design choice, and it buys
   only inter-VM visibility this problem does not need.

   This is the "capture at infrastructure boundary" row of
   `network-proxy-tunneling.md`, sharpened from "Docker bridge capture on
   the Redroid host" to the container's own namespace.

2. **`nsenter --net` plus a systemd unit, not a sidecar container.**
   `tcpdump` comes from Ubuntu main, so an air-gapped lab needs no new
   declared image and the lab's `budget.max_containers` is untouched. Only
   `--net` is entered: the mount namespace stays the guest's, so the pcap
   lands on the VM's disk rather than inside a container whose writes vanish
   when it is recreated. systemd supplies supervision, journald logs, and
   survival across the operator's SSH disconnect -- and the repo already has
   the precedent in `redroid-binderfs.service`.

3. **Declarative capability, imperative sessions.** `spec.capture` in the
   lab YAML says what capture *may* do (limits, retention); the CLI decides
   when it happens. A capture that ran for a lab's whole life would produce
   one unbounded artifact nobody asked for; bracketing an experiment is the
   actual workflow.

4. **Installed on every `redroid-host`.** The cost is one apt package and a
   stopped unit. The alternative -- a separate opt-in role -- means
   discovering you want capture, editing YAML, and re-applying before you
   can look at anything, which is exactly when you least want to re-apply.
   `spec.capture.enabled: false` remains the opt-out for a lean or
   air-gapped guest.

5. **The container PID is resolved at start time, never at provision time.**
   The Redroid container runs `restart_policy: unless-stopped`, so its PID
   changes across restarts. A PID baked into a unit file at provision time
   is a stale-state bug that only shows up after the first restart.

## Design

### 1. Schema

`CaptureOptions` in `src/playground/models/kinds.py`, hung off `LabSpec` as
`capture`, and mirrored onto `ResolvedLab` by the resolver:

```yaml
spec:
  capture:
    enabled: true        # provision the tooling; sessions still start via CLI
    max_file_mb: 100     # tcpdump -C
    max_files: 10        # tcpdump -W
    snaplen: 0           # tcpdump -s; 0 = whole packet
```

`max_file_mb * max_files` is a hard ceiling per device per session -- 1 GB
at these defaults. This is the disk guard, and it is not optional: pcaps
grow without bound, `redroid-host` asks for a 60 GB disk, and filling a
guest's root filesystem breaks sshd and therefore every other verb.

Two properties of that ceiling must be stated rather than assumed:

- **`-C` counts units of 1,000,000 bytes, not MiB.** `max_file_mb: 100`
  produces 100 MB files, ~4.9% smaller than 100 MiB. The field name says
  `mb` and means exactly what tcpdump does.
- **`-C` with `-W` is a ring, so it overwrites the oldest file.** A session
  that outlives its ceiling keeps the most recent 1 GB and silently discards
  the beginning. `status` reports file count so a session sitting at
  `max_files` is visible as "wrapping"; raising the limits is the operator's
  call, and it is better than the alternative of filling the disk.

  > **Post-implementation correction (Task 9):** "hard ceiling" is only
  > true within one uninterrupted `tcpdump` process. `-W`'s ring counts
  > files **per base name**, and the wrapper mints a fresh timestamp on
  > every unit start — including every Redroid container restart, since
  > `Restart=always` re-execs it. So the true ceiling is `max_file_mb *
  > max_files` **per session-start**, and N restarts permit up to N times
  > that much on disk, not one fixed cap. This is a deliberate trade
  > (see the `capture-start` wrapper comment): one fixed filename would
  > give a true global cap, but would silently TRUNCATE the previous
  > session's data on every restart instead. See
  > `docs/architecture/CONTRACTS.md` → "Capture: Redroid device traffic"
  > for the full argument.

Defaults live in `config/defaults.yaml` next to `retention`, so a lab that
declares no `capture` block still gets the bounded ring.

`enabled: false` is a provisioning switch, not just a CLI gate: the
`needs_capture` play is skipped entirely, so no package is installed and no
unit exists. The CLI then fails with `config.capture.disabled` rather than
reporting a dead unit. This is the air-gap and lean-guest opt-out, and it is
the only way `spec.capture` changes what apply does.

### 2. Role wiring

`config/roles/redroid-host.yaml` gains one capability and one provisioner:

```yaml
  capabilities:
    redroid: true
    capture: true
  provisioners:
    - ansible_role: docker
    - ansible_role: redroid
    - ansible_role: capture
```

`_provisioner_group()` in `backend/local_libvirt/inventory.py` derives
`[needs_<role>]` from provisioners generically, so `[needs_capture]` appears
with no renderer change. `ansible/site.yml` gains one play, `hosts:
needs_capture`, ordered after the `redroid` play (the wrapper script
resolves a container that must already exist).

Note the resolver list-*replaces* `provisioners` on `extends`, which is why
all three are re-listed rather than appended.

### 3. Ansible `capture` role

`ansible/roles/capture/` installs `tcpdump` and templates three files.

**`/usr/local/lib/playground/capture-start`** -- resolves the container name
to a live PID, then execs:

```
exec nsenter --target "$pid" --net \
  tcpdump -i any -U -s "$snaplen" -Z root \
    -w "/var/lib/playground/capture/$vm/%Y%m%d-%H%M%S.pcap" \
    -C "$max_file_mb" -W "$max_files"
```

> **Post-implementation correction (Task 9):** the `-w` argument above
> does not do what it looks like it does. `tcpdump` applies `strftime()`
> to `-w` only when `-G` is also given; under `-C`/`-W` alone it writes
> the name **verbatim** and appends a bare rotation counter. Verified
> live: `-w '%Y%m%d-%H%M%S.pcap' -C 1 -W 3` produced a file literally
> named `%Y%m%d-%H%M%S.pcap0`, not a timestamped one. The shipped
> wrapper instead computes the stamp itself with `date -u
> +%Y%m%d-%H%M%S` before invoking `tcpdump`, and passes a literal
> `-w "${dir}/${stamp}.pcap"`. On disk this becomes
> `<stamp>.pcap0`, `<stamp>.pcap1`, ... — so every CLI command that
> globs these files (`status`, `fetch`, `stop`'s cleanup) matches
> `*.pcap*`, never `*.pcap`.

**`playground-capture@.service`** -- `%i` is the VM name.
`After=docker.service`, `Restart=always`, `RestartSec=5`: if the Redroid
container restarts and takes its netns with it, the unit re-resolves the new
PID and resumes rather than silently ending mid-experiment.

> **Post-implementation correction (Task 9):** two things this
> paragraph omitted turned out to matter:
>
> - The unit is `Type=exec`. Under that type, `systemctl start`
>   completes when `/bin/sh` execs -- not when the wrapper's final
>   `exec nsenter ... tcpdump` succeeds -- so a wrapper that dies
>   immediately (no container, an AppArmor denial) still reports a
>   clean start. `playground capture start` compensates: it starts the
>   unit, sleeps into the `RestartSec=5` gap, and requires `systemctl
>   is-active` to report exactly `active` before it records a session.
> - The `[Unit]` section also sets `StartLimitIntervalSec=300` /
>   `StartLimitBurst=20`, wider than systemd's built-in default. At the
>   default window, `Restart=always` + `RestartSec=5` never trips (5
>   starts per 10s does not fire at a 5s interval), so a permanently
>   broken wrapper would retry forever instead of ever reaching
>   `failed`, where `is-active` could actually surface it. The explicit
>   window absorbs a normal container restart (1-2 starts) while a
>   genuinely broken wrapper burns all 20 within roughly 100 seconds.

**`/etc/apparmor.d/local/usr.sbin.tcpdump`** -- a write rule for
`/var/lib/playground/capture/**`.

Two Ubuntu packaging defaults make the naive version of this role fail on a
fresh guest, both instances of the recurring shape `CONTRACTS.md` names
("library default wrong for fresh state"):

- Debian/Ubuntu `tcpdump` drops privileges to the unprivileged `tcpdump`
  user unless told otherwise, and then cannot write into a root-owned
  directory. Hence `-Z root`.
- Ubuntu ships an AppArmor profile confining where `tcpdump` may write, and
  `/var/lib/playground/` is outside it. The shipped profile includes
  `<local/usr.sbin.tcpdump>`, so a local override plus a profile reload is
  the supported fix rather than disabling confinement.

> **Post-implementation correction (Task 9):** the capture directory is
> created `0755`, not the `0750` this design implicitly assumed for a
> root-owned, secret-adjacent path. It doesn't need to be tighter:
> tcpdump's savefiles land as `0644 root:root` regardless of the parent
> directory's mode, so a `0750` parent would block traversal while the
> files underneath stayed world-readable anyway. `capture status`'s
> `find` calls and `capture fetch`'s `scp` are deliberately
> unprivileged (only `start`/`stop`/`clean` go through `sudo -n`), so
> the SSH user genuinely needs to traverse this directory.

`-i any` inside a netns yields the Linux "cooked" (SLL) link type rather
than Ethernet headers. Wireshark and `tshark` read it natively; anything
downstream that assumes an Ethernet link type must be told otherwise.

### 4. CLI: `src/playground/cli/capture_commands.py`

A new module registered as `app.add_typer(capture_app, name="capture")`.
`main.py` is 2064 lines; `app_commands.py` established the precedent for a
verb group living in its own file, including the deferred `_main()` import
that avoids the circular import back into `main`.

| Verb | Action |
| --- | --- |
| `start` | `systemctl start playground-capture@<vm>`; write the session record |
| `stop` | `systemctl stop`; seal the record |
| `status` | `systemctl is-active` plus file count and total bytes per device |
| `fetch` | `scp -r` the guest's capture directory into a run artifact dir |

`--lab / --on / --role / --all / --user` are the same flags
`app_commands.py` defines, fan-out reuses `android.runner.run_on_targets`,
and command strings are pure builders in `src/playground/android/` (or a
sibling `capture/commands.py`) so every shape is unit-testable without a
guest -- the same split that spec's decision 4 established.

Targeting reuses `android.targets`: `android_vm_names()` already filters on
`capabilities.get("redroid")`, so a sibling filtering on `capture` returns
the same `AndroidTarget` shape. No new targeting vocabulary.

### 5. State and artifacts

Session records: `.playground/state/capture/<lab>/<vm>.json` -- session id,
unit name, start time, the resolved limits. `playground reset` and
`destroy` scrub `state/capture/<lab>/`.

> **Post-implementation correction (Task 9):** `destroy` does not scrub
> this. `_clean_state_files` is wired only into the three backends'
> `execute_reset` paths; `destroy` removes no subsystem's state
> (`tofu/`, `inventory/`, `workloads/`, `vbox/`, `cloud-digitalocean/`)
> either, so capture matches the existing convention rather than being
> a special case. `playground reset` alone scrubs
> `state/capture/<lab>/`. This matters because a stale record makes
> `capture start` refuse with `already_running` after a teardown: the
> record lives on the operator's machine, but the unit it names left
> with the guest.

Fetched pcaps: `.playground/runs/<run-id>/artifacts/capture/<vm>/`, with the
run allocated through the existing `start_run` / `finish_run` in
`runs/operation.py`, so a fetch appears in `playground runs list` like any
other operation.

### 6. Diagnostics

Following the established id grammar:

- `config.capture.no_capture_vms` -- lab has no VM with the capability
- `config.capture.unknown_vm` -- `--on` names a non-capturable VM
- `config.capture.disabled` -- `spec.capture.enabled: false`
- `runtime.capture.already_running` -- `start` against a live session
- `runtime.capture.not_running` -- `stop` / `fetch` with nothing captured
- `runtime.backend.verb_not_supported` -- existing id, reused for
  `local-vbox`

  > **Post-implementation correction (Task 9):** shipped as
  > `config.capture.backend_unsupported` instead, a new id. The
  > existing helper's message and suggestion are hardcoded to
  > suspend/resume billing framing ("charge for idle compute"), which
  > would misinform an operator hitting this on `local-vbox`, where
  > billing is not the issue -- there is simply no Redroid device to
  > capture.

## Idempotency

The role converges to `changed=0`: package present, three templated files
unchanged, unit enabled-but-stopped. It does **not** start capture -- a
re-apply must never begin recording, and must never interrupt a session
already in progress.

`start` on a running session is a diagnostic, not a silent no-op, because
"already capturing" and "just started capturing" produce differently
attributable pcaps.

## Testing

Unit (no guest): command-string builders, `CaptureOptions` defaults and
validation, ring-size arithmetic, `[needs_capture]` group derivation,
session-record round-trip, diagnostic ids.

Live on `local-libvirt`: apply, `capture start`, generate traffic with
`playground app` (launch an app, let it phone home), `stop`, `fetch`, then
assert the pcap is non-empty and `tshark -r` parses it. Re-apply mid-session
to prove the role neither stops nor restarts a live capture. Restart the
Redroid container to prove the unit re-resolves the PID.

Then one `cloud-digitalocean` pass. Every live-only bug this repo has found
in the Redroid path surfaced there, not on libvirt.

## Risks

- **AppArmor.** The highest-probability live-only failure, and it will
  present as `tcpdump: <path>: Permission denied` with a correct-looking
  root-owned directory. The local override is the planned fix; confirming it
  reloads correctly under Ansible is a live task.
- **Disk.** Bounded by the ring, but ten devices at 1 GB is 10 GB. `status`
  reports bytes so the operator can see it accumulating.
- **QUIC / HTTP3.** Captured faithfully at the packet level, and opaque.
  This is a limit of passive capture, not a defect -- and it is another
  reason phase 2 exists.
- **`-Z root`** widens what the capture process runs as, on a guest that is
  already running a `--privileged` container. Acceptable in a throwaway lab;
  worth not copying into anything long-lived.
- **Container restart mid-session** produces one pcap per netns generation
  rather than a single continuous file. The timestamped filenames make the
  discontinuity visible instead of hiding it.

## Phase 2 seam

TLS interception reuses every layer above and adds:

- `mitmproxy` on the guest, and `nsenter --net iptables -t nat -A OUTPUT -p
  tcp --dport 443 -j REDIRECT --to-port 8080` -- in the *same* namespace
  this spec already enters. No new capture mechanism.
- A CA generated per-lab into `.playground/state/capture/<lab>/`,
  gitignored, never logged. The private key is a secret under the PRD, and a
  committed one would be a repo-wide trust hole. Only the public cert
  reaches the device.
- The cert bind-mounted into `/system/etc/security/cacerts` at
  `docker_container` create time. This works because the pinned image is
  `redroid/redroid:11.0.0-240527` and Android 11 still keeps trusted roots
  as hashed files on the system partition. **Android 14+ moved that store
  into the Conscrypt APEX**, so bumping the image tag past 13 invalidates
  this approach -- a constraint that belongs in `CONTRACTS.md` when phase 2
  lands, alongside the existing note that the tag must stay date-stamped.
- Certificate pinning still defeats it for some apps. That is expected, and
  the research doc's position holds: Frida is authorized-testing tooling,
  not default product behavior.

## Open questions for implementation

1. Do the pure command builders belong in `playground/android/commands.py`
   or a new `playground/capture/commands.py`? Capture is device-scoped today
   but not Android-specific in principle. Leaning toward a new module, on
   the grounds that `android/` is about adb and this is not.
2. Should `fetch` default to leaving the guest-side pcaps in place or
   removing them? Leaning leave-in-place, with `--clean` to remove, so a
   failed scp cannot lose the only copy.
3. Does `verify-lab` grow a capture assertion? A stopped unit that is
   *loadable* is worth asserting; anything more starts recording.
