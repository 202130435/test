"""Risk, session halt, staged-entry and take-profit planning.

The defaults are developer starting points for paper testing, not values stated
by the course presenter. This module never submits an exchange order.
"""

from dataclasses import dataclass, replace
from math import isclose, isfinite
from typing import Sequence


@dataclass(frozen=True)
class RiskConfig:
    risk_fraction: float = 0.005
    daily_loss_limit_r: float = 3.0
    max_leverage: float = 2.0
    max_consecutive_losses: int = 3
    max_daily_trades: int = 6
    fee_rate_per_side: float = 0.0005
    slippage_rate_per_side: float = 0.0005
    funding_rate_buffer: float = 0.0
    allow_reduced_risk_near_daily_limit: bool = False

    def __post_init__(self):
        numeric = (
            self.risk_fraction,
            self.daily_loss_limit_r,
            self.max_leverage,
            self.fee_rate_per_side,
            self.slippage_rate_per_side,
            self.funding_rate_buffer,
        )
        if not all(isfinite(value) and value >= 0 for value in numeric):
            raise ValueError("risk configuration values must be finite and non-negative")
        if not 0 < self.risk_fraction < 1:
            raise ValueError("risk fraction must be between 0 and 1")
        if self.daily_loss_limit_r < 1 or self.max_leverage < 1:
            raise ValueError("daily loss R and maximum leverage must be at least 1")
        if self.max_consecutive_losses < 1 or self.max_daily_trades < 1:
            raise ValueError("loss and trade count limits must be positive")


@dataclass(frozen=True)
class DailyRiskState:
    session_id: str
    session_start_equity: float
    gross_realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_pnl: float = 0.0
    trade_count: int = 0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str | None = None

    def __post_init__(self):
        if not self.session_id:
            raise ValueError("session ID is required")
        values = (self.session_start_equity, self.gross_realized_pnl, self.fees_paid, self.funding_pnl)
        if not all(isfinite(value) for value in values) or self.session_start_equity <= 0:
            raise ValueError("invalid daily risk state values")
        if self.fees_paid < 0 or self.trade_count < 0 or self.consecutive_losses < 0:
            raise ValueError("fees and counters cannot be negative")

    @property
    def net_realized_pnl(self) -> float:
        return self.gross_realized_pnl - self.fees_paid + self.funding_pnl

    @property
    def consumed_loss(self) -> float:
        return max(0.0, -self.net_realized_pnl)

    def standard_risk(self, config: RiskConfig) -> float:
        return self.session_start_equity * config.risk_fraction

    def daily_loss_limit(self, config: RiskConfig) -> float:
        return self.standard_risk(config) * config.daily_loss_limit_r

    def remaining_daily_risk(self, config: RiskConfig) -> float:
        return max(0.0, self.daily_loss_limit(config) - self.consumed_loss)

    def entry_block_reason(self, config: RiskConfig) -> str | None:
        if self.halted:
            return self.halt_reason or "session is halted"
        if self.trade_count >= config.max_daily_trades:
            return "maximum daily trade count reached"
        if self.consecutive_losses >= config.max_consecutive_losses:
            return "maximum consecutive losses reached"
        standard = self.standard_risk(config)
        remaining = self.remaining_daily_risk(config)
        if remaining <= 0:
            return "daily loss limit reached"
        if remaining < standard and not config.allow_reduced_risk_near_daily_limit:
            return "remaining daily risk is below one standard trade risk"
        return None

    def record_closed_trade(
        self,
        gross_pnl: float,
        fees: float = 0.0,
        funding_pnl: float = 0.0,
        config: RiskConfig = RiskConfig(),
    ) -> "DailyRiskState":
        if not all(isfinite(value) for value in (gross_pnl, fees, funding_pnl)) or fees < 0:
            raise ValueError("invalid closed trade values")
        trade_net = gross_pnl - fees + funding_pnl
        updated = replace(
            self,
            gross_realized_pnl=self.gross_realized_pnl + gross_pnl,
            fees_paid=self.fees_paid + fees,
            funding_pnl=self.funding_pnl + funding_pnl,
            trade_count=self.trade_count + 1,
            consecutive_losses=self.consecutive_losses + 1 if trade_net < 0 else 0,
        )
        reason = updated.entry_block_reason(config)
        if reason:
            return replace(updated, halted=True, halt_reason=reason)
        return updated

    def start_new_session(self, session_id: str, equity: float) -> "DailyRiskState":
        """Explicit reset; callers must not auto-resume the halted session."""
        if session_id == self.session_id:
            raise ValueError("a new session ID is required to reset daily risk")
        return DailyRiskState(session_id=session_id, session_start_equity=equity)


