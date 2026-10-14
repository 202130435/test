"""Bounded failover for public reads only; private orders are never retried."""
import sys

import ccxt

PUBLIC_HOSTS = ('api.binance.com', 'api1.binance.com', 'api2.binance.com', 'api3.binance.com')


class RegionRestricted(RuntimeError):
    pass


class SpotExchange(ccxt.binance):
    def handle_http_status_code(self, code, reason, url, method, body):
        if code == 451:
            raise RegionRestricted(
                'Binance HTTP 451: this execution location is restricted. '
                'No alternate host or order retry was attempted. '
                'Verify eligibility and contact Binance support.'
            )
        if code in (403, 418, 429):
            raise ccxt.PermissionDenied(f'Binance HTTP {code}: access denied or rate limited; stopping')
        return super().handle_http_status_code(code, reason, url, method, body)


def public_read(exchange, operation, *args, **kwargs):
    original = exchange.urls['api']['public']
    try:
        for index, host in enumerate(PUBLIC_HOSTS):
            exchange.urls['api']['public'] = f'https://{host}/api/v3'
            try:
                return operation(*args, **kwargs)
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
                raise
            except ccxt.NetworkError:
                if index == len(PUBLIC_HOSTS) - 1:
                    raise RuntimeError('All Binance public hosts failed; no order submitted by this read') from None
                print(f'Public read unavailable at {host}; trying next host', file=sys.stderr)
    finally:
        exchange.urls['api']['public'] = original
