# Nested virtualization: when L0 refuses your VMs

This doc explains what to do when `playground apply` wedges with
`libvirt_domain_crashed` (or `wait_for_lease` times out after ~5 min)
on a host that's itself running inside a hypervisor. It's the most
common "the platform doesn't work on my machine" failure mode and the
mitigations are precise enough to be worth a single reference page.

## The L0 / L1 model

```
+--------------------------------------------+
|             L0 hypervisor                  |   ← cloud provider, VMware ESXi,
|             (Intel VT-x / AMD-V)           |     workstation hypervisor, ...
|                                            |
|  +--------------------------------------+  |
|  |          L1 host (this machine)      |  |   ← Ubuntu Noble running playground
|  |          KVM / libvirt / QEMU        |  |
|  |                                      |  |
|  |   +------------------------------+   |  |
|  |   |     L2 playground VM         |   |  |   ← `libvirt_domain.playground_node`
|  |   |     Ubuntu Noble guest       |   |  |
|  |   |     Docker / Redroid in here |   |  |
|  |   +------------------------------+   |  |
|  +--------------------------------------+  |
+--------------------------------------------+
```

L0 must explicitly let L1 use the CPU's virtualization features so
L1's KVM can run L2 guests. If L0 doesn't (cloud guest with nested-virt
off, conservative ESXi config), L1's `kvm_intel` module starts
crashing on `vmread` / `vmwrite` instructions the moment QEMU tries to
start L2. The libvirt domain ends up in `paused (unknown)` and the
tofu apply hangs on `wait_for_lease` until the 5-minute internal
timeout fires.

## Symptoms → which rung of the ladder

| Symptom | Most likely rung |
| --- | --- |
| `runtime.apply.libvirt_domain_crashed` post-apply, libvirt state `paused (unknown)` | Rung 1 or 2 |
| `kvm_intel: vmread failed` / `vmwrite failed` in `journalctl -k` | Rung 1 or 2 |
| `playground doctor` reports `runtime.doctor.kvm_intel_recent_failures` | Rung 1 or 2 |
| Apply boots fine but Redroid inside the guest can't start | L2 has no nested VMX — rung 1 may have masked it; consider rung 3 |
| Boot succeeds but every step takes 10× as long | You're on rung 2 (TCG) — expected |

## The escalation ladder

Set these in your lab YAML under `spec.providers.local-libvirt`. Each
rung is a strict superset of the workaround above it — try them in
order, only escalate if the previous didn't take.

### Rung 0 (default): `host-passthrough`

```yaml
spec:
  providers:
    local-libvirt:
      cpu_mode: host-passthrough        # default; required for Redroid
```

What you get: maximum performance, full CPU feature passthrough,
required for the `redroid-host` lab (binderfs in Android needs it).
Fails on hosts where L0 doesn't permit nested VMX.

### Rung 1: `host-model` + disable the offending feature

```yaml
spec:
  providers:
    local-libvirt:
      cpu_mode: host-model
      cpu_features_disable: [vmx]       # or [svm] on AMD
```

