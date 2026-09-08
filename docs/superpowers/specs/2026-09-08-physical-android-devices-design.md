# Physical Android devices as first-class lab targets

**Date:** 2026-09-08
**Status:** Approved design, not yet implemented
**Builds on:**
`docs/superpowers/specs/2026-08-31-android-app-lifecycle-design.md`,
`docs/superpowers/specs/2026-09-03-android-traffic-capture-design.md`

## Problem

Every Android verb in the repo -- `playground app *`, `playground capture
*`, `playground adb` -- is, mechanically, *SSH into a Linux guest and run
`adb` there against `127.0.0.1:5555`*. `AndroidTarget` is
`(vm_name, ssh_host, ssh_port, ssh_user)`; `run_on_targets` calls
`build_ssh_argv` unconditionally; "which VMs are Android?" is answered by
`vm.capabilities["redroid"]`; capture is `nsenter --net` into the Redroid
container's network namespace from the guest that hosts it.

A physical handset plugged into the operator's machine violates all of
that at once. There is no guest, no SSH hop, no container netns, and the
device's address is a USB serial rather than a host/port pair.

The operator wants four things a Redroid container cannot give:
real-device fidelity (Play Integrity, ARM-only native libraries, real
GPU, real radios), traffic from a real handset on real networks, rooted
on-device research, and eventually several handsets driven uniformly.

The goal, in the requester's words, is to "work with it just like we are
currently working with the emulators" -- so the verb surface must stay
one surface, not two.

## Scope

**In:**

- A phone as a declarable, targetable member of a lab, reachable by every
  existing `app` / `adb` / `shell` verb with no new verb vocabulary.
- Device discovery, a persistent known-device registry, committable
  device profiles, and operator-authored markdown notes per device.
- Adoption that is strictly additive to the handset.
- Traffic capture from both rooted and unrooted handsets, behind the
  existing `capture start|stop|status|fetch` verbs.

**Out:**

- **Wireless ADB and remote-host ADB.** The transport seam this spec
  introduces is exactly where they would attach later; nothing here
  precludes them. Not designed for today because the operator attaches
  by USB to the machine running the CLI.
- **TLS interception.** Still the Phase 2 seam the capture spec named.
  Unchanged by this work.
- **Provisioning the handset's OS.** No flashing, no rooting, no OTA
  handling. Playground consumes the device as it finds it.
- **A `DeviceRole` kind.** See "Rejected alternatives".
- **Multi-device fan-out ergonomics** beyond what `--all` already does.
  The transport seam makes it work; tuning it is not this slice.

## Decision 1: transport polymorphism at the runner seam

Three shapes were considered.

**A. Transport polymorphism (chosen).** `AndroidTarget` carries a
transport; `android/commands.py` returns adb *arguments* rather than a
rendered shell string; two renderers turn those arguments into either an
SSH-wrapped guest command or a local `adb -s <serial>` argv. Everything
above the runner -- targeting flags, fan-out, ordering, exit-code
aggregation, the entire `app` and `capture` CLI -- is untouched.

**B. Make the phone look like a guest** by pointing the existing SSH path
at `ssh localhost`. Nearly free to implement, and rejected: it requires
sshd on the operator's workstation, pays an SSH hop per adb call, and is
a fiction that leaks the moment a diagnostic says "VM not reachable"
about a phone.

**C. A parallel `playground device …` verb group** with its own runner.
Zero regression risk to existing paths, and rejected: it doubles the CLI
surface, duplicates the capture state machine, and directly contradicts
the stated goal of the phone behaving like the emulators.

### The refactor, concretely

```python
@dataclass(frozen=True)
class GuestAdb:
    ssh_host: str
    ssh_port: int
    ssh_user: str

@dataclass(frozen=True)
class LocalAdb:
    serial: str

@dataclass(frozen=True)
class AndroidTarget:
    name: str                      # renamed from vm_name
    transport: GuestAdb | LocalAdb
```

`android/commands.py` builders return the adb argument list *after* the
serial: `["install", "-r", path]`, `["shell", script]`.

