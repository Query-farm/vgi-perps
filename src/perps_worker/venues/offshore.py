"""Offshore benchmark venues (Binance USD-M, Bybit linear, Hyperliquid) through ccxt.

These are **not** US-regulated; they are here as the liquidity and funding
benchmark the US venues are compared against. Every call goes through
:mod:`perps_worker.offshore_client`, which allows only public ``fetch*`` methods.

Normalization (see docs/VENUES.md):

* ccxt's unified ``fundingRate`` is a decimal per funding interval with the
  venues' shared convention, **positive = longs pay shorts**: no flip.
* Hyperliquid funds hourly and its API reports the hourly rate (``0.0000125`` =
  the 0.01%/8h interest baseline / 8). Bybit publishes ``fundingInterval`` in
  minutes. Binance varies by symbol (8h, 4h, 1h) and ccxt's market list does not
  carry it, so ``funding()`` infers it from the spacing of the returned history.
* Scaled coins (``1000PEPE``, Hyperliquid ``kPEPE``) are reported per single
  coin: price / 1000, contract size x 1000.
* Quotes are USDT (Binance, Bybit) or USDC (Hyperliquid) and are **not**
  converted to USD; ``quote_ccy`` says which.
* Hyperliquid documents maintenance margin as half the initial margin at max
  leverage, so ``maintenance_margin_rate = 0.5 / max_leverage`` (first tier).
* Because these venues list hundreds of contracts, the per-symbol calls
  (funding history, estimates, marks) require an ``underlying`` filter.
* Hyperliquid HIP-3 (builder-deployed) markets are included: ccxt names them
  ``xyz:GOLD`` and that stays the ``symbol``; the ``underlying`` is the name with
  the dex prefix stripped (``GOLD``), so tickers shared with other venues join
  (``NVDA``). No further mapping: ``GOLD`` does not become ``XAU``. Builders do
  not publish an asset class (``unknown``), a funding cap, or a maintenance
  schedule (their margin tables are deployer-set), so those stay NULL. The same
  stripped name can mean different things on different dexes (main-dex ``STX``
  is Stacks; ``para:STX`` is Seagate): the ``dex`` column tells them apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from .. import offshore_client, replay
from ..model import BookLevel, Candle, FundingPoint, Instrument, Mark, Trade
from ..normalize import from_epoch_ms, interval_from_times, split_unit_prefix, to_float
from .base import Adapter, Capability, VenueError

#: Most instruments a per-symbol call will fan out to.
MAX_SYMBOLS = 4
_PAGE = 500
_MAX_PAGES = 200
_TIMEFRAME = {"1m": "1m", "1h": "1h", "1d": "1d"}


def _funding_interval(venue: str, market: dict[str, Any]) -> float | None:
    info = market.get("info") or {}
    if venue == "hyperliquid":
        return 1.0
    if venue == "bybit":
        minutes = to_float(info.get("fundingInterval"))
        return None if not minutes else minutes / 60.0
    hours = to_float(info.get("fundingIntervalHours"))
    return hours


def instrument_from_market(venue: str, market: dict[str, Any]) -> Instrument | None:
    """Normalize one ccxt unified market; ``None`` for anything not a live linear perp."""
    if not market.get("swap") or not market.get("linear") or market.get("active") is False:
        return None
    info = market.get("info") or {}
    dex = str(info.get("dex") or "") if venue == "hyperliquid" and info.get("hip3") else ""
    base = str(market.get("baseName") or market.get("base") or "")
    if dex:
        base = base.split(":", 1)[1] if ":" in base else base
    underlying, multiplier = split_unit_prefix(base)
    if not underlying or "-" in underlying or ":" in underlying or ":" in str(market.get("id", "")):
        return None
    contract = (to_float(market.get("contractSize")) or 1.0) * multiplier
    factor = 1.0 / multiplier
    tick = to_float((market.get("precision") or {}).get("price"))
    max_leverage = to_float(((market.get("limits") or {}).get("leverage") or {}).get("max"))
    if venue == "hyperliquid":
        max_leverage = to_float(info.get("maxLeverage")) or max_leverage
    mm = 0.5 / max_leverage if venue == "hyperliquid" and max_leverage and not dex else None
    quote = str(market.get("settle") or market.get("quote") or "")
    return Instrument(
        venue=venue,
        # Hyperliquid's ccxt id is an asset index ("0"); its coin name ("BTC", "kPEPE") is the symbol.
        symbol=str(info.get("name") or market["id"]) if venue == "hyperliquid" else str(market["id"]),
        underlying=underlying.upper(),
        asset_class="unknown" if dex else "crypto",
        quote_ccy=quote,
        instrument_type="perpetual",
        status="active",
        contract_size=contract,
        price_factor=factor,
        native_price_unit=f"{quote} per {base}" if multiplier == 1.0 else f"{quote} per {base} ({multiplier:g} units)",
        tick_size=None if tick is None else tick * factor,
        funding_interval_hours=_funding_interval(venue, market),
        funding_cap=0.005 if venue == "hyperliquid" and not dex else None,
        max_leverage=max_leverage,
        initial_margin_rate=None if not max_leverage else 1.0 / max_leverage,
        maintenance_margin_rate=mm,
        margin_source=(
            "hyperliquid docs: maintenance = half of initial margin at max leverage (first margin tier)"
            if mm
            else (
                "hyperliquid HIP-3 maxLeverage (initial only; the builder's margin table is not published)"
                if dex and max_leverage
                else ("ccxt limits.leverage.max (initial only)" if max_leverage else None)
            )
        ),
        extra={"ccxt_symbol": str(market["symbol"]), **({"dex": dex} if dex else {})},
    )


def _second(ts: datetime) -> datetime:
    """Round a millisecond venue stamp (``19:00:00.035``) to the funding second."""
    return (ts + timedelta(milliseconds=500)).replace(microsecond=0)


class OffshoreAdapter(Adapter):
    """One ccxt-backed offshore venue."""

    venue_type = "offshore"
    sign_convention = "ccxt unified: positive = longs pay shorts (no flip)"
    exchange_note: ClassVar[str] = ""

    def __init__(self) -> None:
        """Initialize the shared instrument cache."""
        super().__init__()

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return offshore_client.call(self.name, method, *args, **kwargs)
        except offshore_client.OffshoreError as exc:
            raise VenueError(str(exc)) from exc

    def load_instruments(self) -> list[Instrument]:
        """Live linear perpetuals from ccxt ``fetch_markets``."""
        out = []
        for market in self._call("fetch_markets"):
            inst = instrument_from_market(self.name, market)
            if inst is not None:
                out.append(inst)
        return out

    def _narrow(self, instruments: list[Instrument], what: str) -> list[str]:
        if len(instruments) > MAX_SYMBOLS:
            raise VenueError(
                f"{self.name}: {what} makes one request per contract; pass underlying := '<SYMBOL>' "
                f"(matched {len(instruments)} contracts, limit {MAX_SYMBOLS})"
            )
        return [i.extra["ccxt_symbol"] for i in instruments]

    def funding(self, instruments: list[Instrument], start: datetime, end: datetime) -> list[FundingPoint]:
        """Funding history per symbol, paged forward by time."""
        self._narrow(instruments, "funding history")
        out: list[FundingPoint] = []
        end_ms = int(end.timestamp() * 1000)
        for inst in instruments:
            since = int(start.timestamp() * 1000)
            rows: dict[datetime, float] = {}
            for _ in range(_MAX_PAGES):
                page = self._call("fetch_funding_rate_history", inst.extra["ccxt_symbol"], since, _PAGE)
                fresh = [r for r in page if r.get("timestamp") is not None and int(r["timestamp"]) >= since]
                for row in fresh:
                    when = from_epoch_ms(row["timestamp"])
                    rate = to_float(row.get("fundingRate"))
                    if when is not None and rate is not None and start <= when <= end:
                        rows[_second(when)] = rate
                if not fresh:
                    break
                newest = max(int(r["timestamp"]) for r in fresh)
                if newest >= end_ms or len(page) < _PAGE:
                    break
                since = newest + 1
            hours = inst.funding_interval_hours or interval_from_times(rows) or 8.0
            out += [FundingPoint(inst, when, rate, hours, True) for when, rate in rows.items()]
        return out

    def _groups(self, instruments: list[Instrument]) -> list[list[str]]:
        """Split ccxt symbols into the groups one ccxt call can serve (one group, except Hyperliquid)."""
        return [[i.extra["ccxt_symbol"] for i in instruments]] if instruments else []

    def _funding_rates(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        self._narrow(instruments, "funding estimates")
        out: dict[str, dict[str, Any]] = {}
        for group in self._groups(instruments):
            out.update({str(k): v for k, v in (self._call("fetch_funding_rates", group) or {}).items()})
        return out

    def _tickers(self, instruments: list[Instrument]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for group in self._groups(instruments):
            out.update({str(k): v for k, v in (self._call("fetch_tickers", group) or {}).items()})
        return out

    def funding_estimates(self, instruments: list[Instrument]) -> list[FundingPoint]:
        """Return the venue's current (provisional) rate for the in-progress interval."""
        rates = self._funding_rates(instruments)
        out = []
        for inst in instruments:
            row = rates.get(inst.extra["ccxt_symbol"]) or {}
            rate = to_float(row.get("fundingRate"))
            when = from_epoch_ms(row.get("fundingTimestamp") or row.get("nextFundingTimestamp"))
            if rate is None or when is None:
                continue
            hours = _interval_text(row.get("interval")) or inst.funding_interval_hours or 8.0
            out.append(
                FundingPoint(
                    inst,
                    when,
                    rate,
                    hours,
                    False,
                    mark_price=inst.price(to_float(row.get("markPrice"))),
                    index_price=inst.price(to_float(row.get("indexPrice"))),
                )
            )
        return out

    def marks(self, instruments: list[Instrument]) -> list[Mark]:
        """Mark/index/funding from ``fetch_funding_rates``, bid/ask/last from ``fetch_tickers``."""
        rates = self._funding_rates(instruments)
        tickers = self._tickers(instruments)
        as_of = replay.now()
        out = []
        for inst in instruments:
            sym = inst.extra["ccxt_symbol"]
            rate = rates.get(sym) or {}
            tick = tickers.get(sym) or {}
            info = {**(tick.get("info") or {}), **(rate.get("info") or {})}
            mark = inst.price(to_float(rate.get("markPrice")) or to_float(tick.get("markPrice")))
            oi_base = to_float(info.get("openInterest"))
            oi = None if oi_base is None else oi_base / inst.contract_size * (1.0 / inst.price_factor)
            stamp = from_epoch_ms(tick.get("timestamp")) or as_of
            next_time = from_epoch_ms(rate.get("fundingTimestamp") or rate.get("nextFundingTimestamp"))
            out.append(
                Mark(
                    instrument=inst,
                    as_of=stamp,
                    mark_price=mark,
                    mark_source="venue_mark" if mark is not None else "unavailable",
                    index_price=inst.price(to_float(rate.get("indexPrice")) or to_float(tick.get("indexPrice"))),
                    last_price=inst.price(to_float(tick.get("last"))),
                    bid=inst.price(to_float(tick.get("bid"))),
                    ask=inst.price(to_float(tick.get("ask"))),
                    funding_rate=to_float(rate.get("fundingRate")),
                    funding_rate_is_estimate=True if rate.get("fundingRate") is not None else None,
                    funding_time=None,
                    next_funding_time=next_time,
                    open_interest_contracts=oi,
                    open_interest_usd=None if oi is None or mark is None else oi * inst.contract_size * mark,
                    volume_24h_usd=to_float(tick.get("quoteVolume")),
                )
            )
        return out

    def order_book(self, inst: Instrument, depth: int) -> tuple[list[BookLevel], list[BookLevel]]:
        """Ccxt order book; amounts are contracts."""
        book = self._call("fetch_order_book", inst.extra["ccxt_symbol"], depth) or {}

        def levels(side: str, reverse: bool) -> list[BookLevel]:
            out = [
                BookLevel(float(p) * inst.price_factor, float(a))
                for p, a, *_ in book.get(side) or []
                if p is not None and a
            ]
            return sorted(out, key=lambda lv: -lv.price if reverse else lv.price)[:depth]

        return levels("bids", True), levels("asks", False)

    def trades(self, inst: Instrument, start: datetime, end: datetime, cursor: str) -> tuple[list[Trade], str | None]:
        """Trades paged forward from ``start``; cursor = next ``since`` in ms. ``side`` is the taker side."""
        since = int(cursor) if cursor else int(start.timestamp() * 1000)
        rows = self._call("fetch_trades", inst.extra["ccxt_symbol"], since, 1000) or []
        out = []
        for row in rows:
            when = from_epoch_ms(row.get("timestamp"))
            price, amount = to_float(row.get("price")), to_float(row.get("amount"))
            if when is None or price is None or amount is None or int(row["timestamp"]) < since:
                continue
            if start <= when <= end:
                out.append(Trade(str(row.get("id")), when, price * inst.price_factor, amount, row.get("side"), None))
        stamps = [int(r["timestamp"]) for r in rows if r.get("timestamp") is not None and int(r["timestamp"]) >= since]
        if not stamps or max(stamps) >= int(end.timestamp() * 1000) or len(rows) < 1000:
            return out, None
        return out, str(max(stamps) + 1)

    def candles(self, inst: Instrument, start: datetime, end: datetime, bar: str) -> list[Candle]:
        """OHLCV paged forward; ccxt volume is base units, converted to contracts."""
        since = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        out: dict[datetime, Candle] = {}
        for _ in range(_MAX_PAGES):
            rows = self._call("fetch_ohlcv", inst.extra["ccxt_symbol"], _TIMEFRAME[bar], since, _PAGE) or []
            fresh = [r for r in rows if int(r[0]) >= since]
            for ts, o, h, low, c, v in (r[:6] for r in fresh):
                when = datetime.fromtimestamp(int(ts) / 1000, tz=UTC)
                vol = to_float(v)
                # ccxt volume is in the market's named unit (1000PEPE); coins = vol / price_factor.
                contracts = None if vol is None else vol / inst.price_factor / inst.contract_size
                close = inst.price(to_float(c))
                out[when] = Candle(
                    when,
                    inst.price(to_float(o)),
                    inst.price(to_float(h)),
                    inst.price(to_float(low)),
                    close,
                    contracts,
                    None
                    if vol is None or close is None or contracts is None
                    else contracts * inst.contract_size * close,
                    None,
                )
            if not fresh or len(rows) < _PAGE or int(fresh[-1][0]) >= end_ms:
                break
            since = int(fresh[-1][0]) + 1
        return [c for ts, c in sorted(out.items()) if start <= ts <= end]


