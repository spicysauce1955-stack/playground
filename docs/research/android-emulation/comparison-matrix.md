# Comparison Matrix

Last checked: 2026-08-31.

## Runtime Matrix

| Runtime | Server/headless | ADB | Docker/KVM | App automation fit | Network lab fit | Repo fit |
| --- | --- | --- | --- | --- | --- | --- |
| Redroid | High | Yes | Docker/Podman/K8s; binder/privileged host requirements | Good; validate Appium/UiAutomator per image | Strong via Docker/VM networking | Best first path |
| Android Emulator / AVD | High with KVM | Yes | KVM strongly preferred; official emulator | Excellent | Good, but CA/root varies by image | Compatibility fallback |
| Android Emulator Container Scripts | High with KVM | Yes | Docker plus KVM | Excellent | Good | Good fallback/reference |
| Cuttlefish | High | Yes | Host virtualization/cloud | Good but AOSP-oriented | Good | Future advanced option |
| Docker-Android | High with KVM | Yes | Docker plus emulator/KVM | Strong Appium/noVNC | Good | Reference/fallback |
| HQarroum docker-android | High with KVM | Yes | Docker plus KVM | Good | Good | Reference/fallback |
| Waydroid | Medium | Possible | Linux namespaces/Wayland | Medium | Medium | Not first path |
| Genymotion | High | Yes | Vendor-managed or product-specific | Strong | Product-dependent | Commercial fallback |
| Anbox Cloud | High | Product-supported | LXD/VM product stack | Product-supported | Product-supported | Architecture reference |
| Android-x86/QEMU | Medium | Configurable | Full VM/KVM | Good with setup | Strong boundary capture | Niche fallback |

## Automation Matrix

| Tool | Arbitrary apps | Needs app source | Headless | Best for | Weakness |
| --- | --- | --- | --- | --- | --- |
| ADB shell/input | Yes | No | Yes | Lifecycle and simple smoke actions | Brittle coordinates |
| Monkey | Yes | No | Yes | Random/stability testing | Poor deterministic control |
| UiAutomator dump | Mostly | No | Yes | Inspecting native UI hierarchy | Weak with custom views/games/WebViews |
| openatx uiautomator2 | Yes | No | Yes | Python custom UI automation | Helper service compatibility |
| Appium UiAutomator2 | Yes | No | Yes | General black-box automation | Server/capability complexity |
| Maestro | Yes | No | Yes | Declarative smoke/user flows | Less flexible than code |
| Espresso | No | Yes | Yes | First-party app instrumentation tests | Not for third-party apps |
| scrcpy | Yes | No | Partial | Human debugging/control | Not an automation framework |
| Frida/objection | Yes | No | Yes | Runtime/security analysis | Root/patch/version sensitivity |
| DroidBot | Yes | No | Yes | Exploration and UI transition graphs | Not deterministic enough for core workflows |
| AndroidWorld/Mobilerun/AppAgent | Yes | No | Varies | Autonomous-agent research | Too early for core infra |

## Network Matrix

| Method | Captures | Requires root | Handles TLS | Automation fit | Notes |
| --- | --- | --- | --- | --- | --- |
| Emulator/global HTTP proxy | Proxy-aware HTTP(S) | No | Only trusted/unpinned TLS | High | Simple first step; some apps ignore proxy. |
| mitmproxy system CA | HTTP(S) through proxy | Root/writable system image | Unpinned TLS | High | Strong open-source baseline. |
| Burp CA/proxy | HTTP(S) through proxy | Root/writable system image for system trust | Unpinned TLS | Medium | Best for manual security testing. |
| PCAPdroid VPNService | Per-app traffic metadata/PCAP | No | Limited/dependent on mode | Medium | Useful no-root capture; changes network path. |
| Android tcpdump | Raw packets inside Android | Usually yes | Encrypted unless TLS decrypted elsewhere | Medium | Good for rooted images. |
| Docker/VM boundary capture | Raw packets outside Android | Host privileges | Encrypted unless proxied/decrypted | High | Best infra-aligned capture path. |
| Frida TLS hooks | App runtime TLS APIs | Often yes or patched APK | Can inspect pinned apps in authorized tests | Low/medium | Security workflow, not default. |
| `adb forward` | Device service to host | No | N/A | High | Good for local service access. |
| `adb reverse` | Host service to device | No | N/A | High | Good for app talking to local proxy/API. |
| SSH tunnel | Any TCP reachable from VM/host | No Android root | N/A | High | Good for hiding ADB/proxy from public networks. |
| WireGuard/OpenVPN | Routed overlay | No for app client; host setup needed | N/A | Medium | Good for topology experiments. |

