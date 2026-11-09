"""Binance USDT-M futures paper trader. It never submits an order."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

import ccxt
import portalocker

from connectivity import RegionRestricted
from futures_risk import DailyRiskState, RiskConfig
from futures_strategy import Action, Position, StrategyConfig, atr, decide
from futures_system import plan_entry
from market_evidence import relative_volume_confirmed
from trade_checklist import ChecklistAssessment


class FuturesExchange(ccxt.binanceusdm):
    """Public-data-only exchange with explicit restriction handling."""

    def handle_http_status_code(self, code, reason, url, method, body):
        if code == 451:
            raise RegionRestricted(
                "Binance Futures HTTP 451: this execution location is restricted. "
                "No order was submitted."
            )
        if code in (403, 418, 429):
            raise ccxt.PermissionDenied(
                f"Binance Futures HTTP {code}: access denied or rate limited; stopping"
            )
        return super().handle_http_status_code(code, reason, url, method, body)


class PaperState:
    def __init__(self, path, identity, capital):
        self.lock = open(str(path) + ".lock", "a")
        try:
            portalocker.lock(self.lock, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException:
            self.lock.close()
            raise RuntimeError("Another futures paper bot uses this state database")
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT)"
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                candle INTEGER PRIMARY KEY,
                event TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_at INTEGER NOT NULL UNIQUE,
                closed_at INTEGER NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                gross_pnl REAL NOT NULL,
                fees REAL NOT NULL,
                net_pnl REAL NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        row = self.db.execute("SELECT data FROM state WHERE id=1").fetchone()
        self.data = (
            json.loads(row[0])
            if row
            else {
                "identity": identity,
                "balance": capital,
                "position": None,
                "candle": 0,
                "daily": None,
                "trades": [],
            }
        )
        if self.data["identity"] != identity:
            raise ValueError(
                "State belongs to another futures paper configuration; use another database"
            )
        self.save()

    def save(self):
        payload = json.dumps(self.data, separators=(",", ":"))
        self.db.execute("INSERT OR REPLACE INTO state VALUES (1,?)", (payload,))
        self.db.commit()

    def record_event(self, candle, event, payload):
        self.db.execute(
            "INSERT OR IGNORE INTO events (candle,event,payload) VALUES (?,?,?)",
            (
                candle,
                event,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def record_trade(self, trade):
        self.db.execute(
            """
            INSERT OR IGNORE INTO trades (
                opened_at,closed_at,side,entry_price,exit_price,quantity,
                gross_pnl,fees,net_pnl,reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade["opened_at"],
                trade["closed_at"],
                trade["side"],
                trade["entry_price"],
                trade["exit_price"],
                trade["quantity"],
                trade["gross_pnl"],
                trade["fees"],
                trade["net_pnl"],
                trade["reason"],
            ),
        )


def closed_candles(exchange, symbol, timeframe, limit):
    candles = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    if len(candles) < 2:
        raise ValueError(f"Insufficient {timeframe} candles")
    return candles[:-1]


def candle_session_id(timestamp_ms):
    instant = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return instant.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()


def current_equity(data, mark_price):
    position = data["position"]
    if not position:
        return float(data["balance"])
    direction = 1 if position["side"] == "long" else -1
    unrealized = (
        mark_price - float(position["entry_price"])
    ) * float(position["quantity"]) * direction
    return float(data["balance"]) + unrealized


def paper_fill(price, side, opening, slippage_rate):
    buying = (opening and side == "long") or (not opening and side == "short")
    return price * (1 + slippage_rate if buying else 1 - slippage_rate)


def automatic_assessment(
    trigger_candles,
    side,
    oi_reviewed,
    volume_multiplier,
):
    lookback = 20
    history = trigger_candles[-(lookback + 1) : -1]
    last = trigger_candles[-1]
    total_volume = sum(float(candle[5]) for candle in history)
    if total_volume > 0:
        volume_zone = sum(
            ((float(candle[2]) + float(candle[3]) + float(candle[4])) / 3)
            * float(candle[5])
            for candle in history
        ) / total_volume
        tolerance = max(
            volume_zone * 0.005,
            atr(trigger_candles, 14),
        )
        location_valid = abs(float(last[4]) - volume_zone) <= tolerance
    else:
        location_valid = False
    try:
        volume_confirmed = relative_volume_confirmed(
            [float(candle[5]) for candle in trigger_candles],
            lookback=lookback,
            multiplier=volume_multiplier,
        )
    except ValueError:
        volume_confirmed = False
    return ChecklistAssessment(
        signal_reviewed=True,
        location_reviewed=True,
        location_valid=location_valid,
        timeframe_reviewed=True,
        timeframe_aligned=True,
        volume_reviewed=True,
        volume_confirmed=volume_confirmed,
        repeated_signal_risk_reviewed=True,
        structure_reviewed=True,
        structure_confirmed=side in {"long", "short"},
        oi_reviewed=oi_reviewed,
    )


