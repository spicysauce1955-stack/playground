"""DigitalOcean Droplet pricing lookup.

``DROPLET_PRICES`` maps Droplet size slug to USD monthly price. The
``estimate_cost`` function converts a size + VM count into a
:class:`~playground.planner.plan.CostEstimate`.

These figures are advisory only — the authoritative price is always the
DigitalOcean pricing page. DigitalOcean bills hourly up to a monthly
cap; the hourly rate is ``monthly / 672`` (a 672-hour month).
"""

from __future__ import annotations

from playground.planner.plan import CostEstimate

# prices as of 2026-05-31; advisory only
DROPLET_PRICES: dict[str, float] = {
    "s-1vcpu-1gb": 6.0,
    "s-1vcpu-2gb": 12.0,
    "s-2vcpu-2gb": 18.0,
    "s-2vcpu-4gb": 24.0,
    "s-4vcpu-8gb": 48.0,
}

_NOTE = (
    "advisory; DigitalOcean caps hourly billing at the monthly rate. "
    "Verify at digitalocean.com/pricing (prices as of 2026-05-31)."
)

_HOURS_PER_MONTH = 672
"""DigitalOcean's billing month is defined as 672 hours."""


def estimate_cost(size: str, vm_count: int) -> CostEstimate | None:
    """Return an advisory :class:`CostEstimate` for ``vm_count`` Droplets
    of ``size``, or ``None`` when ``size`` is not in ``DROPLET_PRICES``.

    Callers should treat ``None`` as "no estimate available" rather than
    an error — unknown size slugs are valid; pricing data may simply be
    stale.

    :param size: DigitalOcean Droplet size slug, e.g. ``"s-1vcpu-1gb"``.
    :param vm_count: Number of Droplets of that size in the lab.
    """
    unit_price = DROPLET_PRICES.get(size)
    if unit_price is None:
        return None

    monthly = unit_price * vm_count
    hourly = round(monthly / _HOURS_PER_MONTH, 5)
    return CostEstimate(
        hourly_usd=hourly,
        monthly_usd=monthly,
        note=_NOTE,
        advisory=True,
    )


__all__ = ["DROPLET_PRICES", "estimate_cost"]
