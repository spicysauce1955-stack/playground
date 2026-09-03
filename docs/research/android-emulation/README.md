# Android Emulation Research

Last checked: 2026-08-31.

This directory collects current sources, guides, packages, and open-source
projects for running Android in lab infrastructure, installing and launching
apps, driving app actions, and capturing or tunneling network traffic.

The bias is intentional: this repo's current runtime path is
`tofu/ -> ansible/ -> Redroid -> ADB`, so Redroid-compatible workflows are
treated as the default baseline. Other runtimes are included when they solve a
specific problem Redroid may not solve well.

## Files

| File | Purpose |
| --- | --- |
| [sources.md](sources.md) | Bibliography of official docs, project repos, package docs, and practical guides. |
| [runtime-options.md](runtime-options.md) | Android runtime choices: Redroid, stock emulator, Cuttlefish, Waydroid, Docker images, Genymotion, Anbox Cloud. |
| [app-install-launch.md](app-install-launch.md) | APK, split APK, AAB/APKS, package manager, and launch commands. |
| [automation-tools.md](automation-tools.md) | Tools for making actions in apps: ADB, UiAutomator2, Appium, Maestro, scrcpy, Frida, DroidBot, agent frameworks. |
| [network-proxy-tunneling.md](network-proxy-tunneling.md) | Proxying, CA trust, TLS interception limits, VPNService capture, packet capture, tunnels. |
| [open-source-projects.md](open-source-projects.md) | Shortlist of useful open-source projects and what they are good for. |
| [packages-libraries.md](packages-libraries.md) | Python, Node, Go, and CLI packages for orchestration. |
| [comparison-matrix.md](comparison-matrix.md) | Practical comparison across runtime, automation, and network dimensions. |
| [recommended-stack.md](recommended-stack.md) | Opinionated recommendation and proof-of-concept plan for this repo. |
| [raw-notes/](raw-notes/) | Per-tool notes for fast later lookup. |

## Executive Summary

Use Redroid as the first local/server runtime because it matches the existing
OpenTofu and Ansible direction, exposes ADB over TCP, and is designed for
Docker/Podman/Kubernetes style operation. Keep the stock Android Emulator and
Google's Android Emulator Container Scripts as the fallback for compatibility
with official emulator images, Google Play images, and standard CI test tooling.

For app lifecycle control, use `adb` directly first. It remains the most stable
common denominator for connect, install, uninstall, clear data, grant
permissions, launch activities, stream logs, capture screenshots, forward ports,
and reverse ports. Add a thin Python orchestration wrapper later, likely around
`adbutils` or subprocess calls, after exact multi-device behavior is specified.

For UI actions, start with three layers:

| Layer | Tooling | When to use |
| --- | --- | --- |
| Basic black-box control | `adb shell input`, `am`, `pm`, `uiautomator dump`, screenshots | Smoke tests, device readiness, deterministic taps/text where layout is known. |
| Structured UI automation | Appium UiAutomator2 or openatx `uiautomator2` | Arbitrary third-party apps where element lookup and gestures are needed. |
| Declarative flows | Maestro | Human-readable app flows and CI-friendly smoke/regression tests. |

For network work, split goals clearly:

| Goal | Best starting point |
| --- | --- |
| HTTP(S) proxy for cooperative apps | mitmproxy or Burp with emulator proxy settings and a trusted CA. |
| Capture without root | PCAPdroid or another Android `VPNService` capture app. |
| Raw packet capture | Rooted image plus `tcpdump`, or capture at the Docker/VM network boundary. |
| Tunneling lab traffic | `adb forward`, `adb reverse`, SSH tunnels, WireGuard, or OpenVPN depending on topology. |
| Pinned TLS inspection | Treat as authorized security testing only; expect Frida, app patching, or custom test builds. |

## Main Risks

- Redroid usually needs binder/binderfs host support and privileged container
  settings. That fits a lab, but must be explicit in Ansible and doctor checks.
- Public ADB exposure is dangerous. Bind ADB to lab-private networks or tunnel
  it; never expose `5555` broadly by default.
- TLS interception on Android 7+ is not guaranteed by adding a user CA. Apps
  may ignore user CAs, require a system CA, or pin certificates.
- Google Play emulator images often restrict `adb root`, which complicates
  system CA injection and low-level capture.
- CI runners often lack KVM. Hardware-accelerated stock emulators usually need
  self-hosted runners or cloud VMs with nested virtualization.

