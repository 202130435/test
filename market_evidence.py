"""Closed-candle evidence helpers derived from the course material.

The course did not define pivot widths or numeric thresholds. Callers must
choose and backtest those values; these helpers only encode the stated
relationships and never produce an order by themselves.
"""

from math import isfinite
from typing import Sequence


def regular_bullish_divergence(
    first_price_low: float,
    second_price_low: float,
    first_rsi_low: float,
    second_rsi_low: float,
) -> bool:
    """Price makes a lower low while RSI makes a higher low."""
    values = (first_price_low, second_price_low, first_rsi_low, second_rsi_low)
    if not all(isfinite(value) for value in values):
        raise ValueError("divergence values must be finite")
    return second_price_low < first_price_low and second_rsi_low > first_rsi_low


def regular_bearish_divergence(
    first_price_high: float,
    second_price_high: float,
    first_rsi_high: float,
    second_rsi_high: float,
) -> bool:
    """Price makes a higher high while RSI makes a lower high."""
    values = (first_price_high, second_price_high, first_rsi_high, second_rsi_high)
    if not all(isfinite(value) for value in values):
        raise ValueError("divergence values must be finite")
    return second_price_high > first_price_high and second_rsi_high < first_rsi_high


def bear_trap(low: float, close: float, support: float, recovery_buffer: float = 0.0) -> bool:
    """A completed candle pierces support and closes back above it."""
    values = (low, close, support, recovery_buffer)
    if not all(isfinite(value) for value in values) or support <= 0 or recovery_buffer < 0:
        raise ValueError("invalid bear-trap values")
    return low < support and close >= support * (1 + recovery_buffer)


def bull_trap(high: float, close: float, resistance: float, recovery_buffer: float = 0.0) -> bool:
    """A completed candle pierces resistance and closes back below it."""
    values = (high, close, resistance, recovery_buffer)
    if not all(isfinite(value) for value in values) or resistance <= 0 or recovery_buffer < 0:
        raise ValueError("invalid bull-trap values")
    return high > resistance and close <= resistance * (1 - recovery_buffer)


def relative_volume_confirmed(
    volumes: Sequence[float], lookback: int = 20, multiplier: float = 1.5
) -> bool:
    """Compare the latest completed candle with preceding completed candles."""
    if lookback < 1 or not isfinite(multiplier) or multiplier <= 0:
        raise ValueError("invalid relative-volume settings")
    if len(volumes) < lookback + 1:
        raise ValueError(f"at least {lookback + 1} volume values are required")
    recent = [float(value) for value in volumes[-(lookback + 1) :]]
    if not all(isfinite(value) and value >= 0 for value in recent):
        raise ValueError("volumes must be finite and non-negative")
    baseline = sum(recent[:-1]) / lookback
    return baseline > 0 and recent[-1] >= baseline * multiplier


def short_squeeze_context(
    price_change: float,
    open_interest_change: float,
    funding_rate: float,
    minimum_price_change: float,
    minimum_oi_change: float,
) -> bool:
    """Flag rising price/OI with negative funding as context, not an entry."""
    values = (
        price_change,
        open_interest_change,
        funding_rate,
        minimum_price_change,
        minimum_oi_change,
    )
    if not all(isfinite(value) for value in values):
        raise ValueError("OI context values must be finite")
    if minimum_price_change < 0 or minimum_oi_change < 0:
        raise ValueError("OI context thresholds cannot be negative")
    return (
        price_change >= minimum_price_change
        and open_interest_change >= minimum_oi_change
        and funding_rate < 0
    )


def time_stop_due(
    bars_held: int,
    maximum_bars: int,
    favorable_excursion_r: float,
    minimum_expected_r: float,
    indicator_still_extreme: bool,
) -> bool:
    """Exit after the configured time if the expected reaction did not occur."""
    if bars_held < 0 or maximum_bars < 1:
        raise ValueError("invalid time-stop bars")
    if not all(isfinite(value) and value >= 0 for value in (favorable_excursion_r, minimum_expected_r)):
        raise ValueError("time-stop R values must be finite and non-negative")
    return bars_held >= maximum_bars and (
        favorable_excursion_r < minimum_expected_r or indicator_still_extreme
    )
