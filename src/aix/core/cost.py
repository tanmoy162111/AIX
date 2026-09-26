"""Cost accounting: reported-or-estimated usage (PLAYBOOK §22)."""

from __future__ import annotations

from aix.config.schema import ModelPrice, PricingConfig
from aix.domain.execution import Usage


def price_for(model: str | None, pricing: PricingConfig) -> ModelPrice | None:
    """Exact model match, else the longest configured prefix; ``None`` when unpriced."""
    if not model:
        return None
    if model in pricing.models:
        return pricing.models[model]
    prefixes = [k for k in pricing.models if model.startswith(k)]
    return pricing.models[max(prefixes, key=len)] if prefixes else None


def estimate_usage(usage: Usage, model: str | None, pricing: PricingConfig) -> Usage:
    """Fill a missing ``cost_usd`` from token counts and the price table.

    Contract: a cost the agent reported is never replaced. Otherwise, when both token counts and a
    price for ``model`` (or ``usage.model``) are known, ``cost_usd`` is
    ``in * input_price + out * output_price`` (per million tokens) and ``estimated`` is set.
    Anything else is returned unchanged, so unknown cost stays unknown rather than becoming 0.
    """
    if usage.cost_usd is not None:
        return usage
    price = price_for(model or usage.model, pricing)
    if price is None or usage.input_tokens is None or usage.output_tokens is None:
        return usage
    cost = (
        usage.input_tokens * price.input_per_mtok + usage.output_tokens * price.output_per_mtok
    ) / 1_000_000
    return usage.model_copy(update={"cost_usd": cost, "estimated": True})
