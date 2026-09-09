"""adb command-string builders.

These are pure argument builders so the CLI layer stays thin and every
command shape is unit-testable without a device. Command syntax follows
docs/research/android-emulation/app-install-launch.md.

Builders return `AdbInvocation` (see `playground.android.transport`), not
a string -- every assertion here that inspects the rendered command wraps
the builder call in `render_guest_command(...)` first, which reproduces
exactly the guest shell string these builders used to return directly.
"""

from __future__ import annotations

import shlex

from playground.android.commands import (
    ADB_PORT,
    adb_shell,
    clear_cmd,
    input_cmd,
    install_cmd,
    install_multiple_cmd,
    launch_cmd,
    list_packages_cmd,
    logcat_cmd,
    pull_cmd,
    push_cmd,
    screenshot_cmd,
    stop_cmd,
    ui_dump_cmd,
    uninstall_cmd,
)
from playground.android.transport import guest_prefix, render_guest_command


def _simulate_device_argv(cmd: str) -> list[str]:
    """Approximate the two shell parses `cmd` survives before a real
    device sees it: the guest's login shell (which runs `cmd` via ssh)
    reduces the quoted device script to ONE token for `adb shell`, then
    `adb shell` hands that token to /system/bin/sh -c on the device for a
    second, independent parse. `shlex.split` follows the same POSIX
    quoting rules as `shlex.quote`, so it is an adequate stand-in for both
    hops in a unit test that has no real shell or device."""
    guest_tokens = shlex.split(cmd)
    device_script = guest_tokens[-1]
    return shlex.split(device_script)


def test_every_command_connects_first() -> None:
    """adb must not depend on a pre-existing server on the guest."""
    assert f"adb connect 127.0.0.1:{ADB_PORT}" in guest_prefix()
    assert "|| true" in guest_prefix(), "a stale connect must not fail the command"


def test_adb_shell_targets_the_serial() -> None:
    cmd = render_guest_command(adb_shell("getprop sys.boot_completed"))
    assert f"-s 127.0.0.1:{ADB_PORT}" in cmd
    assert cmd.startswith(guest_prefix())


def test_install_uses_r_so_reinstall_is_allowed() -> None:
    assert "install -r" in render_guest_command(install_cmd("/tmp/a.apk"))


def test_install_multiple_passes_every_split() -> None:
    cmd = render_guest_command(
        install_multiple_cmd(["/tmp/base.apk", "/tmp/split_config.en.apk"])
    )
    assert "install-multiple -r" in cmd
    assert "/tmp/base.apk" in cmd
    assert "/tmp/split_config.en.apk" in cmd


def test_uninstall_removes_the_named_package() -> None:
    assert "uninstall com.example.app" in render_guest_command(
        uninstall_cmd("com.example.app")
    )


def test_launch_prefers_url_over_activity() -> None:
    cmd = render_guest_command(launch_cmd("com.x", activity=".Main", url="https://e.com/p"))
    assert "android.intent.action.VIEW" in cmd
    assert "https://e.com/p" in cmd
    assert ".Main" not in cmd


def test_launch_with_activity_uses_am_start_n() -> None:
    cmd = render_guest_command(launch_cmd("com.x", activity=".Main", url=None))
    assert "am start -n com.x/.Main" in cmd


def test_launch_without_either_resolves_the_launcher_activity() -> None:
    """monkey is not a reliable launcher (see the live-bug test below), so
    the default path discovers the real component first."""
    cmd = render_guest_command(launch_cmd("com.x", activity=None, url=None))
    assert "resolve-activity --brief" in cmd
    assert "am start -n" in cmd
    # monkey is kept only as the last-resort branch.
    assert "monkey -p com.x" in cmd
    assert "android.intent.category.LAUNCHER" in cmd


def test_stop_and_clear_are_distinct() -> None:
    assert "am force-stop com.x" in render_guest_command(stop_cmd("com.x"))
    assert "pm clear com.x" in render_guest_command(clear_cmd("com.x"))