What you get: libvirt computes a stable CPU model and we mask the
nested-virt feature from the guest, so `kvm_intel` inside the guest
won't even try VMX operations. Cheap, fast, often enough — pair it
with `host-model` because `host-passthrough` can still leak the flag
to the guest despite the disable (Ubuntu bug #1830268).

**Host prerequisite**: rung 1 requires `xsltproc` on PATH. The
dmacvicar/libvirt provider's `xml { xslt = ... }` escape hatch shells
out to it to apply the disable transform; without it, apply fails at
domain creation with `exec: "xsltproc": executable file not found in
$PATH`. `playground doctor` flags this as
`runtime.doctor.xsltproc_missing` (warning). Install with
`sudo apt install -y xsltproc`.

Trade-off: Redroid still works (no VMX requirement), but anything
that *needs* nested virt inside the L2 guest will fail.

### Rung 2: `domain_type: qemu` (TCG software emulation)

```yaml
spec:
  providers:
    local-libvirt:
      domain_type: qemu                 # bypasses KVM entirely
      # cpu_mode is left implicit — the renderer auto-coerces it to
      # `host-model` because `host-passthrough` (the platform default,
      # required for Redroid) is incompatible with TCG and libvirt
      # would reject domain creation with "CPU mode 'host-passthrough'
      # is not supported by hypervisor". If you want a different
      # non-passthrough mode, set `cpu_mode` explicitly.
      wait_ssh_timeout_seconds: 1800    # TCG boots are slow
      wait_cloud_init_timeout_seconds: 2400
```

What you get: QEMU's Tiny Code Generator (TCG) emulates the CPU in
software. `/dev/kvm` isn't touched, so `kvm_intel: vmread/vmwrite`
errors are impossible. Always boots, on any host.

Trade-off: 10–100× slower than KVM (CPU-bound workloads worst).
Apply easily takes 20+ minutes; raise the wait timeouts as shown.
`redroid-host` + TCG is **unverified** — the validator surfaces
`config.backend.tcg_mode_slow` with a louder warning for that combo.

**Why cpu_mode matters here**: setting `domain_type: qemu` alongside
`cpu_mode: host-passthrough` (the platform default) makes libvirt
reject the domain — host-passthrough is a KVM-only mode. The
renderer detects this and either auto-coerces to `host-model` (when
cpu_mode is unset) or errors out at render time (when cpu_mode is
explicitly host-passthrough).

### Rung 3: re-run on a host with proper nested support

When none of the above mitigations works, the only fix is to move to
a host whose L0 permits nested VMX/SVM. Typical paths:

- **Bare metal**: any modern Intel/AMD desktop or workstation works.
- **L0 that supports nested**: set in your L0 hypervisor's config:
  - VMware (ESXi/Workstation): "Expose hardware-assisted virtualization to guest OS"
  - VirtualBox: VT-x/AMD-V passthrough
  - KVM: `options kvm_intel nested=1` in `/etc/modprobe.d/kvm.conf`
- **Cloud providers**: typically need a `metal` instance shape or
  explicit nested-virt SKU. Most general-purpose VMs disable nesting.

## Diagnostic IDs related to this page

All in the `runtime.*` namespace; see `docs/system_overview.md` for the
full registry.

- `runtime.apply.libvirt_domain_crashed` — fires post-tofu-apply when
  any libvirt domain isn't `running`. Suggestion text walks the
  ladder.
- `runtime.doctor.nested_disabled` — `/sys/module/kvm_intel/parameters/nested`
  reports off. You can't fix this on the L1 if the L0 disallows it —
  fall straight to rung 1 or 2.
- `runtime.doctor.kvm_intel_recent_failures` — recent
  `vmread/vmwrite failed` in the kernel log. Strong signal you've
  already hit the L0 refusal at least once.
- `runtime.doctor.host_is_virtualized` — info-only; says "you're at
  L1, so nested-virt is a thing." Doesn't block anything.
- `config.backend.tcg_mode_slow` — warning when a lab opts into
  `domain_type: qemu`. Reminder that rung 2 is the slow rung;
  for `redroid-host` it's also experimental.

## Verifying your knobs landed

After editing the lab YAML and re-running apply:

```bash
# CPU mode + disabled features in the generated XML.
virsh dumpxml playground-node-1 | sed -n '/<cpu/,/<\/cpu>/p'

# Domain type — `<domain type='qemu'>` vs `<domain type='kvm'>`.
virsh dumpxml playground-node-1 | head -1
```

## When this doc needs updating

- The provider gains a new escape hatch for nested-virt issues.
- The diagnostic IDs around this fail-mode change.
- A new lab type adds a nested-virt dependency (binderfs, vSphere,
  Hyper-V) — document the rung-3 expectation here.
