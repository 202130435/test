import pytest

from market_evidence import (
    bear_trap,
    bull_trap,
    regular_bearish_divergence,
    regular_bullish_divergence,
    relative_volume_confirmed,
    short_squeeze_context,
    time_stop_due,
)


def test_regular_divergence_relationships():
    assert regular_bullish_divergence(100, 95, 25, 30)
    assert not regular_bullish_divergence(100, 105, 25, 30)
    assert regular_bearish_divergence(100, 105, 75, 70)
    assert not regular_bearish_divergence(100, 105, 75, 80)


def test_traps_require_closed_candle_recovery():
    assert bear_trap(98, 101, 100)
    assert not bear_trap(98, 99, 100)
    assert bull_trap(102, 99, 100)
    assert not bull_trap(102, 101, 100)


def test_relative_volume_uses_prior_completed_bars():
    assert relative_volume_confirmed([10] * 20 + [15], lookback=20, multiplier=1.5)
    assert not relative_volume_confirmed([10] * 20 + [14], lookback=20, multiplier=1.5)
    with pytest.raises(ValueError, match="at least"):
        relative_volume_confirmed([10] * 20, lookback=20)


def test_oi_and_negative_funding_are_context_only():
    assert short_squeeze_context(0.02, 0.10, -0.0005, 0.01, 0.05)
    assert not short_squeeze_context(0.02, 0.10, 0.0005, 0.01, 0.05)


def test_time_stop_requires_elapsed_time_and_missing_reaction():
    assert time_stop_due(4, 4, 0.1, 0.5, False)
    assert time_stop_due(4, 4, 0.6, 0.5, True)
    assert not time_stop_due(3, 4, 0.1, 0.5, True)
    assert not time_stop_due(4, 4, 0.6, 0.5, False)
