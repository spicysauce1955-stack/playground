"""Per-VM images, URL-imported custom images and package_upgrade (cloud-digitalocean).

Plan resolution, tfvars rendering, the tofu root wiring, the plan summary,
and the ForceNew-stability pins: ``image``, ``ssh_keys`` and ``user_data``
force Droplet replacement, so an existing all-Ubuntu lab re-applied with
this code must render exactly what it rendered before these features.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from playground.backend import cloud_digitalocean as do_backend
from playground.backend import dispatch
from playground.backend.cloud_digitalocean.plan import (
    DEFAULT_IMAGE,
    DEFAULT_IMAGE_DISTRIBUTION,
    CustomImage,
    build_do_plan,
    custom_image_key,
    normalise_image,
)
from playground.backend.cloud_digitalocean.tfvars import render_do_tfvars
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
TOFU_DIR = REPO_ROOT / "tofu" / "cloud_digitalocean"

ROCKY_URL = (
    "https://dl.rockylinux.org/vault/rocky/8.8/images/x86_64/"
    "Rocky-8-GenericCloud-Base-8.8-20230518.0.x86_64.qcow2"
)
# A throwaway ed25519 public key; FINGERPRINT is `ssh-keygen -l -E md5` of it.
PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIyWn2kj8/wYihzET8ROnv+dXUPvLQRWvEmo0QVEvvRm"
    " test@host"
)
FINGERPRINT = "d6:42:82:da:42:46:25:16:23:07:30:43:b5:a3:b0:5d"


@pytest.fixture
def resolved():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "cloud-smoke")


def _with_vms(resolved, overrides_by_vm):
    """Clone cloud-smoke's VM into one VM per entry (name -> provider_overrides)."""
    base = resolved.vms[0]
    vms = [
        base.model_copy(update={"name": name, "provider_overrides": ov})
        for name, ov in overrides_by_vm.items()
    ]
    return resolved.model_copy(update={"vms": vms})


def _do(**kw):
    return {"cloud-digitalocean": kw}


def _central_lab(resolved):
    """Five Ubuntu VMs plus `central` importing Rocky 8.8 with no upgrade."""
    ovs = {f"n{i}": {} for i in range(5)}
    ovs["central"] = _do(image_url=ROCKY_URL, package_upgrade=False)
    return _with_vms(resolved, ovs)


# ---------------------------------------------------------------------------
# normalise_image / custom_image_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("rockylinux-8-x64", "rockylinux-8-x64"),
        ("  rockylinux-8-x64 ", "rockylinux-8-x64"),
        (123456789, "123456789"),
        ("123456789", "123456789"),
        (" 42 ", "42"),
        (0, None),
        (-5, None),
        ("0", None),
        ("-3", None),
        ("", None),
        ("   ", None),
        (True, None),
        (False, None),
        (1.5, None),
        (None, None),
        (["x"], None),
    ],
)
def test_normalise_image(value, expected) -> None:
    assert normalise_image(value) == expected


def test_custom_image_key_is_stable_sha256_prefix() -> None:
    import hashlib

    key = custom_image_key(ROCKY_URL)
    assert key == "img-" + hashlib.sha256(ROCKY_URL.encode()).hexdigest()[:10]
    assert re.fullmatch(r"img-[0-9a-f]{10}", key)
    assert custom_image_key(ROCKY_URL) == key
    assert custom_image_key(ROCKY_URL + "?x") != key


# ---------------------------------------------------------------------------
# build_do_plan -- images
# ---------------------------------------------------------------------------


def test_default_lab_images_are_the_default_for_every_vm(resolved) -> None:
    plan = build_do_plan(_with_vms(resolved, {"a": {}, "b": {}}), provider_settings={})
    assert plan.image == DEFAULT_IMAGE
    assert plan.vm_images == {"a": DEFAULT_IMAGE, "b": DEFAULT_IMAGE}
    assert plan.custom_images == {} and plan.vm_custom_images == {}
    assert plan.vms_needing_do_ssh_key == []


def test_vm_image_override_beats_lab_image(resolved) -> None:
    lab = _with_vms(
        resolved,
        {
            "a": _do(image="rockylinux-8-x64"),
            "b": {},
            "c": _do(image=123456),
            "d": {"local-libvirt": {"image": "ignored"}},
        },
    )
    plan = build_do_plan(lab, provider_settings={"image": "ubuntu-22-04-x64"})
    assert plan.vm_images == {
        "a": "rockylinux-8-x64",
        "b": "ubuntu-22-04-x64",
        "c": "123456",
        "d": "ubuntu-22-04-x64",
    }
    assert plan.image == "ubuntu-22-04-x64"


