"""Validation layers: schema shape, identity, cross-reference, budget, offline.

Owners: Team A. Emits ``Diagnostic`` objects rather than raising on user
errors.
"""

from playground.validation.validator import validate

__all__ = ["validate"]
