"""Derived, cross-venue analytics computed in code from the normalized data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import OutputCollector, ProcessParams, bind_fixed_schema, init_single_worker

from .. import derived
from ..meta import object_tags
from ..model import FundingPoint, Mark
from ..normalize import premium_bps
from ..venues import VENUE_NAMES, Adapter, VenueError, get, select
from .common import (
    F64,
    STR,
    TS,
    ArgumentError,
    EndArg,
    RequiredVenueArg,
    ScanState,
    StartArg,
    SymbolArg,
    UnderlyingArg,
    VenueArg,
    VenueScan,
    build_schema,
    emit_rows,
    filter_instruments,
    instrument_row,
    result_columns,
    warn,
    window,
)
from .market import per_contract_scan

# --------------------------------------------------------------------- premium

PREMIUM_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("symbol", STR, "Venue-native contract symbol.", False),
        ("canonical_id", STR, "'<UNDERLYING>-<QUOTE>-PERP'.", False),
        ("underlying", STR, "Canonical underlying.", False),
        ("as_of", TS, "Snapshot time of the contract's mark.", True),
        ("mark_price", F64, "Contract mark per underlying unit.", True),
        ("mark_source", STR, "'venue_mark', 'mid', 'last_funding_mark' or 'unavailable'.", False),
        ("reference_price", F64, "Reference price per underlying unit.", True),
        ("reference_source", STR, "'<venue> index' (default: the contract's own venue) or '<venue> mark'.", False),
        ("premium_bps", F64, "(mark_price / reference_price - 1) in basis points; positive = perp rich.", True),
        ("premium_usd", F64, "mark_price - reference_price, quote currency per underlying unit.", True),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class PremiumArgs:
    """Arguments for ``premium(venue, underlying, reference)``."""

    venue: VenueArg
    underlying: UnderlyingArg
    reference: Annotated[
        str | None,
        Arg(
            "reference",
            default=None,
            doc="NULL (default): each contract's own venue index. A venue name: that venue's index "
            "(or mark, if it has no index) for the same underlying, e.g. reference := 'hyperliquid'.",
            choices=[*VENUE_NAMES],
        ),
    ]


def reference_prices(venue: str, underlying: str | None) -> dict[str, tuple[float, str]]:
    """``underlying -> (price, source)`` from one venue's marks (index preferred)."""
    adapter = get(venue)
    insts = filter_instruments(adapter, underlying)
    out: dict[str, tuple[float, str]] = {}
    for m in sorted(adapter.marks(insts), key=lambda m: m.instrument.symbol):
        key = m.instrument.underlying
        if key in out:
            continue
        if m.index_price is not None:
            out[key] = (m.index_price, f"{venue} index")
        elif m.mark_price is not None:
            out[key] = (m.mark_price, f"{venue} mark")
    return out


def premium_row(m: Mark, ref: tuple[float, str] | None) -> dict[str, Any]:
    """One premium row against ``ref`` (or the venue index when ``ref`` is None)."""
    inst = m.instrument
    price, source = ref if ref else (m.index_price, f"{inst.venue} index")
    return {
        **instrument_row(inst),
        "as_of": m.as_of,
        "mark_price": m.mark_price,
        "mark_source": m.mark_source,
        "reference_price": price,
        "reference_source": source,
        "premium_bps": premium_bps(m.mark_price, price),
        "premium_usd": None if m.mark_price is None or price is None else m.mark_price - price,
    }


