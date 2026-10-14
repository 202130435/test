"""Binance spot SMA bot. Defaults to local paper execution."""
import argparse
import json
import os
import sqlite3
import time
import portalocker
import math

import ccxt
from connectivity import SpotExchange, RegionRestricted, public_read


def signal(candles, fast, slow):
    if len(candles) < slow + 1:
        return None
    closes = [float(c[4]) for c in candles]
    def above(end):
        values = closes[:end] if end else closes
        return sum(values[-fast:]) / fast > sum(values[-slow:]) / slow
    previous, current = above(-1), above(0)
    return 'buy' if current and not previous else 'sell' if previous and not current else None


def order_amount(exchange, symbol, budget, price):
    market = exchange.market(symbol)
    amount = float(exchange.amount_to_precision(symbol, budget / price))
    limits = market.get('limits', {})
    for kind, value in [('amount', amount), ('cost', amount * price)]:
        bounds = limits.get(kind) or {}
        if bounds.get('min') is not None and value < bounds['min']:
            raise ValueError(f'Order below minimum {kind}')
        if bounds.get('max') is not None and value > bounds['max']:
            raise ValueError(f'Order above maximum {kind}')
    if amount <= 0 or amount * price > budget * 1.000001:
        raise ValueError('Invalid order amount')
    return amount


class State:
    def __init__(self, path, identity, cash):
        self.lock = open(str(path) + '.lock', 'a')
        try:
            portalocker.lock(self.lock, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException:
            self.lock.close()
            raise RuntimeError('Another bot uses this state database')
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT)')
        row = self.db.execute('SELECT data FROM state WHERE id=1').fetchone()
        self.data = json.loads(row[0]) if row else dict(identity=identity, cash=cash, position=0, candle=0, pending=None)
        if self.data['identity'] != identity:
            raise ValueError('State belongs to another mode/symbol/strategy; use another database')
        if self.data['pending']:
            raise RuntimeError('Unresolved order: reconcile it with Binance before resuming; do not blindly retry')
        self.save()
    def save(self):
        self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (json.dumps(self.data),))
        self.db.commit()


