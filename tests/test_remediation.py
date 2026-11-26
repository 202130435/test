"""Regression coverage for the November code-review report."""

from dataclasses import asdict
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import ccxt
import pytest
from fastapi.testclient import TestClient

import bot
import futures_paper as paper
import paper_data
import web_app
from futures_risk import DailyRiskState, RiskConfig
from futures_strategy import Position, StrategyConfig
from test_futures_paper import Exchange, args, complete_assessment, turn_down
from test_bot import Exchange as SpotExchange, args as spot_args


def entered(tmp_path, exchange=None):
    exchange = exchange or Exchange()
    state = paper.PaperState(tmp_path / 'btc.sqlite3', ['test'], 1000)
    paper.paper_step(exchange, state, args(), StrategyConfig(), RiskConfig(), complete_assessment)
    return exchange, state


def advance(exchange, price=111, low=109, high=112, opening=110):
    stamp = exchange.trigger[-1][0] + 900_000
    exchange.trigger.append([stamp, opening, high, low, price, 2])
    return stamp


def run(exchange, state):
    paper.paper_step(exchange, state, args(), StrategyConfig(), RiskConfig(), complete_assessment)


def test_actual_paper_database_in_http_dashboard(tmp_path, monkeypatch):
    exchange, state = entered(tmp_path)
    monkeypatch.setattr(web_app, 'discover_databases', lambda *a: [tmp_path / 'btc.sqlite3'])
    with TestClient(web_app.app) as client:
        response = client.get('/api/dashboard')
        event = response.json()['accounts'][0]['recent_events'][0]['payload']
        assert event['price'] == state.data['position']['entry_price']
        assert event['reasons']
        page = client.get('/')
        csp = page.headers['content-security-policy']
        assert 'unsafe-inline' not in csp
        assert 'nonce-' in csp and '__CSP_NONCE__' not in page.text
        assert page.headers['x-content-type-options'] == 'nosniff'
        assert client.get('/openapi.json').status_code == 404
        assert client.get('/health').json()['status'] == 'ok'
    state.db.close()
    state.lock.close()


def test_replay_checks_intermediate_stop_not_only_latest_close(tmp_path):
    exchange, state = entered(tmp_path)
    stop = state.data['position']['stop_price']
    first = advance(exchange, price=111, low=100)
    advance(exchange, price=112, low=110, high=113, opening=111)
    run(exchange, state)
    trade = state.data['trades'][0]
    assert trade['closed_at'] == first
    assert trade['exit_price'] == pytest.approx(stop * (1 - args().slippage))
    assert state.data['candle'] == exchange.trigger[-1][0]


def test_breakeven_stop_survives_restart(tmp_path):
    exchange, state = entered(tmp_path)
    state.data['position']['stop_price'] = state.data['position']['entry_price']
    state.save()
    state.db.close()
    state.lock.close()
    state = paper.PaperState(tmp_path / 'btc.sqlite3', ['test'], 1000)
    assert paper._position(state.data).side == 'long'
    advance(exchange)
    run(exchange, state)
    assert state.data['position'] is None
    assert Position('short', 100, 100).stop_price == 100


@pytest.mark.parametrize('rate', [.001, -.001])
def test_funding_cashflow_once_and_export(tmp_path, rate):
    exchange, state = entered(tmp_path)
    balance = state.data['balance']
    quantity = state.data['position']['quantity']
    stamp = advance(exchange)
    exchange.fetch_funding_rate_history = lambda *a, **kw: [
        {'timestamp': stamp + 900_000, 'fundingRate': rate, 'info': {'markPrice': '111'}}]
    run(exchange, state)
    assert state.data['balance'] == pytest.approx(balance - rate * 111 * quantity)
    assert state.data['daily']['funding_pnl'] == pytest.approx(-rate * 111 * quantity)
    run(exchange, state)
    assert state.db.execute('SELECT COUNT(*) FROM funding_payments').fetchone()[0] == 1
    paper_data.export_database(tmp_path / 'btc.sqlite3', tmp_path / 'export')
    assert '111.0' in (tmp_path / 'export' / 'funding.csv').read_text()


def test_funding_failure_rolls_back_candle_and_cash(tmp_path, monkeypatch):
    exchange, state = entered(tmp_path)
    before = json.dumps(state.data, sort_keys=True)
    advance(exchange)
    def fail(*a, **kw):
        raise ccxt.RequestTimeout('temporary')
    exchange.fetch_funding_rate_history = fail
    monkeypatch.setattr(paper.time, 'sleep', lambda _: None)
    with pytest.raises(ccxt.RequestTimeout):
        run(exchange, state)
    assert json.dumps(state.data, sort_keys=True) == before
    assert state.db.execute('SELECT COUNT(*) FROM funding_payments').fetchone()[0] == 0


def test_exchange_minimum_blocks_paper_entry(tmp_path):
    exchange = Exchange()
    exchange.market = lambda _: {'limits': {'cost': {'min': 10_000}}}
    _, state = entered(tmp_path, exchange)
    assert state.data['position'] is None
    payload = json.loads(state.db.execute('SELECT payload FROM events').fetchone()[0])
    assert 'minimum cost' in payload['reasons'][0]