def test_list_packages_third_party_flag() -> None:
    assert "pm list packages -3" in render_guest_command(
        list_packages_cmd(pattern=None, third_party=True)
    )
    cmd = render_guest_command(list_packages_cmd(pattern="maps", third_party=False))
    assert "pm list packages" in cmd
    assert "maps" in cmd


def test_screenshot_and_ui_dump_write_to_the_given_remote_path() -> None:
    screenshot = render_guest_command(screenshot_cmd("/sdcard/s.png"))
    assert "/sdcard/s.png" in screenshot
    assert "screencap -p" in screenshot
    ui_dump = render_guest_command(ui_dump_cmd("/sdcard/ui.xml"))
    assert "uiautomator dump" in ui_dump
    assert "/sdcard/ui.xml" in ui_dump


def test_push_cmd_operand_order_is_source_then_destination() -> None:
    cmd = render_guest_command(push_cmd("/tmp/staged.apk", "/sdcard/dest.apk"))
    assert "push /tmp/staged.apk /sdcard/dest.apk" in cmd


def test_pull_cmd_operand_order_is_source_then_destination() -> None:
    cmd = render_guest_command(pull_cmd("/sdcard/source.bin", "/tmp/local.bin"))
    assert "pull /sdcard/source.bin /tmp/local.bin" in cmd


def test_push_and_pull_cmd_return_unquoted_args() -> None:
    """`AdbInvocation.args` must stay UNQUOTED -- outer quoting is
    `render_guest_command`'s job, not a builder's (see the module
    docstring); a path with a space proves neither builder pre-quotes."""
    push = push_cmd("/tmp/my app.apk", "/sdcard/dest.apk")
    assert push.args == ("push", "/tmp/my app.apk", "/sdcard/dest.apk")
    pull = pull_cmd("/sdcard/my file.bin", "/tmp/my file.bin")
    assert pull.args == ("pull", "/sdcard/my file.bin", "/tmp/my file.bin")


def test_logcat_dump_vs_follow() -> None:
    assert "-d" in render_guest_command(logcat_cmd(follow=False, lines=None))
    assert "-d" not in render_guest_command(logcat_cmd(follow=True, lines=None))
    assert "-t 50" in render_guest_command(logcat_cmd(follow=False, lines=50))


def test_input_builds_the_subcommand() -> None:
    assert "input text hello" in render_guest_command(input_cmd("text", ["hello"]))
    assert "input tap 10 20" in render_guest_command(input_cmd("tap", ["10", "20"]))
    assert "input keyevent KEYCODE_HOME" in render_guest_command(
        input_cmd("key", ["KEYCODE_HOME"])
    )


def test_shell_metacharacters_are_quoted() -> None:
    """A package or text argument must not be able to inject a shell command."""
    cmd = render_guest_command(input_cmd("text", ["a; rm -rf /"]))
    assert "; rm -rf /" not in cmd.replace("'a; rm -rf /'", "")


def test_monkey_launch_uses_pidof_as_the_success_signal() -> None:
    """LIVE BUG 2026-08-31: monkey returns 251 on Redroid 11 even when the
    app launches fine, while `am start` returns 0. Relying on monkey's exit
    status made `playground app launch` report failure on every successful
    launch, and would have failed the whole apply for any lab declaring
    `launch: true` without an activity."""
    cmd = render_guest_command(launch_cmd("com.example.app", activity=None, url=None))
    assert "resolve-activity --brief" in cmd, "prefer the real launcher component"
    assert "monkey -p" in cmd, "monkey remains the last-resort branch"
    assert "pidof" in cmd, "monkey's exit code must not decide success"
    # The device-side script must be ONE quoted argument so `;` runs on the
    # device, not on the guest that invokes adb.
    assert cmd.count("shell ") == 1
    tail = cmd.split("shell ", 1)[1]
    assert tail.startswith("'"), "device script must be quoted as one arg"


def test_activity_launch_needs_no_pidof_because_am_start_is_honest() -> None:
    cmd = render_guest_command(launch_cmd("com.example.app", activity=".Main", url=None))
    assert "am start -n" in cmd
    assert "pidof" not in cmd


