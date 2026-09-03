# App Install And Launch

Last checked: 2026-08-31.

## Baseline

Use ADB as the first implementation target. It works across Redroid, stock
emulator, Cuttlefish, Docker-Android, Genymotion, and most physical devices.

Primary source: https://developer.android.com/tools/adb

## Common ADB Lifecycle Commands

```bash
adb devices -l

# Preferred for this repo: keep ADB on a private VM/container interface and
# forward it over SSH or the repo's `playground adb` workflow.
ssh -L 15555:127.0.0.1:5555 lab-vm
adb connect 127.0.0.1:15555
adb disconnect 127.0.0.1:15555

# Raw remote ADB is only acceptable on isolated lab-private networks.
adb connect <private_lab_host>:5555

adb install app.apk
adb install -r app.apk
adb install-multiple base.apk split_config.arm64_v8a.apk split_config.en.apk

adb shell pm list packages
adb shell pm path com.example.app
adb shell pm clear com.example.app
adb shell pm grant com.example.app android.permission.POST_NOTIFICATIONS
adb shell pm revoke com.example.app android.permission.POST_NOTIFICATIONS
adb uninstall com.example.app

adb shell monkey -p com.example.app -c android.intent.category.LAUNCHER 1
adb shell am start -n com.example.app/.MainActivity
adb shell am force-stop com.example.app
adb logcat
adb shell screencap -p /sdcard/screen.png
adb pull /sdcard/screen.png .
```

## Split APKs And AAB/APKS

Android App Bundles are not directly installed as a single universal APK unless
they are converted or a matching APK set is available.

Recommended paths:

| Artifact | Preferred install path |
| --- | --- |
| Single `.apk` | `adb install` |
| Multiple split APK files | `adb install-multiple` or `pm install-create` session |
| `.aab` | `bundletool build-apks`, then `bundletool install-apks` |
| `.apks` from bundletool | `bundletool install-apks` or extract and install matching splits |
| `.xapk` / `.apkm` | Extract/normalize in orchestration, or use installer apps only for manual workflows |

Official sources:

- https://developer.android.com/tools/bundletool
- https://github.com/google/bundletool
- https://developer.android.com/tools/apksigner

Practical sources:

- https://gist.github.com/MasonFlint44/4b32d86da40f79d12355bb993fe23953
- https://raccoon.onyxbits.de/blog/install-split-apk-adb/
- https://nickbloor.co.uk/2020/03/29/patching-android-split-apks/
- https://android.googlesource.com/platform/frameworks/base/+/android-8.0.0_r4/services/core/java/com/android/server/pm/PackageManagerShellCommand.java

## Session-Based Split Install

Use this path when `adb install-multiple` is not enough or when an orchestrator
needs explicit control over staged installation.

```bash
adb push base.apk /data/local/tmp/base.apk
adb push split_config.arm64_v8a.apk /data/local/tmp/split_config.arm64_v8a.apk

adb shell pm install-create -S <total_bytes>
adb shell pm install-write -S <base_apk_bytes> <session_id> base /data/local/tmp/base.apk
adb shell pm install-write -S <split_apk_bytes> <session_id> split_config.arm64_v8a /data/local/tmp/split_config.arm64_v8a.apk
adb shell pm install-commit <session_id>
```

Keep this in docs or a helper script later, not embedded as ad hoc string
concatenation in business logic.

## Launch Discovery

Known package, launcher activity unknown:

```bash
adb shell cmd package resolve-activity --brief com.example.app
adb shell monkey -p com.example.app -c android.intent.category.LAUNCHER 1
```

Known activity:

```bash
adb shell am start -n com.example.app/.MainActivity
```

URL/deep link:

```bash
adb shell am start -a android.intent.action.VIEW -d "https://example.com/path"
```

## Repo Implications

- Store app artifacts under `.playground/` only if generated or downloaded at
  runtime; committed config should reference source locations, not embed binary
  APKs.
- Avoid assuming one device. Every command wrapper should accept a serial or
  transport target.
- Do not open unauthenticated ADB broadly. `docs/architecture/CONTRACTS.md`
  says to reach Redroid ADB through SSH forwarding, such as `playground adb`,
  because ADB itself has no authentication.
- Split install needs a device-aware step because `bundletool` selects APKs
  based on device spec.
- Preinstalled app images for Redroid may be useful later, but first implement
  runtime ADB install so app state remains explicit and reproducible.