def _interval_text(value: object) -> float | None:
    """Ccxt ``interval`` (``"8h"``, ``"1h"``, ``"240m"``) -> hours."""
    if not isinstance(value, str) or len(value) < 2:
        return None
    number = to_float(value[:-1])
    if number is None:
        return None
    return {"h": number, "m": number / 60.0, "d": number * 24.0}.get(value[-1])


def _capabilities(note: str, margin: Capability) -> dict[str, Capability]:
    return {
        "instruments": Capability("supported", False, "current", "ccxt fetch_markets (linear swaps)"),
        "funding_history": Capability(
            "supported", False, "venue-dependent", f"ccxt fetch_funding_rate_history; {note}"
        ),
        "funding_estimate": Capability("supported", False, "current interval", "ccxt fetch_funding_rates"),
        "marks": Capability("supported", False, "current", "ccxt fetch_funding_rates + fetch_tickers"),
        "premium_index": Capability("unsupported", False, "n/a", "not exposed through ccxt's unified API"),
        "open_interest": Capability("partial", False, "current", "from ticker/asset-context info where present"),
        "order_book": Capability("supported", False, "current", "ccxt fetch_order_book"),
        "trades": Capability("partial", False, "recent", "ccxt fetch_trades (history depth varies by venue)"),
        "candles": Capability("supported", False, "venue-dependent", "ccxt fetch_ohlcv"),
        "maintenance_margin": margin,
    }