def test_lab_level_integer_image_id_is_normalised(resolved) -> None:
    plan = build_do_plan(_with_vms(resolved, {"a": {}}), provider_settings={"image": 777})
    assert plan.image == "777" and plan.vm_images == {"a": "777"}


@pytest.mark.parametrize("bad", [0, -1, True, "", [1]])
def test_malformed_image_falls_back_quietly(resolved, bad) -> None:
    lab = _with_vms(resolved, {"a": _do(image=bad)})
    plan = build_do_plan(lab, provider_settings={"image": bad})
    assert plan.vm_images == {"a": DEFAULT_IMAGE}


def test_custom_image_from_url(resolved) -> None:
    plan = build_do_plan(_central_lab(resolved), provider_settings={})
    key = custom_image_key(ROCKY_URL)
    assert plan.custom_images == {
        key: CustomImage(url=ROCKY_URL, distribution=DEFAULT_IMAGE_DISTRIBUTION)
    }
    assert plan.vm_custom_images == {"central": key}
    assert plan.vm_images["central"] == key
    assert all(plan.vm_images[f"n{i}"] == DEFAULT_IMAGE for i in range(5))


def test_same_url_is_imported_once(resolved) -> None:
    lab = _with_vms(
        resolved,
        {
            "a": _do(image_url=ROCKY_URL, image_distribution="CentOS"),
            "b": _do(image_url=ROCKY_URL),
            "c": _do(image_url="https://example.com/other.qcow2"),
        },
    )
    plan = build_do_plan(lab, provider_settings={})
    key = custom_image_key(ROCKY_URL)
    assert len(plan.custom_images) == 2
    # First VM's distribution wins (the validator flags the mismatch).
    assert plan.custom_images[key].distribution == "CentOS"
    assert plan.vm_custom_images["a"] == plan.vm_custom_images["b"] == key


@pytest.mark.parametrize(
    "ov",
    [
        _do(image_url="http://insecure.example/x.qcow2"),
        _do(image_url="ftp://x/y"),
        _do(image_url=5),
        _do(image_url=ROCKY_URL, image="rockylinux-8-x64"),  # mutually exclusive
    ],
)
def test_invalid_custom_image_is_not_imported(resolved, ov) -> None:
    plan = build_do_plan(_with_vms(resolved, {"a": ov}), provider_settings={})
    assert plan.custom_images == {} and plan.vm_custom_images == {}


@pytest.mark.parametrize(
    "image,needs",
    [
        ("ubuntu-24-04-x64", False),
        ("ubuntu-22-04-x64", False),
        ("rockylinux-8-x64", True),
        ("debian-12-x64", True),
        ("123456", True),
    ],
)
def test_vms_needing_do_ssh_key(resolved, image, needs) -> None:
    plan = build_do_plan(
        _with_vms(resolved, {"a": _do(image=image), "b": {}}), provider_settings={},
    )
    assert plan.vms_needing_do_ssh_key == (["a"] if needs else [])


def test_custom_image_vm_needs_do_ssh_key(resolved) -> None:
    plan = build_do_plan(_central_lab(resolved), provider_settings={})
    assert plan.vms_needing_do_ssh_key == ["central"]


def test_lab_wide_non_ubuntu_image_makes_every_vm_need_a_key(resolved) -> None:
    plan = build_do_plan(
        _with_vms(resolved, {"a": {}, "b": {}}),
        provider_settings={"image": "rockylinux-8-x64"},
    )
    assert plan.vms_needing_do_ssh_key == ["a", "b"]


# ---------------------------------------------------------------------------
# build_do_plan -- package_upgrade
# ---------------------------------------------------------------------------


def test_package_upgrade_defaults_true_for_every_vm(resolved) -> None:
    plan = build_do_plan(_with_vms(resolved, {"a": {}, "b": {}}), provider_settings={})
    assert plan.package_upgrade is True
    assert plan.vm_package_upgrade == {"a": True, "b": True}


def test_package_upgrade_precedence(resolved) -> None:
    lab = _with_vms(
        resolved,
        {"a": {}, "b": _do(package_upgrade=True), "c": _do(package_upgrade="no")},
    )
    plan = build_do_plan(lab, provider_settings={"package_upgrade": False})
    assert plan.package_upgrade is False
    # a: lab-wide; b: per-VM bool wins; c: malformed -> lab-wide.
    assert plan.vm_package_upgrade == {"a": False, "b": True, "c": False}