# --- BUG A: adb_shell quotes for the guest hop but not the adb-shell hop ---
# `adb shell` flattens whatever argv it receives by joining with spaces
# before handing that string to the device's own sh -c. Anything that
# reaches `adb shell` as more than one token loses its structure there,
# so multi-word arguments truncate and shell metacharacters get a second,
# unwanted parse on the device.


def test_adb_shell_quotes_the_whole_command_as_one_token() -> None:
    cmd = render_guest_command(adb_shell("echo hi there"))
    assert cmd.count("shell ") == 1
    tail = cmd.split("shell ", 1)[1]
    assert tail == shlex.quote("echo hi there")


def test_input_text_multiword_survives_the_double_shell_hop() -> None:
    """LIVE BUG: `input text 'hello world'` must arrive on the device as
    ONE argument, or `input` only types the first word."""
    cmd = render_guest_command(input_cmd("text", ["hello world"]))
    argv = _simulate_device_argv(cmd)
    assert argv[-2:] == ["text", "hello world"]


def test_command_substitution_payload_is_inert_on_the_device() -> None:
    """A `$(...)` payload in a package name or input text must reach the
    device as a literal argument, never something the device's sh
    expands (device-side injection, not just guest-side)."""
    cmd = render_guest_command(input_cmd("text", ["$(reboot)"]))
    argv = _simulate_device_argv(cmd)
    assert argv[-1] == "$(reboot)"

    cmd = render_guest_command(uninstall_cmd("`id`; rm -rf /"))
    # uninstall_cmd does not cross the adb-shell hop (it is a direct adb
    # protocol command, not `adb shell ...`), so device-side sh never
    # sees it at all; guest-side quoting alone is the correct protection.
    assert "`id`; rm -rf /" not in cmd.replace(shlex.quote("`id`; rm -rf /"), "")


def test_stop_cmd_payload_is_inert_on_the_device() -> None:
    cmd = render_guest_command(stop_cmd("$(reboot)"))
    argv = _simulate_device_argv(cmd)
    assert "$(reboot)" in argv
    assert "reboot" not in [a for a in argv if a != "$(reboot)"]


# --- BUG B: resolve-activity guard accepts another package's component ---


def test_launch_resolve_guard_requires_component_to_match_the_package() -> None:
    """`cmd package resolve-activity` can return e.g.
    `android/com.android.internal.app.ResolverActivity` or
    `com.android.settings/.FallbackHome` when the requested package has no
    launcher activity -- both contain a `/` but belong to the WRONG
    package. The guard must also check the component's package prefix."""
    cmd = render_guest_command(launch_cmd("com.x", activity=None, url=None))
    assert '[ "${ACT%%/*}" = com.x ]' in cmd


# --- BUG C: pidof right after monkey races the async process start ---


def test_monkey_fallback_polls_pidof_instead_of_a_single_check() -> None:
    """`monkey` returns once the intent is dispatched, before the process
    necessarily exists; a single `pidof` check right after is a race that
    reports a false failure on a slow cold start."""
    cmd = render_guest_command(launch_cmd("com.example.app", activity=None, url=None))
    assert "while" in cmd
    assert "sleep" in cmd
    assert "pidof com.example.app >/dev/null 2>&1 && exit 0" in cmd


# --- BUG D: stop_cmd cannot fail ---


def test_stop_fails_for_a_package_that_does_not_exist() -> None:
    """`am force-stop` exits 0 unconditionally, even for a package that
    was never installed; stop must verify."""
    cmd = render_guest_command(stop_cmd("com.typo"))
    assert "pm path com.typo" in cmd
    assert "exit 1" in cmd


def test_stop_verifies_the_process_is_actually_gone() -> None:
    cmd = render_guest_command(stop_cmd("com.x"))
    assert "am force-stop com.x" in cmd
    assert "pidof com.x" in cmd
    assert "exit 0" in cmd


# --- logcat filtering: --package / --tag / --priority / --clear ---


