"""Structural guards for OS-family handling in the Ansible roles.

Ubuntu is the original target; the RedHat family (Rocky/Alma 8 and 9)
is supported for the `common` and `docker` roles. The apt-only roles
must fail fast with a clear message instead of dying on a missing apt
module. These break quietly (they only bite on a non-Ubuntu image), so
they are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROLES = Path(__file__).resolve().parents[3] / "ansible" / "roles"
_yaml = YAML(typ="safe")


def _load(role: str, name: str) -> list[dict]:
    return _yaml.load((ROLES / role / "tasks" / name).read_text())


@pytest.mark.parametrize("role", ["redroid", "capture", "lab_scheduler"])
def test_apt_only_roles_assert_debian_family_first(role: str) -> None:
    first = _load(role, "main.yml")[0]
    assert "ansible.builtin.assert" in first, f"{role}: first task must assert"
    body = first["ansible.builtin.assert"]
    assert "ansible_os_family == 'Debian'" in body["that"]
    msg = body["fail_msg"]
    for needle in (role, "inventory_hostname", "ansible_distribution", "VmRole"):
        assert needle in msg, f"{role}: fail_msg must mention {needle}"


@pytest.mark.parametrize("role", ["common", "docker"])
def test_dual_family_roles_have_per_family_files(role: str) -> None:
    for name in ("debian.yml", "redhat.yml"):
        assert (ROLES / role / "tasks" / name).is_file()
    text = (ROLES / role / "tasks" / "main.yml").read_text()
    assert "ansible_os_family not in ['Debian', 'RedHat']" in text


def test_debian_paths_use_apt_and_redhat_paths_do_not() -> None:
    for role in ("common", "docker"):
        assert "ansible.builtin.apt" in (ROLES / role / "tasks" / "debian.yml").read_text()
        redhat = (ROLES / role / "tasks" / "redhat.yml").read_text()
        assert "ansible.builtin.apt" not in redhat
        assert "ansible.builtin.dnf" in redhat


def test_redhat_common_uses_chronyd_not_timesyncd() -> None:
    text = (ROLES / "common" / "tasks" / "redhat.yml").read_text()
    assert "chronyd" in text
    assert "timesyncd" not in text.replace("systemd-timesyncd;", "")  # comment only


def test_redhat_docker_starts_service_and_builds_python_venv() -> None:
    tasks = _load("docker", "redhat.yml")
    systemd = [t for t in tasks if "ansible.builtin.systemd" in t]
    assert systemd and systemd[0]["ansible.builtin.systemd"]["state"] == "started"
    text = (ROLES / "docker" / "tasks" / "redhat.yml").read_text()
    assert "ansible_python_interpreter" in text
    assert "docker-ce-stable" in text


def test_redhat_removes_docker_documented_conflicts() -> None:
    text = (ROLES / "docker" / "tasks" / "redhat.yml").read_text()
    for pkg in ("podman", "runc", "docker-engine", "docker-latest-logrotate"):
        assert pkg in text


@pytest.mark.parametrize("role", ["common", "docker"])
def test_redhat_paths_gate_on_major_version_8_or_9(role: str) -> None:
    """EL10 / Fedora break the docker-ce repo path and the EL8 pip pin, so
    the RedHat path refuses them up front instead of failing late."""
    first = _load(role, "redhat.yml")[0]
    assert "ansible.builtin.assert" in first
    assert (
        "ansible_distribution_major_version in ['8', '9']"
        in first["ansible.builtin.assert"]["that"]
    )
