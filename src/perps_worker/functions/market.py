"""Normalized market data: funding, marks, order books, trades, candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import ProcessParams, bind_fixed_schema, init_single_worker

from ..meta import object_tags
from ..model import FundingPoint, Mark
from ..normalize import annualize, premium_bps
from ..venues import BARS, Adapter, VenueError, get, select
from .common import (
    F64,
    STR,
    TS,
    ArgumentError,
    EndArg,
    RequiredVenueArg,
    StartArg,
    SymbolArg,
    UnderlyingArg,
    VenueArg,
    VenueScan,
    build_schema,
    filter_instruments,
    instrument_row,
    result_columns,
    window,
)

ID_COLUMNS = [
    ("venue", STR, "Venue name.", False),
    ("symbol", STR, "Venue-native contract symbol.", False),
    ("canonical_id", STR, "'<UNDERLYING>-<QUOTE>-PERP', shared across venues.", False),
    ("underlying", STR, "Canonical underlying; join venues on this.", False),
]


def per_contract_scan(params: ProcessParams[Any]) -> tuple[list[Adapter], bool]:
    """Venues for a per-contract scan: offshore venues only when an underlying narrows them."""
    venue = params.args.venue
    if venue is not None:
        return [get(venue)], False
    adapters = select(None)
    if params.args.underlying is None:
        adapters = [a for a in adapters if a.venue_type == "us_regulated"]
    return adapters, True


# --------------------------------------------------------------------- funding

FUNDING_SCHEMA = build_schema(
    [
        *ID_COLUMNS,
        ("funding_time", TS, "When the rate settled (or, for an estimate, will settle), UTC.", False),
        ("rate_per_interval", F64, "Funding rate for the interval, decimal; positive = longs pay shorts.", False),
        ("interval_hours", F64, "Length of the funding interval in hours.", False),
        ("rate_annualized", F64, "Simple annualization: rate_per_interval * 8760 / interval_hours.", False),
        ("rate_bps", F64, "rate_per_interval in basis points.", False),
        ("mark_price", F64, "Mark price at funding, quote currency per underlying unit, where published.", True),
        ("index_price", F64, "Index price at funding, quote currency per underlying unit, where published.", True),
        ("premium_bps", F64, "(mark_price / index_price - 1) in basis points, when both are present.", True),
        ("is_final", pa.bool_(), "True for a settled rate; false for the venue's in-progress estimate.", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class FundingArgs:
    """Arguments for ``funding(venue, underlying, start_date, end_date, include_estimates)``."""

    venue: VenueArg
    underlying: UnderlyingArg
    start_date: StartArg
    end_date: EndArg
    include_estimates: Annotated[
        bool,
        Arg(
            "include_estimates",
            default=False,
            doc="Append each venue's in-progress estimate (is_final = false) where available (default false).",
        ),
    ]


def funding_row(p: FundingPoint) -> dict[str, Any]:
    """One normalized funding row."""
    return {
        **instrument_row(p.instrument),
        "funding_time": p.funding_time,
        "rate_per_interval": p.rate,
        "interval_hours": p.interval_hours,
        "rate_annualized": annualize(p.rate, p.interval_hours),
        "rate_bps": p.rate * 10_000.0,
        "mark_price": p.mark_price,
        "index_price": p.index_price,
        "premium_bps": premium_bps(p.mark_price, p.index_price),
        "is_final": p.is_final,
    }


@init_single_worker
@bind_fixed_schema
class Funding(VenueScan[FundingArgs]):
    """Funding history, one sign convention."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = FUNDING_SCHEMA

    class Meta:
        """Function metadata."""

        name = "funding"
        description = (
            "Funding-rate history across venues, positive = longs pay shorts, with per-interval and annualized rates"
        )
        tags = {
            **object_tags(
                title="Funding Rate History",
                category="market_data",
                keywords="funding rate, funding history, perpetual funding, annualized funding, carry, premium",
                doc_llm=(
                    "Settled funding rates, one row per contract per funding time. `rate_per_interval` is a "
                    "decimal fraction (0.0001 = 1 bp) and positive always means longs pay shorts; "
                    "`rate_annualized` = rate * 8760 / `interval_hours` (simple, not compounded); `rate_bps`; "
                    "`mark_price`/`index_price`/`premium_bps` where the venue publishes them; `is_final`. "
                    "Arguments: `venue := 'kalshi'`, `underlying := 'BTC'`, `start_date`/`end_date` "
                    "(`TIMESTAMPTZ`, default the last 7 days), `include_estimates := true` to add each venue's "
                    "in-progress estimate as is_final = false. Coinbase publishes only its latest hourly rate "
                    "publicly. Offshore venues are included only when `underlying` is given."
                ),
                doc_md=(
                    "## funding(venue := NULL, underlying := NULL, start_date := NULL, end_date := NULL, "
                    "include_estimates := false)\n\nFunding history in one sign convention (positive = longs "
                    "pay shorts) with simple annualization."
                ),
            ),
            "vgi.result_columns_schema": result_columns(FUNDING_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT venue, avg(rate_annualized) AS mean_apr, count(*) AS settlements "
                    "FROM perps.main.funding(underlying := 'BTC', start_date := now() - INTERVAL 7 DAY) "
                    "GROUP BY venue ORDER BY mean_apr DESC"
                ),
                description="Average annualized BTC funding per venue over the last week",
            ),
            FunctionExample(
                sql=(
                    "SELECT funding_time, rate_bps, premium_bps FROM perps.main.funding(venue := 'bitnomial', "
                    "underlying := 'ETH') ORDER BY funding_time DESC LIMIT 9"
                ),
                description="Bitnomial's last nine ETH funding settlements with the mark-vs-index premium",
            ),
        ]

    @classmethod
    def venue_list(cls, params: ProcessParams[FundingArgs]) -> tuple[list[Adapter], bool]:
        """Offshore venues need an underlying."""
        return per_contract_scan(params)

    @classmethod
    def produce(
        cls, params: ProcessParams[FundingArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Settled history (plus estimates) for one venue."""
        start, end = window(params.args.start_date, params.args.end_date)
        insts = filter_instruments(adapter, params.args.underlying, include_inactive=True)
        if not insts:
            return [], None
        points = adapter.funding(insts, start, end)
        if params.args.include_estimates:
            active = [i for i in insts if i.status == "active"]
            try:
                points += adapter.funding_estimates(active)
            except VenueError:
                if params.args.venue is not None and adapter.capabilities["funding_estimate"].support != "unsupported":
                    raise
        points.sort(key=lambda p: (p.instrument.symbol, p.funding_time, p.is_final))
        return [funding_row(p) for p in points], None


# ----------------------------------------------------------------------- marks

MARKS_SCHEMA = build_schema(
    [
        *ID_COLUMNS,
        ("as_of", TS, "Timestamp of the snapshot (for Bitnomial: the last funding calculation).", True),
        ("mark_price", F64, "Mark price, quote currency per underlying unit (see mark_source).", True),
        ("mark_source", STR, "'venue_mark', 'mid' (no public mark), 'last_funding_mark', or 'unavailable'.", False),
        ("index_price", F64, "The venue's index/reference price per underlying unit.", True),
        ("last_price", F64, "Last traded price per underlying unit.", True),
        ("bid", F64, "Best bid per underlying unit.", True),
        ("ask", F64, "Best ask per underlying unit.", True),
        ("premium_bps", F64, "(mark_price / index_price - 1) in basis points.", True),
        ("funding_rate", F64, "Current funding rate per interval (estimate or latest settled), decimal.", True),
        (
            "funding_rate_is_estimate",
            pa.bool_(),
            "True for an in-progress estimate, false for the latest settled rate.",
            True,
        ),
        ("funding_rate_annualized", F64, "funding_rate * 8760 / funding_interval_hours.", True),
        ("funding_interval_hours", F64, "Funding interval in hours.", True),
        ("next_funding_time", TS, "Next scheduled funding time, UTC.", True),
        ("open_interest_contracts", F64, "Open interest in contracts (one side).", True),
        ("open_interest_usd", F64, "Open interest notional in quote currency.", True),
        ("volume_24h_usd", F64, "Trailing 24-hour traded notional in quote currency.", True),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class MarksArgs:
    """Arguments for ``marks(venue := NULL, underlying := NULL)``."""

    venue: VenueArg
    underlying: UnderlyingArg


def mark_row(m: Mark) -> dict[str, Any]:
    """One normalized marks row."""
    inst = m.instrument
    hours = inst.funding_interval_hours
    return {
        **instrument_row(inst),
        "as_of": m.as_of,
        "mark_price": m.mark_price,
        "mark_source": m.mark_source,
        "index_price": m.index_price,
        "last_price": m.last_price,
        "bid": m.bid,
        "ask": m.ask,
        "premium_bps": premium_bps(m.mark_price, m.index_price),
        "funding_rate": m.funding_rate,
        "funding_rate_is_estimate": m.funding_rate_is_estimate,
        "funding_rate_annualized": annualize(m.funding_rate, hours),
        "funding_interval_hours": hours,
        "next_funding_time": m.next_funding_time,
        "open_interest_contracts": m.open_interest_contracts,
        "open_interest_usd": m.open_interest_usd,
        "volume_24h_usd": m.volume_24h_usd,
    }


@init_single_worker
@bind_fixed_schema
class Marks(VenueScan[MarksArgs]):
    """Live mark, index, quotes, funding and open interest."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = MARKS_SCHEMA

    class Meta:
        """Function metadata."""

        name = "marks"
        description = (
            "Live snapshot per contract: mark, index, bid/ask, premium, current funding, open interest in "
            "contracts and USD"
        )
        tags = {
            **object_tags(
                title="Live Marks and Open Interest",
                category="market_data",
                keywords="mark price, index price, premium, basis, open interest, next funding, bid ask, snapshot",
                doc_llm=(
                    "One row per active contract with a live snapshot, all prices per underlying unit: "
                    "`mark_price` (`mark_source` says whether it is the venue's mark, the mid because the "
                    "venue publishes none, or the mark at the last funding), `index_price`, `last_price`, "
                    "`bid`, `ask`, `premium_bps` = mark vs index, `funding_rate` (estimate or latest settled; "
                    "`funding_rate_is_estimate`), `funding_rate_annualized`, `next_funding_time`, "
                    "`open_interest_contracts`, `open_interest_usd`, `volume_24h_usd`. Kalshi and Bitnomial "
                    "funding estimates are fetched only when `underlying` narrows the set (one request per "
                    "contract). Offshore venues need `underlying`. Values change every call."
                ),
                doc_md=(
                    "## marks(venue := NULL, underlying := NULL)\n\nLive mark, index, quotes, premium, current "
                    "funding and open interest, normalized per underlying unit."
                ),
            ),
            "vgi.result_columns_schema": result_columns(MARKS_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT venue, symbol, mark_price, premium_bps, funding_rate_annualized, open_interest_usd "
                    "FROM perps.main.marks(underlying := 'ETH') ORDER BY open_interest_usd DESC NULLS LAST"
                ),
                description="ETH perps side by side: price, premium, carry and size",
            ),
            FunctionExample(
                sql=(
                    "SELECT underlying, open_interest_usd FROM perps.main.marks(venue := 'kalshi') "
                    "ORDER BY open_interest_usd DESC NULLS LAST LIMIT 10"
                ),
                description="Kalshi's ten largest perps by open-interest notional",
            ),
        ]

    @classmethod
    def venue_list(cls, params: ProcessParams[MarksArgs]) -> tuple[list[Adapter], bool]:
        """Offshore venues need an underlying."""
        return per_contract_scan(params)

    @classmethod
    def produce(
        cls, params: ProcessParams[MarksArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Snapshot for one venue."""
        insts = filter_instruments(adapter, params.args.underlying)
        if not insts:
            return [], None
        marks = sorted(adapter.marks(insts), key=lambda m: m.instrument.symbol)
        return [mark_row(m) for m in marks], None


# ------------------------------------------------------------------ order book

BOOK_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("symbol", STR, "Venue-native contract symbol.", False),
        ("side", STR, "'bid' or 'ask'.", False),
        ("level", pa.int32(), "1 = best price on that side.", False),
        ("price", F64, "Level price, quote currency per underlying unit.", False),
        ("size_contracts", F64, "Resting size in contracts.", False),
        ("size_base", F64, "Resting size in underlying units (contracts * contract_size).", False),
        ("size_usd", F64, "Resting notional in quote currency (size_base * price).", False),
        ("cum_usd", F64, "Cumulative notional from the best level to this one, per side.", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class BookArgs:
    """Arguments for ``order_book(venue, symbol, depth := 50)``."""

    venue: RequiredVenueArg
    symbol: SymbolArg
    depth: Annotated[int, Arg("depth", default=50, doc="Levels per side, 1 to 500 (default 50).", ge=1, le=500)]


def book_rows(adapter: Adapter, symbol: str, depth: int) -> list[dict[str, Any]]:
    """Build normalized book rows for one contract."""
    inst = adapter.instrument(symbol)
    bids, asks = adapter.order_book(inst, depth)
    rows = []
    for side, levels in (("bid", bids), ("ask", asks)):
        cum = 0.0
        for n, lv in enumerate(levels, start=1):
            base = lv.contracts * inst.contract_size
            usd = base * lv.price
            cum += usd
            rows.append(
                {
                    "venue": adapter.name,
                    "symbol": inst.symbol,
                    "side": side,
                    "level": n,
                    "price": lv.price,
                    "size_contracts": lv.contracts,
                    "size_base": base,
                    "size_usd": usd,
                    "cum_usd": cum,
                }
            )
    return rows


@init_single_worker
@bind_fixed_schema
class OrderBook(VenueScan[BookArgs]):
    """Aggregated order book, normalized units."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = BOOK_SCHEMA

    class Meta:
        """Function metadata."""

        name = "order_book"
        description = (
            "Aggregated order book for one contract with sizes in contracts, underlying units and USD, "
            "plus cumulative depth"
        )
        tags = {
            **object_tags(
                title="Order Book Depth",
                category="market_data",
                keywords="order book, depth, liquidity, bids, asks, l2, market depth, cumulative depth",
                doc_llm=(
                    "Current aggregated book for one contract: `order_book('kalshi', 'KXBTCPERP', depth := 20)`. "
                    "The venue and symbol are positional (symbol from instruments().symbol). One row per level "
                    "and side: `side` ('bid'/'ask'), `level` (1 = best), `price` per underlying unit, "
                    "`size_contracts`, `size_base` (underlying units), `size_usd`, `cum_usd` (cumulative per "
                    "side). Bitnomial books are WebSocket-only and raise an error."
                ),
                doc_md="## order_book(venue, symbol, depth := 50)\n\nNormalized L2 book with cumulative notional.",
            ),
            "vgi.result_columns_schema": result_columns(BOOK_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT side, max(cum_usd) AS depth_usd FROM perps.main.order_book('kalshi', 'KXBTCPERP', "
                    "depth := 25) GROUP BY side ORDER BY side"
                ),
                description="Notional resting in the top 25 levels of Kalshi's BTC perp, per side",
            ),
            FunctionExample(
                sql=(
                    "SELECT side, level, price, size_usd FROM perps.main.order_book('coinbase', "
                    "'BIP-20DEC30-CDE', depth := 5) ORDER BY side, level"
                ),
                description="Top five levels of Coinbase's nano BTC perp",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[BookArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return the whole book in one batch."""
        return book_rows(adapter, params.args.symbol, params.args.depth), None


# ---------------------------------------------------------------------- trades

TRADES_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("symbol", STR, "Venue-native contract symbol.", False),
        ("trade_id", STR, "Venue trade identifier.", False),
        ("ts", TS, "Execution time, UTC.", False),
        ("price", F64, "Trade price, quote currency per underlying unit.", False),
        ("size_contracts", F64, "Trade size in contracts.", False),
        ("size_usd", F64, "Trade notional in quote currency.", False),
        ("taker_side", STR, "'buy' or 'sell' from the aggressor's side; NULL where the venue does not say.", True),
        ("is_block_trade", pa.bool_(), "True for an off-book block trade, where the venue flags it.", True),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class TradesArgs:
    """Arguments for ``trades(venue, symbol, start_date, end_date, max_trades)``."""

    venue: RequiredVenueArg
    symbol: SymbolArg
    start_date: Annotated[
        datetime | None,
        Arg(
            "start_date",
            default=None,
            arrow_type=TS,
            doc="Start of the time window. Default: one hour before end_date.",
        ),
    ]
    end_date: EndArg
    max_trades: Annotated[
        int,
        Arg(
            "max_trades",
            default=10_000,
            doc="Stop after this many trades, 1 to 1,000,000 (default 10,000).",
            ge=1,
            le=1_000_000,
        ),
    ]


@init_single_worker
@bind_fixed_schema
class Trades(VenueScan[TradesArgs]):
    """Public trades, paged with an externalized cursor."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = TRADES_SCHEMA

    class Meta:
        """Function metadata."""

        name = "trades"
        description = "Public trades for one contract in a time window, normalized price and USD size, with taker side"
        tags = {
            **object_tags(
                title="Public Trades",
                category="market_data",
                keywords="trades, prints, tape, time and sales, taker side, block trades, executions",
                doc_llm=(
                    "Public trades for one contract: `trades('kalshi', 'KXBTCPERP', start_date := now() - "
                    "INTERVAL 10 MINUTE)`. Venue and symbol are positional. Columns: `trade_id`, `ts`, `price` "
                    "per underlying unit, `size_contracts`, `size_usd`, `taker_side` ('buy'/'sell'; Coinbase "
                    "publishes the maker side, so it is inverted), `is_block_trade`. Default window is the last "
                    "hour; `max_trades` caps the result (default 10,000). Pages stream in batches. Bitnomial "
                    "trades are WebSocket-only and raise an error."
                ),
                doc_md=(
                    "## trades(venue, symbol, start_date := NULL, end_date := NULL, max_trades := 10000)\n\n"
                    "Public prints with aggressor side and USD size."
                ),
            ),
            "vgi.result_columns_schema": result_columns(TRADES_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT taker_side, count(*) AS n, sum(size_usd) AS usd FROM perps.main.trades('kalshi', "
                    "'KXBTCPERP', start_date := now() - INTERVAL 15 MINUTE) GROUP BY taker_side ORDER BY taker_side"
                ),
                description="Aggressor buy vs sell notional on Kalshi BTC over 15 minutes",
            ),
            FunctionExample(
                sql=(
                    "SELECT date_trunc('minute', ts) AS minute, sum(size_usd) AS usd FROM perps.main.trades("
                    "'coinbase', 'BIP-20DEC30-CDE', max_trades := 2000) GROUP BY minute ORDER BY minute"
                ),
                description="Per-minute traded notional on Coinbase's BTC perp",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[TradesArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of trades, newest first (the shared loop enforces max_trades)."""
        start, end = window(params.args.start_date, params.args.end_date, default=timedelta(hours=1))
        inst = adapter.instrument(params.args.symbol)
        trades, next_cursor = adapter.trades(inst, start, end, cursor)
        rows = [
            {
                "venue": adapter.name,
                "symbol": inst.symbol,
                "trade_id": t.trade_id,
                "ts": t.ts,
                "price": t.price,
                "size_contracts": t.contracts,
                "size_usd": t.contracts * inst.contract_size * t.price,
                "taker_side": t.taker_side,
                "is_block_trade": t.is_block,
            }
            for t in sorted(trades, key=lambda t: (t.ts, t.trade_id), reverse=True)
        ]
        return rows, next_cursor


# --------------------------------------------------------------------- candles

CANDLES_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("symbol", STR, "Venue-native contract symbol.", False),
        ("ts", TS, "Bar start, UTC.", False),
        ("bar", STR, "Bar length: '1m', '1h' or '1d'.", False),
        ("open", F64, "First trade price per underlying unit; NULL if no trades in the bar.", True),
        ("high", F64, "Highest trade price per underlying unit.", True),
        ("low", F64, "Lowest trade price per underlying unit.", True),
        ("close", F64, "Last trade price per underlying unit.", True),
        ("volume_contracts", F64, "Traded contracts in the bar.", True),
        (
            "volume_usd",
            F64,
            "Traded notional in quote currency, where the venue publishes it or it can be derived.",
            True,
        ),
        ("open_interest_contracts", F64, "Open interest at bar end (Kalshi only).", True),
    ]
)

_DEFAULT_SPAN = {"1m": timedelta(days=1), "1h": timedelta(days=7), "1d": timedelta(days=90)}


@dataclass(slots=True, frozen=True, kw_only=True)
class CandlesArgs:
    """Arguments for ``candles(venue, symbol, start_date, end_date, bar := '1h')``."""

    venue: RequiredVenueArg
    symbol: SymbolArg
    start_date: Annotated[
        datetime | None,
        Arg(
            "start_date",
            default=None,
            arrow_type=TS,
            doc="Start of the time window. Default: 1 day (1m bars), 7 days (1h) or 90 days (1d) before end_date.",
        ),
    ]
    end_date: EndArg
    bar: Annotated[
        str, Arg("bar", default="1h", doc="Bar length: '1m', '1h' or '1d' (default '1h').", choices=list(BARS))
    ]


@init_single_worker
@bind_fixed_schema
class Candles(VenueScan[CandlesArgs]):
    """OHLCV bars, normalized prices."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CANDLES_SCHEMA

    class Meta:
        """Function metadata."""

        name = "candles"
        description = (
            "OHLCV bars for one contract (1m, 1h, 1d) with prices per underlying unit and volume in contracts and USD"
        )
        tags = {
            **object_tags(
                title="OHLCV Bars",
                category="market_data",
                keywords="candles, ohlcv, bars, klines, price history, volume, open interest history",
                doc_llm=(
                    "OHLCV bars for one contract: `candles('kalshi', 'KXETHPERP', bar := '1d')`. Venue and "
                    "symbol are positional; `bar` is '1m', '1h' (default) or '1d' (named `bar` because "
                    "`interval` is reserved). `ts` is the bar start (UTC); open/high/low/close per underlying "
                    "unit (NULL when nothing traded); `volume_contracts`, `volume_usd`, and "
                    "`open_interest_contracts` on Kalshi. Bitnomial serves daily bars only."
                ),
                doc_md=(
                    "## candles(venue, symbol, start_date := NULL, end_date := NULL, bar := '1h')\n\n"
                    "Normalized OHLCV bars."
                ),
            ),
            "vgi.result_columns_schema": result_columns(CANDLES_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT ts, close, volume_usd, open_interest_contracts FROM perps.main.candles('kalshi', "
                    "'KXBTCPERP', bar := '1d') ORDER BY ts DESC LIMIT 14"
                ),
                description="Two weeks of daily Kalshi BTC closes, volume and open interest",
            ),
            FunctionExample(
                sql=(
                    "SELECT max(high) - min(low) AS range_usd FROM perps.main.candles('coinbase', "
                    "'ETP-20DEC30-CDE', start_date := now() - INTERVAL 1 DAY)"
                ),
                description="Coinbase ETH perp trading range over the last day",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[CandlesArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """All bars in the window."""
        bar = params.args.bar
        if bar not in BARS:
            raise ArgumentError(f"bar must be one of {list(BARS)}")
        start, end = window(params.args.start_date, params.args.end_date, default=_DEFAULT_SPAN[bar])
        inst = adapter.instrument(params.args.symbol)
        return [
            {
                "venue": adapter.name,
                "symbol": inst.symbol,
                "ts": c.ts,
                "bar": bar,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume_contracts": c.volume_contracts,
                "volume_usd": c.volume_usd,
                "open_interest_contracts": c.open_interest_contracts,
            }
            for c in adapter.candles(inst, start, end, bar)
        ], None