class HyperliquidAdapter(OffshoreAdapter):
    """Hyperliquid perps: the main dex plus HIP-3 builder-deployed dexes (xyz, para, io, ...)."""

    name = "hyperliquid"
    display_name = "Hyperliquid (offshore benchmark)"
    #: Loading markets costs one request per HIP-3 dex plus two; cache them longer.
    instrument_ttl = 600.0
    regulator = "none (decentralized; not available to US persons per its terms)"
    capabilities = {
        **_capabilities(
            "hourly funding",
            Capability("supported", False, "current", "docs: maintenance = 0.5 / max leverage (first tier)"),
        ),
        "trades": Capability(
            "unsupported", False, "n/a", "no public REST trade history (ccxt fetchTrades needs a wallet address)"
        ),
    }

    def _groups(self, instruments: list[Instrument]) -> list[list[str]]:
        """One group per dex: ccxt infers a single dex for funding rates and tickers from the symbol list."""
        by_dex: dict[str, list[str]] = {}
        for inst in instruments:
            by_dex.setdefault(inst.extra.get("dex", ""), []).append(inst.extra["ccxt_symbol"])
        return [by_dex[dex] for dex in sorted(by_dex)]

    def trades(self, inst: Instrument, start: datetime, end: datetime, cursor: str) -> tuple[list[Trade], str | None]:
        """Hyperliquid publishes public trades on WebSocket only."""
        raise self.unsupported("trades")


class BinanceAdapter(OffshoreAdapter):
    """Binance USD-M futures."""

    name = "binance"
    display_name = "Binance USD-M Futures (offshore benchmark)"
    regulator = "none in the US (geo-blocks US IPs with HTTP 451)"
    capabilities = _capabilities(
        "interval inferred from history spacing",
        Capability("unsupported", True, "n/a", "leverage brackets are a signed (authenticated) endpoint"),
    )


class BybitAdapter(OffshoreAdapter):
    """Bybit linear perpetuals."""

    name = "bybit"
    display_name = "Bybit linear perpetuals (offshore benchmark)"
    regulator = "none in the US (CloudFront geo-blocks US IPs with HTTP 403)"
    capabilities = _capabilities(
        "fundingInterval from instrument info",
        Capability("unsupported", False, "n/a", "risk-limit tiers not wired through the allowlisted ccxt methods"),
    )
