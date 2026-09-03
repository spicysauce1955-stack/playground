# Runtime Options

Last checked: 2026-08-31.

## Baseline Recommendation

Use Redroid first for this repo because it matches the current
`tofu/ -> ansible/ -> Redroid -> ADB` path. It is container-native, exposes ADB
over TCP, can run many Android instances on Linux, and maps naturally to
OpenTofu-provisioned hosts and Ansible-installed Docker workloads.

Keep the stock Android Emulator as the compatibility reference because it is the
official emulator path and is the most likely target for Appium, Maestro,
AndroidX Test, and Google Play image workflows.

## Redroid

- Category: Android-in-container runtime.
- Official/source: https://github.com/remote-android/redroid-doc
- Image: https://hub.docker.com/r/redroid/redroid
- License: repository does not report a standard SPDX license through GitHub API.
- ADB: yes, commonly exposed on TCP `5555`.
- Headless/server fit: high.
- Docker/KVM: Docker/Podman/Kubernetes oriented; does not depend on the stock
  emulator's KVM acceleration path, but does depend on Linux binder support.
- Typical host requirements: binder/binderfs support, privileged container
  settings, suitable kernel modules/devices, enough CPU/RAM for Android userspace.
- App install/launch: through ADB using standard `adb install`, `pm`, and `am`.
- Automation: Appium, UiAutomator2, ADB input, scrcpy, and Frida should be
  evaluated per image/root mode.
- Network: Docker/VM networking can isolate traffic and route it through lab
  proxies; Android-side proxy/CA behavior follows Android version and image.
- Fit: best default for this repo.
- Main risks: privileged container requirements, public ADB exposure, graphics
  and ARM translation needs, image customization process for preinstalled APKs.
- Sources: https://github.com/remote-android/redroid-doc,
  https://hub.docker.com/r/redroid/redroid,
  https://docs.kernel.org/admin-guide/binderfs.html,
  https://docs.docker.com/engine/containers/run/,
  https://github.com/remote-android/redroid-doc/issues/761,
  https://github.com/remote-android/redroid-doc/issues/843

## Android Emulator / AVD

- Category: official QEMU-based Android emulator.
- Official docs: https://developer.android.com/studio/run/emulator-commandline
- Release notes: https://developer.android.com/studio/releases/emulator
- Container scripts: https://github.com/google/android-emulator-container-scripts
- ADB: yes.
- Headless/server fit: good when KVM is available; possible but slower without
  acceleration.
- Docker/KVM: hardware acceleration usually needs `/dev/kvm`; container scripts
  require Docker and host KVM for practical performance.
- Google Play: available through specific system images, but those images often
  restrict `adb root`.
- App install/launch: best-supported path for official Android testing tools.
- Automation: strongest compatibility with Appium, Maestro, Espresso, AndroidX
  Test, and standard emulator flags.
- Network: emulator proxy settings, `adb reverse`, `adb forward`, host routing,
  and system CA injection on rootable images.
- Fit: compatibility fallback and CI baseline, especially for official images.
- Main risks: KVM/nested virtualization availability, graphics dependencies,
  slow boot, image licensing/downloads, Google Play image restrictions.
- Sources: https://developer.android.com/studio/run/emulator-commandline,
  https://developer.android.com/tools/adb,
  https://github.com/google/android-emulator-container-scripts,
  https://medium.com/androiddevelopers/android-emulator-in-a-ci-environment-dd65f63cdcd

## Cuttlefish

- Category: configurable virtual Android device from AOSP.
- Official docs: https://source.android.com/docs/devices/cuttlefish
- Host tools: https://github.com/google/android-cuttlefish
- Generated docs: https://google.github.io/android-cuttlefish/
- ADB: yes.
- Headless/server fit: good for AOSP-style virtual devices and cloud-hosted
  Android development.
- Docker/KVM: uses host virtualization; local Linux and cloud workflows are first
  class in docs.
- App install/launch: standard ADB.
- Automation: should support ADB/Appium-like flows once the device is running,
  but more AOSP-oriented than QA-product oriented.
- Network: virtual networking can be integrated with host/cloud topology; proxy
  and CA behavior follows Android image.
- Fit: strong future option if the project needs AOSP fidelity or virtualized
  Android devices beyond Redroid.
- Main risks: operational complexity, image/build workflow, less direct fit than
  Redroid for current Docker runtime baseline.
- Sources: https://source.android.com/docs/devices/cuttlefish,
  https://github.com/google/android-cuttlefish,
  https://google.github.io/android-cuttlefish/

