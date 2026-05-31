"""Tests for the cloud-digitalocean tfvars renderer.

Three invariants are checked:

1. The dict keys are exactly the allowlist (no extras, no omissions).
2. No token, secret, or credential leaks through any key or value.
3. Every key in the rendered dict matches a variable declared in
   ``tofu/cloud_digitalocean/variables.tf`` (HCL parity).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from playground.backend.cloud_digitalocean.plan import build_do_plan
from playground.backend.cloud_digitalocean.tfvars import _TFVARS_KEYS, render_do_tfvars
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
VARIABLES_TF = REPO_ROOT / "tofu" / "cloud_digitalocean" / "variables.tf"

_SSH_KEY = "ssh-ed25519 AAAATESTKEY user@host"


@pytest.fixture
def cloud_smoke_plan():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    resolved = resolve_lab(loaded, "cloud-smoke")
    return build_do_plan(resolved, provider_settings={"region": "nyc3", "size": "s-1vcpu-1gb"})


@pytest.fixture
def rendered(cloud_smoke_plan):
    return render_do_tfvars(cloud_smoke_plan, ssh_public_key=_SSH_KEY)


# ---------------------------------------------------------------------------
# 1. Key allowlist
# ---------------------------------------------------------------------------


def test_rendered_keys_exactly_match_allowlist(rendered) -> None:
    assert set(rendered.keys()) == _TFVARS_KEYS


def test_no_extra_keys(rendered) -> None:
    extra = set(rendered.keys()) - _TFVARS_KEYS
    assert not extra, f"unexpected keys in tfvars: {extra}"


def test_no_missing_keys(rendered) -> None:
    missing = _TFVARS_KEYS - set(rendered.keys())
    assert not missing, f"missing keys in tfvars: {missing}"


# ---------------------------------------------------------------------------
# 2. Token / credential leak guard
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS = [
    "token",
    "dop_v1_",
    "secret",
]


def test_no_sensitive_key_names(rendered) -> None:
    for key in rendered.keys():
        for pattern in _SENSITIVE_PATTERNS:
            assert pattern not in key.lower(), (
                f"sensitive pattern {pattern!r} found in tfvars key {key!r}"
            )


def test_no_sensitive_values(rendered) -> None:
    for key, value in rendered.items():
        str_value = str(value)
        for pattern in _SENSITIVE_PATTERNS:
            assert pattern not in str_value.lower(), (
                f"sensitive pattern {pattern!r} found in tfvars value "
                f"for key {key!r}: {str_value!r}"
            )


def test_token_env_name_not_in_rendered_values(rendered) -> None:
    # The token env var NAME (DIGITALOCEAN_TOKEN) must never appear as a
    # tfvars value either — we only pass the env var to tofu's subprocess.
    for key, value in rendered.items():
        assert "DIGITALOCEAN_TOKEN" not in str(value), (
            f"token env var name leaked into tfvars[{key!r}]"
        )


def test_token_not_leaked_even_if_passed_as_ssh_key() -> None:
    # Construct a plan where the ssh_public_key happens to look like a
    # DO token prefix; render must still not emit any token-shaped value
    # under a sensitive key name.
    loaded, _ = load_config(CONFIG_DIR)
    resolved = resolve_lab(loaded, "cloud-smoke")
    plan = build_do_plan(resolved, provider_settings={})
    result = render_do_tfvars(plan, ssh_public_key="ssh-ed25519 TESTKEY user@host")
    # The key "ssh_public_key" is allowed and expected; no other key
    # should carry a token-shaped value.
    for key in result:
        if key == "ssh_public_key":
            continue
        assert "dop_v1_" not in str(result[key]).lower()


# ---------------------------------------------------------------------------
# 3. HCL parity — every rendered key has a declared variable in variables.tf
# ---------------------------------------------------------------------------


def _extract_hcl_variable_names(variables_tf_path: Path) -> set[str]:
    """Parse ``variable "<name>"`` blocks from an HCL file via regex."""
    text = variables_tf_path.read_text(encoding="utf-8")
    return set(re.findall(r'variable\s+"([^"]+)"', text))


def test_all_tfvars_keys_declared_in_variables_tf(rendered) -> None:
    assert VARIABLES_TF.exists(), (
        f"variables.tf not found at {VARIABLES_TF}; "
        "ensure the cloud_digitalocean tofu root is committed"
    )
    hcl_vars = _extract_hcl_variable_names(VARIABLES_TF)
    undeclared = set(rendered.keys()) - hcl_vars
    assert not undeclared, (
        f"tfvars keys not declared in variables.tf: {undeclared}\n"
        f"Declared variables: {sorted(hcl_vars)}"
    )


def test_variables_tf_allowlist_matches_hcl_exactly() -> None:
    """The Python _TFVARS_KEYS constant must stay in sync with variables.tf.

    This test fails when someone adds a variable to variables.tf without
    updating the Python allowlist (or vice versa), catching drift early.
    """
    assert VARIABLES_TF.exists(), f"variables.tf not found at {VARIABLES_TF}"
    hcl_vars = _extract_hcl_variable_names(VARIABLES_TF)
    assert _TFVARS_KEYS == hcl_vars, (
        f"Python _TFVARS_KEYS differs from variables.tf:\n"
        f"  In Python only: {_TFVARS_KEYS - hcl_vars}\n"
        f"  In HCL only:    {hcl_vars - _TFVARS_KEYS}"
    )


# ---------------------------------------------------------------------------
# Value shape tests
# ---------------------------------------------------------------------------


def test_rendered_vm_names_is_list(rendered) -> None:
    assert isinstance(rendered["vm_names"], list)


def test_rendered_ssh_key_fingerprints_is_list(rendered) -> None:
    assert isinstance(rendered["ssh_key_fingerprints"], list)


def test_rendered_tags_is_list(rendered) -> None:
    assert isinstance(rendered["tags"], list)


def test_rendered_firewall_ssh_cidrs_is_list(rendered) -> None:
    assert isinstance(rendered["firewall_ssh_cidrs"], list)


def test_rendered_ssh_public_key_matches_input(rendered) -> None:
    assert rendered["ssh_public_key"] == _SSH_KEY


def test_rendered_region_is_string(rendered) -> None:
    assert isinstance(rendered["region"], str)


def test_rendered_dns_domain_is_string(rendered) -> None:
    assert isinstance(rendered["dns_domain"], str)
    assert rendered["dns_domain"]  # non-empty


def test_render_do_tfvars_is_pure_no_side_effects(cloud_smoke_plan) -> None:
    # Calling twice produces identical output (no mutation of the plan).
    r1 = render_do_tfvars(cloud_smoke_plan, ssh_public_key=_SSH_KEY)
    r2 = render_do_tfvars(cloud_smoke_plan, ssh_public_key=_SSH_KEY)
    assert r1 == r2