def test_insufficient_margin_includes_entry_fee(tmp_path):
    exchange = Exchange()
    state = paper.PaperState(tmp_path / 'btc.sqlite3', ['test'], 1000)
    risk = RiskConfig(risk_fraction=.9)
    paper.paper_step(exchange, state, args(), StrategyConfig(), risk, complete_assessment)
    assert state.data['position'] is None
    assert 'margin' in json.loads(state.db.execute('SELECT payload FROM events').fetchone()[0])['reasons'][0]


def test_gap_exit_uses_open_when_worse_than_stop(tmp_path):
    exchange, state = entered(tmp_path)
    advance(exchange, price=90, low=89, high=91, opening=90)
    run(exchange, state)
    assert state.data['trades'][0]['exit_price'] == pytest.approx(90 * (1 - args().slippage))


def test_insolvency_survives_session_reset(tmp_path):
    exchange, state = entered(tmp_path)
    state.data['balance'] = 1
    state.data['position']['quantity'] = 100
    advance(exchange, price=1, low=.5, high=2, opening=1)
    run(exchange, state)
    assert state.data['bankrupt']
    assert state.data['daily']['halted']
    state.data['daily']['session_id'] = '2000-01-01'
    advance(exchange, price=1, low=.5, high=2, opening=1)
    run(exchange, state)
    assert state.data['daily']['halt_reason'] == 'account insolvent'


def test_mtm_loss_limit_latches_even_if_price_recovers(tmp_path):
    exchange, state = entered(tmp_path)
    old_session = paper.candle_session_id(exchange.trigger[-1][0])
    state.data['daily'] = asdict(DailyRiskState(old_session, 1020))
    advance(exchange)
    run(exchange, state)
    assert state.data['daily']['halted']
    assert state.data['daily']['marked_equity_pnl'] == pytest.approx(
        paper.current_equity(state.data, 111) - 1020)


def test_new_session_does_not_charge_old_entry_fee_twice(tmp_path):
    exchange, state = entered(tmp_path)
    state.data['daily']['session_id'] = '2000-01-01'
    old_balance = state.data['balance']
    entry = state.data['position']['entry_price']
    quantity = state.data['position']['quantity']
    advance(exchange, opening=110, price=90, low=89, high=111)
    run(exchange, state)
    opening_equity = old_balance + (110 - entry) * quantity
    assert state.data['daily']['session_start_equity'] == pytest.approx(opening_equity)
    assert state.data['daily']['marked_equity_pnl'] == pytest.approx(state.data['balance'] - opening_equity)


@pytest.mark.parametrize('status', [403, 418, 429, 451])
def test_restricted_status_never_retries(status, monkeypatch):
    exchange = paper.FuturesExchange()
    count = []
    def blocked():
        count.append(1)
        exchange.handle_http_status_code(status, '', '', 'GET', '')
    with pytest.raises((ccxt.PermissionDenied, paper.RegionRestricted)):
        paper.public_read(blocked)
    assert len(count) == 1


def test_public_read_bounded_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(paper.time, 'sleep', calls.append)
    def fail():
        raise ccxt.RequestTimeout('temporary')
    with pytest.raises(ccxt.RequestTimeout):
        paper.public_read(fail)
    assert calls == [1, 2]


def test_failed_initialization_releases_lock(tmp_path):
    path = tmp_path / 'btc.sqlite3'
    state = paper.PaperState(path, ['a'], 1000)
    state.db.close()
    state.lock.close()
    with pytest.raises(ValueError):
        paper.PaperState(path, ['b'], 1000)
    state = paper.PaperState(path, ['a'], 1000)
    state.db.close()
    state.lock.close()


def test_read_and_backup_while_writer_has_uncommitted_changes(tmp_path):
    exchange, state = entered(tmp_path)
    balance = state.data['balance']
    state.db.execute('UPDATE state SET data=? WHERE id=1', (json.dumps({**state.data, 'balance': 123}),))
    with ThreadPoolExecutor() as pool:
        read = pool.submit(web_app.read_dashboard_database, tmp_path / 'btc.sqlite3')
        backup = pool.submit(paper_data.backup_database, tmp_path / 'btc.sqlite3', tmp_path / 'backup.sqlite3')
        assert read.result(timeout=10)['balance'] == balance
        backup.result(timeout=10)
    assert paper_data.read_status(tmp_path / 'backup.sqlite3')['balance'] == balance
    state.db.rollback()


def test_stale_healthcheck_returns_failure(tmp_path, monkeypatch):
    entered(tmp_path)
    monkeypatch.setattr('sys.argv', ['paper_data.py', 'status', '--state', str(tmp_path / 'btc.sqlite3'), '--max-age-seconds', '1800'])
    with pytest.raises(SystemExit) as error:
        paper_data.main()
    assert error.value.code == 1


def test_flat_sma_does_not_trigger_sell():
    assert bot.signal([[0, 0, 0, 0, v] for v in [8, 9, 10, 8]], 2, 3) is None


