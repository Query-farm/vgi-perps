"""Kalshi perps (``/trade-api/v2/margin``), CFTC-regulated DCM, public endpoints only.

Normalization (see docs/VENUES.md for the evidence):

* Kalshi quotes **per contract** in dollars. One contract is ``contract_size``
  units of the instrument's named unit, and ``underlying_multiplier`` base units
  per named unit (``KXKSHIBPERP``: 1000 kSHIB x 1000 SHIB/kSHIB). So the price per
  base unit is ``price / (contract_size * underlying_multiplier)``; checked
  against BTC, gold, WTI and SHIB reference prices on 2026-11-13.
* ``reference_price`` is the underlying index, "scaled per contract", so it gets
  the same factor. ``settlement_mark_price`` is the mark used for funding.
* ``funding_rate`` is a decimal fraction per interval and **positive means longs
  pay shorts** (help.kalshi.com "How funding works"): no sign flip.
* Funding intervals: crypto every 8h (05/13/21 UTC = 00/08/16 ET); equities,
  indices, metals and energy daily. The schedule endpoint needs auth, so these
  are observed from the public history and re-checked by ``make live-check``.
* ``/margin/funding_history`` is the user's own payments (authenticated) and is
  never called.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from .. import http
from ..model import BookLevel, Candle, FundingPoint, Instrument, Mark, Trade
from ..normalize import from_epoch_ms, from_epoch_s, parse_iso, to_float
from .base import Adapter, Capability, VenueError

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

_ASSET_CLASS = {
    "Crypto": "crypto",
    "Equities": "equity",
    "Indices": "index",
    "Metals": "commodity",
    "Energy": "commodity",
}

#: Ticker core -> canonical underlying, where the ticker is not already the symbol.
_UNDERLYING = {
    "GOLD": "XAU",
    "SILVER": "XAG",
    "PLATINUM": "XPT",
    "PALLADIUM": "XPD",
    "HHGAS": "NATGAS_HH",
    "TTF": "NATGAS_TTF",
}

#: Observed funding interval by asset class (hours); see the module docstring.
_INTERVAL_HOURS = {"crypto": 8.0}
_DEFAULT_INTERVAL_HOURS = 24.0

#: Windows used to page the all-markets funding history.
_FUNDING_WINDOW = timedelta(days=7)
#: Above this many instruments, history is fetched for all markets per window.
_PER_TICKER_LIMIT = 6
#: Estimates cost one request per market; fetch them only for a narrowed set.
ESTIMATE_LIMIT = 8
_TRADE_PAGE = 1000
_MAX_BOOK_DEPTH = 100
_MAX_CANDLES_PER_CALL = 5000
_PERIOD_MINUTES = {"1m": 1, "1h": 60, "1d": 1440}


def base_url() -> str:
    """Return the API base, overridable with ``PERPS_KALSHI_BASE_URL``."""
    return os.environ.get("PERPS_KALSHI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return http.get_json("kalshi", f"{base_url()}{path}", params)


def _price(obj: Any) -> float | None:
    """``{"price": "7.08", "ts_ms": ...}`` -> 7.08."""
    return to_float(obj.get("price")) if isinstance(obj, dict) else None


def underlying_for(ticker: str, multiplier: float) -> tuple[str, str]:
    """``KXBTCPERP`` -> ``("BTC", "perpetual")``; ``KXWTIFUTURE`` -> ``("WTI", "perp_style_future")``."""
    core = ticker[2:] if ticker.startswith("KX") else ticker
    if core.endswith("PERP"):
        core, kind = core[: -len("PERP")], "perpetual"
    elif core.endswith("FUTURE"):
        core, kind = core[: -len("FUTURE")], "perp_style_future"
    else:
        kind = "perpetual"
    if multiplier == 1000.0 and core.startswith("K") and len(core) > 1:
        core = core[1:]  # KSHIB: the market trades kSHIB = 1000 SHIB
    return _UNDERLYING.get(core, core), kind


def instrument_from_market(market: dict[str, Any]) -> Instrument:
    """Normalize one ``/margin/markets`` entry."""
    ticker = str(market["ticker"])
    contract_size = to_float(market.get("contract_size")) or 1.0
    multiplier = to_float(market.get("underlying_multiplier")) or 1.0
    units = contract_size * multiplier
    factor = 1.0 / units
    asset_class = _ASSET_CLASS.get(str(market.get("asset_class") or ""), "other")
    underlying, kind = underlying_for(ticker, multiplier)
    tick = to_float(market.get("tick_size"))
    cap_unit = market.get("funding_limit_unit")
    leverage = to_float(market.get("leverage_estimate"))
    return Instrument(
        venue="kalshi",
        symbol=ticker,
        underlying=underlying,
        asset_class=asset_class,
        quote_ccy="USD",
        instrument_type=kind,
        status=str(market.get("status") or "unknown"),
        contract_size=units,
        price_factor=factor,
        native_price_unit=f"USD per contract ({market.get('title')})",
        tick_size=None if tick is None else tick * factor,
        funding_interval_hours=_INTERVAL_HOURS.get(asset_class, _DEFAULT_INTERVAL_HOURS),
        # A cap in "dollar" units is per contract, not a rate; it does not normalize.
        funding_cap=to_float(market.get("funding_cap")) if cap_unit == "percentage" else None,
        max_leverage=leverage,
        margin_source=(
            "kalshi leverage_estimate (1/margin_rate at a small notional); maintenance schedule not public"
            if leverage
            else None
        ),
        extra={"market_id": str(market.get("market_id") or "")},
    )


class KalshiAdapter(Adapter):
    """Kalshi perpetual futures."""

    name = "kalshi"
    display_name = "Kalshi (perps / margin)"
    venue_type = "us_regulated"
    regulator = "CFTC (DCM: KalshiEX LLC; clearing: Kalshi Klear)"
    sign_convention = "native: positive = longs pay shorts (no flip)"
    capabilities = {
        "instruments": Capability("supported", False, "current", "GET /margin/markets"),
        "funding_history": Capability(
            "supported", False, "since listing (BTC: 2026-06-03)", "GET /margin/funding_rates/historical"
        ),
        "funding_estimate": Capability(
            "supported", False, "current interval", "GET /margin/funding_rates/estimate (one call per market)"
        ),
        "marks": Capability("supported", False, "current", "settlement_mark_price, reference_price, bid/ask"),
        "premium_index": Capability(
            "supported", False, "<= 1 hour per call, recent retention only", "GET /margin/funding_rates/premium_index"
        ),
        "open_interest": Capability("supported", False, "current; hourly/daily via candles", "/margin/markets"),
        "order_book": Capability("supported", False, "current", "GET /margin/markets/{ticker}/orderbook"),
        "trades": Capability("supported", False, "since listing", "GET /margin/trades, cursor paged"),
        "candles": Capability("supported", False, "since listing", "1m / 1h / 1d candlesticks"),
        "maintenance_margin": Capability(
            "unsupported",
            True,
            "n/a",
            "portfolio VaR model; GET /margin/estimate_maintenance_margin/metadata needs auth and is not a "
            "per-position schedule",
        ),
    }

    def load_instruments(self) -> list[Instrument]:
        """All margin markets, active and inactive."""
        body = _get("/margin/markets")
        return [instrument_from_market(m) for m in body.get("markets", [])]

    def raw_markets(self) -> list[dict[str, Any]]:
        """Return the unnormalized ``/margin/markets`` payload."""
        return list(_get("/margin/markets").get("markets", []))

    # ----------------------------------------------------------------- funding
    def funding(self, instruments: list[Instrument], start: datetime, end: datetime) -> list[FundingPoint]:
        """Settled funding; per ticker for a few markets, else all markets per 7-day window."""
        by_symbol = {i.symbol: i for i in instruments}
        raw: list[dict[str, Any]] = []
        if len(by_symbol) <= _PER_TICKER_LIMIT:
            for ticker in sorted(by_symbol):
                raw += _get(
                    "/margin/funding_rates/historical",
                    {"ticker": ticker, "start_ts": int(start.timestamp()), "end_ts": int(end.timestamp())},
                ).get("funding_rates", [])
        else:
            cursor = start
            while cursor < end:
                window_end = min(cursor + _FUNDING_WINDOW, end)
                raw += _get(
                    "/margin/funding_rates/historical",
                    {"start_ts": int(cursor.timestamp()), "end_ts": int(window_end.timestamp())},
                ).get("funding_rates", [])
                cursor = window_end
        points: dict[tuple[str, datetime], FundingPoint] = {}
        for row in raw:
            inst = by_symbol.get(str(row.get("market_ticker")))
            when = parse_iso(row.get("funding_time"))
            rate = to_float(row.get("funding_rate"))
            if inst is None or when is None or rate is None or not (start <= when <= end):
                continue
            points[(inst.symbol, when)] = FundingPoint(
                instrument=inst,
                funding_time=when,
                rate=rate,
                interval_hours=inst.funding_interval_hours or _DEFAULT_INTERVAL_HOURS,
                is_final=True,
                mark_price=inst.price(to_float(row.get("mark_price"))),
            )
        return list(points.values())

    def _estimate(self, inst: Instrument) -> dict[str, Any]:
        return dict(_get("/margin/funding_rates/estimate", {"ticker": inst.symbol}))

    def _estimates(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        active = [i for i in instruments if i.status == "active"]
        if len(active) > ESTIMATE_LIMIT:
            return {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self._estimate, active))
        return {inst.symbol: body for inst, body in zip(active, results, strict=True)}

    def funding_estimates(self, instruments: list[Instrument]) -> list[FundingPoint]:
        """Provisional rate for the in-progress interval (time-weighted premium so far)."""
        points = []
        for symbol, body in self._estimates(instruments).items():
            inst = next(i for i in instruments if i.symbol == symbol)
            when = parse_iso(body.get("next_funding_time"))
            rate = to_float(body.get("funding_rate"))
            if when is None or rate is None:
                continue
            points.append(
                FundingPoint(
                    instrument=inst,
                    funding_time=when,
                    rate=rate,
                    interval_hours=inst.funding_interval_hours or _DEFAULT_INTERVAL_HOURS,
                    is_final=False,
                    mark_price=inst.price(to_float(body.get("mark_price"))),
                )
            )
        return points

    # ------------------------------------------------------------------- marks
    def marks(self, instruments: list[Instrument]) -> list[Mark]:
        """Snapshot from ``/margin/markets``, plus estimates for a narrowed set."""
        wanted = {i.symbol: i for i in instruments}
        estimates = self._estimates(instruments)
        out: list[Mark] = []
        for market in self.raw_markets():
            inst = wanted.get(str(market.get("ticker")))
            if inst is None:
                continue
            est = estimates.get(inst.symbol, {})
            settle = market.get("settlement_mark_price")
            out.append(
                Mark(
                    instrument=inst,
                    as_of=from_epoch_ms(settle.get("ts_ms")) if isinstance(settle, dict) else None,
                    mark_price=inst.price(_price(settle)),
                    mark_source="venue_mark" if settle else "unavailable",
                    index_price=inst.price(_price(market.get("reference_price"))),
                    last_price=inst.price(to_float(market.get("price"))),
                    bid=inst.price(to_float(market.get("bid"))),
                    ask=inst.price(to_float(market.get("ask"))),
                    funding_rate=to_float(est.get("funding_rate")),
                    funding_rate_is_estimate=True if est else None,
                    funding_time=None,
                    next_funding_time=parse_iso(est.get("next_funding_time")),
                    open_interest_contracts=to_float(market.get("open_interest")),
                    open_interest_usd=to_float(market.get("open_interest_notional_value_dollars")),
                    volume_24h_usd=to_float(market.get("volume_24h_notional_value_dollars")),
                )
            )
        return out

    def premium_index(self, inst: Instrument, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        """Kalshi's informational per-second premium index (fraction), window <= 1 hour."""
        if end <= start or end - start > timedelta(hours=1):
            raise VenueError("kalshi premium_index: the window must be positive and at most one hour")
        body = _get(
            "/margin/funding_rates/premium_index",
            {"ticker": inst.symbol, "start_ts": int(start.timestamp()), "end_ts": int(end.timestamp())},
        )
        out = []
        for point in body.get("points", []):
            when = parse_iso(point.get("second_ts"))
            value = to_float(point.get("premium_index"))
            if when is not None and value is not None:
                out.append((when, value))
        return out

    # ------------------------------------------------------------- book/trades
    def order_book(self, inst: Instrument, depth: int) -> tuple[list[BookLevel], list[BookLevel]]:
        """Return the aggregated book; Kalshi returns levels worst-first, so sort explicitly."""
        # Kalshi rejects depth > 100 with 400 invalid_selector; depth=0 means every level.
        body = _get(
            f"/margin/markets/{http.segment(inst.symbol)}/orderbook",
            {"depth": depth if depth <= _MAX_BOOK_DEPTH else 0},
        )
        book = body.get("orderbook") or {}

        def levels(side: str) -> list[BookLevel]:
            out = []
            for level in book.get(side) or []:
                price, size = to_float(level[0]), to_float(level[1])
                if price is not None and size:
                    out.append(BookLevel(price * inst.price_factor, size))
            return out

        bids = sorted(levels("bids"), key=lambda lv: -lv.price)[:depth]
        asks = sorted(levels("asks"), key=lambda lv: lv.price)[:depth]
        return bids, asks

    def trades(self, inst: Instrument, start: datetime, end: datetime, cursor: str) -> tuple[list[Trade], str | None]:
        """One page (newest first) of public trades; ``taker_side`` bid->buy, ask->sell."""
        body = _get(
            "/margin/trades",
            {
                "ticker": inst.symbol,
                "limit": _TRADE_PAGE,
                "min_ts": int(start.timestamp()),
                "max_ts": int(end.timestamp()),
                "cursor": cursor or None,
            },
        )
        out = []
        for row in body.get("trades") or []:
            when = parse_iso(row.get("created_time"))
            price = to_float(row.get("price"))
            count = to_float(row.get("count"))
            if when is None or price is None or count is None or not (start <= when <= end):
                continue
            side = {"bid": "buy", "ask": "sell"}.get(str(row.get("taker_side")))
            out.append(
                Trade(str(row.get("trade_id")), when, price * inst.price_factor, count, side, row.get("is_block_trade"))
            )
        next_cursor = body.get("cursor") or None
        if not body.get("trades"):
            next_cursor = None
        return out, next_cursor

    def candles(self, inst: Instrument, start: datetime, end: datetime, bar: str) -> list[Candle]:
        """Candlesticks; Kalshi stamps the bar end, we report the bar start."""
        minutes = _PERIOD_MINUTES[bar]
        span = timedelta(minutes=minutes * _MAX_CANDLES_PER_CALL)
        out: dict[datetime, Candle] = {}
        cursor = start
        while cursor < end:
            window_end = min(cursor + span, end)
            body = _get(
                f"/margin/markets/{http.segment(inst.symbol)}/candlesticks",
                {
                    "start_ts": int(cursor.timestamp()),
                    "end_ts": int(window_end.timestamp()),
                    "period_interval": minutes,
                },
            )
            for row in body.get("candlesticks") or []:
                bar_end = from_epoch_s(row.get("end_period_ts"))
                if bar_end is None:
                    continue
                bar_start = bar_end - timedelta(minutes=minutes)
                price = row.get("price") or {}
                out[bar_start] = Candle(
                    ts=bar_start,
                    open=inst.price(to_float(price.get("open"))),
                    high=inst.price(to_float(price.get("high"))),
                    low=inst.price(to_float(price.get("low"))),
                    close=inst.price(to_float(price.get("close"))),
                    volume_contracts=to_float(row.get("volume")),
                    volume_usd=to_float(row.get("volume_notional_value_dollars")),
                    open_interest_contracts=to_float(row.get("open_interest")),
                )
            cursor = window_end
        return [c for ts, c in sorted(out.items()) if start <= ts <= end]
