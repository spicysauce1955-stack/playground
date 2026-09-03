# Automation Tools

Last checked: 2026-08-31.

## Selection Model

Use layered automation rather than choosing one tool for every task.

| Layer | Tools | Use |
| --- | --- | --- |
| Device lifecycle | ADB, `pm`, `am`, `logcat`, `screencap` | Connect, install, launch, clear data, collect logs, basic checks. |
| Low-level UI input | `adb shell input`, `uiautomator dump`, Monkey | Simple black-box actions and smoke checks. |
| Structured black-box UI | Appium UiAutomator2, openatx `uiautomator2` | Element lookup, gestures, text input, screenshots, arbitrary apps. |
| Declarative UI flows | Maestro | Readable flows, CI smoke tests, product-level test scenarios. |
| Remote visual/manual control | scrcpy, noVNC/WebRTC wrappers | Debugging and operator intervention. |
| Deep instrumentation | Frida, objection | Authorized security testing, dynamic analysis, TLS pinning investigation. |
| Exploration/fuzzing | DroidBot, AndroidViewClient/culebra | Discover app state graphs and generate broad interaction coverage. |
| Agent-oriented automation | AndroidWorld, Mobilerun, AppAgent, Mobile-Agent | Research and future autonomous task execution. |

## ADB Input And UI Dumps

- Official base: https://developer.android.com/tools/adb
- Useful for: deterministic taps, text entry, key events, screen captures,
  layout dumps, boot readiness probes.
- Works on: nearly every runtime with ADB.
- Requires app source: no.
- Headless: yes.
- Limitations: coordinate-driven actions are brittle; `uiautomator dump` can
  miss custom-rendered UI, WebViews, games, or constantly animating screens.

Example:

```bash
adb shell input tap 500 1200
adb shell input text "hello%sworld"
adb shell input keyevent ENTER
adb shell uiautomator dump /sdcard/window.xml
adb pull /sdcard/window.xml .
```

## Appium UiAutomator2

- Docs: https://appium.io/docs/en/2.0/
- Server repo: https://github.com/appium/appium
- Android driver: https://github.com/appium/appium-uiautomator2-driver
- Python client: https://github.com/appium/python-client
- Category: WebDriver-based black-box automation.
- Works on: Android emulator, Redroid if driver dependencies install and ADB is
  stable, physical devices, cloud devices.
- Requires app source: no.
- Headless: yes.
- Strengths: mature ecosystem, multi-language clients, native/hybrid/web support,
  standard protocol, broad CI support.
- Limitations: Appium server/driver setup, desired capabilities complexity,
  slower than direct UiAutomator in some workflows, version compatibility.
- Fit: best general-purpose UI automation layer after ADB lifecycle is stable.

## openatx uiautomator2

- Source: https://github.com/openatx/uiautomator2
- PyPI: https://pypi.org/project/uiautomator2/
- Category: Python client plus device-side HTTP service over UiAutomator.
- Works on: ADB-connected Android devices/emulators.
- Requires app source: no.
- Headless: yes.
- Strengths: Python-native, practical API, direct device control, good for
  custom orchestration.
- Limitations: installs/starts helper APK/service, compatibility depends on
  device image and Android version, less standard than WebDriver.
- Fit: strong candidate for Python-first lab automation when Appium feels too
  heavy.

## Maestro

- Docs: https://docs.maestro.dev/
- Source: https://github.com/mobile-dev-inc/Maestro
- Category: declarative YAML mobile UI automation.
- Works on: Android emulators/devices and iOS simulators/devices.
- Requires app source: no for many flows.
- Headless: yes when device/emulator is available.
- Strengths: readable flows, fast ramp-up, useful for smoke tests and demos,
  active upstream.
- Limitations: less flexible than code for dynamic workflows; ecosystem narrower
  than Appium; cloud/commercial features may matter at scale.
- Fit: good for lab presets and acceptance scenarios.

## Espresso And AndroidX Test

- Source: https://github.com/android/android-test
- Docs: https://developer.android.com/training/testing
- Category: instrumentation testing framework.
- Works on: app under test with Android test APK.
- Requires app source/test APK: usually yes.
- Headless: yes with emulator/device.
- Strengths: fast and stable for first-party app testing.
- Limitations: not suitable for arbitrary third-party app control.
- Fit: not a primary tool for this repo unless testing owned apps.

## Monkey

- Docs base: https://developer.android.com/tools/adb
- Category: random or scripted input events through Android shell.
- Works on: almost every ADB runtime.
- Requires app source: no.
- Strengths: cheap stability smoke testing.
- Limitations: poor semantic control; random input can be noisy.
- Fit: useful for crash/stability presets, not for reliable workflows.

## scrcpy

- Source: https://github.com/Genymobile/scrcpy
- Category: display and control over ADB.
- Works on: ADB-connected devices/emulators without root.
- Headless: can run server-side, but typically needs display/client for human
  interaction.
- Strengths: excellent manual debugging and visual control.
- Limitations: not a test framework by itself.
- Fit: operator inspection tool for lab sessions.

## Frida And Objection

- Frida source: https://github.com/frida/frida
- Frida docs: https://frida.re/docs/home/
- objection source: https://github.com/sensepost/objection
- objection wiki: https://github.com/sensepost/objection/wiki/Using-objection
- Category: dynamic instrumentation and runtime exploration.
- Works on: rooted devices/emulators with `frida-server`, or patched apps with
  Frida Gadget.
- Requires app source: no, but authorization matters.
- Strengths: inspect runtime behavior, hook APIs, support security testing.
- Limitations: version-sensitive, root/patched APK friction, may break on newer
  Android runtimes, not appropriate as the default automation layer.
- Fit: optional security-analysis preset after baseline app automation works.

## DroidBot

- Source: https://github.com/honeynet/droidbot
- Paper: https://ylimit.github.io/static/files/DroidBot_ICSE2017.pdf
- Category: UI-guided input generator and app explorer.
- Works on: Android devices/emulators through ADB.
- Requires app source: no.
- Strengths: generates UI transition graph, useful for exploration.
- Limitations: research/fuzzing style rather than deterministic product flows.
- Fit: good for future "explore app" lab capability.

## Device Farms

- DeviceFarmer STF: https://github.com/DeviceFarmer/stf
- OpenSTF legacy: https://github.com/openstf/stf
- Category: browser-based remote control and device management.
- Works on: ADB-connected devices, likely adaptable to emulator fleets.
- Strengths: multi-device web UI, remote debugging, ADB integration.
- Limitations: larger platform, original OpenSTF is legacy, DeviceFarmer is
  community-maintained.
- Fit: future reference if the repo evolves from one active lab to a device farm.

## Agent-Oriented Android Control

These are useful research references, but should not drive the first
implementation slice.

| Project | URL | Notes |
| --- | --- | --- |
| AndroidWorld | https://github.com/google-research/android_world | Reproducible Android emulator benchmark for autonomous agents. |
| Mobilerun | https://github.com/droidrun/mobilerun | CLI/Python API for LLM-driven mobile actions. |
| DroidBot-GPT | https://github.com/MobileLLM/DroidBot-GPT | Natural-language Android UI agent built on DroidBot. |
| AppAgent | https://github.com/TencentQQGYLab/AppAgent | Multimodal smartphone agent framework. |
| Mobile-Agent | https://github.com/X-PLUG/MobileAgent | Cross-platform GUI/mobile agent framework. |
| AndroidLab | https://github.com/THUDM/Android-Lab | Android autonomous-agent framework and benchmark. |

