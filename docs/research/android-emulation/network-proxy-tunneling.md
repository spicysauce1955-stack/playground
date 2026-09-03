# Network Proxying, Capture, And Tunneling

Last checked: 2026-08-31.

This file assumes authorized lab work. Traffic interception and instrumentation
should only be used for systems and apps the operator is allowed to test.

## Goals

| Goal | First tool to evaluate | Root required |
| --- | --- | --- |
| HTTP(S) proxy for cooperative apps | mitmproxy or Burp | No for user CA; yes/system image modification for system CA. |
| Modify HTTP(S) flows programmatically | mitmproxy addons | Same as proxy setup. |
| Inspect pinned TLS apps | Frida/objection or test build network config | Often yes, or patched APK. |
| Capture per-app traffic without root | PCAPdroid | No. |
| Raw packet capture inside Android | tcpdump | Usually yes. |
| Capture at infrastructure boundary | Docker bridge, VM tap, router VM, tcpdump/Wireshark | No Android root, but host privileges. |
| Connect host service to app | `adb reverse` | No. |
| Expose device service to host | `adb forward` | No. |
| Route lab through private path | SSH tunnel, WireGuard, OpenVPN | Depends on topology. |

## mitmproxy

- Site: https://www.mitmproxy.org/
- Source: https://github.com/mitmproxy/mitmproxy
- Getting started: https://docs.mitmproxy.org/stable/overview/getting-started/
- Certificates: https://docs.mitmproxy.org/stable/concepts/certificates/
- Android system CA guide:
  https://docs.mitmproxy.org/stable/howto/install-system-trusted-ca-android/
- Addon docs: https://docs.mitmproxy.org/stable/addons/overview/
- Addon examples: https://docs.mitmproxy.org/stable/addons/examples/
- Fit: best open-source proxy baseline for programmatic lab capture and
  modification.
- Strengths: CLI/web/terminal modes, Python addon API, flow dump/replay,
  scriptable.
- Limitations: Android trust store behavior, pinned TLS, proxy-unaware protocols,
  QUIC/HTTP3 edge cases.

Practical use:

```bash
mitmweb --listen-host 127.0.0.1 --listen-port 8080
ssh -L 18080:127.0.0.1:8080 lab-vm
adb shell settings put global http_proxy 10.0.0.10:8080
adb shell settings put global http_proxy :0
```

Bind proxy listeners to localhost or an explicit lab-private address by default.
Use SSH, WireGuard, or another private tunnel for remote operator access. Only
bind to `0.0.0.0` inside an isolated lab network with firewall rules that make
the exposure intentional.

For production-grade lab behavior, do not rely only on global proxy settings.
Some apps ignore proxy settings or use pinned TLS. Network topology should also
allow host/VM-level capture.

## Burp Suite

- Android docs:
  https://portswigger.net/burp/documentation/desktop/mobile/config-android-device
- CA docs:
  https://portswigger.net/burp/documentation/desktop/external-browser-config/certificate
- Fit: excellent interactive security testing proxy; less ideal than mitmproxy
  for automated open-source lab flows.
- Strengths: mature manual analysis, repeater/intruder/scanner ecosystem.
- Limitations: commercial features, automation integration less natural for this
  Python/OpenTofu/Ansible repo.

## Android CA Trust Constraints

Important design facts:

- User-installed CAs are not enough for every app.
- Apps targeting modern Android can opt out of trusting user CAs.
- First-party apps can explicitly configure trust behavior with Android Network
  Security Configuration.
- Google Play emulator images often block `adb root`, which makes system CA
  installation harder.
- Certificate pinning can still block both user and system CAs.
- For first-party apps, prefer debug builds or network security config that
  explicitly trusts the lab CA.
- For third-party apps, treat Frida/app patching as authorized security testing,
  not default product behavior.

Useful background:

- https://docs.mitmproxy.org/stable/howto/install-system-trusted-ca-android/
- https://developer.android.com/privacy-and-security/security-config
- https://blog.ropnop.com/configuring-burp-suite-with-android-nougat/
- https://www.dev-eth0.de/2022/01/04/debug-android-application-traffic/
- https://httptoolkit.com/blog/frida-certificate-pinning/

## Frida / Objection For TLS Investigation

- Frida: https://github.com/frida/frida
- Docs: https://frida.re/docs/home/
- objection: https://github.com/sensepost/objection
- objection wiki: https://github.com/sensepost/objection/wiki/Using-objection

Use for authorized dynamic analysis when CA-based proxying fails. Do not make
this the default path because it introduces root/patched-APK requirements,
version sensitivity, and legal/authorization concerns.

## PCAPdroid And VPNService Capture

- Source: https://github.com/emanuele-f/PCAPdroid
- Docs: https://emanuele-f.github.io/PCAPdroid/quick_start.html
- Architecture:
  https://github.com/emanuele-f/PCAPdroid/blob/master/docs/how_it_works.md

PCAPdroid uses Android's `VPNService` approach, so it can capture without root
by routing selected app traffic through a local VPN interface. It is useful for
per-app connection inventory, PCAP export, and no-root experiments.

Limitations:

- It changes the network path visible to apps.
- Some apps may block or malfunction under a VPNService.
- It does not replace full host-side packet capture for infrastructure tests.

## tcpdump And Boundary Capture

- Android tcpdump: https://androidtcpdump.com/

Inside Android, tcpdump normally requires root. For this repo, the more robust
capture strategy may be outside Android:

- Docker bridge capture on the Redroid host.
- VM tap/interface capture on the libvirt host.
- A dedicated router VM that all Android instances use as gateway.
- A proxy container placed on the same lab network.

This aligns with the product requirement that network topology should be
first-class and future packet capture should be possible without redesign.

## Tunneling And Port Forwarding

ADB:

```bash
adb forward tcp:9000 tcp:9000
adb reverse tcp:8080 tcp:8080
```

SSH:

```bash
ssh -L 15555:127.0.0.1:5555 lab-vm
ssh -D 1080 lab-vm
```

VPN:

- WireGuard Android: https://github.com/WireGuard/wireguard-android
- OpenVPN Android: https://github.com/schwabe/ics-openvpn

Repo fit:

- Use `adb forward`/`reverse` for service connectivity during automation.
- Use SSH tunnels for operator access to lab-private ADB/proxy ports.
- Use WireGuard/OpenVPN when the Android instance must become part of a routed
  overlay network.
- Avoid public ADB exposure by default.
