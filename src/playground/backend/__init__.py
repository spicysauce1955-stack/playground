"""Backend adapter layer.

Adapters translate a backend-neutral :class:`ResolvedLab` into the
inputs and side effects of a concrete backend (libvirt, future cloud,
etc.). Each adapter lives under its own subpackage so its modules,
diagnostics, and tests stay self-contained.

Current adapters:

- ``local_libvirt`` — libvirt provisioning + Ansible inventory rendering.
"""