@dataclass(frozen=True)
class TradePlan:
    side: str
    entry_price: float
    stop_price: float
    quantity: float
    notional: float
    leverage: float
    required_margin: float
    price_loss: float
    estimated_fees: float
    estimated_slippage: float
    funding_buffer: float
    total_planned_loss: float
    risk_budget: float


def build_trade_plan(
    side: str,
    entry_price: float,
    stop_price: float,
    equity: float,
    leverage: float,
    daily_state: DailyRiskState,
    config: RiskConfig = RiskConfig(),
    risk_multiplier: float = 1.0,
) -> TradePlan:
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    values = (entry_price, stop_price, equity, leverage, risk_multiplier)
    if not all(isfinite(value) and value > 0 for value in values):
        raise ValueError("trade plan inputs must be finite and positive")
    if leverage > config.max_leverage:
        raise ValueError("requested leverage exceeds configured maximum")
    if not 0 < risk_multiplier <= 1:
        raise ValueError("risk multiplier must be between 0 and 1")
    if side == "long" and stop_price >= entry_price:
        raise ValueError("long stop must be below entry")
    if side == "short" and stop_price <= entry_price:
        raise ValueError("short stop must be above entry")
    reason = daily_state.entry_block_reason(config)
    if reason:
        raise RuntimeError(reason)

    # A profitable session must not silently increase the configured 1R. A
    # drawdown, however, immediately reduces it with the current equity.
    risk_equity = min(equity, daily_state.session_start_equity)
    standard_budget = risk_equity * config.risk_fraction * risk_multiplier
    remaining = daily_state.remaining_daily_risk(config)
    if remaining < standard_budget and not config.allow_reduced_risk_near_daily_limit:
        raise RuntimeError("remaining daily risk is below requested trade risk")
    risk_budget = min(standard_budget, remaining)
    price_distance = abs(entry_price - stop_price)
    fee_distance = entry_price * config.fee_rate_per_side * 2
    slippage_distance = entry_price * config.slippage_rate_per_side * 2
    funding_distance = entry_price * config.funding_rate_buffer
    loss_per_unit = price_distance + fee_distance + slippage_distance + funding_distance
    quantity_by_risk = risk_budget / loss_per_unit
    quantity_by_leverage = equity * leverage / entry_price
    quantity = min(quantity_by_risk, quantity_by_leverage)
    if quantity <= 0:
        raise ValueError("calculated quantity must be positive")
    notional = quantity * entry_price
    price_loss = quantity * price_distance
    estimated_fees = quantity * fee_distance
    estimated_slippage = quantity * slippage_distance
    funding_buffer = quantity * funding_distance
    total = price_loss + estimated_fees + estimated_slippage + funding_buffer
    if total > risk_budget and not isclose(total, risk_budget, rel_tol=1e-12):
        raise AssertionError("planned loss exceeds risk budget")
    return TradePlan(
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        quantity=quantity,
        notional=notional,
        leverage=leverage,
        required_margin=notional / leverage,
        price_loss=price_loss,
        estimated_fees=estimated_fees,
        estimated_slippage=estimated_slippage,
        funding_buffer=funding_buffer,
        total_planned_loss=total,
        risk_budget=risk_budget,
    )