def step(exchange, state, args):
    candles = public_read(exchange, exchange.fetch_ohlcv, args.symbol, args.timeframe, limit=args.slow + 2)
    # Ignore the final, still-forming candle.
    closed = candles[:-1]
    if len(closed) < args.slow + 1:
        raise ValueError('Insufficient closed candles')
    stamp, price = closed[-1][0], float(closed[-1][4])
    if stamp <= state.data['candle']:
        return
    side = signal(closed, args.fast, args.slow)
    d = state.data
    if (side == 'buy' and d['position'] == 0) or (side == 'sell' and d['position'] > 0):
        if side == 'buy':
            budget = min(args.quote_amount, d['cash'] / (1 + args.fee))
            if args.live:
                balance = exchange.fetch_balance()
                budget = min(budget, float(balance['free'].get(exchange.market(args.symbol)['quote'], 0)) / (1 + args.fee))
            amount = order_amount(exchange, args.symbol, budget, price)
        else:
            amount = float(exchange.amount_to_precision(args.symbol, d['position']))
            order_amount(exchange, args.symbol, amount * price, price)
            if args.live:
                free = float(exchange.fetch_balance()['free'].get(exchange.market(args.symbol)['base'], 0))
                if free < amount:
                    raise ValueError('Insufficient base balance; reconcile position')
        client_id = f'spotbot-{stamp}-{side}'
        if args.live:
            # Durable intent precedes submission. Any uncertain result halts restart.
            d['pending'] = client_id
            state.save()
            order = exchange.create_order(args.symbol, 'market', side, amount, None, {'newClientOrderId': client_id})
            if order.get('status') != 'closed' or not order.get('filled') or order.get('cost') is None:
                raise RuntimeError('Order not conclusively filled; reconcile pending order')
            filled, cost = float(order['filled']), float(order['cost'])
            fees = order.get('fees') or ([order['fee']] if order.get('fee') else [])
            base_fee = sum(float(f['cost']) for f in fees if f.get('currency') == exchange.market(args.symbol)['base'])
            quote_fee = sum(float(f['cost']) for f in fees if f.get('currency') == exchange.market(args.symbol)['quote'])
        else:
            filled, cost, base_fee, quote_fee = amount, amount * price, 0, amount * price * args.fee
        if side == 'buy':
            d['cash'] -= cost + quote_fee
            d['position'] += filled - base_fee
        else:
            d['cash'] += cost - quote_fee
            d['position'] -= filled + base_fee
            if abs(d['position']) < 1e-12:
                d['position'] = 0
        d['pending'] = None
        print(json.dumps(dict(side=side, amount=filled, cost=cost, mode='live' if args.live else 'paper')), flush=True)
    d['candle'] = stamp
    state.save()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbol', default='BTC/USDT')
    p.add_argument('--timeframe', default='1m')
    p.add_argument('--fast', type=int, default=5)
    p.add_argument('--slow', type=int, default=20)
    p.add_argument('--quote-amount', type=float, default=10)
    p.add_argument('--capital', type=float, default=100)
    p.add_argument('--fee', type=float, default=0.001)
    p.add_argument('--timeout-ms', type=int, default=3500)
    p.add_argument('--interval', type=float, default=15)
    p.add_argument('--state', default='paper.sqlite3')
    p.add_argument('--once', action='store_true')
    p.add_argument('--live', action='store_true')
    p.add_argument('--confirm-live', default='')
    args = p.parse_args()
    if not 100 <= args.timeout_ms <= 60000:
        p.error('Timeout must be between 100 and 60000 milliseconds')
    if not all(math.isfinite(v) for v in [args.quote_amount, args.capital, args.fee, args.interval]):
        p.error('Numeric options must be finite')
    if not 0 < args.fast < args.slow <= 500 or not 0 < args.quote_amount <= args.capital or not 0 <= args.fee < 1 or args.interval < 1:
        p.error('Invalid strategy, capital, fee or interval')
    if args.live and (args.confirm_live != 'BINANCE_SPOT_REAL_ORDERS' or args.state == 'paper.sqlite3'):
        p.error('Live mode requires --confirm-live BINANCE_SPOT_REAL_ORDERS and a separate --state')
    config = {'enableRateLimit': True, 'timeout': args.timeout_ms, 'maxRetriesOnFailure': 0, 'options': {'defaultType': 'spot', 'fetchMarkets': {'types': ['spot']}, 'createMarketBuyOrderRequiresPrice': False}}
    if args.live:
        if not all(os.getenv(k) for k in ['BINANCE_API_KEY', 'BINANCE_API_SECRET']):
            p.error('Missing Binance credentials in secure environment variables')
        config.update(apiKey=os.environ['BINANCE_API_KEY'], secret=os.environ['BINANCE_API_SECRET'])
    exchange = SpotExchange(config)
    exchange.session.trust_env = True
    public_read(exchange, exchange.load_markets)
    market = exchange.market(args.symbol)
    if not market.get('spot') or market.get('active') is False:
        p.error('An active spot market is required')
    if args.timeframe not in exchange.timeframes:
        p.error('Unsupported timeframe')
    identity = [args.live, args.symbol, args.timeframe, args.fast, args.slow, args.capital, args.quote_amount, args.fee]
    state = State(args.state, identity, args.capital)
    while True:
        step(exchange, state, args)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    try:
        main()
    except (RegionRestricted, ccxt.BaseError, RuntimeError) as error:
        if isinstance(error, (RegionRestricted, RuntimeError)):
            print(str(error), file=__import__('sys').stderr)
        else:
            print(f'Binance operation failed ({type(error).__name__}); stopped. Check network/account and reconcile any pending order.', file=__import__('sys').stderr)
        raise SystemExit(1)
