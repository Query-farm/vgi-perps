"""The one ``ccxt`` chokepoint, for the offshore benchmark venues.

Only the public market-data methods in :data:`ALLOWED_METHODS` can be called.
:func:`call` refuses anything else before ccxt is touched, and no exchange is
ever constructed with an API key, so even a mistaken call to an account method
would have no credentials to use. ``tests/test_readonly_guard.py`` asserts this
module is the only one importing ``ccxt``.

Hyperliquid's public market data is served by ``POST /info``. That is a read
query (the request body names the data wanted), sent by ccxt on our behalf; it
is not an order or account endpoint.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import replay

#: Offshore venues this worker knows, keyed by our venue name -> ccxt exchange id.
EXCHANGES: dict[str, str] = {
    "binance": "binanceusdm",
    "bybit": "bybit",
    "hyperliquid": "hyperliquid",
}

#: The only ccxt methods this worker may call: public market data.
ALLOWED_METHODS = frozenset(
    {
        "fetch_markets",
        "fetch_funding_rate_history",
        "fetch_funding_rates",
        "fetch_order_book",
        "fetch_trades",
        "fetch_ohlcv",
        "fetch_tickers",
    }
)

#: Per-exchange constructor options: perpetual swaps only, so loading markets
#: does not pull every spot pair and option chain.
_OPTIONS: dict[str, dict[str, Any]] = {
    "binanceusdm": {"fetchMarkets": {"types": ["linear"]}},
    "bybit": {"fetchMarkets": {"types": ["linear"]}},
    "hyperliquid": {"fetchMarkets": {"types": ["swap"]}},
}

_TIMEOUT_MS = 20_000


class OffshoreError(RuntimeError):
    """A ccxt call failed or was refused."""


_instances: dict[str, Any] = {}
_lock = threading.Lock()


def _exchange(exchange_id: str) -> Any:
    """Return a cached, credential-free ccxt exchange instance."""
    with _lock:
        if exchange_id not in _instances:
            import ccxt

            cls = getattr(ccxt, exchange_id)
            _instances[exchange_id] = cls(
                {"enableRateLimit": True, "timeout": _TIMEOUT_MS, "options": _OPTIONS.get(exchange_id, {})}
            )
        return _instances[exchange_id]


def call_key(exchange_id: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Build the canonical replay key for a ccxt call."""
    return f"CCXT {exchange_id}.{method} {json.dumps([list(args), kwargs], sort_keys=True, default=str)}"


def call(venue: str, method: str, *args: Any, **kwargs: Any) -> Any:
    """Call one allowlisted public ccxt method for ``venue``.

    Args:
        venue: Our venue name (a key of :data:`EXCHANGES`).
        method: ccxt unified method name; must be in :data:`ALLOWED_METHODS`.
        *args: Positional arguments for the ccxt method.
        **kwargs: Keyword arguments for the ccxt method.

    Returns:
        ccxt's unified result (plain JSON-compatible structures).

    Raises:
        OffshoreError: Unknown venue, a method outside the allowlist, a
            recorded failure, or any ccxt error (network, geo-block, bad symbol).

    """
    if venue not in EXCHANGES:
        raise OffshoreError(f"unknown offshore venue {venue!r}; expected one of {sorted(EXCHANGES)}")
    if method not in ALLOWED_METHODS:
        raise OffshoreError(f"ccxt method {method!r} is not an allowlisted public market-data method")
    exchange_id = EXCHANGES[venue]
    key = call_key(exchange_id, method, args, kwargs)
    recorded = replay.lookup(key)
    if recorded is not None:
        if recorded.status >= 400:
            raise OffshoreError(f"{venue} (ccxt {exchange_id}) {method} failed: {recorded.body}")
        return recorded.body
    try:
        result = getattr(_exchange(exchange_id), method)(*args, **kwargs)
    except Exception as exc:  # ccxt raises its own hierarchy; surface one clear error
        replay.record(key, 599, f"{type(exc).__name__}: {str(exc)[:400]}")
        raise OffshoreError(f"{venue} (ccxt {exchange_id}) {method} failed: {type(exc).__name__}: {exc}") from exc
    result = json.loads(json.dumps(result, default=str))
    replay.record(key, 200, result)
    return result