- `GuestAdb` renders `adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true;
  adb -s 127.0.0.1:5555 <quoted args>` and hands it to `ssh`.
  **Byte-identical to today's output.**
- `LocalAdb` renders `["adb", "-s", serial, *args]`, executed via
  `subprocess` with **no shell in the chain at all**.

This is the highest-risk edit in the spec. `adb_shell`'s quoting is the
most carefully reasoned code in the repo: it defends against the guest
login shell and the device's own `sh -c` each parsing the command string,
and `docs/architecture/CONTRACTS.md` documents the hazard. That defense
does not disappear -- it *moves* into the `GuestAdb` renderer, which is
the only path that has a shell to defend against. The `LocalAdb` path is
structurally immune because no shell ever sees the argv.

**Mitigation is procedural, and it is a hard requirement:**
characterization tests that snapshot every current builder's exact output
string land *first*, on unchanged code. The refactor is then judged by
those tests reproducing the guest rendering byte-for-byte, before a line
of device code is written.

Two further consequences of the transport seam:

- **File staging becomes transport-conditional.** `app install` currently
  scp's an APK to the guest and installs from the guest path; `LocalAdb`
  installs the local path directly. Symmetrically, `pull` and
  `screenshot` land straight on the controller instead of
  guest-then-scp.
- **`capture/targets.py`'s `_CAPTURE_BACKENDS` allowlist becomes
  capability-based.** A device's capturability has nothing to do with
  `spec.backend`; today's hardcoded tuple would wrongly exclude a phone
  in a `local-vbox` lab.

## Decision 2: config declares intent, state holds facts

The resolver produces `ResolvedLab` from configuration alone, with no
handset attached. It therefore *cannot* hold probed facts about a phone.
`LabStatus`'s own docstring already draws this line for VMs -- status is
"a snapshot comparison between **intent** and **observed state**", and VM
IPs live in observed state, never in config. Devices follow the same
rule.

Config names *which* phone and *what it must be*. It never describes what
the phone *is*.

```yaml
spec:
  backend: local-libvirt      # governs VMs only; devices ignore it
  devices:
    - name: pixel7            # logical slot; this is what --on takes
      kind: android
      profile: my-pixel       # optional, see Decision 3
      match:                  # OPTIONAL selector
        serial: 1A2B3C4D5E    # any subset; model/manufacturer also allowed
      require:                # OPTIONAL assertions, checked at adopt time
        root: true
        min_sdk: 30
      required: true          # apply fails if the slot cannot be filled
```

- **`match` is a selector, not a description, and it is optional.**
  Omitted with exactly one device attached binds that device. Omitted
  with several attached fails with a diagnostic *listing the candidates
  and their serials*. The operator never needs to know a serial before
  plugging a phone in.
- **`require` is the honest direction of the dependency.** Config cannot
  know a phone is rooted; it can assert that this lab needs root, which
  adoption verifies and reports against the actual device.
- **Devices are backend-independent.** A `local-libvirt` lab may own a
  phone, because no backend provisions it. Backend dispatch runs
  unchanged for VMs; a device phase runs alongside it regardless of
  backend.
- **Devices are excluded from budget accounting.** They consume no host
  vcpu, memory or disk, so `max_vms` must not count them.

### Model changes

- `LabDevice` in `models/kinds.py`; `LabSpec.devices: list[LabDevice] = []`.
- `ResolvedDevice` in `models/resolved.py` -- **pure intent**:
  `name, kind, match, require, tags, required`. No capabilities field;
  that was an error in an earlier draft of this design.
- `ResolvedLab.devices: list[ResolvedDevice] = []`.
- `DeviceStatus` in `models/status.py` and `LabStatus.devices` -- observed
  state: bound serial, model, SDK, ABI, root, and
  `state: connected | unauthorized | offline | missing`.

`unauthorized` is a first-class state rather than a flavour of
unreachable. Never having tapped the RSA trust prompt is the single most
common real-world failure, and it deserves a diagnostic that says so with
the remedy, not "device not reachable".

Capability gating for `app` and `capture` targeting therefore reads
**status**, not resolved -- which the existing
`resolve_targets(resolved, status, ...)` signature already accommodates
without a signature change.

