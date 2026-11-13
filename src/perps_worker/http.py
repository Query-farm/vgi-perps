"""The one outbound-HTTP chokepoint for the US venues (Kalshi, Coinbase, Bitnomial).

Every US-venue request in this package goes through :func:`get_json`, which
issues ``GET`` and nothing else. ``tests/test_readonly_guard.py`` asserts that
this is the only module importing ``httpx`` and that no write verb appears
anywhere in the package. The offshore benchmark goes through the separate
:mod:`perps_worker.offshore_client` chokepoint (``ccxt``).

Values that come from SQL and land in a URL path go through :func:`segment`,
which percent-encodes ``/``, ``?`` and ``#`` so a symbol cannot escape its path
segment into an account or order endpoint.
"""

from __future__ import annotations

import atexit
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from . import replay

#: Connect and read timeouts for every request.
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

#: Retry policy: the rate limiter (429) and transient 5xx, plus transport errors.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_ATTEMPTS = 4
RETRY_BASE_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 20.0

USER_AGENT = "vgi-perps-worker/0.1 (+https://query.farm)"


class VenueHTTPError(RuntimeError):
    """A venue answered with a non-2xx status (after retries) or a non-JSON body."""

    def __init__(self, venue: str, status: int, url: str, body: str) -> None:
        """Build the error; the message carries the status code and a body excerpt."""
        super().__init__(f"{venue} API returned HTTP {status} for {url}: {body[:300]}")
        self.venue = venue
        self.status = status
        self.url = url


def segment(value: str) -> str:
    """Percent-encode one URL path segment so a SQL value cannot leave it.

    ``../../portfolio`` becomes ``..%2F..%2Fportfolio`` and ``X?limit=1``
    becomes ``X%3Flimit%3D1``: the request still names what was asked for, but
    it can no longer resolve to a different endpoint.
    """
    return quote(value, safe="")


def request_key(url: str, params: dict[str, Any] | None) -> str:
    """Build the canonical replay key: method, URL and sorted, non-null query parameters."""
    clean = sorted((k, str(v)) for k, v in (params or {}).items() if v is not None)
    return f"GET {url}?{urlencode(clean)}" if clean else f"GET {url}"


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _shared_client() -> httpx.Client:
    """Return a process-wide connection pool (one TLS handshake per host, not per page)."""
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        return _client


def _close_client() -> None:
    """Close the pool at interpreter exit."""
    global _client
    with _client_lock:
        if _client is not None and not _client.is_closed:
            _client.close()
        _client = None


atexit.register(_close_client)


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before the next attempt, honouring ``Retry-After``."""
    if response is not None:
        retry_after = response.headers.get("retry-after", "")
        if retry_after.strip().isdigit():
            return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
    return float(RETRY_BASE_SECONDS * (2**attempt))


def get_json(venue: str, url: str, params: dict[str, Any] | None = None) -> Any:
    """GET ``url`` and return the decoded JSON body.

    Replays a recording when ``PERPS_FIXTURES_DIR`` is set (see
    :mod:`perps_worker.replay`). Live requests retry 429/5xx and transport
    errors with exponential backoff, honouring ``Retry-After``.

    Args:
        venue: Venue name, used in error messages.
        url: Absolute URL. Any SQL-derived path part must already be encoded
            with :func:`segment`.
        params: Query parameters; ``None`` values are dropped.

    Returns:
        The decoded JSON body.

    Raises:
        VenueHTTPError: Non-2xx after retries, or a 2xx body that is not JSON.
        httpx.TransportError: Every attempt failed to connect.

    """
    key = request_key(url, params)
    recorded = replay.lookup(key)
    if recorded is not None:
        if recorded.status >= 400:
            raise VenueHTTPError(venue, recorded.status, url, str(recorded.body))
        return recorded.body
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    client = _shared_client()
    response: httpx.Response | None = None
    for attempt in range(RETRY_ATTEMPTS):
        last = attempt == RETRY_ATTEMPTS - 1
        try:
            response = client.get(url, params=clean)
        except httpx.TransportError:
            if last:
                raise
            response = None
        else:
            if response.status_code not in RETRYABLE_STATUSES or last:
                break
        time.sleep(_retry_delay(response, attempt))
    assert response is not None
    if response.status_code >= 400:
        replay.record(key, response.status_code, response.text[:2000])
        raise VenueHTTPError(venue, response.status_code, url, response.text)
    try:
        body = response.json()
    except ValueError as exc:
        raise VenueHTTPError(venue, response.status_code, url, f"non-JSON body: {response.text}") from exc
    replay.record(key, response.status_code, body)
    return body