@init_single_worker
@bind_fixed_schema
class Premium(VenueScan[PremiumArgs]):
    """Mark versus index, or versus another venue."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = PREMIUM_SCHEMA

    class Meta:
        """Function metadata."""

        name = "premium"
        description = (
            "Perp premium in bps: each contract's mark against its own venue index, or against another venue's price"
        )
        tags = {
            **object_tags(
                title="Perp Premium to Index",
                category="analytics",
                keywords="premium, basis, mark vs index, perp premium, dislocation, cross-venue basis",
                doc_llm=(
                    "Premium of each contract's mark over a reference, in bps (positive = perp trades rich). "
                    "Default reference is the contract's own venue index; `reference := 'hyperliquid'` (any "
                    "venue name) compares against that venue's index for the same underlying (or its mark if "
                    "it publishes no index), which exposes cross-venue basis. Columns: `mark_price`, "
                    "`mark_source`, `reference_price`, `reference_source`, `premium_bps`, `premium_usd`. "
                    "Arguments `venue` and `underlying` filter as in marks(). Kalshi's index-perp reference "
                    "levels are venue-specific and not comparable to other venues' index perps."
                ),
                doc_md=(
                    "## premium(venue := NULL, underlying := NULL, reference := NULL)\n\nMark vs the venue "
                    "index, or vs another venue's price for the same underlying."
                ),
            ),
            "vgi.result_columns_schema": result_columns(PREMIUM_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT venue, symbol, premium_bps FROM perps.main.premium(underlying := 'BTC') "
                    "ORDER BY premium_bps DESC NULLS LAST"
                ),
                description="Which BTC perp trades richest to its own index",
            ),
            FunctionExample(
                sql=(
                    "SELECT venue, symbol, reference_source, premium_bps FROM perps.main.premium("
                    "underlying := 'ETH', reference := 'hyperliquid') ORDER BY venue"
                ),
                description="US ETH perps priced against Hyperliquid's oracle",
            ),
        ]

    @classmethod
    def venue_list(cls, params: ProcessParams[PremiumArgs]) -> tuple[list[Adapter], bool]:
        """Offshore venues need an underlying."""
        return per_contract_scan(params)

    @classmethod
    def produce(
        cls, params: ProcessParams[PremiumArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Premiums for one venue."""
        insts = filter_instruments(adapter, params.args.underlying)
        if not insts:
            return [], None
        refs = reference_prices(params.args.reference, params.args.underlying) if params.args.reference else {}
        marks = sorted(adapter.marks(insts), key=lambda m: m.instrument.symbol)
        if params.args.reference:
            return [
                premium_row(m, refs.get(m.instrument.underlying)) for m in marks if m.instrument.underlying in refs
            ], None
        return [premium_row(m, None) for m in marks], None


# -------------------------------------------------------------- funding_spread

