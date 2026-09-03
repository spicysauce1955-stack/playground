# Sources

Last checked: 2026-08-31.

Sources are grouped by confidence. Official documentation and active upstream
repositories should be preferred when designing repo behavior. Practical guides
and issue threads are included because they expose operational failure modes.

## Official Android And AOSP

| Source | URL | Notes |
| --- | --- | --- |
| Android Debug Bridge docs | https://developer.android.com/tools/adb | Canonical reference for ADB architecture, wireless debugging, shell access, install/debug workflows. |
| Android Emulator command line | https://developer.android.com/studio/run/emulator-commandline | Emulator launch options such as `-avd`, `-no-window`, `-no-snapshot`, proxy flags. |
| Android Emulator release notes | https://developer.android.com/studio/releases/emulator | Current emulator capabilities and host support. |
| Cuttlefish docs | https://source.android.com/docs/devices/cuttlefish | Official virtual Android device documentation. |
| Cuttlefish host tools repo | https://github.com/google/android-cuttlefish | Host-side utilities for local and cloud-hosted Cuttlefish. |
| Cuttlefish generated docs | https://google.github.io/android-cuttlefish/ | `cvd` command documentation and host utilities. |
| bundletool docs | https://developer.android.com/tools/bundletool | Official AAB/APKS tooling. |
| bundletool repo | https://github.com/google/bundletool | CLI for building, extracting, and installing APK sets. |
| apksigner docs | https://developer.android.com/tools/apksigner | Official APK signing and verification tool. |
| AndroidX Test repo | https://github.com/android/android-test | AndroidX Test, Espresso, UiAutomator test libraries. |
| Android test docs | https://developer.android.com/training/testing | Official testing overview. |
| Android Network Security Configuration | https://developer.android.com/privacy-and-security/security-config | Official app-side CA trust and cleartext/security config behavior. |
| Linux binderfs docs | https://docs.kernel.org/admin-guide/binderfs.html | Primary Linux kernel docs for binderfs. |
| Docker container run docs | https://docs.docker.com/engine/containers/run/ | Docker runtime flags, including privileged containers and device/capability handling. |

## Android Runtimes

| Source | URL | Notes |
| --- | --- | --- |
| Redroid docs repo | https://github.com/remote-android/redroid-doc | Primary Redroid documentation and deployment examples. |
| Redroid Docker image | https://hub.docker.com/r/redroid/redroid | Image tags and runtime notes. |
| Android Emulator Container Scripts | https://github.com/google/android-emulator-container-scripts | Google-maintained scripts for running Android Emulator in containers. |
| Docker-Android | https://github.com/budtmo/docker-android | Popular Android emulator Docker images with noVNC/Appium support. |
| HQarroum docker-android | https://github.com/HQarroum/docker-android | Minimal Android emulator service image with WebRTC, KVM focus. |
| Waydroid website | https://waydro.id/ | Android in a Linux container, Wayland-oriented. |
| Waydroid docs | https://docs.waydro.id/ | Installation and usage docs. |
| Waydroid repo | https://github.com/waydroid/waydroid | Source and issue tracker. |
| Anbox Cloud | https://canonical.com/anbox-cloud | Commercial Android cloud runtime from Canonical. |
| Anbox Cloud docs | https://github.com/canonical/anbox-cloud-docs | Public docs source. |
| Genymotion Cloud | https://www.genymotion.com/product-cloud/ | Commercial cloud Android devices for tests and CI. |
| Genymotion docs | https://docs.genymotion.com/ | Product docs and integrations. |
| Android-x86 | https://github.com/android-x86/android-x86.github.io | Android on x86 hardware/VMs. |
| Dock-Droid | https://github.com/sickcodes/dock-droid | QEMU/Android-in-container helper, useful as reference more than baseline. |

## Automation

