"""Connect strategy decisions to the checklist and risk planner.

This module creates reviewable plans only. It does not call an exchange or
submit orders.
"""

from dataclasses import dataclass
from typing import Sequence

from futures_risk import DailyRiskState, RiskConfig, TradePlan, build_trade_plan
from futures_strategy import (
    Action,
    Candle,
    Decision,
    Position,
    StrategyConfig,
    decide,
)
from trade_checklist import ChecklistAssessment, ChecklistResult, evaluate_checklist


@dataclass(frozen=True)
class EntryPlanResult:
    decision: Decision
    checklist: ChecklistResult | None
    trade_plan: TradePlan | None
    blocked_reasons: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.trade_plan is not None and not self.blocked_reasons


def plan_entry(
    daily: Sequence[Candle],
    four_hour: Sequence[Candle],
    trigger_candles: Sequence[Candle],
    assessment: ChecklistAssessment,
    equity: float,
    leverage: float,
    daily_state: DailyRiskState,
    position: Position = Position(),
    strategy_config: StrategyConfig = StrategyConfig(),
    risk_config: RiskConfig = RiskConfig(),
) -> EntryPlanResult:
    """Return an entry plan only after strategy, checklist and risk approval."""
    decision = decide(
        daily=daily,
        four_hour=four_hour,
        trigger_candles=trigger_candles,
        position=position,
        config=strategy_config,
    )
    entry_actions = {Action.ENTER_LONG: "long", Action.ENTER_SHORT: "short"}
    side = entry_actions.get(decision.action)
    if side is None:
        return EntryPlanResult(decision=decision, checklist=None, trade_plan=None)

    checklist = evaluate_checklist(assessment)
    if not checklist.allowed:
        return EntryPlanResult(
            decision=decision,
            checklist=checklist,
            trade_plan=None,
            blocked_reasons=checklist.reasons,
        )

    entry_price = float(trigger_candles[-1][4])
    try:
        trade_plan = build_trade_plan(
            side=side,
            entry_price=entry_price,
            stop_price=decision.stop_price,
            equity=equity,
            leverage=leverage,
            daily_state=daily_state,
            config=risk_config,
            risk_multiplier=decision.risk_multiplier,
        )
    except RuntimeError as error:
        return EntryPlanResult(
            decision=decision,
            checklist=checklist,
            trade_plan=None,
            blocked_reasons=(str(error),),
        )
    return EntryPlanResult(
        decision=decision,
        checklist=checklist,
        trade_plan=trade_plan,
    )
