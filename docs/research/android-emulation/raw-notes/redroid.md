# Redroid Notes

Last checked: 2026-08-31.

- Source: https://github.com/remote-android/redroid-doc
- Image: https://hub.docker.com/r/redroid/redroid
- Category: Android-in-container runtime.
- Fit: primary candidate for this repo.
- ADB: yes, commonly TCP `5555`.
- Host concerns: binder/binderfs devices, privileged container, Docker/Podman/K8s
  runtime config, GPU/ARM translation needs depending on workload.
- App control: standard `adb install`, `pm`, `am`, `monkey`, `input`.
- Network: leverage Docker/VM networking; avoid public ADB exposure.
- Useful issues:
  - https://github.com/remote-android/redroid-doc/issues/761
  - https://github.com/remote-android/redroid-doc/issues/843