def open_interest_reviewed(exchange, symbol):
    if not exchange.has.get("fetchOpenInterest"):
        return False, None
    value = exchange.fetch_open_interest(symbol)
    amount = value.get("openInterestAmount")
    if amount is None:
        amount = value.get("openInterestValue")
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return False, None
    return math.isfinite(number) and number >= 0, number


def _position(data):
    stored = data["position"]
    if not stored:
        return Position()
    return Position(
        side=stored["side"],
        entry_price=float(stored["entry_price"]),
        stop_price=float(stored["stop_price"]),
    )


def _daily_state(data, session_id, equity):
    stored = data["daily"]
    if stored is None:
        return DailyRiskState(session_id, equity)
    daily = DailyRiskState(**stored)
    if daily.session_id != session_id:
        return daily.start_new_session(session_id, equity)
    return daily


def _emit(**values):
    print(json.dumps(values, ensure_ascii=False, separators=(",", ":")), flush=True)


def _record_event(state, candle, event, **values):
    payload = {"event": event, **values}
    state.record_event(candle, event, payload)
    _emit(**payload)


def paper_step(
    exchange,
    state,
    args,
    strategy_config,
    risk_config,
    assessment_builder=automatic_assessment,
):
    daily = closed_candles(
        exchange, args.symbol, "1d", strategy_config.regime_slow + 2
    )
    four_hour = closed_candles(
        exchange, args.symbol, "4h", strategy_config.regime_slow + 2
    )
    trigger_limit = max(
        strategy_config.trigger_slow + 2,
        strategy_config.atr_period + 2,
        strategy_config.swing_lookback + 2,
        62,
    )
    trigger = closed_candles(exchange, args.symbol, "15m", trigger_limit)
    stamp = int(trigger[-1][0])
    mark_price = float(trigger[-1][4])
    if stamp <= state.data["candle"]:
        _emit(event="already_processed", candle=stamp)
        return

    equity = current_equity(state.data, mark_price)
    session_id = candle_session_id(stamp)
    daily_state = _daily_state(state.data, session_id, equity)
    position = _position(state.data)
    decision = decide(daily, four_hour, trigger, position, strategy_config)

    if decision.action in {Action.EXIT_LONG, Action.EXIT_SHORT}:
        stored = state.data["position"]
        side = stored["side"]
        if decision.reasons == ("protective stop crossed",):
            candle_open = float(trigger[-1][1])
            stop = float(stored["stop_price"])
            base_exit = min(candle_open, stop) if side == "long" else max(candle_open, stop)
        else:
            base_exit = mark_price
        exit_price = paper_fill(base_exit, side, False, args.slippage)
        quantity = float(stored["quantity"])
        direction = 1 if side == "long" else -1
        gross_pnl = (exit_price - float(stored["entry_price"])) * quantity * direction
        exit_fee = exit_price * quantity * args.fee
        total_fees = float(stored["entry_fee"]) + exit_fee
        state.data["balance"] += gross_pnl - exit_fee
        daily_state = daily_state.record_closed_trade(
            gross_pnl=gross_pnl,
            fees=total_fees,
            config=risk_config,
        )
        trade = {
            "side": side,
            "entry_price": stored["entry_price"],
            "exit_price": exit_price,
            "quantity": quantity,
            "gross_pnl": gross_pnl,
            "fees": total_fees,
            "net_pnl": gross_pnl - total_fees,
            "opened_at": stored["opened_at"],
            "closed_at": stamp,
            "reason": decision.reasons[0],
        }
        state.data["trades"] = (state.data["trades"] + [trade])[-100:]
        state.data["position"] = None
        state.record_trade(trade)
        _record_event(state, stamp, "exit", balance=state.data["balance"], **trade)
    elif decision.action in {Action.HOLD_LONG, Action.HOLD_SHORT}:
        state.data["position"]["stop_price"] = decision.stop_price
        _record_event(
            state,
            stamp,
            "hold",
            side=position.side,
            mark_price=mark_price,
            stop_price=decision.stop_price,
            equity=equity,
            reasons=decision.reasons,
        )
    elif decision.action in {Action.ENTER_LONG, Action.ENTER_SHORT}:
        side = "long" if decision.action == Action.ENTER_LONG else "short"
        oi_reviewed, open_interest = open_interest_reviewed(exchange, args.symbol)
        assessment = assessment_builder(
            trigger,
            side,
            oi_reviewed,
            args.volume_multiplier,
        )
        result = plan_entry(
            daily=daily,
            four_hour=four_hour,
            trigger_candles=trigger,
            assessment=assessment,
            equity=equity,
            leverage=args.leverage,
            daily_state=daily_state,
            strategy_config=strategy_config,
            risk_config=risk_config,
        )
        if result.ready:
            plan = result.trade_plan
            entry_price = paper_fill(mark_price, side, True, args.slippage)
            entry_fee = entry_price * plan.quantity * args.fee
            state.data["balance"] -= entry_fee
            state.data["position"] = {
                "side": side,
                "entry_price": entry_price,
                "stop_price": plan.stop_price,
                "quantity": plan.quantity,
                "entry_fee": entry_fee,
                "opened_at": stamp,
            }
            _record_event(
                state,
                stamp,
                "entry",
                side=side,
                entry_price=entry_price,
                stop_price=plan.stop_price,
                quantity=plan.quantity,
                risk_budget=plan.risk_budget,
                open_interest=open_interest,
            )
        else:
            _record_event(
                state,
                stamp,
                "blocked",
                action=decision.action.value,
                mark_price=mark_price,
                open_interest=open_interest,
                reasons=result.blocked_reasons,
            )
    else:
        _record_event(
            state,
            stamp,
            "wait",
            regime=decision.regime.value,
            mark_price=mark_price,
            equity=equity,
            reasons=decision.reasons,
        )

    state.data["daily"] = asdict(daily_state)
    state.data["candle"] = stamp
    state.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--capital", type=float, default=1_000)
    parser.add_argument("--leverage", type=float, default=2)
    parser.add_argument("--risk", type=float, default=0.005)
    parser.add_argument("--daily-loss-r", type=float, default=3)
    parser.add_argument("--fee", type=float, default=0.0005)
    parser.add_argument("--slippage", type=float, default=0.0005)
    parser.add_argument("--funding-buffer", type=float, default=0.0001)
    parser.add_argument("--volume-multiplier", type=float, default=1.2)
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--timeout-ms", type=int, default=3_500)
    parser.add_argument("--state", default="futures-paper.sqlite3")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    numeric = (
        args.capital,
        args.leverage,
        args.risk,
        args.daily_loss_r,
        args.fee,
        args.slippage,
        args.funding_buffer,
        args.volume_multiplier,
        args.interval,
    )
    if not all(math.isfinite(value) for value in numeric):
        parser.error("Numeric options must be finite")
    if not 0 < args.capital or not 1 <= args.leverage <= 2:
        parser.error("Capital must be positive and leverage must be between 1 and 2")
    if not 0 < args.risk < 1 or args.daily_loss_r < 1:
        parser.error("Risk must be between 0 and 1 and daily loss R at least 1")
    if not 0 <= args.fee < 1 or not 0 <= args.slippage < 1:
        parser.error("Fee and slippage must be between 0 and 1")
    if args.funding_buffer < 0 or args.volume_multiplier <= 0 or args.interval < 1:
        parser.error("Invalid funding, volume or interval setting")
    if not 100 <= args.timeout_ms <= 60_000:
        parser.error("Timeout must be between 100 and 60000 milliseconds")

    strategy_config = StrategyConfig(
        allow_short=args.allow_short,
        risk_fraction=args.risk,
        max_leverage=args.leverage,
    )
    risk_config = RiskConfig(
        risk_fraction=args.risk,
        daily_loss_limit_r=args.daily_loss_r,
        max_leverage=args.leverage,
        fee_rate_per_side=args.fee,
        slippage_rate_per_side=args.slippage,
        funding_rate_buffer=args.funding_buffer,
    )
    exchange = FuturesExchange(
        {
            "enableRateLimit": True,
            "timeout": args.timeout_ms,
            "maxRetriesOnFailure": 0,
        }
    )
    exchange.session.trust_env = True
    exchange.load_markets()
    market = exchange.market(args.symbol)
    if not market.get("contract") or market.get("linear") is False:
        parser.error("An active linear futures market is required")
    if market.get("active") is False:
        parser.error("The futures market is inactive")
    for timeframe in ("15m", "4h", "1d"):
        if timeframe not in exchange.timeframes:
            parser.error(f"Unsupported timeframe: {timeframe}")

    identity = [
        args.symbol,
        args.capital,
        args.leverage,
        args.risk,
        args.daily_loss_r,
        args.fee,
        args.slippage,
        args.funding_buffer,
        args.volume_multiplier,
        args.allow_short,
    ]
    state = PaperState(args.state, identity, args.capital)
    while True:
        paper_step(exchange, state, args, strategy_config, risk_config)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except (RegionRestricted, ccxt.BaseError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