def test_spot_dust_allows_next_buy_and_never_negative(tmp_path, monkeypatch):
    exchange = SpotExchange()
    state = bot.State(tmp_path / 'spot.db', ['live'], 100)
    state.data['position'] = .5009
    exchange.create_order = lambda *a: {'status': 'closed', 'filled': .5, 'cost': 10, 'fees': [{'currency': 'BTC', 'cost': .0001}]}
    monkeypatch.setattr(bot, 'signal', lambda *a: 'sell')
    bot.step(exchange, state, spot_args(True))
    assert state.data['position'] == 0
    assert state.data['dust'] == pytest.approx(.0008)
    state.data['candle'] = 0
    monkeypatch.setattr(bot, 'signal', lambda *a: 'buy')
    bot.step(exchange, state, spot_args(True))
    assert state.data['position'] > 0
    state.data['position'] = .5
    state.data['candle'] = 0
    exchange.create_order = lambda *a: {'status': 'closed', 'filled': .5, 'cost': 10, 'fees': [{'currency': 'BTC', 'cost': .01}]}
    monkeypatch.setattr(bot, 'signal', lambda *a: 'sell')
    bot.step(exchange, state, spot_args(True))
    assert state.data['position'] == 0


@pytest.mark.parametrize('order', [{'status': 'open', 'filled': .1, 'cost': 2}, {'status': 'closed', 'filled': .5, 'cost': None}])
def test_spot_inconclusive_fill_keeps_pending(tmp_path, order):
    state = bot.State(tmp_path / 'spot.db', ['live'], 100)
    exchange = SpotExchange()
    exchange.create_order = lambda *a: order
    with pytest.raises(RuntimeError, match='conclusively'):
        bot.step(exchange, state, spot_args(True))
    assert state.data['pending']
    state.db.close()
    state.lock.close()
    with pytest.raises(RuntimeError, match='Unresolved'):
        bot.State(tmp_path / 'spot.db', ['live'], 100)


def test_short_entry_reduced_risk_and_gap_exit(tmp_path):
    class ShortExchange(Exchange):
        def fetch_ohlcv(self, symbol, timeframe, limit):
            values = super().fetch_ohlcv(symbol, timeframe, limit)
            if timeframe != '15m':
                return [[c[0], 300-c[1], 300-c[3], 300-c[2], 300-c[4], c[5]] for c in values]
            return values
    exchange = ShortExchange()
    exchange.trigger = turn_down(exchange.trigger[0][0])
    state = paper.PaperState(tmp_path / 'short.sqlite3', ['short'], 1000)
    config = StrategyConfig(allow_short=True)
    paper.paper_step(exchange, state, args(), config, RiskConfig(), complete_assessment)
    assert state.data['position']['side'] == 'short'
    assert state.data['position']['entry_price'] == pytest.approx(90 * (1 - args().slippage))
    event = json.loads(state.db.execute('SELECT payload FROM events').fetchone()[0])
    assert event['risk_budget'] == 1.25
    advance(exchange, price=100, low=99, high=101, opening=100)
    paper.paper_step(exchange, state, args(), config, RiskConfig(), complete_assessment)
    assert state.data['trades'][0]['exit_price'] == pytest.approx(100 * (1 + args().slippage))


def test_stale_candles_and_temporary_oi_failure_block_safely(monkeypatch):
    exchange = Exchange()
    now = exchange.milliseconds()
    exchange.milliseconds = lambda: now + 3_600_000
    with pytest.raises(ccxt.NetworkError, match='Stale'):
        paper.closed_candles(exchange, args().symbol, '15m', 1000)
    monkeypatch.setattr(paper.time, 'sleep', lambda _: None)
    def fail(*a):
        raise ccxt.RequestTimeout()
    exchange.fetch_open_interest = fail
    assert paper.open_interest_reviewed(exchange, args().symbol) == (False, None)


def test_gap_beyond_available_history_persists_warning(tmp_path):
    exchange, state = entered(tmp_path)
    for candle in exchange.trigger:
        candle[0] += 2000 * 900_000
    with pytest.raises(ccxt.NetworkError, match='history'):
        run(exchange, state)
    assert state.data['recovery_halt']
    assert state.db.execute("SELECT COUNT(*) FROM events WHERE event='gap_warning'").fetchone()[0] == 1


def test_spot_live_cli_requires_confirmation_and_separate_db(monkeypatch):
    for argv in [['bot.py', '--live'], ['bot.py', '--live', '--confirm-live', 'BINANCE_SPOT_REAL_ORDERS']]:
        monkeypatch.setattr('sys.argv', argv)
        with pytest.raises(SystemExit) as error:
            bot.main()
        assert error.value.code == 2


def test_corrupt_database_error_is_sanitized(tmp_path):
    (tmp_path / 'broken.sqlite3').write_text('not a database')
    result = web_app.build_dashboard(tmp_path)
    assert result['errors'] == [{'database': 'broken.sqlite3', 'error': 'database_read_error'}]
