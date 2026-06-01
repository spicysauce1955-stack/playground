"""Verify that the cpu_features_disable xslt template renders to
well-formed XSLT that inserts the expected `<feature>` elements.

This is the on-disk template OpenTofu's ``templatefile()`` consumes
when ``var.cpu_features_disable`` is non-empty. If the template ever
produces malformed XML, ``tofu apply`` would surface it as a confusing
xslt error against the libvirt domain XML — we want to catch that here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = REPO_ROOT / "tofu" / "cpu_features_disable.xslt.tftpl"


def _render(features: list[str]) -> str:
    """Emulate OpenTofu's ``templatefile()`` for the narrow subset this
    template uses: a single ``%{ for X in Y ~} ... %{ endfor ~}`` loop
    and ``${X}`` substitutions inside it.

    Kept intentionally tiny — if we ever extend the template beyond
    this shape, switch to actual ``tofu console``."""
    text = TEMPLATE.read_text()
    pattern = re.compile(
        r"%\{\s*for\s+(\w+)\s+in\s+(\w+)\s*~\}(.*?)%\{\s*endfor\s*~\}",
        re.DOTALL,
    )
    match = pattern.search(text)
    assert match is not None, "template no longer uses %{ for ... ~} loop"
    var, src, body = match.groups()
    assert src == "features", f"template loops over {src!r}, expected 'features'"
    expanded = "".join(body.replace(f"${{{var}}}", f) for f in features)
    return text[: match.start()] + expanded + text[match.end():]


def test_template_file_exists() -> None:
    assert TEMPLATE.exists(), TEMPLATE


def test_template_renders_empty_when_no_features() -> None:
    rendered = _render([])
    # No <feature policy="disable" ...> elements injected when the
    # list is empty — the identity transform passes the document
    # through unchanged. (The template's XML comment contains the
    # string "<feature ..." as documentation, so match the actual
    # emitted-element shape rather than a substring.)
    assert 'policy="disable"' not in rendered


def test_template_renders_one_feature_disable() -> None:
    rendered = _render(["vmx"])
    assert '<feature policy="disable" name="vmx"/>' in rendered


def test_template_renders_multiple_feature_disables() -> None:
    rendered = _render(["vmx", "svm", "hypervisor"])
    for flag in ("vmx", "svm", "hypervisor"):
        assert f'<feature policy="disable" name="{flag}"/>' in rendered


@pytest.mark.skipif(
    shutil.which("xmllint") is None,
    reason="xmllint not installed; skipping XSLT well-formedness check",
)
def test_rendered_template_is_well_formed_xml() -> None:
    """A live check: the rendered output must be well-formed XML so
    ``tofu apply``'s xslt step can parse it. Catches `&` / `<` / quoting
    regressions if the template ever stops escaping correctly."""
    rendered = _render(["vmx"])
    result = subprocess.run(  # noqa: S603 — explicit args, no shell
        ["xmllint", "--noout", "-"],
        input=rendered, text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, (
        f"xmllint rejected the rendered template:\n"
        f"--- stderr ---\n{result.stderr}\n"
        f"--- rendered ---\n{rendered}"
    )


@pytest.mark.skipif(
    shutil.which("tofu") is None,
    reason="tofu not installed; skipping templatefile() integration check",
)
def test_tofu_templatefile_accepts_template(tmp_path: Path) -> None:
    """Regression guard for Bug A (2026-05-28): on the first live apply
    with `cpu_features_disable: [vmx]`, ``templatefile()`` rejected the
    file because the XML comment contained literal ``${...}`` /
    ``%{...}`` patterns which tofu's HCL template parser interpreted as
    code (it doesn't care that they sit inside an XML comment).

    The Python emulator in :func:`_render` doesn't exercise the HCL
    parser, so it gave false confidence. This test calls the real
    ``tofu console`` to evaluate ``templatefile(...)`` against the
    on-disk template and asserts the call succeeds. Skipped when tofu
    isn't on PATH; runs in <1 s when it is.

    Runs in an isolated temp dir holding only a copy of the template, NOT
    in ``tofu/``. ``tofu console`` in ``tofu/`` initializes the libvirt
    provider and acquires the state lock, so the check used to fail
    whenever a concurrent ``tofu apply`` held that lock. An empty dir
    needs no provider init and no state, so the render is hermetic."""
    shutil.copy(TEMPLATE, tmp_path / TEMPLATE.name)
    expr = (
        f'templatefile("{TEMPLATE.name}", '
        '{ features = ["vmx", "svm", "hypervisor"] })\n'
    )
    result = subprocess.run(  # noqa: S603 — explicit args, no shell
        ["tofu", "console"], input=expr, text=True,
        capture_output=True, check=False, cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 0, (
        "tofu console rejected templatefile() against "
        f"{TEMPLATE.name}:\n--- stderr ---\n{result.stderr}\n"
        f"--- stdout ---\n{result.stdout}"
    )
    # The rendered XSLT must include the disable elements we asked for.
    for flag in ("vmx", "svm", "hypervisor"):
        assert f'name="{flag}"' in result.stdout, (
            f"expected feature {flag!r} missing from rendered output"
        )
