import pytest

from futures_risk import (
    DailyRiskState,
    PositionLeg,
    RiskConfig,
    allow_scale_in,
    build_take_profit_plan,
    build_trade_plan,
    validate_liquidation_buffer,
)


def state():
    return DailyRiskState("2026-11-09", 1_000)


def test_trade_plan_includes_costs_and_stays_within_one_r():
    config = RiskConfig(
        risk_fraction=0.005,
        fee_rate_per_side=0.0005,
        slippage_rate_per_side=0.0005,
        funding_rate_buffer=0.0001,
    )
    plan = build_trade_plan("long", 100, 95, 1_000, 2, state(), config)
    assert plan.risk_budget == pytest.approx(5)
    assert plan.estimated_fees > 0
    assert plan.estimated_slippage > 0
    assert plan.funding_buffer > 0
    assert plan.total_planned_loss == pytest.approx(5)


def test_trade_plan_caps_leverage_and_rejects_excess_leverage():
    config = RiskConfig(max_leverage=2)
    plan = build_trade_plan("long", 100, 99.99, 1_000, 2, state(), config)
    assert plan.notional == pytest.approx(2_000)
    with pytest.raises(ValueError, match="exceeds"):
        build_trade_plan("long", 100, 95, 1_000, 3, state(), config)


def test_session_profit_does_not_raise_one_r_but_drawdown_reduces_it():
    config = RiskConfig(risk_fraction=0.005)
    profitable = build_trade_plan("long", 100, 95, 1_100, 2, state(), config)
    drawdown = build_trade_plan("long", 100, 95, 900, 2, state(), config)
    assert profitable.risk_budget == pytest.approx(5)
    assert drawdown.risk_budget == pytest.approx(4.5)


def test_daily_loss_limit_halts_session_and_requires_new_id():
    config = RiskConfig(daily_loss_limit_r=3, max_consecutive_losses=10)
    current = state()
    for _ in range(3):
        current = current.record_closed_trade(-5, config=config)
    assert current.halted
    assert current.halt_reason == "daily loss limit reached"
    with pytest.raises(ValueError, match="new session ID"):
        current.start_new_session("2026-11-09", 985)
    restarted = current.start_new_session("2026-11-10", 985)
    assert not restarted.halted
    assert restarted.trade_count == 0


def test_consecutive_loss_and_trade_count_guards_halt():
    loss_config = RiskConfig(max_consecutive_losses=2, daily_loss_limit_r=10)
    after_losses = state().record_closed_trade(-1, config=loss_config)
    after_losses = after_losses.record_closed_trade(-1, config=loss_config)
    assert after_losses.halt_reason == "maximum consecutive losses reached"

    trade_config = RiskConfig(max_daily_trades=2, daily_loss_limit_r=10)
    after_trades = state().record_closed_trade(1, config=trade_config)
    after_trades = after_trades.record_closed_trade(1, config=trade_config)
    assert after_trades.halt_reason == "maximum daily trade count reached"


def test_liquidation_must_remain_beyond_stop_with_buffer():
    assert validate_liquidation_buffer("long", 100, 95, 90, 0.01)
    assert not validate_liquidation_buffer("long", 100, 95, 94.5, 0.01)
    assert validate_liquidation_buffer("short", 100, 105, 110, 0.01)
    assert not validate_liquidation_buffer("short", 100, 105, 105.5, 0.01)


def test_scale_in_only_adds_to_confirmed_winner_without_widening_stop():
    existing = [PositionLeg(100, 1)]
    proposed = PositionLeg(105, 1)
    assert allow_scale_in("long", existing, proposed, 106, 95, 100, 10, True)
    assert not allow_scale_in("long", existing, proposed, 99, 95, 100, 10, True)
    assert not allow_scale_in("long", existing, proposed, 106, 95, 94, 20, True)
    assert not allow_scale_in("long", existing, PositionLeg(99, 1), 106, 95, 100, 20, True)
    assert not allow_scale_in("long", existing, proposed, 106, 95, 100, 10, False)
    assert not allow_scale_in("long", existing, proposed, 106, 95, 100, 10, True, cost_buffer=6)


def test_optional_four_part_take_profit_plan_is_directional():
    plan = build_take_profit_plan(
        "long", 100, 8, [101, 102, 103, 104], [0.25, 0.25, 0.25, 0.25]
    )
    assert [order.quantity for order in plan] == [2, 2, 2, 2]
    with pytest.raises(ValueError, match="above entry"):
        build_take_profit_plan("long", 100, 8, [99, 101], [0.5, 0.5])
    with pytest.raises(ValueError, match="total 1"):
        build_take_profit_plan("short", 100, 8, [99, 98], [0.25, 0.25])
