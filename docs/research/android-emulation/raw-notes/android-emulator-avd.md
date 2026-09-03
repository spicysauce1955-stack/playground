# Android Emulator / AVD Notes

Last checked: 2026-08-31.

- Command-line docs: https://developer.android.com/studio/run/emulator-commandline
- ADB docs: https://developer.android.com/tools/adb
- Container scripts: https://github.com/google/android-emulator-container-scripts
- Category: official emulator.
- Fit: compatibility fallback and official test baseline.
- Headless: `-no-window`; KVM strongly recommended for server use.
- Google Play images: useful for Play Services testing but often restrict
  `adb root`.
- Network: emulator proxy flags/global proxy, `adb forward`, `adb reverse`,
  system CA on rootable images.

