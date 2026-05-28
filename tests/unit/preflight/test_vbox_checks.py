"""Tests for the vbox-related doctor checks (warning-only)."""

from __future__ import annotations

from playground.preflight import doctor


def test_vboxmanage_present(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda n: "/usr/bin/VBoxManage")
    assert doctor.check_vboxmanage() == []


def test_vboxmanage_missing_is_warning(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    diags = doctor.check_vboxmanage()
    assert [d.id for d in diags] == ["runtime.doctor.vboxmanage_missing"]
    assert diags[0].severity == "warning"  # never blocks a libvirt-only operator


def test_qemu_img_present(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda n: "/usr/bin/qemu-img")
    assert doctor.check_qemu_img() == []


def test_qemu_img_missing_is_warning(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    diags = doctor.check_qemu_img()
    assert [d.id for d in diags] == ["runtime.doctor.qemu_img_missing"]
    assert diags[0].severity == "warning"
