# Packages And Libraries

Last checked: 2026-08-31.

## Python

| Package | URL | Use | Notes |
| --- | --- | --- | --- |
| `adbutils` | https://github.com/openatx/adbutils | Python client for ADB server | Active, MIT, good first candidate for orchestration. |
| `uiautomator2` | https://github.com/openatx/uiautomator2 | Python UI automation through device-side service | Strong for custom automation; installs helper service. |
| `Appium-Python-Client` | https://github.com/appium/python-client | Python Appium client | Official Appium Python path. |
| `pure-python-adb` | https://github.com/Swind/pure-python-adb | Pure Python ADB client | Less active than `adbutils`; evaluate before use. |
| `python-adb` | https://github.com/google/python-adb | ADB/Fastboot protocol implementation | Repo itself recommends better-maintained alternatives. |
| `mitmproxy` addons | https://docs.mitmproxy.org/stable/addons/overview/ | Programmatic proxy behavior | Best path for Python traffic modification. |
| `frida` Python bindings | https://frida.re/docs/home/ | Dynamic instrumentation control | Use only for authorized security workflows. |
| `objection` | https://github.com/sensepost/objection | CLI/runtime mobile exploration | Useful operator tool, not core library. |

## Node / TypeScript

| Package | URL | Use | Notes |
| --- | --- | --- | --- |
| `adbkit` legacy | https://github.com/openstf/adbkit | Node ADB client | Development moved to DeviceFarmer. |
| DeviceFarmer `adbkit` | https://github.com/DeviceFarmer/adbkit | Node/TypeScript ADB client | Useful if a web/device-farm layer is built. |
| `adbkit-logcat` | https://github.com/openstf/adbkit-logcat | Logcat parser | Legacy moved to DeviceFarmer equivalents. |
| `adbkit-monkey` | https://github.com/openstf/adbkit-monkey | Monkey protocol wrapper | Interesting for non-random Monkey control. |

## Go / Rust

| Package | URL | Use | Notes |
| --- | --- | --- | --- |
| `go-adbkit` | https://github.com/codeskyblue/go-adbkit | Go ADB client | Useful reference if Go tools are added later. |
| `goadb` | https://pkg.go.dev/github.com/kottle/goadb | Go ADB interface | Older/heavy-development warning. |
| `Rust-ADB` | https://github.com/ImKKingshuk/Rust-ADB | Rust ADB library | Newer; not needed for Python-first control layer. |

## CLI Tools To Keep As External Dependencies

| Tool | URL | Use |
| --- | --- | --- |
| `adb` | https://developer.android.com/tools/adb | Device lifecycle, shell, install, port forwarding. |
| `emulator` | https://developer.android.com/studio/run/emulator-commandline | Official AVD launch. |
| `sdkmanager` / `avdmanager` | https://developer.android.com/tools | SDK/image provisioning. |
| `bundletool` | https://developer.android.com/tools/bundletool | AAB/APKS handling. |
| `apksigner` | https://developer.android.com/tools/apksigner | APK signing and verification. |
| `mitmproxy` / `mitmdump` / `mitmweb` | https://www.mitmproxy.org/ | Proxy and scripted traffic modification. |
| `scrcpy` | https://github.com/Genymobile/scrcpy | Manual visual control. |
| `frida-tools` | https://frida.re/docs/home/ | Authorized instrumentation workflows. |
| `tcpdump` | https://androidtcpdump.com/ | Rooted Android packet capture. |

## Repo Preference

For the Python control layer, start with subprocess-backed `adb` wrappers unless
the wrapper API becomes too noisy. This keeps behavior transparent and matches
the repo principle that backend modules should remain visible. If a library is
needed, evaluate `adbutils` first because it is active, Python-native, and
covers devices, shell, forward/reverse, file transfer, and logcat.