| Source | URL | Notes |
| --- | --- | --- |
| Appium docs | https://appium.io/docs/en/2.0/ | Appium 2 documentation. |
| Appium repo | https://github.com/appium/appium | Main Appium server. |
| Appium UiAutomator2 driver | https://github.com/appium/appium-uiautomator2-driver | Android native/hybrid/mobile-web automation driver. |
| Appium Python client | https://github.com/appium/python-client | Python bindings. |
| Maestro docs | https://docs.maestro.dev/ | Declarative mobile UI test flows. |
| Maestro repo | https://github.com/mobile-dev-inc/Maestro | Open-source Maestro implementation. |
| openatx uiautomator2 | https://github.com/openatx/uiautomator2 | Python wrapper exposing Android UiAutomator through a device-side HTTP service. |
| openatx adbutils | https://github.com/openatx/adbutils | Python ADB client library. |
| scrcpy | https://github.com/Genymobile/scrcpy | Display and control Android devices over ADB, no root required. |
| DroidBot | https://github.com/honeynet/droidbot | UI-guided input generator and app explorer. |
| DroidBot paper | https://ylimit.github.io/static/files/DroidBot_ICSE2017.pdf | Research basis for DroidBot. |
| AndroidViewClient/culebra | https://github.com/dtmilano/AndroidViewClient | Python UI inspection and automation toolkit. |
| AndroidViewClient docs | https://dtmilano.github.io/AndroidViewClient/index.html | Project documentation. |
| DeviceFarmer STF | https://github.com/DeviceFarmer/stf | Browser-based Android device farm/control system. |
| OpenSTF legacy repo | https://github.com/openstf/stf | Legacy project; development moved to DeviceFarmer. |
| DroidRun/Mobilerun | https://github.com/droidrun/mobilerun | LLM-oriented mobile control framework. |
| AndroidWorld | https://github.com/google-research/android_world | Android emulator environment and benchmark for autonomous agents. |
| AndroidWorld site | https://google-research.github.io/android_world/ | Project overview and benchmark details. |
| AppAgent | https://github.com/TencentQQGYLab/AppAgent | Multimodal smartphone agent framework. |
| Mobile-Agent | https://github.com/X-PLUG/MobileAgent | Cross-platform GUI/mobile agent framework. |

## Network Capture, Proxying, And Tunneling

| Source | URL | Notes |
| --- | --- | --- |
| mitmproxy site | https://www.mitmproxy.org/ | Main project page. |
| mitmproxy repo | https://github.com/mitmproxy/mitmproxy | Source and issue tracker. |
| mitmproxy certificate docs | https://docs.mitmproxy.org/stable/concepts/certificates/ | CA installation concepts. |
| mitmproxy Android system CA guide | https://docs.mitmproxy.org/stable/howto/install-system-trusted-ca-android/ | Practical AVD system CA notes, including Google Play image limitations. |
| mitmproxy addon overview | https://docs.mitmproxy.org/stable/addons/overview/ | Python addon architecture. |
| mitmproxy addon examples | https://docs.mitmproxy.org/stable/addons/examples/ | Request/response modification examples. |
| Burp Android device config | https://portswigger.net/burp/documentation/desktop/mobile/config-android-device | Official Burp Android proxy setup. |
| Burp CA certificate docs | https://portswigger.net/burp/documentation/desktop/external-browser-config/certificate | CA trust background. |
| mitmproxy getting started | https://docs.mitmproxy.org/stable/overview/getting-started/ | Default local proxy posture and startup behavior. |
| Frida | https://github.com/frida/frida | Dynamic instrumentation toolkit. |
| Frida docs | https://frida.re/docs/home/ | Official documentation. |
| objection | https://github.com/sensepost/objection | Frida-powered mobile runtime exploration toolkit. |
| objection wiki | https://github.com/sensepost/objection/wiki/Using-objection | Usage notes and patched APK workflow. |
| PCAPdroid | https://github.com/emanuele-f/PCAPdroid | Open-source no-root Android traffic capture using VPNService. |
| PCAPdroid docs | https://emanuele-f.github.io/PCAPdroid/quick_start.html | Usage and architecture notes. |
| PCAPdroid how it works | https://github.com/emanuele-f/PCAPdroid/blob/master/docs/how_it_works.md | VPNService capture details and limitations. |
| Android tcpdump | https://androidtcpdump.com/ | Prebuilt tcpdump binaries for rooted Android packet capture. |
| WireGuard Android | https://github.com/WireGuard/wireguard-android | Android WireGuard client. |
| ics-openvpn | https://github.com/schwabe/ics-openvpn | Open-source Android OpenVPN client. |

## Practical Guides And Issue Threads

