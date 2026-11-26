"""Binance USDT-M futures paper trader. It never submits an order."""

import argparse
from dataclasses import asdict, replace
import copy
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
from futures_risk import DailyRiskState, RiskConfig, build_trade_plan, validate_liquidation_buffer
from futures_strategy import Action, Position, StrategyConfig, atr, decide
from futures_system import plan_entry
from market_evidence import relative_volume_confirmed
from trade_checklist import ChecklistAssessment
from state_resources import cleanup_on_error

TF_MS = {'15m': 900_000, '4h': 14_400_000, '1d': 86_400_000}


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
    @cleanup_on_error
    def __init__(self, path, identity, capital):
        self.lock = open(str(path) + ".lock", "a")
        try:
            portalocker.lock(self.lock, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException:
            self.lock.close()
            raise RuntimeError("Another futures paper bot uses this state database")
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS funding_payments (
            opened_at INTEGER, timestamp INTEGER, rate REAL, mark_price REAL, pnl REAL,
            PRIMARY KEY(opened_at,timestamp))''')
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
        if 'funding_pnl' not in {r[1] for r in self.db.execute('PRAGMA table_info(trades)')}:
            self.db.execute('ALTER TABLE trades ADD COLUMN funding_pnl REAL NOT NULL DEFAULT 0')
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
                gross_pnl,fees,net_pnl,reason,funding_pnl
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
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
                trade.get('funding_pnl', 0.0),
            ),
        )


def public_read(method, *args, **kwargs):
    for attempt in range(3):
        try:
            return method(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def closed_candles(exchange, symbol, timeframe, limit):
    candles = public_read(exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
    now = exchange.milliseconds()
    candles = [c for c in candles if int(c[0]) + TF_MS[timeframe] <= now]
    if len(candles) < 2:
        raise ValueError(f"Insufficient {timeframe} candles")
    if now - (int(candles[-1][0]) + TF_MS[timeframe]) >= TF_MS[timeframe]:
        raise ccxt.NetworkError(f'Stale {timeframe} candles; skipping cycle')
    if any(int(b[0]) - int(a[0]) != TF_MS[timeframe] for a, b in zip(candles, candles[1:])):
        raise ccxt.NetworkError(f'Non-contiguous {timeframe} candles')
    return candles


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
    try:
        value = public_read(exchange.fetch_open_interest, symbol)
    except (ccxt.NetworkError, ccxt.ExchangeNotAvailable):
        return False, None
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
    if 'price' not in values:
        values['price'] = next((values[k] for k in ('mark_price', 'exit_price', 'entry_price') if k in values), None)
    payload = {"event": event, **values}
    state.record_event(candle, event, payload)
    _emit(**payload)


def _save_candle(state, daily_state, stamp, mark_price, risk_config):
    equity = current_equity(state.data, mark_price)
    daily_state = replace(daily_state, marked_equity_pnl=equity - daily_state.session_start_equity)
    if equity <= 0:
        state.data['bankrupt'] = True
    if state.data.get('bankrupt'):
        daily_state = replace(daily_state, halted=True, halt_reason='account insolvent')
    elif daily_state.consumed_loss >= daily_state.daily_loss_limit(risk_config):
        daily_state = replace(daily_state, halted=True, halt_reason='daily loss limit reached')
    state.data['daily'] = asdict(daily_state)
    state.data['candle'] = stamp
    state.save()


def _process_candle(
    exchange,
    state,
    args,
    strategy_config,
    risk_config,
    assessment_builder=automatic_assessment,
    *, daily, four_hour, trigger, allow_entry=True,
):
    stamp = int(trigger[-1][0])
    mark_price = float(trigger[-1][4])
    if stamp <= state.data["candle"]:
        _emit(event="already_processed", candle=stamp)
        return

    equity = current_equity(state.data, mark_price)
    session_id = candle_session_id(stamp + TF_MS['15m'] - 1)
    # A new KST session starts at the opening equity of its first candle.
    opening_equity = current_equity(state.data, float(trigger[-1][1]))
    if opening_equity <= 0 or equity <= 0 or state.data.get('bankrupt'):
        state.data['bankrupt'] = True
    daily_state = _daily_state(state.data, session_id, max(opening_equity, 1e-12))
    stored = state.data['position']
    stop_crossed = stored and (float(trigger[-1][3]) <= stored['stop_price'] if stored['side'] == 'long'
                              else float(trigger[-1][2]) >= stored['stop_price'])
    funding_delta = settle_funding(exchange, state, args, trigger[-1], exit_at_open=bool(stop_crossed))
    equity = current_equity(state.data, mark_price)
    daily_state = replace(daily_state, marked_equity_pnl=equity - daily_state.session_start_equity,
                          funding_pnl=daily_state.funding_pnl + funding_delta)
    position = _position(state.data)
    decision = decide(daily, four_hour, trigger, position, strategy_config)
    if position.side != 'flat':
        liquidation = state.data['position'].get('liquidation_price')
        touched = liquidation and (float(trigger[-1][3]) <= liquidation if position.side == 'long'
                                   else float(trigger[-1][2]) >= liquidation)
        if touched and decision.reasons != ('protective stop crossed',):
            decision = replace(decision, action=Action.EXIT_LONG if position.side == 'long' else Action.EXIT_SHORT,
                               reasons=('approximate liquidation crossed',))

    if decision.action in {Action.EXIT_LONG, Action.EXIT_SHORT}:
        stored = state.data["position"]
        side = stored["side"]
        if decision.reasons == ("protective stop crossed",):
            candle_open = float(trigger[-1][1])
            stop = float(stored["stop_price"])
            base_exit = min(candle_open, stop) if side == "long" else max(candle_open, stop)
        elif decision.reasons == ('approximate liquidation crossed',):
            base_exit = float(stored['liquidation_price'])
        else:
            base_exit = mark_price
        exit_price = paper_fill(base_exit, side, False, args.slippage)
        quantity = float(stored["quantity"])
        direction = 1 if side == "long" else -1
        gross_pnl = (exit_price - float(stored["entry_price"])) * quantity * direction
        exit_fee = exit_price * quantity * args.fee
        total_fees = float(stored["entry_fee"]) + exit_fee
        state.data["balance"] += gross_pnl - exit_fee
        daily_state = replace(daily_state, marked_equity_pnl=state.data['balance'] - daily_state.session_start_equity)
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
            "net_pnl": gross_pnl - total_fees + stored.get('funding_pnl', 0.0),
            'funding_pnl': stored.get('funding_pnl', 0.0),
            "opened_at": stored["opened_at"],
            "closed_at": stamp,
            "reason": decision.reasons[0],
        }
        state.data["trades"] = (state.data["trades"] + [trade])[-100:]
        state.data["position"] = None
        state.record_trade(trade)
        _record_event(state, stamp, "exit", balance=state.data["balance"], **trade)
    elif decision.action in {Action.HOLD_LONG, Action.HOLD_SHORT}:
        rounded_stop = float(exchange.price_to_precision(args.symbol, decision.stop_price))
        state.data["position"]["stop_price"] = (max(position.stop_price, rounded_stop)
            if position.side == 'long' else min(position.stop_price, rounded_stop))
        _record_event(
            state,
            stamp,
            "hold",
            side=position.side,
            mark_price=mark_price,
            stop_price=state.data['position']['stop_price'],
            equity=equity,
            reasons=decision.reasons,
        )
    elif decision.action in {Action.ENTER_LONG, Action.ENTER_SHORT}:
        if not allow_entry or state.data.get('bankrupt') or state.data.get('recovery_halt'):
            _record_event(state, stamp, 'blocked', mark_price=mark_price,
                          reasons=['historical replay, unrecoverable gap or insolvent account: entry disabled'])
            _save_candle(state, daily_state, stamp, mark_price, risk_config)
            return
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
            entry_price = paper_fill(mark_price, side, True, args.slippage)
            try:
                stop_price = float(exchange.price_to_precision(args.symbol, result.trade_plan.stop_price))
                plan = build_trade_plan(side, entry_price, stop_price, equity,
                                        args.leverage, daily_state, risk_config, decision.risk_multiplier)
                quantity = float(exchange.amount_to_precision(args.symbol, plan.quantity))
                validate_paper_order(exchange, args.symbol, quantity, entry_price)
                entry_fee = entry_price * quantity * args.fee
                if entry_price * quantity / args.leverage + entry_fee > state.data['balance']:
                    raise ValueError('insufficient free margin including entry fee')
                # Approximation only: maintenance margin 0.5%; no exchange tier simulation.
                liquidation = entry_price * (1 - 1 / args.leverage) / 0.995 if side == 'long' else entry_price * (1 + 1 / args.leverage) / 1.005
                if not validate_liquidation_buffer(side, entry_price, plan.stop_price, liquidation):
                    raise ValueError('insufficient liquidation buffer')
            except (ValueError, RuntimeError, ccxt.InvalidOrder) as error:
                _record_event(state, stamp, 'blocked', mark_price=mark_price, reasons=[str(error)])
                _save_candle(state, daily_state, stamp, mark_price, risk_config)
                return
            state.data["balance"] -= entry_fee
            state.data["position"] = {
                "side": side,
                "entry_price": entry_price,
                "stop_price": plan.stop_price,
                "quantity": quantity,
                "entry_fee": entry_fee,
                "opened_at": stamp,
                'funding_after': stamp + TF_MS['15m'],
                'funding_pnl': 0.0,
                'liquidation_price': liquidation,
            }
            _record_event(
                state,
                stamp,
                "entry",
                side=side,
                entry_price=entry_price,
                stop_price=plan.stop_price,
                quantity=quantity,
                risk_budget=plan.risk_budget,
                open_interest=open_interest,
                reasons=decision.reasons,
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

    _save_candle(state, daily_state, stamp, mark_price, risk_config)


def validate_paper_order(exchange, symbol, quantity, price):
    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError('invalid rounded quantity')
    market = exchange.market(symbol)
    contract_size = market.get('contractSize') or 1
    if contract_size != 1:
        raise ValueError('paper executor supports unit-size linear contracts only')
    for kind, value in (('amount', quantity), ('cost', quantity * price)):
        limits = (market.get('limits', {}).get(kind) or {})
        if limits.get('min') is not None and value < limits['min']:
            raise ValueError(f'below exchange minimum {kind}')
        if limits.get('max') is not None and value > limits['max']:
            raise ValueError(f'above exchange maximum {kind}')


def settle_funding(exchange, state, args, candle, exit_at_open=False):
    stored = state.data['position']
    if not stored:
        return 0.0
    # Legacy positions start at their actual entry close; retain a durable cursor.
    after = stored.get('funding_after', stored['opened_at'] + TF_MS['15m'])
    # Intrabar stop time is unknown; assume earliest execution and exclude later funding.
    end = int(candle[0]) if exit_at_open else int(candle[0]) + TF_MS['15m']
    if end <= after:
        return 0.0
    total = 0.0
    cursor = after + 1
    while cursor <= end:
        rates = public_read(exchange.fetch_funding_rate_history, args.symbol, since=cursor,
                            limit=1000, params={'endTime': end})
        rates = sorted((r for r in rates if cursor <= r['timestamp'] <= end), key=lambda r: r['timestamp'])
        for item in rates:
            rate = float(item['fundingRate'])
            mark = float(item.get('markPrice') or (item.get('info') or {}).get('markPrice') or candle[1])
            if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise ValueError('invalid funding history')
            pnl = -rate * mark * stored['quantity'] * (1 if stored['side'] == 'long' else -1)
            inserted = state.db.execute('INSERT OR IGNORE INTO funding_payments VALUES (?,?,?,?,?)',
                (stored['opened_at'], item['timestamp'], rate, mark, pnl)).rowcount
            if inserted:
                total += pnl
        if len(rates) < 1000:
            break
        cursor = rates[-1]['timestamp'] + 1
    state.data['balance'] += total
    stored['funding_pnl'] = stored.get('funding_pnl', 0.0) + total
    stored['funding_after'] = end
    return total


def paper_step(exchange, state, args, strategy_config, risk_config,
               assessment_builder=automatic_assessment):
    daily = closed_candles(exchange, args.symbol, '1d', 1000)
    four_hour = closed_candles(exchange, args.symbol, '4h', 1000)
    trigger = closed_candles(exchange, args.symbol, '15m', 1000)
    last = state.data['candle']
    pending = [i for i, c in enumerate(trigger) if c[0] > last]
    if not pending:
        _emit(event='already_processed', candle=trigger[-1][0])
        return
    if not last:
        pending = [len(trigger) - 1]
    elif trigger[pending[0]][0] > last + TF_MS['15m']:
        state.data['recovery_halt'] = True
        _record_event(state, last + 1, 'gap_warning', reasons=['history window exceeded; new entries disabled'],
                      missing_from=last + TF_MS['15m'], available_from=trigger[pending[0]][0])
        state.save()
    for index in pending:
        end = int(trigger[index][0]) + TF_MS['15m']
        d = [c for c in daily if c[0] + TF_MS['1d'] <= end]
        h = [c for c in four_hour if c[0] + TF_MS['4h'] <= end]
        if not d or not h or end - (d[-1][0] + TF_MS['1d']) >= TF_MS['1d'] or end - (h[-1][0] + TF_MS['4h']) >= TF_MS['4h']:
            raise ccxt.NetworkError('Regime candles are not aligned with trigger close')
        required = max(strategy_config.trigger_slow + 1, strategy_config.atr_period + 1,
                       strategy_config.swing_lookback, 21)
        if len(d) < strategy_config.regime_slow + 1 or len(h) < strategy_config.regime_slow + 1 or index + 1 < required:
            # Never silently skip a candle that could have stopped an open position.
            raise ccxt.NetworkError('Insufficient aligned history for replay; collect older history before resuming')
        before = copy.deepcopy(state.data)
        try:
            _process_candle(exchange, state, args, strategy_config, risk_config, assessment_builder,
                            daily=d, four_hour=h, trigger=trigger[:index + 1],
                            allow_entry=index == len(trigger) - 1)
        except BaseException:
            state.db.rollback()
            state.data = before
            raise


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
    public_read(exchange.load_markets)
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
    try:
        while True:
            try:
                paper_step(exchange, state, args, strategy_config, risk_config)
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as error:
                _emit(event='network_skip', reason=type(error).__name__)
                if args.once:
                    raise
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        state.db.close()
        state.lock.close()


if __name__ == "__main__":
    try:
        main()
    except (RegionRestricted, ccxt.BaseError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
