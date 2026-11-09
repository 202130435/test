import pytest

from futures_strategy import (
    Action,
    Position,
    Regime,
    StrategyConfig,
    classify_timeframe,
    decide,
    market_regime,
    position_size,
)


def candles(closes):
    return [
        [index, close, close + 1, max(0.01, close - 1), close, 1]
        for index, close in enumerate(closes)
    ]


def rising():
    return candles([100 + index * 0.25 for index in range(220)])


def falling():
    return candles([200 - index * 0.25 for index in range(220)])


def turn_up():
    return candles([100] * 21 + [110])


def turn_down():
    return candles([100] * 21 + [90])


def test_daily_and_four_hour_must_agree():
    config = StrategyConfig()
    assert classify_timeframe(rising(), config) == Regime.BULL
    assert classify_timeframe(falling(), config) == Regime.BEAR
    assert market_regime(rising(), rising(), config) == Regime.BULL
    assert market_regime(rising(), falling(), config) == Regime.NEUTRAL


def test_bull_regime_enters_only_on_small_turn():
    decision = decide(rising(), rising(), turn_up())
    assert decision.action == Action.ENTER_LONG
    assert decision.stop_price < turn_up()[-1][4]
    assert decision.risk_multiplier == 1

    no_turn = candles([100 + index for index in range(30)])
    assert decide(rising(), rising(), no_turn).action == Action.WAIT


def test_bear_regime_waits_by_default():
    decision = decide(falling(), falling(), turn_down())
    assert decision.action == Action.WAIT
    assert decision.regime == Regime.BEAR


def test_optional_short_uses_reduced_risk():
    config = StrategyConfig(allow_short=True, bear_risk_multiplier=0.25)
    decision = decide(falling(), falling(), turn_down(), config=config)
    assert decision.action == Action.ENTER_SHORT
    assert decision.stop_price > turn_down()[-1][4]
    assert decision.risk_multiplier == 0.25


def test_long_exits_when_large_alignment_is_lost():
    position = Position("long", entry_price=100, stop_price=80)
    decision = decide(rising(), falling(), candles([100] * 30), position)
    assert decision.action == Action.EXIT_LONG
    assert "alignment lost" in decision.reasons[0]


def test_long_exits_on_protective_stop_before_other_rules():
    position = Position("long", entry_price=100, stop_price=99.5)
    decision = decide(rising(), rising(), candles([100] * 30), position)
    assert decision.action == Action.EXIT_LONG
    assert decision.reasons == ("protective stop crossed",)


def test_long_exits_on_small_bearish_cycle():
    position = Position("long", entry_price=80, stop_price=70)
    decision = decide(rising(), rising(), turn_down(), position)
    assert decision.action == Action.EXIT_LONG
    assert decision.reasons == ("small bearish cycle started",)


def test_position_size_risks_fixed_equity_fraction_and_caps_leverage():
    config = StrategyConfig(risk_fraction=0.01, max_leverage=2)
    assert position_size(1_000, 100, 95, config) == pytest.approx(2)
    assert position_size(1_000, 100, 99.9, config) == pytest.approx(20)
    assert position_size(1_000, 100, 95, config, 0.25) == pytest.approx(0.5)


def test_invalid_position_and_config_are_rejected():
    with pytest.raises(ValueError):
        Position("long")
    with pytest.raises(ValueError):
        Position("unknown", entry_price=100, stop_price=90)
    with pytest.raises(ValueError):
        Position("flat", entry_price=100, stop_price=90)
    with pytest.raises(ValueError):
        StrategyConfig(regime_fast=200, regime_slow=50)
    with pytest.raises(ValueError):
        StrategyConfig(allow_short=True, bear_risk_multiplier=0)
    with pytest.raises(ValueError):
        position_size(1_000, 100, 100)