def validate_liquidation_buffer(
    side: str,
    entry_price: float,
    stop_price: float,
    liquidation_price: float,
    minimum_buffer_fraction: float = 0.005,
) -> bool:
    values = (entry_price, stop_price, liquidation_price, minimum_buffer_fraction)
    if not all(isfinite(value) and value >= 0 for value in values) or entry_price <= 0:
        raise ValueError("invalid liquidation buffer inputs")
    if side == "long":
        return liquidation_price < stop_price and (stop_price - liquidation_price) / entry_price >= minimum_buffer_fraction
    if side == "short":
        return liquidation_price > stop_price and (liquidation_price - stop_price) / entry_price >= minimum_buffer_fraction
    raise ValueError("side must be long or short")


@dataclass(frozen=True)
class PositionLeg:
    entry_price: float
    quantity: float

    def __post_init__(self):
        if not all(isfinite(value) and value > 0 for value in (self.entry_price, self.quantity)):
            raise ValueError("position leg values must be finite and positive")


def combined_price_risk(legs: Sequence[PositionLeg], stop_price: float) -> float:
    if not legs or not isfinite(stop_price) or stop_price <= 0:
        raise ValueError("legs and positive stop price are required")
    return sum(leg.quantity * abs(leg.entry_price - stop_price) for leg in legs)


def allow_scale_in(
    side: str,
    existing_legs: Sequence[PositionLeg],
    proposed_leg: PositionLeg,
    current_price: float,
    old_stop: float,
    new_stop: float,
    risk_budget: float,
    confirmation_present: bool,
    cost_buffer: float = 0.0,
) -> bool:
    if side not in {"long", "short"} or not existing_legs:
        raise ValueError("valid side and existing position are required")
    values = (current_price, old_stop, new_stop, risk_budget)
    if not all(isfinite(value) and value > 0 for value in values):
        raise ValueError("scale-in values must be finite and positive")
    if not isfinite(cost_buffer) or cost_buffer < 0:
        raise ValueError("cost buffer must be finite and non-negative")
    average = sum(leg.entry_price * leg.quantity for leg in existing_legs) / sum(
        leg.quantity for leg in existing_legs
    )
    profitable = current_price > average if side == "long" else current_price < average
    adding_to_winner = (
        proposed_leg.entry_price >= average
        if side == "long"
        else proposed_leg.entry_price <= average
    )
    stop_not_widened = new_stop >= old_stop if side == "long" else new_stop <= old_stop
    total_risk = combined_price_risk((*existing_legs, proposed_leg), new_stop) + cost_buffer
    return (
        confirmation_present
        and profitable
        and adding_to_winner
        and stop_not_widened
        and total_risk <= risk_budget
    )


@dataclass(frozen=True)
class TakeProfitOrder:
    price: float
    fraction: float
    quantity: float


def build_take_profit_plan(
    side: str,
    entry_price: float,
    total_quantity: float,
    levels: Sequence[float],
    fractions: Sequence[float],
) -> tuple[TakeProfitOrder, ...]:
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    if (
        not isfinite(entry_price)
        or entry_price <= 0
        or not isfinite(total_quantity)
        or total_quantity <= 0
        or not levels
        or len(levels) != len(fractions)
    ):
        raise ValueError("quantity and matching take-profit levels are required")
    if not all(isfinite(value) and value > 0 for value in (*levels, *fractions)):
        raise ValueError("take-profit values must be finite and positive")
    if not isclose(sum(fractions), 1.0, abs_tol=1e-12):
        raise ValueError("take-profit fractions must total 1")
    if side == "long" and any(left >= right for left, right in zip(levels, levels[1:])):
        raise ValueError("long take-profit levels must increase")
    if side == "short" and any(left <= right for left, right in zip(levels, levels[1:])):
        raise ValueError("short take-profit levels must decrease")
    if side == "long" and any(price <= entry_price for price in levels):
        raise ValueError("long take-profit levels must be above entry")
    if side == "short" and any(price >= entry_price for price in levels):
        raise ValueError("short take-profit levels must be below entry")
    return tuple(
        TakeProfitOrder(price=price, fraction=fraction, quantity=total_quantity * fraction)
        for price, fraction in zip(levels, fractions)
    )