def test_malformed_lab_package_upgrade_falls_back_to_true(resolved) -> None:
    plan = build_do_plan(
        _with_vms(resolved, {"a": {}}), provider_settings={"package_upgrade": "false"},
    )
    assert plan.vm_package_upgrade == {"a": True}


# ---------------------------------------------------------------------------
# render_do_tfvars
# ---------------------------------------------------------------------------


def test_tfvars_for_the_central_lab(resolved) -> None:
    plan = build_do_plan(_central_lab(resolved), provider_settings={})
    out = render_do_tfvars(plan, ssh_public_key=PUBKEY)
    key = custom_image_key(ROCKY_URL)
    assert out["vm_images"] == {f"n{i}": DEFAULT_IMAGE for i in range(5)}
    assert out["custom_images"] == {
        key: {"url": ROCKY_URL, "distribution": "Unknown"}
    }
    assert out["vm_custom_images"] == {"central": key}
    assert out["vm_ssh_keys"] == {"central": [FINGERPRINT]}
    assert out["vm_package_upgrade"] == {
        **{f"n{i}": True for i in range(5)}, "central": False,
    }
    assert out["image"] == DEFAULT_IMAGE


def test_vm_ssh_keys_skips_a_fingerprint_already_listed(resolved) -> None:
    lab = _with_vms(resolved, {"a": _do(image="rockylinux-8-x64")})
    for listed in (FINGERPRINT, FINGERPRINT.upper()):
        plan = build_do_plan(lab, provider_settings={"ssh_key_fingerprints": [listed]})
        out = render_do_tfvars(plan, ssh_public_key=PUBKEY)
        assert out["vm_ssh_keys"] == {}
        assert out["ssh_key_fingerprints"] == [listed]


def test_vm_ssh_keys_added_next_to_other_listed_fingerprints(resolved) -> None:
    lab = _with_vms(resolved, {"a": _do(image="rockylinux-8-x64"), "b": {}})
    plan = build_do_plan(lab, provider_settings={"ssh_key_fingerprints": ["aa:bb"]})
    out = render_do_tfvars(plan, ssh_public_key=PUBKEY)
    assert out["vm_ssh_keys"] == {"a": [FINGERPRINT]}


def test_vm_ssh_keys_empty_for_an_unparseable_key(resolved) -> None:
    lab = _with_vms(resolved, {"a": _do(image="rockylinux-8-x64")})
    plan = build_do_plan(lab, provider_settings={})
    assert render_do_tfvars(plan, ssh_public_key="ssh-rsa AAAA x")["vm_ssh_keys"] == {}


# ---------------------------------------------------------------------------
# ForceNew stability pins (an existing all-Ubuntu lab must not be replaced)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [PUBKEY, "ssh-rsa AAAA fakekey", ""])
def test_default_lab_renders_no_forcenew_inputs(resolved, key) -> None:
    lab = _with_vms(resolved, {"node1": {}, "node2": _do(size="s-2vcpu-4gb")})
    plan = build_do_plan(lab, provider_settings={"ssh_key_fingerprints": ["x"]})
    out = render_do_tfvars(plan, ssh_public_key=key)
    assert out["image"] == DEFAULT_IMAGE
    assert set(out["vm_images"].values()) == {DEFAULT_IMAGE}
    assert out["custom_images"] == {} and out["vm_custom_images"] == {}
    assert out["vm_ssh_keys"] == {}
    assert out["ssh_key_fingerprints"] == ["x"]
    assert set(out["vm_package_upgrade"].values()) == {True}


def test_committed_cloud_labs_render_no_forcenew_inputs() -> None:
    loaded, _ = load_config(CONFIG_DIR)
    labs = [n for n, lab in loaded.labs.items() if lab.spec.backend == "cloud-digitalocean"]
    assert labs
    for name in labs:
        res = resolve_lab(loaded, name)
        plan = build_do_plan(
            res, provider_settings=do_backend.merge_provider_settings(res, loaded=loaded),
        )
        out = render_do_tfvars(plan, ssh_public_key=PUBKEY)
        assert out["vm_ssh_keys"] == {}, name
        assert out["custom_images"] == {} and out["vm_custom_images"] == {}, name
        assert set(out["vm_images"].values()) == {out["image"]}, name
        assert set(out["vm_package_upgrade"].values()) == {True}, name