## Decision 3: two stores, split by owner

| | `.playground/state/devices/registry.json` | `config/devices/<name>.yaml` |
|---|---|---|
| Owner | machine | operator |
| Written by | every probe, automatically | only on an explicit yes |
| Holds | serial, model, SDK, ABI, root, first/last/verified seen, private notes | `match`, `require`, `tags`, shared notes (`.md` sidecar) |
| Committed to git | no | yes |

The **registry** is every handset this machine has ever seen. It is what
makes a device reusable across labs and across time: `device list` reads
it, so a phone that is currently unplugged still appears as
known-but-absent. It is machine-owned and never hand-edited.

The **profile** is a new `DeviceProfile` kind sitting alongside `VmRole`,
`NetworkProfile` and `CommandPreset`, so it reuses the existing
`ResourceEnvelope`, loader and validator machinery rather than inventing
a parallel format. Labs reference it by name and may override per-lab,
layering the way roles already do.

Per-device state paths follow `capture/state.py`'s established layout --
`.playground/state/devices/<lab>/<name>.json` for the per-lab binding,
with the cross-lab registry at the subdirectory root.

**Nothing is ever written under `config/` without an explicit human yes.**
Auto-writing tracked files would put churn in `git status` on every adb
poll. Only three paths create a profile, each an explicit human action:
answering yes to the discovery prompt, `playground device save <serial>
--as <name>`, and writing a shared note against a device that has no
profile yet.

### Notes

Two note surfaces, keyed and stored differently because they answer
different questions. **The shared, committed surface is the default**
(operator decision, 2026-09-08; the rationale for the alternative is
preserved below).

| | shared note | private note |
|---|---|---|
| Path | `config/devices/<name>.md` | `.playground/state/devices/<serial>/notes.md` |
| Keyed by | profile name | **serial** |
| Committed to git | yes | no (`.playground/` is gitignored) |
| Holds | anything about this handset a second operator should also see | anything that must not leave this machine |
| API default | yes | requires `--private` |

**A note is portable by default.** Keeping it beside the profile means it
travels with the repository, survives a re-clone, and is visible to
whoever else works the lab -- which is what makes it worth writing at
all for a shared fleet.

#### Consequence: writing a note may create a profile

The shared surface keys on profile name, and under `discovery: silent` no
profile exists. Rather than leave the default surface unwritable in that
state, `device note` **creates the profile when one is missing**. This
does not violate "nothing is written under `config/` without an explicit
human yes" -- writing a note *is* that yes, which is why it is the third
and last path that may create a profile, alongside the discovery prompt
and `device save`.

The profile needs a name. `--as <name>` supplies it; an interactive
session may prompt; a non-TTY session with neither fails with
`config.device.note_name_required` rather than inventing one.

#### Consequence: credential-shaped text warns, it does not refuse

