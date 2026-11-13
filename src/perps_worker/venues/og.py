"""OG.com crypto perpetuals (CFTC DCM + DCO), via Crypto.com's public FCM market-data API.

OG.com is the trading name of North American Derivatives Exchange, Inc. (formerly
Nadex; also styled "Crypto.com | Derivatives North America", CDNA), a Crypto.com
affiliate. Its US perpetuals launched on 2026-10-27 and are distributed through
OG.com, the Crypto.com App and the Crypto.com Exchange. The public endpoints are
documented as the "GEN4 FCM US B2C API" (Preview):
https://exchange-developer.crypto.com/fcm-b2c/v1/docs/api/rest-introduction.
They answer plain ``GET`` with query parameters and need no key ("Public
market-data endpoints can be explored before onboarding"); the limit is 100
requests per second per method per IP.

Normalization (see docs/VENUES.md):

* ``get-instruments?inst_type=PERPETUAL_SWAP`` lists the perps (``BTC-USD-PERP``
  ...). Prices are USD per coin and quantities are in coins
  (``contract_size`` 1). ``max_leverage`` is published per instrument.
* Funding is paid at 00:00, 08:00 and 16:00 UTC and "the rate is shown as an
  amount per eight-hour interval" (OG.com help, Funding Payments); positive =
  longs pay shorts (Nadex Rule 16.14). No flip.
* ``get-valuations?valuation_type=funding_hist`` is stamped **hourly** but its
  value changes only in 8-hour blocks covering ``T-7h .. T`` for each funding
  time ``T``; the point stamped at ``T`` is taken as the rate settled at ``T``.
  Retention is about 14 days.
* ``valuation_type=estimated_funding_rate`` (per minute, about 1000 minutes
  kept) tracks the premium during a window; its last value before a window opens
  is the rate the funding history then shows for that window (observed on
  SEI-USD-PERP, 2026-11-13). So the rate settling at the next funding time ``N``
  is the last estimate before ``N - 8h``: it is fixed in advance, and
  :meth:`OgAdapter.funding_estimates` reports it as not final until ``N``.
* ``get-trades`` returns at most 150 trades, newest first; ``end_ts`` is
  exclusive and ``start_ts`` inclusive. ``get-candlestick`` returns at most
  300 bars, the latest in the requested range.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import http, replay
from ..model import BookLevel, Candle, FundingPoint, Instrument, Mark, Trade
from ..normalize import from_epoch_ms, to_float
from .base import Adapter, Capability

DEFAULT_BASE_URL = "https://api.crypto.com/fcm/v1/public"

#: Funding times are every 8 hours from 00:00 UTC.
FUNDING_HOURS = 8
_TRADE_PAGE = 150
_MAX_CANDLES = 300
_MAX_HIST_POINTS = 5000
_TIMEFRAME = {"1m": ("1m", 60), "1h": ("1h", 3600), "1d": ("1D", 86400)}


def base_url() -> str:
    """Return the API base, overridable with ``PERPS_OG_BASE_URL``."""
    return os.environ.get("PERPS_OG_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _get(method: str, params: dict[str, Any] | None = None) -> Any:
    body = http.get_json("og", f"{base_url()}/{method}", params)
    result = body.get("result") if isinstance(body, dict) else None
    return result if isinstance(result, dict) else {}


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def next_funding_time(now: datetime) -> datetime:
    """Return the first funding time strictly after ``now`` (00:00, 08:00 or 16:00 UTC)."""
    now = now.astimezone(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    block = now.hour // FUNDING_HOURS + 1
    return day + timedelta(hours=block * FUNDING_HOURS)


def is_funding_time(when: datetime) -> bool:
    """Return whether ``when`` is exactly 00:00, 08:00 or 16:00 UTC."""
    when = when.astimezone(UTC)
    return when.hour % FUNDING_HOURS == 0 and when.minute == 0 and when.second == 0 and when.microsecond == 0


def instrument_from_row(row: dict[str, Any]) -> Instrument:
    """Normalize one ``get-instruments`` ``PERPETUAL_SWAP`` row."""
    base = str(row.get("base_ccy") or str(row["symbol"]).split("-")[0])
    attrs = (row.get("event_details") or {}).get("attributes") or {}
    opened = str(attrs.get("OPEN_TIME") or "")
    listed = None
    if opened:
        try:
            listed = datetime.strptime(opened[:17], "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            listed = None
    leverage = to_float(row.get("max_leverage"))
    return Instrument(
        venue="og",
        symbol=str(row["symbol"]),
        underlying=base,
        asset_class="crypto",
        quote_ccy=str(row.get("quote_ccy") or "USD"),
        instrument_type="perpetual",
        status="active" if row.get("tradable") else "inactive",
        contract_size=to_float(row.get("contract_size")) or 1.0,
        price_factor=1.0,
        native_price_unit=f"USD per {base}",
        tick_size=to_float(row.get("price_tick_size")),
        funding_interval_hours=float(FUNDING_HOURS),
        funding_cap=None,
        max_leverage=leverage,
        initial_margin_rate=None if not leverage else 1.0 / leverage,
        margin_source="og max_leverage (initial = 1 / max_leverage; no maintenance schedule published)"
        if leverage
        else None,
        listed_at=listed,
        extra={"index_symbol": str(row.get("underlying_symbol") or ""), "inst_id": str(row.get("inst_id") or "")},
    )


class OgAdapter(Adapter):
    """OG.com (North American Derivatives Exchange) crypto perpetuals."""

    name = "og"
    display_name = "OG.com (North American Derivatives Exchange, Crypto.com affiliate)"
    venue_type = "us_regulated"
    regulator = "CFTC (DCM + DCO: North American Derivatives Exchange, Inc., formerly Nadex)"
    sign_convention = "native: positive = longs pay shorts (no flip)"
    capabilities = {
        "instruments": Capability("supported", False, "current", "GET get-instruments?inst_type=PERPETUAL_SWAP"),
        "funding_history": Capability(
            "supported", False, "about 14 days", "get-valuations funding_hist (hourly stamps, 8h rate)"
        ),
        "funding_estimate": Capability(
            "supported", False, "next funding time", "rate fixed one window ahead (last estimate before it opens)"
        ),
        "marks": Capability("supported", False, "current", "get-tickers + get-valuations mark_price/index_price"),
        "premium_index": Capability(
            "unsupported", False, "n/a", "only per-minute mark and index (get-valuations), no premium series"
        ),
        "open_interest": Capability("supported", False, "current", "get-tickers oi"),
        "order_book": Capability("supported", False, "current", "GET get-book"),
        "trades": Capability("supported", False, "recent", "GET get-trades, 150 per page, newest first"),
        "candles": Capability("supported", False, "since listing", "GET get-candlestick, 300 per page"),
        "maintenance_margin": Capability(
            "unsupported", False, "n/a", "only max_leverage is published (help-centre example implies 15% at 3x)"
        ),
    }

    def raw_instruments(self) -> list[dict[str, Any]]:
        """``PERPETUAL_SWAP`` rows from ``get-instruments``."""
        rows = _get("get-instruments", {"inst_type": "PERPETUAL_SWAP"}).get("data") or []
        return [r for r in rows if r.get("inst_type") == "PERPETUAL_SWAP" and r.get("symbol")]

    def load_instruments(self) -> list[Instrument]:
        """Perpetuals only."""
        return [instrument_from_row(r) for r in self.raw_instruments()]

    def _valuations(
        self, name: str, kind: str, *, count: int, start: datetime | None = None, end: datetime | None = None
    ) -> list[tuple[datetime, float]]:
        """``(time, value)`` from ``get-valuations``, oldest first."""
        params: dict[str, Any] = {"instrument_name": name, "valuation_type": kind, "count": count}
        if start is not None:
            params["start_ts"] = _ms(start)
        if end is not None:
            params["end_ts"] = _ms(end)
        out = []
        for row in _get("get-valuations", params).get("data") or []:
            when, value = from_epoch_ms(row.get("t")), to_float(row.get("v"))
            if when is not None and value is not None:
                out.append((when, value))
        return sorted(out)

    def funding(self, instruments: list[Instrument], start: datetime, end: datetime) -> list[FundingPoint]:
        """Settled funding: the hourly ``funding_hist`` points stamped at a funding time."""
        hours = int((end - start).total_seconds() // 3600) + 2
        out = []
        for inst in instruments:
            series = self._valuations(
                inst.symbol,
                "funding_hist",
                count=min(max(hours, 1), _MAX_HIST_POINTS),
                start=start,
                end=end + timedelta(milliseconds=1),
            )
            for when, rate in series:
                if is_funding_time(when) and start <= when <= end:
                    out.append(FundingPoint(inst, when, rate, float(FUNDING_HOURS), True))
        return out

    def _next_rate(self, inst: Instrument, now: datetime) -> FundingPoint | None:
        """Return the rate settling at the next funding time: the last estimate before that window opened."""
        upcoming = next_funding_time(now)
        opened = upcoming - timedelta(hours=FUNDING_HOURS)
        last = self._valuations(inst.symbol, "estimated_funding_rate", count=1, end=opened)
        if not last:
            return None
        return FundingPoint(inst, upcoming, last[-1][1], float(FUNDING_HOURS), False)

    def funding_estimates(self, instruments: list[Instrument]) -> list[FundingPoint]:
        """Rate for the next funding time (already fixed; final once it settles)."""
        now = replay.now()
        return [p for p in (self._next_rate(i, now) for i in instruments) if p]

    def marks(self, instruments: list[Instrument]) -> list[Mark]:
        """Ticker (last, bid, ask, OI, 24h USD volume) plus the latest mark and index valuations."""
        now = replay.now()
        out = []
        for inst in instruments:
            rows = _get("get-tickers", {"instrument_name": inst.symbol}).get("data") or []
            tick = rows[0] if rows else {}
            # As of "now" (exclusive end), so a pinned PERPS_NOW replays a consistent snapshot.
            until = now + timedelta(milliseconds=1)
            mark = self._valuations(inst.symbol, "mark_price", count=1, end=until)
            index_name = inst.extra.get("index_symbol")
            index = self._valuations(index_name, "index_price", count=1, end=until) if index_name else []
            nxt = self._next_rate(inst, now)
            mark_price = mark[-1][1] if mark else None
            oi = to_float(tick.get("oi"))
            out.append(
                Mark(
                    instrument=inst,
                    as_of=mark[-1][0] if mark else from_epoch_ms(tick.get("t")),
                    mark_price=mark_price,
                    mark_source="venue_mark" if mark_price is not None else "unavailable",
                    index_price=index[-1][1] if index else None,
                    last_price=to_float(tick.get("a")),
                    bid=to_float(tick.get("b")),
                    ask=to_float(tick.get("k")),
                    funding_rate=nxt.rate if nxt else None,
                    funding_rate_is_estimate=False if nxt else None,
                    funding_time=nxt.funding_time if nxt else None,
                    next_funding_time=None if nxt is None else nxt.funding_time + timedelta(hours=FUNDING_HOURS),
                    open_interest_contracts=oi,
                    open_interest_usd=None
                    if oi is None or mark_price is None
                    else oi * inst.contract_size * mark_price,
                    volume_24h_usd=to_float(tick.get("vv")),
                )
            )
        return out

    def order_book(self, inst: Instrument, depth: int) -> tuple[list[BookLevel], list[BookLevel]]:
        """Return the aggregated book; sizes are coins (contract_size 1)."""
        data = _get("get-book", {"instrument_name": inst.symbol, "depth": min(max(depth, 1), 50)}).get("data") or []
        book = data[0] if data else {}

        def levels(side: str, best_high: bool) -> list[BookLevel]:
            out = []
            for level in book.get(side) or []:
                price, size = to_float(level[0]), to_float(level[1])
                if price is not None and size:
                    out.append(BookLevel(price, size))
            return sorted(out, key=lambda lv: -lv.price if best_high else lv.price)[:depth]

        return levels("bids", True), levels("asks", False)

    def trades(self, inst: Instrument, start: datetime, end: datetime, cursor: str) -> tuple[list[Trade], str | None]:
        """Page backwards; the cursor is the next exclusive ``end_ts`` (epoch ms).

        A full page drops its oldest millisecond and resumes with ``end_ts`` just
        after it, so trades sharing a millisecond across pages are emitted once.
        """
        window_end = int(cursor) if cursor else _ms(end) + 1
        rows = (
            _get(
                "get-trades",
                {"instrument_name": inst.symbol, "count": _TRADE_PAGE, "start_ts": _ms(start), "end_ts": window_end},
            ).get("data")
            or []
        )
        parsed: list[tuple[int, Trade]] = []
        for row in rows:
            ms = int(to_float(row.get("t")) or 0)
            when = from_epoch_ms(ms) if ms else None
            price, size = to_float(row.get("p")), to_float(row.get("q"))
            if when is None or price is None or size is None:
                continue
            side = str(row.get("s") or "").lower()
            parsed.append(
                (ms, Trade(str(row.get("d")), when, price, size, side if side in ("buy", "sell") else None, None))
            )
        parsed = [(ms, t) for ms, t in parsed if start <= t.ts <= end and ms < window_end]
        if len(rows) < _TRADE_PAGE or not parsed:
            return [t for _, t in parsed], None
        oldest = min(ms for ms, _ in parsed)
        if oldest + 1 >= window_end:
            return [t for _, t in parsed], None  # a whole page in one millisecond; nothing older is reachable
        return [t for ms, t in parsed if ms > oldest], str(oldest + 1)

    def candles(self, inst: Instrument, start: datetime, end: datetime, bar: str) -> list[Candle]:
        """Bars in windows of 300; volume is in coins."""
        timeframe, seconds = _TIMEFRAME[bar]
        out: dict[datetime, Candle] = {}
        cursor = start
        while cursor <= end:
            window_end = min(cursor + timedelta(seconds=seconds * _MAX_CANDLES), end + timedelta(seconds=1))
            data = (
                _get(
                    "get-candlestick",
                    {
                        "instrument_name": inst.symbol,
                        "timeframe": timeframe,
                        "count": _MAX_CANDLES,
                        "start_ts": _ms(cursor),
                        "end_ts": _ms(window_end),
                    },
                ).get("data")
                or []
            )
            for row in data:
                when = from_epoch_ms(row.get("t"))
                if when is None:
                    continue
                volume, close = to_float(row.get("v")), to_float(row.get("c"))
                out[when] = Candle(
                    ts=when,
                    open=to_float(row.get("o")),
                    high=to_float(row.get("h")),
                    low=to_float(row.get("l")),
                    close=close,
                    volume_contracts=volume,
                    volume_usd=None if volume is None or close is None else volume * inst.contract_size * close,
                    open_interest_contracts=None,
                )
            if window_end > end:
                break
            cursor = window_end
        return [c for ts, c in sorted(out.items()) if start <= ts <= end]
