# ADB Notes

Last checked: 2026-08-31.

- Docs: https://developer.android.com/tools/adb
- Use for: connect, install, uninstall, shell, package manager, activity manager,
  logcat, screenshots, port forward/reverse.
- Repo fit: first control API.
- Implementation note: every wrapper should accept an explicit device serial or
  transport target.

```bash
ssh -L 15555:127.0.0.1:5555 lab-vm
adb connect 127.0.0.1:15555
adb -s <serial> install app.apk
adb -s <serial> shell pm clear <package>
adb -s <serial> shell am start -n <package>/<activity>
adb -s <serial> reverse tcp:8080 tcp:8080
adb -s <serial> forward tcp:9000 tcp:9000
```

Raw `adb connect <host>:5555` belongs only on isolated lab-private networks.
This repo's contract prefers SSH-forwarded ADB because ADB has no authentication.
