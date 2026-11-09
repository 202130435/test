from dataclasses import replace

from futures_risk import DailyRiskState
from futures_strategy import Action
from futures_system import plan_entry
from trade_checklist import ChecklistAssessment


def candles(closes):
    return [
        [index, close, close + 1, max(0.01, close - 1), close, 1]
        for index, close in enumerate(closes)
    ]


def rising():
    return candles([100 + index * 0.25 for index in range(220)])


def turn_up():
    return candles([100] * 21 + [110])


def complete_assessment():
    return ChecklistAssessment(
        signal_reviewed=True,
        location_reviewed=True,
        location_valid=True,
        timeframe_reviewed=True,
        timeframe_aligned=True,
        volume_reviewed=True,
        volume_confirmed=True,
        repeated_signal_risk_reviewed=True,
        structure_reviewed=True,
        structure_confirmed=True,
        oi_reviewed=True,
    )


def test_entry_candidate_becomes_cost_aware_plan_after_all_gates():
    result = plan_entry(
        rising(),
        rising(),
        turn_up(),
        complete_assessment(),
        equity=1_000,
        leverage=2,
        daily_state=DailyRiskState("session-1", 1_000),
    )
    assert result.decision.action == Action.ENTER_LONG
    assert result.ready
    assert result.trade_plan.total_planned_loss <= result.trade_plan.risk_budget


def test_failed_checklist_blocks_strategy_entry():
    result = plan_entry(
        rising(),
        rising(),
        turn_up(),
        replace(complete_assessment(), structure_confirmed=False),
        equity=1_000,
        leverage=2,
        daily_state=DailyRiskState("session-1", 1_000),
    )
    assert result.decision.action == Action.ENTER_LONG
    assert not result.ready
    assert result.blocked_reasons == ("price structure is not confirmed",)


def test_halted_daily_state_blocks_even_complete_setup():
    result = plan_entry(
        rising(),
        rising(),
        turn_up(),
        complete_assessment(),
        equity=1_000,
        leverage=2,
        daily_state=DailyRiskState(
            "session-1", 1_000, halted=True, halt_reason="manual review required"
        ),
    )
    assert not result.ready
    assert result.blocked_reasons == ("manual review required",)


def test_no_strategy_entry_does_not_consume_checklist():
    no_turn = candles([100 + index for index in range(30)])
    result = plan_entry(
        rising(),
        rising(),
        no_turn,
        complete_assessment(),
        equity=1_000,
        leverage=2,
        daily_state=DailyRiskState("session-1", 1_000),
    )
    assert result.decision.action == Action.WAIT
    assert result.checklist is None
    assert not result.ready
