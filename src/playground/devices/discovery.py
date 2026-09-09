"""Whether an unknown attached handset raises a question or is filed silently.

The configured mode is `spec.device_discovery` -- deliberately NOT
`spec.devices.discovery`, because `LabSpec.devices` is a list and a
same-named mapping on the `Defaults` kind would make two config kinds
disagree about one key's shape. Do not "tidy" this back to a nested key.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from playground.devices.adb import AttachedDevice
from playground.devices.registry import mark_ignored, read_registry
from playground.models.kinds import DiscoveryMode

__all__ = ["DiscoveryMode", "effective_mode", "offer_unknown"]


def effective_mode(
    configured: DiscoveryMode, *, is_tty: bool, no_prompt: bool, yes: bool
) -> DiscoveryMode:
    """Resolve the configured mode against this invocation's reality.

    A non-TTY session can never prompt, whatever the config says: JSON
    output, CI and the TUI must not block on stdin. `--yes` and
    `--no-prompt` also force silent.
    """
    if not is_tty or no_prompt or yes:
        return "silent"
    return configured


def offer_unknown(
    state_dir: Path,
    attached: list[AttachedDevice],
    *,
    mode: DiscoveryMode,
    ask: Callable[[AttachedDevice], str | None],
) -> dict[str, str]:
    """Offer to save a profile for each attached device we do not know.

    Returns serial -> chosen profile name for the ones the operator named.
    A device already marked `ignored` in the registry is never re-offered.
    A device the operator declines (``ask`` returns a falsy value) is
    marked ignored so the same question is not asked on every subsequent
    adb poll. ``ask`` is injected so this is testable without a terminal;
    the CLI passes a `typer.prompt` wrapper -- this module never calls
    `input()` or `typer.prompt` itself.
    """
    if mode == "silent":
        return {}
    known = read_registry(state_dir)
    chosen: dict[str, str] = {}
    for device in attached:
        entry = known.get(device.serial)
        if entry is not None and entry.ignored:
            continue
        name = ask(device)
        if name:
            chosen[device.serial] = name
        else:
            mark_ignored(state_dir, device.serial)
    return chosen