def test_logcat_clear_uses_dash_c_and_ignores_other_flags() -> None:
    """`--clear` maps straight to `adb logcat -c`; any other flag values
    passed alongside it are irrelevant because the CLI layer rejects that
    combination before this builder is ever called (see app_commands.py)."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=None, clear=True))
    assert cmd == f"{guest_prefix()} logcat -c"


def test_logcat_tag_uses_dash_s_with_default_verbose_priority() -> None:
    cmd = render_guest_command(logcat_cmd(follow=False, lines=None, tag="ActivityManager"))
    assert "-s ActivityManager:V" in cmd


def test_logcat_tag_and_priority_combine_into_one_filterspec() -> None:
    cmd = render_guest_command(
        logcat_cmd(follow=False, lines=None, tag="ActivityManager", priority="e")
    )
    assert "-s ActivityManager:E" in cmd


def test_logcat_priority_alone_uses_the_wildcard_tag() -> None:
    """No `-s` (default-to-silent) filterspec flag without a `--tag` --
    only `adb -s <serial>` (device selection) should appear."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=None, priority="w"))
    assert "*:W" in cmd
    assert "logcat -s" not in cmd
    assert " -s *:W" not in cmd


def test_logcat_package_resolves_pid_via_pidof_on_the_device() -> None:
    """`adb logcat` cannot resolve a package name to a pid itself, and the
    CLI/guest side cannot know the device's pid table -- `pidof` must run
    ON THE DEVICE, inside the same quoted script `adb shell` hands to
    /system/bin/sh -c (the same double-hop hazard `launch_cmd` guards
    against), not as a separate guest-side computation."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=None, package="com.example.app"))
    assert "pidof com.example.app" in cmd
    assert '--pid="$PID"' in cmd
    assert cmd.count("shell ") == 1
    tail = cmd.split("shell ", 1)[1]
    assert tail.startswith("'"), "device script must be quoted as one arg"


def test_logcat_package_not_running_exits_with_a_clear_message() -> None:
    """A malformed `--pid=` (empty pid) must never reach `logcat`; the
    device-side script instead reports the app is not running and exits
    non-zero itself."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=None, package="com.example.app"))
    assert "is not running" in cmd
    assert "exit 1" in cmd


def test_logcat_package_dump_mode_keeps_dash_d_and_tails_matching_lines() -> None:
    """Dump mode keeps -d, but --lines must NOT become `-t N`: on Redroid 11
    `-t N` applies the pid filter to the last N RAW lines and returns
    nothing when the newest lines belong to another process. Verified live
    2026-09-03."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=50, package="com.x"))
    argv = _simulate_device_argv(cmd)
    assert "-d" in argv
    assert "-t" not in argv, "-t N filters the wrong side of the pipe"
    assert "tail" in argv
    assert "50" in argv


def test_logcat_package_follow_mode_omits_dash_d() -> None:
    cmd = render_guest_command(logcat_cmd(follow=True, lines=None, package="com.x"))
    argv = _simulate_device_argv(cmd)
    assert "-d" not in argv


def test_logcat_package_combines_with_tag_and_priority() -> None:
    cmd = render_guest_command(
        logcat_cmd(follow=False, lines=None, package="com.x", tag="MyTag", priority="i")
    )
    assert '--pid="$PID"' in cmd
    assert "-s MyTag:I" in cmd


def test_logcat_package_tag_metacharacters_are_quoted_against_device_injection() -> None:
    """LIVE BUG: `_logcat_package_cmd` extended its device-script argv
    with the (deliberately unquoted, for the plain protocol form)
    `_logcat_filterspec` tokens directly, then handed the result to a
    device shell via `adb_shell` -- an unquoted `;` in `--tag` (free
    text; only `--priority` is validated by the CLI layer) closed the
    `logcat` statement early and started a second one. `--tag 'A; id #'`
    executed `id` on the device. `test_logcat_package_combines_with_tag_
    and_priority` above cannot catch this: `shlex.quote("MyTag:I")` is a
    no-op, so it asserts the same substring whether or not the fix is
    applied. This one needs an actual shell metacharacter, and must
    verify quoting held, not merely that the value appears somewhere."""
    cmd = render_guest_command(
        logcat_cmd(follow=False, lines=None, package="com.x", tag="A; id #")
    )
    argv = _simulate_device_argv(cmd)
    # The filterspec must survive the device-side parse as ONE argument
    # attached to `-s` -- if the `;` were unquoted it would instead split
    # into separate tokens and `id` would appear as its own command.
    assert argv[argv.index("-s") + 1] == "A; id #:V"
    assert "id" not in argv, "the semicolon must not start a second device command"


def test_priority_filterspec_is_quoted_against_guest_globbing() -> None:
    """`*:W` is interpreted by the GUEST shell for the plain `adb logcat`
    form. Unquoted, a file matching `*:W` in the ssh working directory
    would glob-expand and silently rewrite the filter."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=None, priority="W"))
    assert "'*:W'" in cmd or '"*:W"' in cmd, f"unquoted glob in: {cmd}"


