# Recommended Stack

Last checked: 2026-08-31.

## Recommendation

Build the first Android lab stack around:

1. Redroid as the runtime.
2. ADB as the device lifecycle API.
3. Direct `adb` subprocess wrappers first, with `adbutils` as the likely Python
   library if subprocess code becomes too large.
4. Maestro for declarative smoke flows.
5. Appium UiAutomator2 or openatx `uiautomator2` for structured arbitrary-app UI
   automation.
6. mitmproxy for programmable HTTP(S) capture and modification.
7. Host/VM/Docker network capture as the reliable fallback when Android-side
   proxying is insufficient.
8. Frida/objection only as an optional authorized security-analysis preset.

## Why This Fits The Repo

- Redroid aligns with the existing OpenTofu and Ansible runtime baseline.
- ADB is already the boundary exposed by the current pipeline.
- mitmproxy and ADB are CLI-first and scriptable, which fits the current Python
  control layer without hiding backend behavior.
- Network capture can be modeled as lab topology later, matching the product
  requirement that networks are first-class.
- Appium/Maestro can be optional workloads rather than hard dependencies of the
  core platform.

## Proof-Of-Concept Checklist

### Phase 1: Redroid Lifecycle

- Start one Redroid container through the existing Ansible path.
- Verify ADB through the repo's SSH-forwarded path, such as
  `playground adb --lab <lab> --on <vm>`, or an equivalent `ssh -L` tunnel.
- Install a simple APK.
- Launch by package with `monkey`.
- Launch by activity with `am start`.
- Capture screenshot and logcat.
- Clear app data and re-launch.

### Phase 2: Automation

- Run a minimal `adb shell input` flow.
- Dump UI hierarchy with `uiautomator`.
- Run one Maestro YAML flow.
- Run one Appium UiAutomator2 session against the same app.
- Run one openatx `uiautomator2` script if Python-native automation is desired.

### Phase 3: Network

- Start mitmproxy on the lab host or a proxy container bound to localhost or a
  lab-private address.
- Route Android global proxy to mitmproxy.
- Install a user CA and test a cooperative HTTPS endpoint.
- Test a system CA path only on a rootable image.
- Capture traffic at the Docker/VM boundary.
- Validate `adb reverse` from app to host-local service.
- Validate SSH tunnel for ADB so `5555` does not need public exposure.

### Phase 4: Reproducibility

- Store app artifacts and captured outputs under `.playground/`.
- Record runtime metadata: Android version, image tag, ADB serial, app package,
  APK hash, proxy settings, tunnel endpoints.
- Add doctor checks for ADB, Docker, binder/binderfs, privileged container
  capability, and port exposure. Primary references:
  https://docs.kernel.org/admin-guide/binderfs.html and
  https://docs.docker.com/engine/containers/run/
- Add validation warnings for public ADB exposure and mutable runtime image tags.

## Avoid For First Slice

- Do not start with autonomous-agent frameworks. They are useful research
  references, but the platform needs deterministic lifecycle and network
  primitives first.
- Do not require Frida for normal traffic capture. It belongs in an explicit
  security-testing mode.
- Do not make Genymotion or Anbox Cloud the default because they weaken the
  local-first and visible-backend design goals.
- Do not assume Google Play emulator images can be rooted or modified.

## Open Questions

- Which Android image tags should become blessed presets for Redroid?
- Does the project need Play Services compatibility, or is vanilla Android
  enough for the first security/automation labs?
- Should the first UI automation API be Appium-compatible, Python-native, or both?
- Should packet capture be modeled as a router VM, a proxy container, or a host
  capture command preset?
- What is the intended trust posture for ADB: lab-private only, SSH-tunneled, or
  temporarily exposed with explicit warnings?
