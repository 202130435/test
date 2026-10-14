import ccxt
import pytest
from connectivity import SpotExchange, RegionRestricted, public_read


def test_public_timeout_fallback_preserves_private_url():
    exchange = SpotExchange()
    original_public = exchange.urls['api']['public']
    original_private = exchange.urls['api']['private']
    visited = []
    def operation():
        visited.append(exchange.urls['api']['public'])
        if len(visited) == 1:
            raise ccxt.RequestTimeout('timeout')
        return [1, 2]
    assert public_read(exchange, operation) == [1, 2]
    assert len(visited) == 2
    assert 'api1.binance.com' in visited[1]
    assert exchange.urls['api']['public'] == original_public
    assert exchange.urls['api']['private'] == original_private


@pytest.mark.parametrize('code', [451, 403, 418, 429])
def test_restriction_never_falls_back(code):
    exchange = SpotExchange()
    calls = []
    def operation():
        calls.append(1)
        exchange.handle_http_status_code(code, 'denied', '', 'GET', '')
    with pytest.raises((RegionRestricted, ccxt.PermissionDenied)):
        public_read(exchange, operation)
    assert len(calls) == 1


def test_attempts_are_bounded():
    exchange = SpotExchange()
    calls = []
    def operation():
        calls.append(1)
        raise ccxt.ExchangeNotAvailable('503')
    with pytest.raises(RuntimeError, match='All Binance public'):
        public_read(exchange, operation)
    assert len(calls) == 4


def test_invalid_request_is_not_retried():
    exchange = SpotExchange()
    def operation():
        raise ccxt.BadSymbol('invalid')
    with pytest.raises(ccxt.BadSymbol):
        public_read(exchange, operation)
