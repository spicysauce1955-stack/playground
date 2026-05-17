"""Typed contract models shared across teams.

See ``ai/architecture/shared_contracts.md`` for the field-level
contract these models must implement. The public surface is:

- ``Diagnostic``
- ``ResolvedLab`` and its sub-models
- ``OperationRun``
- ``OperationEvent``
- ``ResourceStatus``
- ``Plan`` (input/output type for :class:`ProviderAdapter`)

Owners: Team A. Other teams import from here and must not redefine
these types locally.
"""