An earlier draft made `--shared` **refuse** credential-shaped text. That
rule cannot survive shared becoming the default: the motivating example
for the whole feature ("this phone has Telegram with account X and
password Y") is itself credential-shaped, so a refusing default would
reject the use case it exists to serve.

The detector stays, downgraded to a warning that names `--private` as
the alternative, and it is advisory only -- **substring heuristics do not
reliably detect secrets and must never be described as a safety
guarantee.**

**The standing constraint is unchanged and now applies to a default
path.** `config/` is tracked; `.gitignore` excludes only `.playground/`.
Notes written the default way are committed, so anything genuinely
sensitive needs `--private` -- and the CLI help, the `device note`
first-run output and the docs must all say so plainly, because the safe
choice is now the one requiring a flag.

**Neither surface is a secret store.** Both are plaintext on disk;
`--private` only keeps the file out of git and off a teammate's machine.
Playground must not be described as protecting note contents.

A sidecar `.md` rather than a `notes:` key inside YAML, because
multi-paragraph markdown inside a block scalar means fighting
indentation, and a real `.md` file is what editors and `git diff` handle
well.

Notes surface in three places, and the third is why the feature earns its
keep: `device show` renders them, `device list` shows the first line, and
**a failed `require` check prints them alongside the failure**. When
adoption reports `require.root: true not satisfied`, the operator's own
note reading "root broken since the September OTA, needs Magisk re-flash"
is exactly the context they would otherwise go hunting for.

**Loader caveat:** `config/` discovery globs YAML and validates every file
as a `kind:`. The `.md` sidecars must be skipped explicitly, not tripped
over.

### The note API

```
playground device note <device>                  # $EDITOR on the shared note
playground device note <device> --set -          # read markdown from stdin
playground device note <device> --append "..."
playground device note <device> --show [--json]
playground device note <device> --clear
playground device note <device> --as <name>      # name the profile to create
playground device note <device> --private ...    # the gitignored, serial-keyed note
```

`<device>` accepts a profile name **or** a raw serial. A serial with no
profile yet is the case `--as` exists for; `--private` accepts a bare
serial unconditionally, since it keys on serial and needs no profile at
all.

`--json` returns `{device, serial, scope, path, markdown, updated_at}`
for programmatic use, where `scope` is `shared` or `private`.

The body stays free markdown in this slice. The motivating example
("Telegram version X, account Y") is key-value shaped and invites YAML
front matter with typed fields, but schema creep would cost the feature
the thing that makes it useful -- being free-form. Revisit once there are
real notes to look at.

#### Why the private surface still keys on serial

Unchanged by the default flip, and still the right key for that surface:
it needs no profile, it exists from the first probe, and a note about
what is installed on a handset describes the *hardware* rather than a
lab-local slot, so it should follow the device into every lab that uses
it.

### Identity across a disconnect

The serial is what re-identifies a handset across a disconnect, whichever
note surface is in play: the shared note is reached through the profile
whose `match.serial` resolved, and the private note is keyed on that
serial directly. Three edges are worth writing down.

**Serial collision.** Cheap and cloned handsets sometimes ship duplicate
USB serials, and `adb` itself cannot tell two such devices apart. The
registry must detect same-serial-different-fingerprint and refuse, rather
than silently merging two handsets' notes and probe history into one
record.

**Factory reset.** The serial survives a reset; the adb key does not, so
the handset returns as `unauthorized` and still matches its profile --
while the note now describes an account that no longer exists on it.
Playground cannot detect this. The mitigation is honest display rather
than cleverness: record `last_verified` in the registry and show the
note's age beside it.

**Refreshed facts on reconnect.** Re-probe overwrites facts and never
touches notes. A handset that lost root to an OTA therefore fails
`require: {root: true}` at the next adopt and prints the operator's note
with the failure -- the intended behaviour, not a regression.

## Decision 4: discovery policy, prompt or silent

```yaml
# config/defaults.yaml
spec:
  device_discovery: prompt   # or: silent
```

The key is deliberately **not** spelled `spec.devices.discovery`.
`LabSpec.devices` is a *list* of device slots, and giving the `Defaults`
kind a same-named *mapping* would make two sibling config files disagree
about the shape of one key -- exactly the kind of cross-layer trap
`CONTRACTS.md` exists to catch. A lab may override it with its own
top-level `spec.device_discovery`.

- **`prompt`** -- on encountering an attached device absent from the
  registry, ask: *"New device 1A2B3C4D5E (Pixel 7, Android 14, unrooted)
  -- save as a profile? [name]"*. Yes writes a profile; no records it as
  seen-and-ignored so the question is not repeated.
- **`silent`** -- never ask. Unknown devices are recorded in the registry
  and otherwise ignored; an ambiguous match fails with a diagnostic
  listing candidates rather than blocking on a question.

Two non-negotiables:

1. **A non-TTY session forces `silent`.** JSON output, CI and the TUI must
   never block on stdin.
2. `--yes` / `--no-prompt` override per invocation, matching how `apply`
   already handles confirmation.

## Decision 5: adoption is strictly additive

`apply` on a lab with devices performs adoption: resolve the selector
against attached devices, assert the device is `connected` rather than
`unauthorized` or `offline`, probe facts (model, SDK, ABI, root), check
every `require`, and record the binding in state. It changes nothing on
the handset except installing helpers the lab explicitly declares.

`destroy` releases: forget the binding and uninstall only
playground-installed packages, tracked by package name in state. Never a
wipe, never a factory reset, never reverting a setting playground did not
set. `reset` re-installs declared helpers over a released binding.

**Rebinding is deliberately never automatic.** Every `app` and `capture`
verb re-verifies the binding before acting. A bound serial that is gone
fails with `device.not_connected`. A *different* handset that now matches
the selector is refused pending an explicit `--rebind`, accepted by
`apply` and by every `app` / `capture` verb (the refusal names the flag). Installing an APK onto the
wrong phone because a cable was swapped is precisely the failure that
must not be silent.

## Decision 6: capture, two strategies behind one verb

Selected by the probed `root` capability -- the same "choose an
implementation from a capability" shape the repo uses for backends.

### Rooted: on-device tcpdump

Push a static `tcpdump` to `/data/local/tmp`, run it under `su`, ring
buffer with the same `-C`/`-W` flags the VM path uses, `adb pull` the
result. Real packets, native framing, and it captures cellular as well as
WiFi. No relay, no host privileges, no VPN consent dialog.

Reusing the same tcpdump flags means the documented gotchas carry over
intact: `-C` counts 1,000,000-byte units rather than MiB, `-W` zero-pads
to its own width, so files on disk are `.pcap00…` and every glob stays
`*.pcap*`.

Two device-specific additions: a **smaller default ring** (a 1 GB ring on
a handset is not a sensible default) and a free-space preflight before
starting. The liveness cross-check is `pidof tcpdump` on the device,
playing the role the netns marker plays for Redroid.

### Unrooted: reverse tether into a network namespace

VpnService client app on the phone -> raw IP packets over `adb reverse` ->
relay on the controller -> **TUN device inside a dedicated network
namespace** -> veth and NAT to the internet. `tcpdump` runs inside that
netns.

That is structurally identical to the existing Redroid capture -- an
isolated netns entered with `nsenter --net` -- which is why the verb
surface, session records, ring semantics and stale-detection watchdog
port over rather than being rewritten.

**The netns is the load-bearing decision; the TUN is the upgrade.** If the
spike shows raw packets are not obtainable, the *stock* relay runs inside
the netns anyway. The pcap then contains only the phone's traffic and none
of the workstation's -- with host-side framing and NAT'd addresses
instead of the phone's real packets. Architecture and verbs are identical
either way; only fidelity differs. This is what makes the design safe to
commit to before the spike reports.

### The spike (first work item, gates Plan 2)

1. Does gnirehtet's client/relay protocol carry raw IP packets in a form a
   small relay can tee into a TUN, or would it require a patched client?
2. **Does the controller need root?** Today `playground capture` requires
   zero privileges on the operator's machine -- everything happens inside
   the guest. `ip netns` plus veth plus NAT does require root, which would
   be a real regression in how invasive the tool is. The unprivileged
   alternative is `unshare -rn` with `slirp4netns`, as rootless podman
   does it. **The spike tries the unprivileged path first.**
3. Is USB throughput acceptable, and does VpnService consent survive
   reconnects? (Believed to be a one-time per-app "always allow"; to be
   confirmed, not assumed.)
4. gnirehtet's license, and whether its APK can be a declared artifact in
   `config/artifacts/sources.yaml` -- required by the air-gap constraint.
   Unverified at the time of writing and deliberately not asserted here.

Fallback ladder: raw-packet TUN -> stock relay inside the netns -> stock
relay unconfined with a documented fidelity caveat.

### Smaller capture pieces

`fetch` becomes transport-conditional like `install`, using `adb pull`
rather than scp-from-guest. `capture/state.py` keys its records by `vm`;
that field wants renaming to `target`. Its reader already treats
malformed records as `None`, so the migration is tolerable -- but it must
be deliberate rather than incidental.

## Preflight

`doctor` grows a device section following the existing `shutil.which`
pattern: `adb` on PATH, adb server reachable and its version, `adb devices
-l` parseable, and each attached device reported -- with `unauthorized`
called out explicitly and its remedy ("tap Allow on the handset") named.
Capture-related checks (netns capability, `slirp4netns`) land with Plan 2,
gated on the spike's answer to question 2.

## Testing

Matching the existing `tests/unit` and `tests/cli` split:

- **Characterization tests land first, on unchanged code.** Snapshot the
  exact string every current `android/commands.py` builder emits; assert
  the `GuestAdb` renderer reproduces them byte-for-byte after the
  refactor. This is what makes the riskiest edit safe -- a quoting
  regression fails at the boundary rather than on a handset.
- `tests/unit/android/` -- rendering for both transports, including that
  the `LocalAdb` argv never passes through a shell.
- `tests/unit/config/` -- device schema, profile layering, `.md` sidecar
  association, and the loader skipping `.md` without choking.
- `tests/unit/devices/` -- selector matching across no-match, unique
  match, ambiguous match, bound-serial-absent, and
  different-device-now-matches (which must refuse, not rebind).
- `tests/cli/test_device.py` -- `list` / `save` / `show`, prompt versus
  silent, and the non-TTY forcing rule.
- `tests/unit/devices/test_notes.py` -- the note API's scopes and paths,
  that the default target is the committed shared surface, that a shared
  note written against a profile-less serial creates the profile (and
  fails `config.device.note_name_required` in a non-TTY session with no
  `--as`), that credential-shaped text warns without blocking the write,
  that `--private` writes the gitignored serial-keyed file and survives a
  disconnect/reconnect cycle with no profile in existence, and that
  re-probe refreshes facts without touching either note.
- Serial collision: two fake devices reporting one serial with differing
  fingerprints must raise `device.serial_collision` rather than merge.

A fake `adb` on PATH emitting canned `devices -l` output keeps all of the
above hardware-free. Live handset runs are manual, as the live libvirt and
DigitalOcean runs already are.

## Diagnostics

New IDs, following existing naming:

- `config.device.ambiguous_match` -- selector matched several devices;
  lists candidates and serials.
- `config.device.no_match` -- selector matched nothing attached.
- `config.device.unknown_device` -- `--on` named a device not in the lab.
- `config.device.profile_not_found` -- `profile:` names a missing
  `DeviceProfile`.
- `device.not_connected` -- bound serial absent.
- `device.unauthorized` -- attached but RSA prompt not accepted.
- `device.rebind_required` -- a different device now matches the selector.
- `device.require_unmet` -- a `require` assertion failed; prints the
  device's markdown note alongside.
- `device.serial_collision` -- two handsets report the same serial with
  differing fingerprints; the registry refuses to merge them.
- `device.note_credential_warning` -- a note being written to the
  committed surface looks credential-shaped; advisory warning naming
  `--private`, never a refusal.
- `config.device.note_name_required` -- a shared note was written against
  a serial with no profile, in a non-TTY session, with no `--as <name>`.

## Rejected alternatives

**A `DeviceRole` kind mirroring `VmRole`.** The VM role system carries an
image, resource sizing and *Ansible* provisioners; none of the three means
anything on a handset, and bending the kind to fit would make role
resolution answer questions it cannot answer. Device capabilities are
probed rather than declared. `DeviceProfile` covers the real need
(reusable selector plus notes), and a `DeviceRole` remains available later
if declared capabilities are ever genuinely required.

**Putting the serial in the committed lab YAML.** A serial is
machine-specific in the same way a VM IP is. Keeping the binding in state
lets a lab be committed and used by a second operator with a different
handset of the same model.

**Auto-rebinding to whatever is attached.** Convenient and unsafe; see
Decision 5.

## Delivery: two plans

**Plan 1 -- device as a target.** Schema, registry, profiles, notes,
discovery policy, the transport refactor behind its characterization
tests, adoption lifecycle, `device` verbs, doctor checks, and every
existing `app` / `adb` / `shell` verb working against a handset. Root is
probed and reported but not yet exploited. Delivers standalone value.

**Plan 2 -- capture.** Gated on the spike. Rooted on-device tcpdump and
the unrooted reverse-tether netns path, behind the existing `capture`
verbs.

The split is not optional: Plan 2 cannot be written honestly until the
spike answers whether the controller needs root, and Plan 1 is useful
without it.
