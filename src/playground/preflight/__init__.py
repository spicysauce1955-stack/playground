"""Host-prerequisite probes for the playground CLI.

The :mod:`doctor` module bundles the checks that ``playground doctor``
runs against the local host before an operator tries to ``playground
apply`` a lab. Each check is a pure function returning a list of
:class:`~playground.models.diagnostic.Diagnostic` so the CLI can render
them the same way as config/validator diagnostics.
"""

from playground.preflight.doctor import (
    CheckResult,
    check_ansible_and_collections,
    check_ansible_config,
    check_ansible_config_wired,
    check_cloud_init_on_image,
    check_default_pool,
    check_iso_tool,
    check_libvirt_apparmor,
    check_libvirt_group_membership,
    check_pool_path_permissions,
    check_ssh_public_key,
    check_tofu_state_alignment,
    check_virsh,
    run_all_checks,
)

__all__ = [
    "CheckResult",
    "check_ansible_and_collections",
    "check_ansible_config",
    "check_ansible_config_wired",
    "check_cloud_init_on_image",
    "check_default_pool",
    "check_iso_tool",
    "check_libvirt_apparmor",
    "check_libvirt_group_membership",
    "check_pool_path_permissions",
    "check_ssh_public_key",
    "check_tofu_state_alignment",
    "check_virsh",
    "run_all_checks",
]