def _main_tf() -> str:
    return (TOFU_DIR / "main.tf").read_text(encoding="utf-8")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_droplet_image_expression() -> None:
    main = _squash(_main_tf())
    assert (
        "image = ( contains(keys(var.vm_custom_images), var.vm_names[count.index]) "
        "? tostring(digitalocean_custom_image.lab[var.vm_custom_images"
        "[var.vm_names[count.index]]].image_id) "
        ": lookup(var.vm_images, var.vm_names[count.index], var.image) )"
    ) in main


def test_droplet_ssh_keys_expression_has_no_distinct() -> None:
    main = _squash(_main_tf())
    assert (
        "ssh_keys = concat( var.ssh_key_fingerprints, "
        "lookup(var.vm_ssh_keys, var.vm_names[count.index], []), )"
    ) in main
    assert "distinct(" not in main


def test_custom_image_ignores_distribution_drift() -> None:
    """DO stores "Unknown" as "Unknown OS" (seen live); without this every
    apply plans an image update and `isolate` (firewall-only plans) refuses."""
    main = _squash(_main_tf())
    start = main.index('resource "digitalocean_custom_image" "lab"')
    block = main[start:main.index('resource "digitalocean_droplet"')]
    assert "lifecycle { ignore_changes = [distribution] }" in block


def test_template_vars_include_package_upgrade_defaulting_true() -> None:
    main = _squash(_main_tf())
    assert (
        "package_upgrade = lookup(var.vm_package_upgrade, "
        "var.vm_names[count.index], true)"
    ) in main


# The committed cloud_init.cfg as it was before the package_upgrade knob. The
# rendered user_data must stay byte-identical for package_upgrade = true, or
# re-applying every existing lab replaces every Droplet.
_PRE_KNOB_TEMPLATE = """\
#cloud-config
# IMPORTANT: keep this file ASCII-only. DigitalOcean delivers user-data via
# the ConfigDrive datasource, whose YAML loader rejects non-ASCII bytes and
# then discards the ENTIRE cloud-config ("empty cloud config") -- which
# silently skips the users block below, so the `ubuntu` user is never created
# and SSH key auth fails. A stray em-dash in a comment was enough to break it.
#
# Sets hostname/FQDN locally so `hostname`/`hostnamectl` return the lab name
# and DHCP advertises it.
hostname: ${vm_name}
fqdn: ${vm_name}.${dns_domain}
preserve_hostname: false

# Default cloud-init user creation and SSH key injection.
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: users, admin
    lock_passwd: true
    ssh_authorized_keys:
      - ${ssh_public_key}

# Update packages non-interactively.
package_update: true
package_upgrade: true
package_reboot_if_required: false

# Ensure ssh starts; no password auth.
ssh_pwauth: false
"""


def _render(template: str, values: dict[str, object]) -> str:
    """Faithful ``templatefile`` for a template with only ``${name}`` interpolations."""
    assert "%{" not in template and "$${" not in template

    def sub(m: re.Match[str]) -> str:
        value = values[m.group(1)]
        if isinstance(value, bool):
            return "true" if value else "false"  # HCL bool -> string
        return str(value)

    return re.sub(r"\$\{(\w+)\}", sub, template)


def test_cloud_init_renders_byte_identical_for_the_default() -> None:
    current = (TOFU_DIR / "cloud_init.cfg").read_text(encoding="utf-8")
    base = {"vm_name": "node1", "dns_domain": "cloud-smoke.lab", "ssh_public_key": PUBKEY}
    assert _render(current, {**base, "package_upgrade": True}) == _render(
        _PRE_KNOB_TEMPLATE, base
    )


def test_cloud_init_renders_package_upgrade_false() -> None:
    current = (TOFU_DIR / "cloud_init.cfg").read_text(encoding="utf-8")
    out = _render(
        current,
        {"vm_name": "c", "dns_domain": "d", "ssh_public_key": "k", "package_upgrade": False},
    )
    assert "\npackage_upgrade: false\n" in out
    assert "package_upgrade: true" not in out


# ---------------------------------------------------------------------------
# tofu root: the custom image resource
# ---------------------------------------------------------------------------


def _custom_image_block() -> str:
    main = _main_tf()
    start = main.index('resource "digitalocean_custom_image" "lab"')
    return main[start : main.index('resource "digitalocean_droplet"', start)]


