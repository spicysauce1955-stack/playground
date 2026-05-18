""":class:`StateStore` over the ``.playground/`` directory.

Owners: Team A. All filesystem state reads/writes go through this
module; other teams must not write to ``.playground/`` directly.

Layout and invariants are defined in
``ai/architecture/shared_contracts.md`` §9.
"""
