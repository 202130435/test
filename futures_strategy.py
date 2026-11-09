"""Pure multi-timeframe signal and risk logic for USDT-M futures.

Only closed candles may be passed to this module. It never sends orders.
OHLCV rows use the CCXT layout: timestamp, open, high, low, close, volume.
"""

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Sequence


Candle = Sequence[float]


class Regime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"


class Action(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    HOLD_LONG = "hold_long"
    HOLD_SHORT = "hold_short"
    WAIT = "wait"


@dataclass(frozen=True)
class StrategyConfig:
    regime_fast: int = 50
    regime_slow: int = 200
    trigger_fast: int = 5
    trigger_slow: int = 20
    atr_period: int = 14
    atr_multiple: float = 2.0
    swing_lookback: int = 10
    stop_buffer: float = 0.001
    max_stop_distance: float = 0.03
    risk_fraction: float = 0.005
    max_leverage: float = 2.0
    allow_short: bool = False
    bear_risk_multiplier: float = 0.25

    def __post_init__(self):
        if not 1 < self.regime_fast < self.regime_slow:
            raise ValueError("regime windows must satisfy 1 < fast < slow")
        if not 1 < self.trigger_fast < self.trigger_slow:
            raise ValueError("trigger windows must satisfy 1 < fast < slow")
        if self.atr_period < 2 or self.swing_lookback < 2:
            raise ValueError("ATR period and swing lookback must be at least 2")
        values = (
            self.atr_multiple,
            self.stop_buffer,
            self.max_stop_distance,
            self.risk_fraction,
            self.max_leverage,
            self.bear_risk_multiplier,
        )
        if not all(isfinite(value) and value >= 0 for value in values):
            raise ValueError("risk settings must be finite and non-negative")
        if self.atr_multiple <= 0 or not 0 <= self.stop_buffer < 1:
            raise ValueError("invalid ATR multiple or stop buffer")
        if not 0 < self.max_stop_distance < 1 or not 0 < self.risk_fraction < 1:
            raise ValueError("stop distance and risk fraction must be between 0 and 1")
        if self.max_leverage < 1 or not 0 <= self.bear_risk_multiplier <= 1:
            raise ValueError("invalid leverage or bearish risk multiplier")
        if self.allow_short and self.bear_risk_multiplier == 0:
            raise ValueError("enabled shorts require a positive bearish risk multiplier")


@dataclass(frozen=True)
class Position:
    side: str = "flat"
    entry_price: float | None = None
    stop_price: float | None = None

    def __post_init__(self):
        if self.side not in {"flat", "long", "short"}:
            raise ValueError("position side must be flat, long, or short")
        if self.side == "flat":
            if self.entry_price is not None or self.stop_price is not None:
                raise ValueError("flat position cannot have entry or stop prices")
            return
        if self.entry_price is None or self.stop_price is None:
            raise ValueError("open position requires entry and stop prices")
        if not all(
            isfinite(value) and value > 0
            for value in (self.entry_price, self.stop_price)
        ):
            raise ValueError("position prices must be finite and positive")
        if self.stop_price == self.entry_price:
            raise ValueError("stop must differ from entry")

@dataclass(frozen=True)
class Decision:
    action: Action
    regime: Regime
    reasons: tuple[str, ...]
    stop_price: float | None = None
    risk_multiplier: float = 0.0


def _closes(candles: Sequence[Candle]) -> list[float]:
    values = [float(candle[4]) for candle in candles]
    if not values or not all(isfinite(value) and value > 0 for value in values):
        raise ValueError("candles require finite positive closes")
    return values


def sma(values: Sequence[float], period: int, end: int | None = None) -> float:
    subset = values if end is None else values[:end]
    if len(subset) < period:
        raise ValueError(f"at least {period} values are required")
    return sum(subset[-period:]) / period


def classify_timeframe(candles: Sequence[Candle], config: StrategyConfig) -> Regime:
    closes = _closes(candles)
    required = config.regime_slow + 1
    if len(closes) < required:
        raise ValueError(f"at least {required} closed regime candles are required")
    fast = sma(closes, config.regime_fast)
    slow = sma(closes, config.regime_slow)
    previous_fast = sma(closes, config.regime_fast, -1)
    previous_slow = sma(closes, config.regime_slow, -1)
    price = closes[-1]
    if price > fast > slow and fast > previous_fast and slow > previous_slow:
        return Regime.BULL
    if price < fast < slow and fast < previous_fast and slow < previous_slow:
        return Regime.BEAR
    return Regime.NEUTRAL


def market_regime(
    daily: Sequence[Candle], four_hour: Sequence[Candle], config: StrategyConfig
) -> Regime:
    daily_regime = classify_timeframe(daily, config)
    four_hour_regime = classify_timeframe(four_hour, config)
    if daily_regime == four_hour_regime == Regime.BULL:
        return Regime.BULL
    if daily_regime == four_hour_regime == Regime.BEAR:
        return Regime.BEAR
    return Regime.NEUTRAL


def _trigger(candles: Sequence[Candle], config: StrategyConfig) -> str | None:
    closes = _closes(candles)
    required = config.trigger_slow + 1
    if len(closes) < required:
        raise ValueError(f"at least {required} closed trigger candles are required")
    fast_now = sma(closes, config.trigger_fast)
    slow_now = sma(closes, config.trigger_slow)
    fast_previous = sma(closes, config.trigger_fast, -1)
    slow_previous = sma(closes, config.trigger_slow, -1)
    if fast_previous <= slow_previous and fast_now > slow_now and closes[-1] > fast_now:
        return "up"
    if fast_previous >= slow_previous and fast_now < slow_now and closes[-1] < fast_now:
        return "down"
    return None


def atr(candles: Sequence[Candle], period: int) -> float:
    if len(candles) < period + 1:
        raise ValueError(f"at least {period + 1} candles are required for ATR")
    ranges = []
    for previous, current in zip(candles[-period - 1 : -1], candles[-period:]):
        high, low, previous_close = float(current[2]), float(current[3]), float(previous[4])
        if not all(isfinite(value) and value > 0 for value in (high, low, previous_close)):
            raise ValueError("candles require finite positive OHLC values")
        if low > high:
            raise ValueError("candle low cannot exceed high")
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges) / period