def test_custom_image_resource_shape() -> None:
    block = _squash(_custom_image_block())
    for fragment in (
        "for_each = var.custom_images",
        'name = "${var.name_prefix}-${each.key}"',
        "url = each.value.url",
        "regions = [var.region]",
        "distribution = each.value.distribution",
        'description = "playground lab ${var.name_prefix}"',
        "tags = var.tags",
        'timeouts { create = "60m" }',
    ):
        assert fragment in block, fragment


def test_custom_image_tags_comment_says_they_select_nothing() -> None:
    assert "they select nothing" in _custom_image_block()


def test_firewall_still_has_no_tags() -> None:
    main = _main_tf()
    fw = main[main.index('resource "digitalocean_firewall" "lab"') :]
    assert not re.search(r"^\s*tags\s*=", fw, re.MULTILINE)


# ---------------------------------------------------------------------------
# plan summary (dispatch.plan_provider_summary)
# ---------------------------------------------------------------------------


def _fake_merge(resolved, *, config_dir=None, loaded=None):
    return {"size": "s-1vcpu-2gb"}


def test_summary_for_an_all_ubuntu_lab_is_unchanged(monkeypatch, resolved) -> None:
    monkeypatch.setattr(do_backend, "merge_provider_settings", _fake_merge)
    summary = dispatch.plan_provider_summary(_with_vms(resolved, {"a": {}, "b": {}}))
    assert summary is not None
    assert list(summary) == ["region", "size", "image", "ssh_exposure"]
    assert summary["image"] == DEFAULT_IMAGE


def test_summary_counts_mixed_images(monkeypatch, resolved) -> None:
    monkeypatch.setattr(do_backend, "merge_provider_settings", _fake_merge)
    ovs = {f"n{i}": {} for i in range(5)}
    ovs["r"] = _do(image="rockylinux-8-x64")
    summary = dispatch.plan_provider_summary(_with_vms(resolved, ovs))
    assert summary is not None
    assert summary["image"] == "ubuntu-24-04-x64 x5, rockylinux-8-x64 x1"
    assert "r: custom/non-Ubuntu image needs a DigitalOcean-registered SSH key" in (
        summary["ssh_key"]
    )
    assert "custom_images" not in summary and "package_upgrade" not in summary


def test_summary_for_the_central_lab(monkeypatch, resolved) -> None:
    monkeypatch.setattr(do_backend, "merge_provider_settings", _fake_merge)
    summary = dispatch.plan_provider_summary(_central_lab(resolved))
    assert summary is not None
    key = custom_image_key(ROCKY_URL)
    assert summary["image"] == f"ubuntu-24-04-x64 x5, {key} x1"
    note = summary["custom_images"]
    assert note.startswith(f"{key} <- {ROCKY_URL} (distribution Unknown)")
    assert "can take 15+ minutes" in note
    assert "removed on destroy/suspend" in note
    assert "numeric ID via `image`" in note
    assert summary["ssh_key"].startswith("central:")
    assert "destroy keeps it" in summary["ssh_key"]
    assert summary["package_upgrade"].startswith("off for central")


# ---------------------------------------------------------------------------
# plan VM lines (dispatch.plan_vm_images -> render_plan)
# ---------------------------------------------------------------------------


def test_plan_vm_lines_show_the_droplet_image_not_the_role_image(
    monkeypatch, resolved,
) -> None:
    """The VmRole image (`ubuntu-noble`, a libvirt artifact) is never what a
    Droplet boots; the plan's VM lines show the per-VM DO image instead."""
    from playground.planner import render_plan

    monkeypatch.setattr(do_backend, "merge_provider_settings", _fake_merge)
    lab = _central_lab(resolved)
    images = dispatch.plan_vm_images(lab)
    assert images is not None
    assert images["central"] == custom_image_key(ROCKY_URL)
    assert images["n0"] == DEFAULT_IMAGE

    plan = render_plan(lab, vm_images=images)
    by_name = {a.name: a for a in plan.actions if a.resource_type == "vm"}
    assert by_name["central"].details["image"] == custom_image_key(ROCKY_URL)
    assert f"on {DEFAULT_IMAGE} (" in by_name["n0"].summary


def test_plan_vm_images_is_none_for_local_backends(resolved) -> None:
    local = resolved.model_copy(update={"backend": "local-libvirt"})
    assert dispatch.plan_vm_images(local) is None
