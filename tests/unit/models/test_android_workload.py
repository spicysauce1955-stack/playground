"""The android_app workload type and its options block."""

from __future__ import annotations

from textwrap import dedent

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from playground.models.kinds import AndroidAppOptions, Lab, parse_resource

_yaml = YAML(typ="safe")


def _lab(workload_yaml: str) -> Lab:
    raw = _yaml.load(dedent(f"""
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: droid-lab
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: droid1
              role: redroid-host
              networks: [net]
          workloads:
        {workload_yaml}
        """).lstrip("\n"))
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    return lab


def test_android_app_is_an_accepted_workload_type() -> None:
    lab = _lab("""
            - name: my-app
              type: android_app
              source: ./apks/my-app.apk
              placement: {target_role: redroid-host}
              android:
                package: com.example.app
    """)
    wl = lab.spec.workloads[0]
    assert wl.type == "android_app"
    assert wl.android is not None
    assert wl.android.package == "com.example.app"
    assert wl.android.launch is False
    assert wl.android.reinstall is False
    assert wl.android.activity is None
    assert wl.android.permissions == []


def test_android_options_accept_launch_activity_and_permissions() -> None:
    lab = _lab("""
            - name: my-app
              type: android_app
              source: ./apks/my-app.apk
              placement: {target_role: redroid-host}
              android:
                package: com.example.app
                launch: true
                activity: .MainActivity
                permissions: [android.permission.CAMERA]
                reinstall: true
    """)
    opts = lab.spec.workloads[0].android
    assert opts == AndroidAppOptions(
        package="com.example.app",
        launch=True,
        activity=".MainActivity",
        permissions=["android.permission.CAMERA"],
        reinstall=True,
    )


def test_android_block_is_optional_on_other_types() -> None:
    lab = _lab("""
            - name: web
              type: container
              source: nginx:alpine
              placement: {auto: true}
    """)
    assert lab.spec.workloads[0].android is None


def test_package_is_required_when_the_android_block_is_present() -> None:
    with pytest.raises(ValidationError):
        AndroidAppOptions()  # type: ignore[call-arg]


def test_unknown_android_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AndroidAppOptions(package="com.x", nope=1)  # type: ignore[call-arg]