## Docker-Android

- Category: stock Android Emulator packaged as Docker images, commonly with
  noVNC and Appium support.
- Source: https://github.com/budtmo/docker-android
- ADB: yes.
- Headless/server fit: good for test farms and CI when host supports the emulator.
- Docker/KVM: KVM strongly preferred; image abstracts display/noVNC/Appium setup.
- App install/launch: standard ADB and Appium.
- Automation: Appium-first, with browser/noVNC inspection.
- Network: Docker network plus emulator proxy settings.
- Fit: useful reference or fallback when wanting a batteries-included Appium
  emulator container.
- Main risks: heavy images, emulator performance, version matrix complexity,
  cloud-host performance issues.
- Sources: https://github.com/budtmo/docker-android,
  https://github.com/budtmo/docker-android/issues/529

## HQarroum Docker Android

- Category: minimal Android emulator service in Docker.
- Source: https://github.com/HQarroum/docker-android
- ADB: yes.
- Headless/server fit: good.
- Docker/KVM: KVM-focused.
- Automation: works as an emulator service; add Appium or ADB orchestration
  externally.
- Network: standard Docker and emulator routing.
- Fit: useful if the project wants a smaller emulator container reference than
  Docker-Android.
- Sources: https://github.com/HQarroum/docker-android

## Waydroid

- Category: Android in a Linux container, integrated with a Wayland Linux host.
- Website: https://waydro.id/
- Docs: https://docs.waydro.id/
- Source: https://github.com/waydroid/waydroid
- ADB: available through Android debugging setup, but not as central as Redroid.
- Headless/server fit: medium; excellent for Linux desktop/kiosk integration,
  less direct for headless lab fleets.
- Docker/KVM: container/namespaces model rather than stock emulator KVM.
- App install/launch: APK install supported; Play/GApps depends on image/setup.
- Automation: ADB and UI tools may work, but Wayland session assumptions matter.
- Network: host/container network integration; Android CA/proxy limitations still
  apply.
- Fit: useful alternative for Linux desktop Android-app compatibility, not first
  pick for this repo's server lab path.
- Sources: https://waydro.id/, https://docs.waydro.id/,
  https://github.com/waydroid/waydroid

## Genymotion

- Category: commercial Android virtual devices, desktop and cloud.
- Product: https://www.genymotion.com/product-cloud/
- Docs: https://docs.genymotion.com/
- ADB: yes.
- Headless/server fit: high for SaaS/cloud workflows.
- Docker/KVM: vendor-managed for SaaS; self-hosting details depend on product.
- App install/launch: ADB and vendor APIs.
- Automation: strong Appium/CI integration.
- Network: proxy/CA/root features depend on image/product tier.
- Fit: good benchmark or fallback when outsourcing the device-farm layer is
  acceptable.
- Main risks: commercial dependency and less alignment with local-first lab goals.
- Sources: https://www.genymotion.com/product-cloud/,
  https://docs.genymotion.com/,
  https://www.genymotion.com/blog/tutorial/parallel-tests-appium-saas/

## Anbox Cloud

- Category: commercial Android cloud runtime from Canonical.
- Product: https://canonical.com/anbox-cloud
- Docs source: https://github.com/canonical/anbox-cloud-docs
- ADB: supported through product workflows.
- Headless/server fit: high.
- Docker/KVM: LXD/system-container and VM-backed Android workloads, product-managed.
- Automation: cloud Android workload orchestration, streaming, and lifecycle APIs.
- Network: product-managed networking and streaming topology.
- Fit: useful architecture reference for scale, not a local-first open-source
  baseline.
- Main risks: commercial platform, Juju/LXD operational model, license/cost.
- Sources: https://canonical.com/anbox-cloud,
  https://github.com/canonical/anbox-cloud-docs,
  https://ubuntu.com/blog/virtualized-android-comes-to-anbox-cloud

## Android-x86 / QEMU

- Category: Android port running in full x86 VMs.
- Source: https://github.com/android-x86/android-x86.github.io
- ADB: possible depending on image/config.
- Headless/server fit: medium.
- Docker/KVM: QEMU/KVM host setup.
- App install/launch: standard Android once booted.
- Automation: ADB/Appium possible with setup.
- Network: VM-level capture is straightforward; Android CA limitations remain.
- Fit: useful for full-VM isolation and experiments, but heavier than Redroid.
- Sources: https://github.com/android-x86/android-x86.github.io,
  https://help.clouding.io/hc/en-us/articles/4405454393756-How-to-virtualize-Android-with-QEMU-KVM
