from __future__ import annotations

from aix.config.schema import ModelPrice, PricingConfig
from aix.core.cost import estimate_usage, price_for
from aix.domain.execution import Usage

PRICES = PricingConfig(
    models={
        "m-large": ModelPrice(input_per_mtok=10, output_per_mtok=30),
        "m-": ModelPrice(input_per_mtok=1, output_per_mtok=2),
    }
)


def test_estimates_from_tokens_and_marks_it() -> None:
    u = estimate_usage(Usage(input_tokens=2_000_000, output_tokens=1_000_000), "m-large", PRICES)
    assert u.cost_usd == 50.0 and u.estimated


def test_reported_cost_is_never_replaced() -> None:
    reported = Usage(input_tokens=10, output_tokens=10, cost_usd=0.5)
    assert estimate_usage(reported, "m-large", PRICES) is reported


def test_unknown_stays_unknown() -> None:
    for u, m in (
        (Usage(input_tokens=5, output_tokens=5), "other"),  # unpriced model
        (Usage(input_tokens=5), "m-large"),  # tokens missing
        (Usage(), None),
    ):
        assert estimate_usage(u, m, PRICES).cost_usd is None


def test_prefix_match_prefers_longest_and_usage_model_is_a_fallback() -> None:
    assert price_for("m-large-2", PRICES) == PRICES.models["m-large"]
    assert price_for("m-small", PRICES) == PRICES.models["m-"]
    u = estimate_usage(
        Usage(input_tokens=1_000_000, output_tokens=0, model="m-small"), None, PRICES
    )
    assert u.cost_usd == 1.0


def test_default_config_ships_no_prices() -> None:
    assert PricingConfig().models == {}