| Source | URL | Notes |
| --- | --- | --- |
| Android Emulator in CI | https://medium.com/androiddevelopers/android-emulator-in-a-ci-environment-dd65f63cdcd | Android Developers blog on containerized emulator internals. |
| Minimal command-line emulator on Linux | https://blogs.igalia.com/jaragunde/2023/12/setting-up-a-minimal-command-line-android-emulator-on-linux/ | Practical SDK/emulator setup without Android Studio. |
| AVD command-line testing guide | https://yarygintech.com/articles/android_emulator_avd_testing_guide/ | Practical 2026 AVD commands. |
| Genymotion Appium parallel tests | https://www.genymotion.com/blog/tutorial/parallel-tests-appium-saas/ | Vendor guide for Appium on Genymotion SaaS. |
| Headless Debian Appium/noVNC setup | https://medium.com/@amitsriv99/setting-up-android-emulator-appium-driver-novnc-on-headless-debian-12-compute-engine-e5811c997310 | Practical headless VM guide. |
| Install split APKs with ADB gist | https://gist.github.com/MasonFlint44/4b32d86da40f79d12355bb993fe23953 | Session-based split install example. |
| Manually install split APKs | https://raccoon.onyxbits.de/blog/install-split-apk-adb/ | Practical split APK install walkthrough. |
| Patching split APKs | https://nickbloor.co.uk/2020/03/29/patching-android-split-apks/ | Useful for analysis workflows. |
| Burp with Android Nougat | https://blog.ropnop.com/configuring-burp-suite-with-android-nougat/ | Explains Android 7+ CA trust changes. |
| Debug Android traffic with mitmproxy | https://www.dev-eth0.de/2022/01/04/debug-android-application-traffic/ | Practical AVD CA injection. |
| HTTP Toolkit Frida pinning article | https://httptoolkit.com/blog/frida-certificate-pinning/ | Useful background on pinning limitations and Frida-based testing. |
| AndroidWorld issue list | https://github.com/google-research/android_world/issues | Operational limitations and Docker issues. |
| Redroid ADB issue | https://github.com/remote-android/redroid-doc/issues/761 | Example Redroid ADB connectivity failure mode. |
| Redroid custom APK image issue | https://github.com/remote-android/redroid-doc/issues/843 | Notes for image customization with preinstalled APKs. |
| Docker-Android performance issue | https://github.com/budtmo/docker-android/issues/529 | Recent cloud-server performance concerns. |
| Objection newer Android issue | https://github.com/sensepost/objection/issues/800 | Example compatibility risk for newer Android/ART. |

## GitHub Metadata Snapshot

Collected through GitHub API on 2026-08-31.

| Repo | Stars | Forks | Archived | License | Pushed |
| --- | ---: | ---: | --- | --- | --- |
| remote-android/redroid-doc | 6743 | 476 | false | NOASSERTION | 2026-05-17 |
| google/android-emulator-container-scripts | 2076 | 291 | false | Apache-2.0 | 2026-07-24 |
| google/android-cuttlefish | 704 | 242 | false | NOASSERTION | 2026-08-31 |
| budtmo/docker-android | 15797 | 1745 | false | NOASSERTION | 2026-08-30 |
| Genymobile/scrcpy | 148608 | 13666 | false | Apache-2.0 | 2026-08-17 |
| appium/appium | 21912 | 6284 | false | Apache-2.0 | 2026-08-30 |
| appium/appium-uiautomator2-driver | 871 | 230 | false | Apache-2.0 | 2026-08-28 |
| mobile-dev-inc/Maestro | 15460 | 936 | false | Apache-2.0 | 2026-08-31 |
| openatx/uiautomator2 | 8315 | 1593 | false | MIT | 2026-08-07 |
| openatx/adbutils | 1073 | 227 | false | MIT | 2026-08-10 |
| Swind/pure-python-adb | 602 | 111 | false | MIT | 2024-04-02 |
| mitmproxy/mitmproxy | 44863 | 4704 | false | MIT | 2026-08-25 |
| frida/frida | 21802 | 2201 | false | NOASSERTION | 2026-08-27 |
| emanuele-f/PCAPdroid | 4626 | 536 | false | GPL-3.0 | 2026-08-30 |
| WireGuard/wireguard-android | 1593 | 581 | false | Apache-2.0 | 2026-06-29 |
| schwabe/ics-openvpn | 3972 | 1364 | false | NOASSERTION | 2026-07-12 |
| DeviceFarmer/stf | 4548 | 620 | false | NOASSERTION | 2026-08-29 |
| google/bundletool | 4030 | 427 | false | Apache-2.0 | 2025-12-15 |
| android/android-test | 1220 | 344 | false | Apache-2.0 | 2026-08-24 |
