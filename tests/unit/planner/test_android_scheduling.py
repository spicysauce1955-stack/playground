"""Placement and staging for android_app workloads."""

from __future__ import annotations

from pathlib import Path

from playground.models.kinds import AndroidAppOptions, WorkloadPlacement
from playground.models.resolved import ResolvedVm, ResolvedWorkload, SshConfig
from playground.planner.scheduling import _pick_target_vm, stage_workload_files


def _vm(name: str, **caps: bool) -> ResolvedVm:
    return ResolvedVm(
        name=name, role="r", roles=["r"], image="ubuntu-noble",
        vcpu=1, memory_mb=1024, disk_gb=10, networks=["net"],
        ssh=SshConfig(user="ubuntu"),
        capabilities=dict(caps),
    )


def _android_workload(source: str) -> ResolvedWorkload:
    return ResolvedWorkload(
        name="my-app", type="android_app", source=source,
        placement=WorkloadPlacement(auto=True),
        android=AndroidAppOptions(package="com.example.app"),
    )


def test_auto_placement_picks_a_redroid_vm_not_a_docker_one() -> None:
    """`auto` was hardcoded to capabilities['docker']; an APK must not land
    on a plain docker host."""
    vms = [_vm("web", docker=True), _vm("droid", docker=True, redroid=True)]
    picked = _pick_target_vm(_android_workload("./a.apk"), vms)
    assert picked is not None
    assert picked.name == "droid"


def test_auto_placement_for_container_still_picks_docker() -> None:
    wl = ResolvedWorkload(
        name="web", type="container", source="nginx:alpine",
        placement=WorkloadPlacement(auto=True),
    )
    vms = [_vm("web", docker=True), _vm("droid", docker=True, redroid=True)]
    picked = _pick_target_vm(wl, vms)
    assert picked is not None
    assert picked.name == "web"


def test_single_apk_is_staged_as_a_file(tmp_path: Path) -> None:
    base = tmp_path / "src"
    base.mkdir()
    (base / "a.apk").write_bytes(b"PK\x03\x04fake")
    staged, diagnostics = stage_workload_files(
        {"droid": [_android_workload("a.apk")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert diagnostics == []
    path = staged["droid"]["my-app"]
    assert path.is_file()
    assert path.suffix == ".apk"
    assert path.is_absolute()


def test_split_apk_directory_is_staged_as_a_directory(tmp_path: Path) -> None:
    """A directory of splits must stage, not fail as a missing source."""
    base = tmp_path / "src"
    splits = base / "split-app"
    splits.mkdir(parents=True)
    (splits / "base.apk").write_bytes(b"PK\x03\x04base")
    (splits / "split_config.en.apk").write_bytes(b"PK\x03\x04en")
    staged, diagnostics = stage_workload_files(
        {"droid": [_android_workload("split-app")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert diagnostics == []
    path = staged["droid"]["my-app"]
    assert path.is_dir()
    assert {p.name for p in path.glob("*.apk")} == {
        "base.apk", "split_config.en.apk"
    }


def test_split_directory_without_base_apk_is_an_error(tmp_path: Path) -> None:
    base = tmp_path / "src"
    splits = base / "split-app"
    splits.mkdir(parents=True)
    (splits / "split_config.en.apk").write_bytes(b"PK\x03\x04en")
    _, diagnostics = stage_workload_files(
        {"droid": [_android_workload("split-app")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert [d.id for d in diagnostics] == ["config.workload.split_apk_invalid"]


def test_directory_with_no_apks_is_an_error(tmp_path: Path) -> None:
    base = tmp_path / "src"
    (base / "empty").mkdir(parents=True)
    _, diagnostics = stage_workload_files(
        {"droid": [_android_workload("empty")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert [d.id for d in diagnostics] == ["config.workload.split_apk_invalid"]


def test_aab_is_rejected_with_a_bundletool_message(tmp_path: Path) -> None:
    base = tmp_path / "src"
    base.mkdir()
    (base / "a.aab").write_bytes(b"PK\x03\x04aab")
    _, diagnostics = stage_workload_files(
        {"droid": [_android_workload("a.aab")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert [d.id for d in diagnostics] == ["config.workload.bundle_unsupported"]
    assert "bundletool" in diagnostics[0].suggestion
