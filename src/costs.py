"""OpenAI token accounting shared by interactive and background model calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# USD per one million tokens. Keep aliases explicit so unknown/custom models
# are still logged with tokens while their cost remains unknown.
PRICES_PER_MILLION: dict[str, tuple[float, float, float]] = {
    "gpt-5.6-terra": (2.0, 0.2, 12.0),
    "gpt-5.6-luna": (0.2, 0.02, 1.2),
}


def field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from an SDK object or mapping."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def usage_numbers(response_or_usage: Any) -> dict[str, int]:
    """Normalize Responses API usage, including prompt-cache counters."""
    usage = field(response_or_usage, "usage", None)
    if usage is None:
        usage = response_or_usage or {}
    input_details = field(usage, "input_tokens_details", {}) or {}
    output_details = field(usage, "output_tokens_details", {}) or {}
    input_tokens = int(field(usage, "input_tokens", 0) or 0)
    output_tokens = int(field(usage, "output_tokens", 0) or 0)
    cached_tokens = int(field(input_details, "cached_tokens", 0) or 0)
    cache_write_tokens = int(
        field(input_details, "cache_write_tokens", 0)
        or field(input_details, "cache_creation_tokens", 0)
        or field(usage, "cache_write_tokens", 0)
        or field(usage, "cache_creation_input_tokens", 0)
        or 0
    )
    reasoning_tokens = int(field(output_details, "reasoning_tokens", 0) or 0)
    total_tokens = int(
        field(usage, "total_tokens", input_tokens + output_tokens)
        or input_tokens + output_tokens
    )
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def estimated_cost(model: str, usage: Mapping[str, int]) -> float | None:
    """Estimate request cost from normalized token counters."""
    prices = PRICES_PER_MILLION.get(model)
    if prices is None:
        return None
    input_price, cached_price, output_price = prices
    if usage["input_tokens"] > 272_000:
        input_price *= 2
        cached_price *= 2
        output_price *= 1.5
    cached = usage["cached_tokens"]
    cache_write = usage["cache_write_tokens"]
    regular = max(0, usage["input_tokens"] - cached - cache_write)
    return (
        regular * input_price
        + cached * cached_price
        + cache_write * input_price * 1.25
        + usage["output_tokens"] * output_price
    ) / 1_000_000