SPREAD_SCHEMA = build_schema(
    [
        ("underlying", STR, "Canonical underlying both legs reference.", False),
        ("venue_a", STR, "Venue of leg A (alphabetically first of the pair).", False),
        ("symbol_a", STR, "Contract of leg A.", False),
        ("venue_b", STR, "Venue of leg B.", False),
        ("symbol_b", STR, "Contract of leg B.", False),
        ("annualized_a", F64, "Mean annualized funding of A over the window (decimal).", False),
        ("annualized_b", F64, "Mean annualized funding of B over the window (decimal).", False),
        ("spread_annualized", F64, "annualized_a - annualized_b: carry earned per year by short A / long B.", False),
        ("spread_bps", F64, "spread_annualized in basis points per year.", False),
        ("observations_a", pa.int64(), "Settled funding observations for A in the window.", False),
        ("observations_b", pa.int64(), "Settled funding observations for B in the window.", False),
        ("carry_trade", STR, "Which way collects the spread: 'short A / long B', the reverse, or 'flat'.", False),
        ("window_start", TS, "Start of the averaging window, UTC.", False),
        ("window_end", TS, "End of the averaging window, UTC.", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class SpreadArgs:
    """Arguments for ``funding_spread(underlying, start_date, end_date)``."""

    underlying: Annotated[str, Arg(0, doc="Canonical underlying to compare across venues, e.g. 'BTC'.")]
    start_date: StartArg
    end_date: EndArg


@init_single_worker
@bind_fixed_schema
class FundingSpread(VenueScan[SpreadArgs]):
    """Pairwise cross-venue carry over a window."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = SPREAD_SCHEMA

    class Meta:
        """Function metadata."""

        name = "funding_spread"
        description = (
            "Cross-venue funding carry for one underlying: mean annualized rates per contract and the "
            "spread for every venue pair"
        )
        tags = {
            **object_tags(
                title="Cross-Venue Funding Carry Spread",
                category="analytics",
                keywords="funding spread, carry trade, funding arbitrage, basis trade, cross-venue, annualized carry",
                doc_llm=(
                    "For one underlying (positional: `funding_spread('BTC')`), averages each contract's "
                    "settled, annualized funding over the window (`start_date`/`end_date`, default last 7 "
                    "days) and returns every pair of contracts on different venues: `annualized_a`, "
                    "`annualized_b`, `spread_annualized` = a - b, `spread_bps`, observation counts and "
                    "`carry_trade` ('short a / long b' collects a positive spread, because positive funding "
                    "means longs pay). Includes the offshore benchmark venues. Before fees, basis moves and "
                    "margin cost. Venues that fail are skipped with a warning."
                ),
                doc_md=(
                    "## funding_spread(underlying, start_date := NULL, end_date := NULL)\n\nMean annualized "
                    "funding per contract and the carry spread for every cross-venue pair."
                ),
            ),
            "vgi.result_columns_schema": result_columns(SPREAD_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT venue_a, venue_b, spread_bps, carry_trade FROM perps.main.funding_spread('BTC') "
                    "ORDER BY abs(spread_bps) DESC LIMIT 5"
                ),
                description="The widest BTC funding carry between any two venues this week",
            ),
            FunctionExample(
                sql=(
                    "SELECT symbol_a, symbol_b, spread_bps FROM perps.main.funding_spread('ETH', "
                    "start_date := now() - INTERVAL 30 DAY) WHERE venue_a = 'bitnomial' OR venue_b = 'bitnomial' "
                    "ORDER BY spread_bps"
                ),
                description="30-day ETH carry of Bitnomial against every other venue",
            ),
        ]

    @classmethod
    def process(cls, params: ProcessParams[SpreadArgs], state: ScanState, out: OutputCollector) -> None:
        """Gather every venue's funding in one tick, then emit the pairs."""
        if state.step > 0:
            out.finish()
            return
        state.step = 1
        start, end = window(params.args.start_date, params.args.end_date)
        points: list[FundingPoint] = []
        for adapter in select(None):
            try:
                insts = filter_instruments(adapter, params.args.underlying, include_inactive=True)
                if insts:
                    points += adapter.funding(insts, start, end)
            except ArgumentError:
                raise
            except Exception as exc:
                warn(out, f"perps: funding_spread skipped venue {adapter.name}: {exc}")
        rows = derived.carry_pairs(derived.summarize_carry(points))
        emit_rows(out, params, [{**r, "window_start": start, "window_end": end} for r in rows])


# --------------------------------------------------------------- depth_metrics

DEPTH_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("symbol", STR, "Venue-native contract symbol.", False),
        ("best_bid", F64, "Best bid per underlying unit.", True),
        ("best_ask", F64, "Best ask per underlying unit.", True),
        ("mid", F64, "(best_bid + best_ask) / 2.", True),
        ("spread_bps", F64, "(best_ask - best_bid) / mid in basis points.", True),
        ("notional_usd", F64, "The notional each side was asked to fill.", False),
        ("buy_cost_bps", F64, "Distance of the VWAP to buy notional_usd (walking asks) from mid, bps.", True),
        ("sell_cost_bps", F64, "Distance of the VWAP to sell notional_usd (walking bids) from mid, bps.", True),
        ("buy_filled_usd", F64, "Notional the fetched asks could absorb (< notional_usd if the book ran out).", False),
        ("sell_filled_usd", F64, "Notional the fetched bids could absorb.", False),
        ("fully_filled", pa.bool_(), "True when both sides could fill the whole notional.", False),
        ("bid_depth_usd", F64, "Total bid notional in the fetched levels.", False),
        ("ask_depth_usd", F64, "Total ask notional in the fetched levels.", False),
        ("imbalance", F64, "(bid_depth_usd - ask_depth_usd) / (bid + ask): +1 all bids, -1 all asks.", True),
        ("bid_levels", pa.int32(), "Bid levels fetched.", False),
        ("ask_levels", pa.int32(), "Ask levels fetched.", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class DepthArgs:
    """Arguments for ``depth_metrics(venue, symbol, notional_usd, depth)``."""

    venue: RequiredVenueArg
    symbol: SymbolArg
    notional_usd: Annotated[
        float,
        Arg(
            "notional_usd",
            default=100_000.0,
            doc="Notional to fill on each side, in quote currency (default 100,000).",
            gt=0,
        ),
    ]
    depth: Annotated[
        int, Arg("depth", default=200, doc="Book levels to fetch per side, 1 to 500 (default 200).", ge=1, le=500)
    ]


@init_single_worker
@bind_fixed_schema
class DepthMetrics(VenueScan[DepthArgs]):
    """Spread, fill cost and imbalance for one book."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = DEPTH_SCHEMA

    class Meta:
        """Function metadata."""

        name = "depth_metrics"
        description = (
            "Liquidity summary for one contract: spread, bps cost to fill a USD notional on each side, "
            "depth and imbalance"
        )
        tags = {
            **object_tags(
                title="Book Depth and Fill Cost",
                category="analytics",
                keywords="slippage, market impact, fill cost, spread, liquidity, depth, imbalance",
                doc_llm=(
                    "One row summarizing a contract's book: `depth_metrics('kalshi', 'KXBTCPERP', notional_usd "
                    ":= 250000)`. Venue and symbol are positional. `spread_bps`; `buy_cost_bps` / "
                    "`sell_cost_bps` = distance of the fill VWAP from mid when walking the asks / bids for "
                    "`notional_usd` (default 100,000); `buy_filled_usd` / `sell_filled_usd` and `fully_filled` "
                    "say whether the fetched `depth` (default 200 levels) was enough; `bid_depth_usd`, "
                    "`ask_depth_usd`, `imbalance` in [-1, 1]. Excludes fees."
                ),
                doc_md=(
                    "## depth_metrics(venue, symbol, notional_usd := 100000, depth := 200)\n\nSpread, cost to "
                    "fill a notional on each side, and book imbalance."
                ),
            ),
            "vgi.result_columns_schema": result_columns(DEPTH_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT spread_bps, buy_cost_bps, sell_cost_bps, imbalance FROM perps.main.depth_metrics("
                    "'kalshi', 'KXETHPERP', notional_usd := 50000)"
                ),
                description="What a $50k ETH clip costs on Kalshi",
            ),
            FunctionExample(
                sql=(
                    "SELECT venue, buy_cost_bps, fully_filled FROM perps.main.depth_metrics('coinbase', "
                    "'BIP-20DEC30-CDE', notional_usd := 1000000)"
                ),
                description="Can Coinbase's BTC perp absorb a $1m market buy",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[DepthArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One summary row."""
        inst = adapter.instrument(params.args.symbol)
        bids, asks = adapter.order_book(inst, params.args.depth)
        metrics = derived.depth_metrics(bids, asks, inst.contract_size, params.args.notional_usd)
        return [{"venue": adapter.name, "symbol": inst.symbol, **metrics}], None


# -------------------------------------------------------- liquidation_estimate

LIQ_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("symbol", STR, "Venue-native contract symbol.", False),
        ("side", STR, "'long' or 'short' holders.", False),
        ("leverage", F64, "Assumed leverage bucket.", False),
        ("weight", F64, "Share of open interest assumed at this leverage (weights sum to 1 per side).", False),
        ("entry_price", F64, "Assumed entry price per underlying unit (default: current mark).", False),
        ("liquidation_price", F64, "Estimated liquidation price; NULL when the leverage is not reachable.", True),
        ("distance_pct", F64, "(liquidation_price / entry_price - 1) * 100.", True),
        ("contracts", F64, "Open interest assumed in this bucket, contracts.", False),
        ("notional_usd", F64, "contracts * contract_size * entry_price.", False),
        (
            "cum_notional_usd",
            F64,
            "Feasible notional liquidated by the time price reaches this level, per side.",
            False,
        ),
        ("feasible", pa.bool_(), "False when leverage exceeds the venue max or maintenance >= 1/leverage.", False),
        ("maintenance_margin_rate", F64, "Maintenance margin fraction used.", False),
        (
            "mm_source",
            STR,
            "Where the maintenance rate came from (venue documentation or the mm_rate argument).",
            False,
        ),
        ("open_interest_contracts", F64, "Total open interest the buckets split, contracts.", False),
        (
            "estimate_method",
            STR,
            "Model version: isolated linear margin, entry at mark, no fees or funding accrual.",
            False,
        ),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class LiquidationArgs:
    """Arguments for ``liquidation_estimate(venue, symbol, leverages, weights, mm_rate, entry_price)``."""

    venue: RequiredVenueArg
    symbol: SymbolArg
    leverages: Annotated[
        str,
        Arg(
            "leverages",
            default=derived.DEFAULT_LEVERAGES,
            doc=f"Comma-separated leverage buckets, each > 1 (default '{derived.DEFAULT_LEVERAGES}').",
        ),
    ]
    weights: Annotated[
        str,
        Arg(
            "weights",
            default=derived.DEFAULT_LEVERAGE_WEIGHTS,
            doc=f"Comma-separated share of open interest per bucket, renormalized to 1 "
            f"(default '{derived.DEFAULT_LEVERAGE_WEIGHTS}'). An assumption, not venue data.",
        ),
    ]
    mm_rate: Annotated[
        float | None,
        Arg(
            "mm_rate",
            default=None,
            doc="Maintenance margin fraction to assume. Default NULL uses the venue's documented schedule and "
            "refuses venues that publish none.",
            gt=0,
            lt=1,
        ),
    ]
    entry_price: Annotated[
        float | None,
        Arg("entry_price", default=None, doc="Assumed entry price per underlying unit (default: current mark).", gt=0),
    ]


@init_single_worker
@bind_fixed_schema
class LiquidationEstimate(VenueScan[LiquidationArgs]):
    """Parameterized liquidation bands: an ESTIMATE, labelled as one."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = LIQ_SCHEMA

    class Meta:
        """Function metadata."""

        name = "liquidation_estimate"
        description = (
            "ESTIMATE of liquidation price bands from open interest, an assumed leverage mix and the "
            "venue's maintenance margin"
        )
        tags = {
            **object_tags(
                title="Liquidation Band Estimate",
                category="analytics",
                keywords="liquidation, liquidation price, leverage, maintenance margin, liquidation heatmap",
                doc_llm=(
                    "An ESTIMATE, not venue data: splits a contract's open interest (both sides hold all of it) "
                    "across an assumed leverage distribution and computes each bucket's liquidation price for "
                    "an isolated linear position entered at the mark: X = entry * (side - 1/L) / (side - mm). "
                    "`liquidation_estimate('hyperliquid', 'BTC')`; venue and symbol are positional. "
                    "Parameters: `leverages` (default '2,3,5,10,20'), `weights` (default "
                    "'0.30,0.25,0.20,0.15,0.10'), `mm_rate` (maintenance fraction), `entry_price`. With "
                    "`mm_rate` NULL it uses the venue's documented maintenance margin and REFUSES venues that "
                    "publish none (Kalshi, Coinbase, Bitnomial, Binance, Bybit): pass `mm_rate` to estimate them "
                    "under your own assumption. Every row carries `estimate_method`, `mm_source` and "
                    "`feasible`; `cum_notional_usd` accumulates per side nearest-first."
                ),
                doc_md=(
                    "## liquidation_estimate(venue, symbol, leverages, weights, mm_rate := NULL, "
                    "entry_price := NULL)\n\n**Estimate.** Liquidation bands from open interest and an "
                    "assumed leverage mix; refuses venues without a published maintenance schedule unless "
                    "`mm_rate` is given."
                ),
            ),
            "vgi.result_columns_schema": result_columns(LIQ_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT side, leverage, liquidation_price, cum_notional_usd FROM perps.main."
                    "liquidation_estimate('hyperliquid', 'ETH') WHERE feasible ORDER BY side, distance_pct"
                ),
                description="Estimated ETH liquidation bands on Hyperliquid with the default leverage mix",
            ),
            FunctionExample(
                sql=(
                    "SELECT side, leverage, distance_pct FROM perps.main.liquidation_estimate('kalshi', "
                    "'KXBTCPERP', mm_rate := 0.05, leverages := '3,5,10', weights := '0.5,0.3,0.2') "
                    "ORDER BY side, leverage"
                ),
                description="Kalshi BTC bands under an assumed 5% maintenance margin",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[LiquidationArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Bands for one contract; refuses an unsourced maintenance schedule."""
        args = params.args
        try:
            distribution = derived.parse_weights(args.leverages, args.weights)
        except ValueError as exc:
            raise ArgumentError(str(exc)) from exc
        inst = adapter.instrument(args.symbol)
        if args.mm_rate is not None:
            mm, source = args.mm_rate, "mm_rate argument (user assumption)"
        elif inst.maintenance_margin_rate is not None:
            mm, source = inst.maintenance_margin_rate, inst.margin_source or f"{adapter.name} documentation"
        else:
            reason = adapter.capabilities["maintenance_margin"].notes
            raise VenueError(
                f"liquidation_estimate refuses {adapter.name}: no published maintenance-margin schedule ({reason}). "
                "Pass mm_rate := <fraction> to estimate under your own assumption."
            )
        marks = adapter.marks([inst])
        if not marks or marks[0].open_interest_contracts is None:
            raise VenueError(f"{adapter.name} {inst.symbol}: no open interest available to estimate from")
        mark = marks[0]
        entry = args.entry_price or mark.mark_price
        if entry is None:
            raise VenueError(f"{adapter.name} {inst.symbol}: no mark price; pass entry_price := <price>")
        bands = derived.liquidation_bands(
            mark=entry,
            open_interest_contracts=mark.open_interest_contracts or 0.0,
            contract_size=inst.contract_size,
            mm_rate=mm,
            max_leverage=inst.max_leverage,
            distribution=distribution,
        )
        return [
            {
                "venue": adapter.name,
                "symbol": inst.symbol,
                "entry_price": entry,
                "maintenance_margin_rate": mm,
                "mm_source": source,
                "open_interest_contracts": mark.open_interest_contracts,
                "estimate_method": derived.LIQUIDATION_METHOD,
                **band,
            }
            for band in bands
        ], None


__all__ = ["DepthMetrics", "FundingSpread", "LiquidationEstimate", "Premium", "RequiredVenueArg"]
