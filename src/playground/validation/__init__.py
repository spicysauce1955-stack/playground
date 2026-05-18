"""Validation layers: schema shape, identity, cross-reference, budget, offline.

Validation emits ``Diagnostic`` objects rather than raising on user errors.
"""

from playground.validation.validator import validate

__all__ = ["validate"]
