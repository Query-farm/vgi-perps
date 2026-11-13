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

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
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
    # "hip3" adds the builder-deployed (HIP-3) perp dexes; ccxt's default would also load spot.
    # The dex list is filled in by _options() (each dex is one extra request per market load).
    "hyperliquid": {"fetchMarkets": {"types": ["swap", "hip3"]}},
}

#: HIP-3 dexes loaded by default: the ones with active markets on 2026-11-13 (xyz 122, para 33,
#: io 15 of 171). ``PERPS_HYPERLIQUID_DEXES`` overrides it with a comma list, or "none".
DEFAULT_HYPERLIQUID_DEXES = ("xyz", "para", "io")

#: Live market lists are shared between worker processes through a small file cache for this long.
#: DuckDB starts short-lived worker processes, and a Hyperliquid market load costs five requests.
MARKETS_CACHE_SECONDS = 600.0

#: Rate-limit (HTTP 429) retries for a live call, with exponential backoff from RETRY_BASE_SECONDS.
RATE_LIMIT_RETRIES = 2
RETRY_BASE_SECONDS = 0.5


def hyperliquid_dexes() -> list[str]:
    """Return the HIP-3 dexes to load, from ``PERPS_HYPERLIQUID_DEXES`` or the default list."""
    value = os.environ.get("PERPS_HYPERLIQUID_DEXES")
    if value is None:
        return list(DEFAULT_HYPERLIQUID_DEXES)
    names = [v.strip() for v in value.split(",") if v.strip()]
    return [] if names in ([], ["none"]) else names


def _options(exchange_id: str) -> dict[str, Any]:
    options: dict[str, Any] = json.loads(json.dumps(_OPTIONS.get(exchange_id, {})))
    if exchange_id == "hyperliquid":
        dexes = hyperliquid_dexes()
        if dexes:
            options["fetchMarkets"]["hip3"] = {"dexes": dexes}
        else:
            options["fetchMarkets"]["types"] = ["swap"]
    return options


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
                {"enableRateLimit": True, "timeout": _TIMEOUT_MS, "options": _options(exchange_id)}
            )
        return _instances[exchange_id]


def _markets_cache_path(exchange_id: str) -> Path:
    """Cache file for one exchange's market list, keyed by its constructor options."""
    digest = hashlib.sha1(json.dumps(_options(exchange_id), sort_keys=True).encode()).hexdigest()[:12]
    root = Path(os.environ.get("PERPS_CACHE_DIR") or Path(tempfile.gettempdir()) / "vgi-perps-cache")
    return root / f"{exchange_id}-markets-{digest}.json"


def _cached_markets(exchange_id: str) -> Any:
    """Return a fresh cached market list, or ``None``."""
    path = _markets_cache_path(exchange_id)
    try:
        if time.time() - path.stat().st_mtime > MARKETS_CACHE_SECONDS:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _store_markets(exchange_id: str, markets: Any) -> None:
    """Write the market list atomically; a cache that cannot be written is skipped."""
    path = _markets_cache_path(exchange_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(markets))
        tmp.replace(path)
    except OSError:
        pass


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
    import ccxt

    exchange = _exchange(exchange_id)
    if method == "fetch_markets" and not args and not kwargs:
        cached = _cached_markets(exchange_id)
        if cached is not None:
            exchange.set_markets(cached)  # later per-symbol calls must not reload them
            return cached
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            result = getattr(exchange, method)(*args, **kwargs)
            break
        except ccxt.RateLimitExceeded as exc:
            # ccxt also raises RateLimitExceeded for Bybit's CloudFront geo-block (HTTP 403): only a real
            # 429 is worth waiting for.
            if " 429 " not in f" {exc} ":
                replay.record(key, 599, f"{type(exc).__name__}: {str(exc)[:400]}")
                raise OffshoreError(
                    f"{venue} (ccxt {exchange_id}) {method} failed: {type(exc).__name__}: {exc}"
                ) from exc
            if attempt == RATE_LIMIT_RETRIES:
                raise OffshoreError(
                    f"{venue} (ccxt {exchange_id}) {method} failed: HTTP 429 rate limit after "
                    f"{RATE_LIMIT_RETRIES} retries"
                ) from None
            time.sleep(RETRY_BASE_SECONDS * 2**attempt)
        except Exception as exc:  # ccxt raises its own hierarchy; surface one clear error
            replay.record(key, 599, f"{type(exc).__name__}: {str(exc)[:400]}")
            raise OffshoreError(f"{venue} (ccxt {exchange_id}) {method} failed: {type(exc).__name__}: {exc}") from exc
    result = json.loads(json.dumps(result, default=str))
    if method == "fetch_markets" and not args and not kwargs:
        exchange.set_markets(result)  # ccxt would otherwise load every market again on the next call
        _store_markets(exchange_id, result)
    replay.record(key, 200, result)
    return result