def test_tag_filterspec_is_quoted() -> None:
    cmd = render_guest_command(logcat_cmd(follow=False, lines=None, tag="My Tag", priority="E"))
    assert "'My Tag:E'" in cmd


def test_lines_with_a_filter_tails_matching_lines_not_raw_lines() -> None:
    """LIVE BUG 2026-09-03: `logcat -t N -s TAG:V` applies the filter to the
    last N RAW lines, so it returns nothing whenever the newest lines are
    not the tag's -- silently defeating the filter. Verified on Redroid 11:
    `-t 5 -s Zygote:V` gave only the header while the unfiltered buffer was
    full of Zygote lines. --lines must mean the last N MATCHING lines."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=5, tag="Zygote"))
    assert "-t 5" not in cmd, "-t N filters the wrong side"
    assert "tail -n 5" in cmd
    assert "-s" in cmd and "Zygote:V" in cmd


def test_lines_with_priority_filter_also_tails() -> None:
    cmd = render_guest_command(logcat_cmd(follow=False, lines=3, priority="E"))
    assert "-t 3" not in cmd
    assert "tail -n 3" in cmd


def test_lines_without_a_filter_still_uses_the_cheap_protocol_form() -> None:
    """No filter means no pipe is needed, so stay on the plain adb logcat
    form rather than paying for a device shell."""
    cmd = render_guest_command(logcat_cmd(follow=False, lines=5))
    assert "-t 5" in cmd
    assert "tail -n" not in cmd


def test_follow_never_tails() -> None:
    """A stream has no last N lines to seek to."""
    cmd = render_guest_command(logcat_cmd(follow=True, lines=None, tag="X"))
    assert "tail -n" not in cmd
    assert "-d" not in cmd


def test_input_kind_is_quoted_like_its_arguments() -> None:
    """`kind` reaches `input_cmd` as a free-form CLI argument (`playground
    app input <kind> ...`) and is NOT validated against a known-subcommand
    list, so unquoted it is an injection point: `input 'text; id #' x`
    would run `id` on the handset. Same bug class as the `_logcat_package_cmd`
    injection fixed earlier -- and this path now points at a real,
    possibly-rooted phone rather than a throwaway container."""
    cmd = render_guest_command(input_cmd("text; id #", ["hello"]))
    argv = _simulate_device_argv(cmd)
    assert argv == ["input", "text; id #", "hello"], (
        "the metacharacter-bearing kind must survive as ONE literal argument"
    )
    # Nothing on the device gets a second parse: no bare `;` or `#`
    # escapes the quoting on either hop.
    assert "; id #" not in cmd.replace(shlex.quote("input 'text; id #' hello"), "")


def test_input_kind_quoting_leaves_well_formed_kinds_byte_identical() -> None:
    """Quoting a legitimate kind is a no-op, so the rendered command for
    every real call is unchanged -- which is why this fix could not have
    moved a single characterization string."""
    assert "input text hello" in render_guest_command(input_cmd("text", ["hello"]))
    assert "input keyevent KEYCODE_HOME" in render_guest_command(
        input_cmd("key", ["KEYCODE_HOME"])
    )
