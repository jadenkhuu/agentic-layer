"""Anthropic API pricing — estimate run cost from token usage.

Public list prices in USD per million tokens (MTok). Source:
https://www.anthropic.com/pricing — captured 2026-05-16.

The Claude Agent SDK reports `total_cost_usd` on its `ResultMessage`; when
that figure is present it is authoritative and is used directly (see
`agent.py::_record_sdk_cost`). This table is the fallback for SDK paths or
versions that do not surface a cost, and it documents the rates the
`cost` event is derived from.

Cache multipliers follow Anthropic's published scheme: a 5-minute cache
*write* costs 1.25x the base input rate, a cache *read* costs 0.10x.
"""
from __future__ import annotations

from typing import NamedTuple


class ModelPrice(NamedTuple):
    """USD per 1M tokens for one model family."""

    input: float
    output: float
    cache_write: float
    cache_read: float


# Keyed by model *family*. Concrete model ids ("claude-opus-4-7", ...) are
# matched to a family by substring in `model_family()`.
MODEL_PRICING: dict[str, ModelPrice] = {
    "opus": ModelPrice(input=15.00, output=75.00, cache_write=18.75, cache_read=1.50),
    "sonnet": ModelPrice(input=3.00, output=15.00, cache_write=3.75, cache_read=0.30),
    "haiku": ModelPrice(input=1.00, output=5.00, cache_write=1.25, cache_read=0.10),
}

# Family used when a model id matches none of the known families. Sonnet is
# the SDK's default tier, so an unrecognised id is most likely a Sonnet revision.
DEFAULT_FAMILY = "sonnet"


def model_family(model: str) -> str:
    """Map a concrete model id to a pricing family.

    >>> model_family("claude-opus-4-7")
    'opus'
    >>> model_family("claude-3-5-haiku-20241022")
    'haiku'
    >>> model_family("stub")
    'sonnet'
    """
    m = (model or "").lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in m:
            return family
    return DEFAULT_FAMILY


def price_for(model: str) -> ModelPrice:
    """Return the price table for `model`'s family."""
    return MODEL_PRICING[model_family(model)]


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate the USD cost of one SDK round-trip from its token usage.

    Rounded to 6 decimal places — sub-cent precision, enough that summing
    many small agent calls does not shed pennies.
    """
    p = price_for(model)
    total = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_creation_tokens * p.cache_write
        + cache_read_tokens * p.cache_read
    ) / 1_000_000
    return round(total, 6)
