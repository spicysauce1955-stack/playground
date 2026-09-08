"""`check_adb_available` and `check_attached_devices` -- doctor's physical
Android handset checks.

`check_attached_devices` makes exactly ONE `adb devices -l` read
(`_raw_adb_devices_l`) and parses it twice (once via
`playground.devices.adb.parse_devices_l` for the `unauthorized` check,
once via its own scan for the two states that parser drops) -- every
test here monkeypatches `shutil.which` and/or `_raw_adb_devices_l`, per
the repo-wide constraint that no test may invoke a real `adb`.
"""

from __future__ import annotations

from playground.preflight.doctor import check_adb_available, check_attached_devices


def test_missing_adb_is_a_warning_not_a_hard_error(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: None)
    result = check_adb_available()
    assert result.severity == "warning"
    assert "android-platform-tools" in result.suggestion


def test_present_adb_passes(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    assert check_adb_available() is None or check_adb_available().severity != "error"


def test_check_adb_available_returns_none_when_present(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    assert check_adb_available() is None


def test_check_attached_devices_skipped_when_adb_missing(monkeypatch):
    """No adb on PATH -> nothing to probe; must not attempt a subprocess call."""
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "playground.preflight.doctor._raw_adb_devices_l",
        lambda: (_ for _ in ()).throw(AssertionError("must not read adb devices -l")),
    )
    assert check_attached_devices() == []


def test_check_attached_devices_reads_the_raw_listing_exactly_once(monkeypatch):
    """Regression guard for Minor 3: the unauthorized check and the
    dropped-state scan must share one `_raw_adb_devices_l()` call, not
    each invoke `adb` separately."""
    calls: list[int] = []

    def _raw() -> str:
        calls.append(1)
        return "List of devices attached\nS1\tunauthorized\n"

    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    monkeypatch.setattr("playground.preflight.doctor._raw_adb_devices_l", _raw)

    diagnostics = check_attached_devices()

    assert len(calls) == 1
    assert [d.id for d in diagnostics] == ["preflight.adb.unauthorized"]


def test_check_attached_devices_silent_when_all_connected(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    monkeypatch.setattr(
        "playground.preflight.doctor._raw_adb_devices_l",
        lambda: "List of devices attached\nS1\tdevice model:Pixel_7\n",
    )
    assert check_attached_devices() == []


def test_check_attached_devices_flags_unauthorized_with_the_rsa_remedy(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    monkeypatch.setattr(
        "playground.preflight.doctor._raw_adb_devices_l",
        lambda: "List of devices attached\nS1\tunauthorized\n",
    )

    diagnostics = check_attached_devices()

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "preflight.adb.unauthorized"
    assert diagnostics[0].severity == "warning"
    assert "S1" in diagnostics[0].message
    assert "Allow" in diagnostics[0].suggestion


def test_check_attached_devices_flags_no_permissions_with_the_udev_remedy(monkeypatch):
    """`no permissions` is silently DROPPED by `parse_devices_l`
    (see its docstring) -- without this, a udev misconfiguration
    presents as "no devices attached", the least informative failure
    the operator could see. `doctor` reads the raw listing directly to
    catch it."""
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    monkeypatch.setattr(
        "playground.preflight.doctor._raw_adb_devices_l",
        lambda: (
            "List of devices attached\n"
            "0123456789ABCDEF\tno permissions (user in plugdev group; are "
            "your udev rules wrong?)\n"
        ),
    )

    diagnostics = check_attached_devices()

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "preflight.adb.no_permissions"
    assert diagnostics[0].severity == "warning"
    assert "0123456789ABCDEF" in diagnostics[0].message
    assert "udev" in diagnostics[0].suggestion


def test_check_attached_devices_flags_authorizing_as_a_pairing_in_progress(monkeypatch):
    """`authorizing` is a transient, distinct-from-`unauthorized` state
    (a pairing mid-flight) that `parse_devices_l` also drops silently."""
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    monkeypatch.setattr(
        "playground.preflight.doctor._raw_adb_devices_l",
        lambda: "List of devices attached\nS2\tauthorizing\n",
    )

    diagnostics = check_attached_devices()

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "preflight.adb.authorizing"
    assert "S2" in diagnostics[0].message
    assert "progress" in diagnostics[0].message


def test_check_attached_devices_reports_a_warning_when_the_read_fails(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    monkeypatch.setattr("playground.preflight.doctor._raw_adb_devices_l", lambda: None)

    diagnostics = check_attached_devices()

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "preflight.adb.unavailable"
    assert diagnostics[0].severity == "warning"


def test_check_attached_devices_never_calls_a_real_adb(monkeypatch):
    """Regression guard for the repo-wide "no test invokes real adb"
    constraint: `subprocess.run` must not be reached when the raw-listing
    helper itself is monkeypatched away."""
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")

    def _boom(*_a, **_kw):
        raise AssertionError("must not shell out to a real adb")

    monkeypatch.setattr("playground.preflight.doctor.subprocess.run", _boom)
    monkeypatch.setattr("playground.preflight.doctor._raw_adb_devices_l", lambda: None)

    assert len(check_attached_devices()) == 1
