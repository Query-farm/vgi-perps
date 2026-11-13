"""Bitnomial perpetual futures (CFTC DCM + DCO), public REST endpoints.

Normalization (see docs/VENUES.md):

* Specs give ``contract_size`` in base units (``contract_size_unit``) and
  ``price_increment`` in USD per base unit. Product-data and funding prices are
  integer **ticks**: USD per unit = ``ticks * price_increment``.
* ``funding_rate`` is a decimal per 8h interval; "If the funding rate is
  positive, longs pay shorts" (Digital Asset Perpetual Pricing): no flip.
  Intervals are Central-time aligned (7pm/3am/11am CT), so their UTC hours move
  with daylight saving; ``interval_end - interval_start`` is used as the interval.
* ``funding_rate_upper_bound`` from the spec is reported as ``funding_cap``.
* Product data's ``mark_price`` is the mark at the last funding calculation, not
  a live mark, so ``marks()`` stamps it with that interval's close and pairs it
  with the same interval's ``price_index``.
* Order book and trades are WebSocket-only, so they are unsupported here.
* Rate limit: 200 requests per 5 minutes (HTTP 403 beyond it).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import http
from ..model import Candle, FundingPoint, Instrument, Mark
from ..normalize import parse_iso, to_float
from .base import Adapter, Capability, VenueError

DEFAULT_ROOT_URL = "https://bitnomial.com/exchange/api/v1"

_PAGE = 500
_MAX_PAGES = 50
_PER_PRODUCT_LIMIT = 6
ESTIMATE_LIMIT = 6


def root_url() -> str:
    """Return the API root (without ``/prod``), overridable with ``PERPS_BITNOMIAL_ROOT_URL``."""
    return os.environ.get("PERPS_BITNOMIAL_ROOT_URL", DEFAULT_ROOT_URL).rstrip("/")


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return http.get_json("bitnomial", f"{root_url()}{path}", params)


def instrument_from_spec(spec: dict[str, Any]) -> Instrument:
    """Normalize one ``/prod/product/specs/`` entry of type ``perpetual``."""
    increment = to_float(spec.get("price_increment")) or 1.0
    cap = to_float(spec.get("funding_rate_upper_bound"))
    unit = str(spec.get("contract_size_unit") or spec.get("base_symbol"))
    return Instrument(
        venue="bitnomial",
        symbol=str(spec["symbol"]),
        underlying=unit,
        asset_class="crypto",
        quote_ccy=str(spec.get("quote_asset") or "USD"),
        instrument_type="perpetual",
        status=str(spec.get("product_status") or "unknown"),
        contract_size=to_float(spec.get("contract_size")) or 1.0,
        price_factor=increment,
        native_price_unit=f"ticks of {increment:g} USD per {unit}",
        tick_size=increment,
        funding_interval_hours=to_float(spec.get("funding_interval_hours")),
        funding_cap=cap,
        listed_at=parse_iso(f"{spec['first_trading_day']}T00:00:00Z") if spec.get("first_trading_day") else None,
        extra={"product_id": str(spec["product_id"]), "base_symbol": str(spec.get("base_symbol") or "")},
    )


def _as_list(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return list(body["data"])
    return []


class BitnomialAdapter(Adapter):
    """Bitnomial perpetual futures."""

    name = "bitnomial"
    display_name = "Bitnomial Exchange (perpetual futures)"
    venue_type = "us_regulated"
    regulator = "CFTC (DCM: Bitnomial Exchange, LLC; clearing: Bitnomial Clear)"
    sign_convention = "native: positive = longs pay shorts (no flip)"
    capabilities = {
        "instruments": Capability("supported", False, "current", "GET /prod/product/specs/"),
        "funding_history": Capability(
            "supported", False, "since listing (2026-05-16)", "GET /funding-rates/, cursor paged"
        ),
        "funding_estimate": Capability("supported", False, "current interval", "GET /funding-rates/estimated/{base}"),
        "marks": Capability(
            "partial", False, "as of the last funding interval", "mark and index are published per funding interval"
        ),
        "premium_index": Capability("unsupported", False, "n/a", "only price_index and mark at each funding"),
        "open_interest": Capability("supported", False, "current; daily via /web/charts/voi", "/prod/product/data/"),
        "order_book": Capability("unsupported", False, "n/a", "WebSocket only (wss://bitnomial.com/exchange/ws)"),
        "trades": Capability("unsupported", False, "n/a", "WebSocket only; REST has block trades only"),
        "candles": Capability("partial", False, "daily bars only", "GET /web/charts/price/{id} (marked non-stable)"),
        "maintenance_margin": Capability(
            "unsupported", False, "n/a", "product_group_margin_rate is published but not labelled initial/maintenance"
        ),
    }

    def raw_specs(self) -> list[dict[str, Any]]:
        """Active product specs, every type."""
        return _as_list(_get("/prod/product/specs/", {"active": "true"}))

    def raw_data(self) -> list[dict[str, Any]]:
        """Today's data for active products (prices in ticks)."""
        return _as_list(_get("/prod/product/data/", {"active": "true"}))

    def load_instruments(self) -> list[Instrument]:
        """Perpetuals only."""
        return [instrument_from_spec(s) for s in self.raw_specs() if s.get("type") == "perpetual"]

    def _funding_rows(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            body = _get("/funding-rates/", {**params, "cursor": cursor})
            page = _as_list(body)
            rows += page
            cursor = ((body.get("pagination") or {}).get("cursor")) if isinstance(body, dict) else None
            if not page or not cursor or len(page) < int(params.get("limit", _PAGE)):
                return rows
        raise VenueError(f"bitnomial funding history still paging after {_MAX_PAGES} pages; narrow the date range")

    def _point(self, row: dict[str, Any], inst: Instrument, final: bool) -> FundingPoint | None:
        start = parse_iso(row.get("interval_start"))
        end = parse_iso(row.get("interval_end"))
        rate = to_float(row.get("funding_rate"))
        if end is None or rate is None:
            return None
        hours = (end - start).total_seconds() / 3600.0 if start else inst.funding_interval_hours or 8.0
        return FundingPoint(
            instrument=inst,
            funding_time=end,
            rate=rate,
            interval_hours=hours,
            is_final=final,
            mark_price=inst.price(to_float(row.get("mark_price"))),
            index_price=inst.price(to_float(row.get("price_index"))),
        )

    def funding(self, instruments: list[Instrument], start: datetime, end: datetime) -> list[FundingPoint]:
        """Settled funding, matched to instruments by ``product_id``."""
        by_id = {i.extra["product_id"]: i for i in instruments}
        base = {
            "begin_time": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order": "asc",
            "limit": _PAGE,
        }
        rows: list[dict[str, Any]] = []
        if len(by_id) <= _PER_PRODUCT_LIMIT:
            for product_id in sorted(by_id):
                rows += self._funding_rows({**base, "product_id": product_id})
        else:
            rows = self._funding_rows(base)
        points: dict[tuple[str, datetime], FundingPoint] = {}
        for row in rows:
            inst = by_id.get(str(row.get("product_id")))
            point = self._point(row, inst, True) if inst else None
            if point and start <= point.funding_time <= end:
                points[(point.instrument.symbol, point.funding_time)] = point
        return list(points.values())

    def funding_estimates(self, instruments: list[Instrument]) -> list[FundingPoint]:
        """In-progress rate, for at most :data:`ESTIMATE_LIMIT` instruments (rate limit)."""
        if len(instruments) > ESTIMATE_LIMIT:
            return []
        out = []
        for inst in instruments:
            body = _get(f"/funding-rates/estimated/{http.segment(inst.extra['base_symbol'])}")
            point = self._point(body, inst, False) if isinstance(body, dict) else None
            if point:
                out.append(point)
        return out

    def marks(self, instruments: list[Instrument]) -> list[Mark]:
        """Mark and index as of the last funding calculation, plus live last price and OI."""
        data = {str(d.get("product_id")): d for d in self.raw_data()}
        latest: dict[str, dict[str, Any]] = {}
        for r in _as_list(_get("/funding-rates/")):
            key = str(r.get("product_id"))
            if key not in latest or str(r.get("interval_end")) > str(latest[key].get("interval_end")):
                latest[key] = r
        estimates = {p.instrument.symbol: p for p in self.funding_estimates(instruments)}
        out = []
        for inst in instruments:
            row = data.get(inst.extra["product_id"])
            if row is None:
                continue
            fund = latest.get(inst.extra["product_id"], {})
            close = parse_iso(row.get("funding_interval_close")) or parse_iso(fund.get("interval_end"))
            mark = inst.price(to_float(row.get("mark_price")))
            last = inst.price(to_float(row.get("last_price")))
            oi = to_float(row.get("open_interest"))
            est = estimates.get(inst.symbol)
            out.append(
                Mark(
                    instrument=inst,
                    as_of=close,
                    mark_price=mark,
                    mark_source="last_funding_mark" if mark is not None else "unavailable",
                    index_price=inst.price(to_float(fund.get("price_index")))
                    if fund.get("interval_end") == row.get("funding_interval_close")
                    else None,
                    last_price=last,
                    bid=None,
                    ask=None,
                    funding_rate=est.rate if est else to_float(row.get("funding_rate")),
                    funding_rate_is_estimate=bool(est) if (est or row.get("funding_rate") is not None) else None,
                    funding_time=close,
                    next_funding_time=est.funding_time
                    if est
                    else (None if close is None else close + timedelta(hours=inst.funding_interval_hours or 8.0)),
                    open_interest_contracts=oi,
                    open_interest_usd=None
                    if oi is None or (mark or last) is None
                    else oi * inst.contract_size * (mark or last or 0.0),
                    volume_24h_usd=None
                    if row.get("notional_volume") is None
                    else float(row["notional_volume"]) / 100.0,
                )
            )
        return out

    def candles(self, inst: Instrument, start: datetime, end: datetime, bar: str) -> list[Candle]:
        """Daily settlement bars (USD already, not ticks); ``ts`` is the trading date at 00:00 UTC."""
        if bar != "1d":
            raise self.unsupported("candles") from VenueError(f"bitnomial publishes daily bars only, not {bar!r}")
        body = http.get_json(
            "bitnomial",
            f"{root_url().removesuffix('/v1')}/web/charts/price/{http.segment(inst.extra['product_id'])}",
            {
                "begin_time": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_time": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        out = []
        for row in _as_list(body):
            day = row.get("date")
            if not day:
                continue
            when = datetime.fromisoformat(f"{day}T00:00:00+00:00")
            ohlc = row.get("ohlc") or {}
            volume = to_float(ohlc.get("volume"))
            close = to_float(ohlc.get("close"))
            out.append(
                Candle(
                    ts=when,
                    open=to_float(ohlc.get("open")),
                    high=to_float(ohlc.get("high")),
                    low=to_float(ohlc.get("low")),
                    close=close,
                    volume_contracts=volume,
                    volume_usd=None if volume is None or close is None else volume * inst.contract_size * close,
                    open_interest_contracts=None,
                )
            )
        return [c for c in sorted(out, key=lambda c: c.ts) if start <= c.ts <= end]
