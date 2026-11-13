"""Coinbase Derivatives (CFTC DCM) perpetual-style futures, via Advanced Trade's public endpoints.

The FairX REST API (``api.exchange.fairx.net``) is a clearing-member API and
answers every path, ``/rest/funding-rate`` included, with
``401 missing request header: CB-ACCESS-KEY``. So this adapter reads
Advanced Trade's unauthenticated ``/api/v3/brokerage/market/*`` surface, which
lists the US perp-style futures (venue ``cde``) with the latest hourly funding
rate, index price and open interest.

Normalization (see docs/VENUES.md):

* Products are perp-style when ``future_product_details.funding_interval`` is
  set (5-year "perp style" contracts such as ``BIP-20DEC30-CDE``).
* Prices are USD per named unit; ``1000SHIB``/``1000PEPE`` products quote per
  1000 coins, so price is divided by 1000 and ``contract_size`` multiplied.
* Funding accrues hourly. CFTC filing #2025-32 Appendix B: payment =
  ``rate * qty * multiplier * futures mark * -1``, so a positive rate debits
  longs: **positive = longs pay shorts**, no flip. No cap is specified.
* Only the most recent hourly rate is public; history needs member credentials.
* The public ``side`` of a trade is the **maker** side (Advanced Trade docs),
  so the taker side is its opposite.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from .. import http, replay
from ..model import BookLevel, Candle, FundingPoint, Instrument, Mark, Trade
from ..normalize import parse_iso, split_unit_prefix, to_float
from .base import Adapter, Capability

DEFAULT_BASE_URL = "https://api.coinbase.com/api/v3/brokerage"

_PAGE = 100
_MAX_PAGES = 20
_TRADE_PAGE = 1000
_MAX_CANDLES = 350
_GRANULARITY = {"1m": ("ONE_MINUTE", 60), "1h": ("ONE_HOUR", 3600), "1d": ("ONE_DAY", 86400)}


def base_url() -> str:
    """Return the API base, overridable with ``PERPS_COINBASE_BASE_URL``."""
    return os.environ.get("PERPS_COINBASE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return http.get_json("coinbase", f"{base_url()}{path}", params)


def _interval_hours(text: str | None) -> float | None:
    """``"3600s"`` -> 1.0."""
    if not text or not text.endswith("s"):
        return None
    seconds = to_float(text[:-1])
    return None if not seconds else seconds / 3600.0


def is_perp_style(product: dict[str, Any]) -> bool:
    """Tell whether a US futures product is perp-style (it has a funding interval)."""
    details = product.get("future_product_details") or {}
    return bool(details.get("funding_interval")) and details.get("venue") == "cde"


def instrument_from_product(product: dict[str, Any]) -> Instrument:
    """Normalize one Advanced Trade futures product."""
    details = product.get("future_product_details") or {}
    display = str(details.get("contract_display_name") or product.get("display_name") or "")
    _, multiplier = split_unit_prefix(display.split(" ")[0] if display else "")
    root = str(details.get("contract_root_unit") or product["product_id"])
    asset_type = str(details.get("futures_asset_type") or "")
    asset_class = "index" if asset_type == "FUTURES_ASSET_TYPE_STOCKS" else "crypto"
    contract_size = (to_float(details.get("contract_size")) or 1.0) * multiplier
    factor = 1.0 / multiplier
    tick = to_float(product.get("price_increment"))
    intraday = details.get("intraday_margin_rate") or {}
    im_rates = [
        r for r in (to_float(intraday.get("long_margin_rate")), to_float(intraday.get("short_margin_rate"))) if r
    ]
    initial = max(im_rates) if im_rates else None
    unit = f"1000 {root}" if multiplier == 1000.0 else root
    return Instrument(
        venue="coinbase",
        symbol=str(product["product_id"]),
        underlying=root,
        asset_class=asset_class,
        quote_ccy="USD",
        instrument_type="perp_style_future",
        status="active" if not product.get("trading_disabled") and not product.get("is_disabled") else "inactive",
        contract_size=contract_size,
        price_factor=factor,
        native_price_unit=f"USD per {unit}",
        tick_size=None if tick is None else tick * factor,
        funding_interval_hours=_interval_hours(details.get("funding_interval")),
        funding_cap=None,
        max_leverage=None if not initial else 1.0 / initial,
        initial_margin_rate=initial,
        margin_source=(
            "coinbase intraday_margin_rate, max of long/short (FCM initial requirement; overnight is higher; "
            "no maintenance schedule published)"
            if initial
            else None
        ),
        listed_at=parse_iso(product.get("new_at")),
        extra={"contract_code": str(details.get("contract_code") or "")},
    )


class CoinbaseAdapter(Adapter):
    """Coinbase Derivatives perp-style futures."""

    name = "coinbase"
    display_name = "Coinbase Derivatives (perp-style futures)"
    venue_type = "us_regulated"
    regulator = "CFTC (DCM: Coinbase Derivatives, LLC; clearing: Nodal Clear)"
    sign_convention = "native: positive = longs pay shorts (CFTC filing 2025-32 App. B; no flip)"
    capabilities = {
        "instruments": Capability("supported", False, "current", "GET /market/products?product_type=FUTURE"),
        "funding_history": Capability(
            "partial",
            True,
            "latest hourly rate only (public)",
            "full history is FairX GET /rest/funding-rate, which needs clearing-member API keys (HTTP 401)",
        ),
        "funding_estimate": Capability("unsupported", False, "n/a", "no public estimate; rates publish hourly"),
        "marks": Capability(
            "partial", False, "current", "index_price, mid, last; no explicit mark price or public bid/ask in listing"
        ),
        "premium_index": Capability("unsupported", False, "n/a", "no public premium series"),
        "open_interest": Capability("supported", False, "current", "future_product_details.open_interest"),
        "order_book": Capability("supported", False, "current", "GET /market/product_book"),
        "trades": Capability("supported", False, "recent; paged by time window", "GET /market/products/{id}/ticker"),
        "candles": Capability(
            "supported", False, "since listing (350 bars per call)", "GET /market/products/{id}/candles"
        ),
        "maintenance_margin": Capability(
            "unsupported", False, "n/a", "only initial (intraday/overnight) FCM rates are public"
        ),
    }

    def raw_products(self) -> list[dict[str, Any]]:
        """Every US futures product (perp-style and dated), offset-paged."""
        products: list[dict[str, Any]] = []
        for page in range(_MAX_PAGES):
            body = _get("/market/products", {"product_type": "FUTURE", "limit": _PAGE, "offset": page * _PAGE})
            batch = list(body.get("products") or [])
            products += batch
            if len(batch) < _PAGE:
                break
        return products

    def load_instruments(self) -> list[Instrument]:
        """Perp-style products only."""
        return [instrument_from_product(p) for p in self.raw_products() if is_perp_style(p)]

    def _products_by_id(self) -> dict[str, dict[str, Any]]:
        return {str(p["product_id"]): p for p in self.raw_products() if is_perp_style(p)}

    def funding(self, instruments: list[Instrument], start: datetime, end: datetime) -> list[FundingPoint]:
        """Return the latest published hourly rate per product, when it falls in ``[start, end]``."""
        products = self._products_by_id()
        out = []
        for inst in instruments:
            details = (products.get(inst.symbol) or {}).get("future_product_details") or {}
            when = parse_iso(details.get("funding_time"))
            rate = to_float(details.get("funding_rate"))
            if when is None or rate is None or not (start <= when <= end):
                continue
            out.append(
                FundingPoint(
                    instrument=inst,
                    funding_time=when,
                    rate=rate,
                    interval_hours=inst.funding_interval_hours or 1.0,
                    is_final=True,
                    index_price=inst.price(to_float(details.get("index_price"))),
                )
            )
        return out

    def marks(self, instruments: list[Instrument]) -> list[Mark]:
        """Snapshot from the product listing. Mark = mid (Coinbase publishes no live mark)."""
        products = self._products_by_id()
        as_of = replay.now()
        out = []
        for inst in instruments:
            product = products.get(inst.symbol)
            if product is None:
                continue
            details = product.get("future_product_details") or {}
            mid = inst.price(to_float(product.get("mid_market_price")))
            oi = to_float(details.get("open_interest"))
            funding_time = parse_iso(details.get("funding_time"))
            out.append(
                Mark(
                    instrument=inst,
                    as_of=as_of,
                    mark_price=mid,
                    mark_source="mid" if mid is not None else "unavailable",
                    index_price=inst.price(to_float(details.get("index_price"))),
                    last_price=inst.price(to_float(product.get("price"))),
                    bid=inst.price(to_float(product.get("best_bid_price"))),
                    ask=inst.price(to_float(product.get("best_ask_price"))),
                    funding_rate=to_float(details.get("funding_rate")),
                    funding_rate_is_estimate=False if details.get("funding_rate") else None,
                    funding_time=funding_time,
                    next_funding_time=None
                    if funding_time is None
                    else funding_time + timedelta(hours=inst.funding_interval_hours or 1.0),
                    open_interest_contracts=oi,
                    open_interest_usd=None if oi is None or mid is None else oi * inst.contract_size * mid,
                    volume_24h_usd=to_float(product.get("approximate_quote_24h_volume")),
                )
            )
        return out

    def order_book(self, inst: Instrument, depth: int) -> tuple[list[BookLevel], list[BookLevel]]:
        """Return the aggregated L2 book; sizes are contracts."""
        body = _get("/market/product_book", {"product_id": inst.symbol, "limit": depth})
        book = body.get("pricebook") or {}

        def levels(side: str, reverse: bool) -> list[BookLevel]:
            out = []
            for level in book.get(side) or []:
                price, size = to_float(level.get("price")), to_float(level.get("size"))
                if price is not None and size:
                    out.append(BookLevel(price * inst.price_factor, size))
            return sorted(out, key=lambda lv: -lv.price if reverse else lv.price)[:depth]

        return levels("bids", True), levels("asks", False)

    def trades(self, inst: Instrument, start: datetime, end: datetime, cursor: str) -> tuple[list[Trade], str | None]:
        """Page backwards through ``[start, end]``; the cursor is the next window end (epoch s).

        A full page drops its oldest second and resumes from it, so a second
        split across pages is emitted exactly once whatever ``end``'s inclusivity.
        """
        window_end = int(cursor) if cursor else int(end.timestamp()) + 1
        body = _get(
            f"/market/products/{http.segment(inst.symbol)}/ticker",
            {"limit": _TRADE_PAGE, "start": int(start.timestamp()), "end": window_end},
        )
        rows = body.get("trades") or []
        parsed: list[Trade] = []
        for row in rows:
            when = parse_iso(row.get("time"))
            price = to_float(row.get("price"))
            size = to_float(row.get("size"))
            if when is None or price is None or size is None:
                continue
            maker = str(row.get("side") or "").upper()
            taker = {"BUY": "sell", "SELL": "buy"}.get(maker)
            parsed.append(Trade(str(row.get("trade_id")), when, price * inst.price_factor, size, taker, None))
        parsed = [t for t in parsed if start <= t.ts <= end and t.ts.timestamp() < window_end]
        if len(rows) < _TRADE_PAGE or not parsed:
            return parsed, None
        oldest_second = int(min(t.ts.timestamp() for t in parsed))
        if oldest_second + 1 >= window_end:
            return parsed, None  # one second holds a whole page; nothing older can be reached safely
        return [t for t in parsed if t.ts.timestamp() >= oldest_second + 1], str(oldest_second + 1)

    def candles(self, inst: Instrument, start: datetime, end: datetime, bar: str) -> list[Candle]:
        """Candles in windows of 350 bars; volume is in contracts."""
        granularity, seconds = _GRANULARITY[bar]
        out: dict[datetime, Candle] = {}
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(seconds=seconds * _MAX_CANDLES), end)
            body = _get(
                f"/market/products/{http.segment(inst.symbol)}/candles",
                {
                    "start": int(cursor.timestamp()),
                    "end": int(window_end.timestamp()),
                    "granularity": granularity,
                    "limit": _MAX_CANDLES,
                },
            )
            for row in body.get("candles") or []:
                ts = to_float(row.get("start"))
                if ts is None:
                    continue
                when = datetime.fromtimestamp(ts, tz=start.tzinfo)
                out[when] = Candle(
                    ts=when,
                    open=inst.price(to_float(row.get("open"))),
                    high=inst.price(to_float(row.get("high"))),
                    low=inst.price(to_float(row.get("low"))),
                    close=inst.price(to_float(row.get("close"))),
                    volume_contracts=to_float(row.get("volume")),
                    volume_usd=None,
                    open_interest_contracts=None,
                )
            cursor = window_end
        return [c for ts, c in sorted(out.items()) if start <= ts <= end]