def initial_stop(candles: Sequence[Candle], side: str, config: StrategyConfig) -> float:
    if side not in {"long", "short"}:
        raise ValueError("stop side must be long or short")
    if len(candles) < max(config.atr_period + 1, config.swing_lookback):
        raise ValueError("insufficient trigger candles for a protective stop")
    price = float(candles[-1][4])
    volatility = atr(candles, config.atr_period)
    recent = candles[-config.swing_lookback :]
    if side == "long":
        technical = min(
            float(candle[3]) for candle in recent
        ) * (1 - config.stop_buffer)
        volatility_stop = price - volatility * config.atr_multiple
        return max(price * (1 - config.max_stop_distance), min(technical, volatility_stop))
    technical = max(float(candle[2]) for candle in recent) * (1 + config.stop_buffer)
    volatility_stop = price + volatility * config.atr_multiple
    return min(price * (1 + config.max_stop_distance), max(technical, volatility_stop))


def _trailing_stop(
    candles: Sequence[Candle], position: Position, config: StrategyConfig
) -> float:
    closes = _closes(candles)
    price = closes[-1]
    slow = sma(closes, config.trigger_slow)
    volatility = atr(candles, config.atr_period)
    assert position.stop_price is not None
    if position.side == "long":
        candidate = min(
            slow * (1 - config.stop_buffer),
            price - volatility * config.atr_multiple,
        )
        return max(position.stop_price, candidate)
    candidate = max(
        slow * (1 + config.stop_buffer),
        price + volatility * config.atr_multiple,
    )
    return min(position.stop_price, candidate)


def decide(
    daily: Sequence[Candle],
    four_hour: Sequence[Candle],
    trigger_candles: Sequence[Candle],
    position: Position = Position(),
    config: StrategyConfig = StrategyConfig(),
) -> Decision:
    """Return one deterministic decision using closed candles only."""
    regime = market_regime(daily, four_hour, config)
    turn = _trigger(trigger_candles, config)
    last_high = float(trigger_candles[-1][2])
    last_low = float(trigger_candles[-1][3])

    if position.side == "long":
        assert position.stop_price is not None
        if last_low <= position.stop_price:
            return Decision(Action.EXIT_LONG, regime, ("protective stop crossed",))
        if regime != Regime.BULL:
            return Decision(Action.EXIT_LONG, regime, ("daily/4h bullish alignment lost",))
        if turn == "down":
            return Decision(Action.EXIT_LONG, regime, ("small bearish cycle started",))
        return Decision(
            Action.HOLD_LONG,
            regime,
            ("large bullish cycle intact", "no small-cycle exit"),
            _trailing_stop(trigger_candles, position, config),
            1.0,
        )

    if position.side == "short":
        assert position.stop_price is not None
        if last_high >= position.stop_price:
            return Decision(Action.EXIT_SHORT, regime, ("protective stop crossed",))
        if regime != Regime.BEAR:
            return Decision(Action.EXIT_SHORT, regime, ("daily/4h bearish alignment lost",))
        if turn == "up":
            return Decision(Action.EXIT_SHORT, regime, ("small bullish cycle started",))
        return Decision(
            Action.HOLD_SHORT,
            regime,
            ("large bearish cycle intact", "no small-cycle exit"),
            _trailing_stop(trigger_candles, position, config),
            config.bear_risk_multiplier,
        )

    if regime == Regime.BULL and turn == "up":
        return Decision(
            Action.ENTER_LONG,
            regime,
            ("daily and 4h 50/200 bullish alignment", "small bullish cycle turned up"),
            initial_stop(trigger_candles, "long", config),
            1.0,
        )
    if regime == Regime.BEAR:
        if config.allow_short and turn == "down":
            return Decision(
                Action.ENTER_SHORT,
                regime,
                ("daily and 4h 50/200 bearish alignment", "reduced-size short enabled"),
                initial_stop(trigger_candles, "short", config),
                config.bear_risk_multiplier,
            )
        return Decision(
            Action.WAIT,
            regime,
            ("bearish alignment: long blocked", "short disabled or no bearish turn"),
        )
    return Decision(
        Action.WAIT,
        regime,
        ("timeframes are mixed or small cycle has no confirmed turn",),
    )


def position_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    config: StrategyConfig = StrategyConfig(),
    risk_multiplier: float = 1.0,
) -> float:
    """Return base-asset size, capped by configured notional leverage."""
    values = (equity, entry_price, stop_price, risk_multiplier)
    if not all(isfinite(value) for value in values):
        raise ValueError("position inputs must be finite")
    distance = abs(entry_price - stop_price)
    if equity <= 0 or entry_price <= 0 or stop_price <= 0 or distance == 0:
        raise ValueError("equity and prices must be positive and stop must differ from entry")
    if not 0 < risk_multiplier <= 1:
        raise ValueError("risk multiplier must be between 0 and 1")
    risk_size = equity * config.risk_fraction * risk_multiplier / distance
    leverage_cap = equity * config.max_leverage / entry_price
    return min(risk_size, leverage_cap)
