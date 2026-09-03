# Open-Source Projects

Last checked: 2026-08-31.

## Runtime And Device Infrastructure

| Project | URL | Use | Notes |
| --- | --- | --- | --- |
| Redroid | https://github.com/remote-android/redroid-doc | Android-in-Docker runtime | Best fit for this repo's current path. Needs binder/privileged host setup. |
| Android Emulator Container Scripts | https://github.com/google/android-emulator-container-scripts | Official emulator in Docker | Good compatibility fallback; KVM matters. |
| Cuttlefish | https://github.com/google/android-cuttlefish | AOSP virtual Android devices | Strong future option for AOSP/device fidelity. |
| Docker-Android | https://github.com/budtmo/docker-android | Emulator + noVNC/Appium Docker images | Useful all-in-one test image; heavy. |
| HQarroum docker-android | https://github.com/HQarroum/docker-android | Minimal emulator service | Useful reference for WebRTC/KVM emulator service. |
| Waydroid | https://github.com/waydroid/waydroid | Android in Linux container | More desktop/Wayland oriented than server lab oriented. |
| Android-x86 | https://github.com/android-x86/android-x86.github.io | Android in full x86 VM | Useful for VM isolation experiments. |
| Dock-Droid | https://github.com/sickcodes/dock-droid | QEMU Android container wrapper | Interesting reference, not first baseline. |

## Device Farm And Remote Control

| Project | URL | Use | Notes |
| --- | --- | --- | --- |
| DeviceFarmer STF | https://github.com/DeviceFarmer/stf | Browser-based device farm | Community continuation of OpenSTF. |
| OpenSTF | https://github.com/openstf/stf | Legacy device farm | Development moved to DeviceFarmer; use as historical reference. |
| scrcpy | https://github.com/Genymobile/scrcpy | Remote display/control over ADB | Excellent operator debugging tool. |
| VK DeviceHub | https://github.com/VKCOM/devicehub | Browser device control | Newer device control reference; evaluate if a web UI becomes a goal. |

## Automation And Testing

| Project | URL | Use | Notes |
| --- | --- | --- | --- |
| Appium | https://github.com/appium/appium | WebDriver mobile automation | Mature, active, broad ecosystem. |
| Appium UiAutomator2 Driver | https://github.com/appium/appium-uiautomator2-driver | Android Appium driver | Default Appium Android backend. |
| Maestro | https://github.com/mobile-dev-inc/Maestro | Declarative UI flows | Strong for readable smoke tests and app journeys. |
| openatx uiautomator2 | https://github.com/openatx/uiautomator2 | Python UI automation | Good custom orchestration fit. |
| AndroidX Test | https://github.com/android/android-test | Instrumentation tests | Best for first-party app test APKs. |
| DroidBot | https://github.com/honeynet/droidbot | UI exploration/input generation | Useful for app crawling and UTG generation. |
| AndroidViewClient/culebra | https://github.com/dtmilano/AndroidViewClient | Python UI automation and inspection | Older but still useful reference. |

## App Packaging And Install

| Project | URL | Use | Notes |
| --- | --- | --- | --- |
| bundletool | https://github.com/google/bundletool | AAB/APKS build/install | Official CLI. |
| SAI | https://github.com/aefyr/SAI | Split APK installer app | Useful manual/reference tool, not orchestration baseline. |
| Universal Installer | https://github.com/pass-with-high-score/universal-installer | APK/APKS/XAPK installer | Manual Android-side installer; supports modern package formats. |
| VInstall | https://github.com/vinstall/VInstall | APK/APKS/XAPK installer | Similar manual installer reference. |
| apksig | https://android.googlesource.com/platform/tools/apksig | APK signing library/tool | Underlies `apksigner`. |

## Network And Security

| Project | URL | Use | Notes |
| --- | --- | --- | --- |
| mitmproxy | https://github.com/mitmproxy/mitmproxy | HTTP(S) proxy and programmable flow modification | Best open-source proxy baseline. |
| Frida | https://github.com/frida/frida | Dynamic instrumentation | Security testing and runtime hooks. |
| objection | https://github.com/sensepost/objection | Frida-powered mobile exploration | Handy security toolkit; check Android version compatibility. |
| PCAPdroid | https://github.com/emanuele-f/PCAPdroid | No-root Android traffic capture | VPNService-based capture app. |
| WireGuard Android | https://github.com/WireGuard/wireguard-android | VPN tunnel | Overlay/routed lab networking. |
| ics-openvpn | https://github.com/schwabe/ics-openvpn | VPN tunnel | OpenVPN client for Android. |
| android-unpinner | https://github.com/mitmproxy/android-unpinner | APK pinning removal research tool | Authorized security-testing reference only. |

## Agent And Benchmark Projects

| Project | URL | Use | Notes |
| --- | --- | --- | --- |
| AndroidWorld | https://github.com/google-research/android_world | Reproducible Android agent benchmark | Useful if the repo later hosts autonomous Android agents. |
| Mobilerun | https://github.com/droidrun/mobilerun | LLM-driven mobile control | Newer agent-oriented automation framework. |
| DroidBot-GPT | https://github.com/MobileLLM/DroidBot-GPT | Natural language DroidBot control | Research reference. |
| AppAgent | https://github.com/TencentQQGYLab/AppAgent | Multimodal phone agent | Research reference. |
| Mobile-Agent | https://github.com/X-PLUG/MobileAgent | Cross-platform GUI/mobile agent | Research reference. |
| AndroidLab | https://github.com/THUDM/Android-Lab | Android agent framework and benchmark | Research reference. |
| Mobile-Bench | https://github.com/XiaoMi/MobileBench | LLM mobile-agent benchmark | Older strict dependency stack; useful for benchmark ideas. |

