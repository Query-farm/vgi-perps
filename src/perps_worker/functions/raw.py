"""Venue-specific fields with no cross-venue equivalent (``raw_<venue>_*``)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import ProcessParams, bind_fixed_schema, init_single_worker

from ..meta import object_tags
from ..normalize import from_epoch_ms, from_epoch_s, parse_iso, to_float
from ..venues import Adapter, get
from ..venues.bitnomial import BitnomialAdapter, instrument_from_spec
from ..venues.coinbase import CoinbaseAdapter, is_perp_style
from ..venues.kalshi import KalshiAdapter
from .common import F64, STR, TS, EndArg, VenueScan, build_schema, result_columns, window


class _NoArgsVenue:
    """Mixin: a raw table bound to exactly one venue."""

    VENUE: ClassVar[str]

    @classmethod
    def venue_list(cls, params: ProcessParams[Any]) -> tuple[list[Adapter], bool]:
        return [get(cls.VENUE)], False


def _dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


@dataclass(slots=True, frozen=True, kw_only=True)
class RawArgs:
    """Arguments for the per-venue raw tables: an optional symbol filter."""

    symbol: Annotated[
        str | None,
        Arg(
            "symbol",
            default=None,
            doc="Keep only this venue-native contract symbol (case-insensitive). NULL (default) keeps all.",
        ),
    ]


def _keep(params: ProcessParams[RawArgs], symbol: object) -> bool:
    """Apply the optional ``symbol`` filter."""
    wanted = params.args.symbol
    return wanted is None or str(symbol).upper() == wanted.strip().upper()


# ------------------------------------------------------------ raw_kalshi_markets

KALSHI_SCHEMA = build_schema(
    [
        ("symbol", STR, "Kalshi market ticker.", False),
        ("market_id", STR, "Stable market UUID (survives ticker renames).", True),
        ("market_version", pa.int64(), "Increments after a corporate action such as a split.", True),
        ("title", STR, "Kalshi's market title (the contract's unit, e.g. '0.0001 BTC').", True),
        ("status", STR, "'active', 'inactive' or 'closed'.", True),
        ("asset_class", STR, "Kalshi asset class: Crypto, Equities, Indices, Metals, Energy.", True),
        ("contract_size_native", F64, "Kalshi contract_size, in the title's unit.", True),
        ("underlying_multiplier", F64, "Underlying units per contract-size unit (1000 for kSHIB).", True),
        ("price_per_contract", F64, "Last price in Kalshi's native USD-per-contract quoting.", True),
        ("settlement_mark_per_contract", F64, "Mark used for settlement and funding, USD per contract.", True),
        ("liquidation_mark_per_contract", F64, "Mark used for liquidation, USD per contract.", True),
        ("reference_price_per_contract", F64, "Underlying reference price scaled per contract.", True),
        ("reference_price_time", TS, "Timestamp of the reference price (stale when the underlying is closed).", True),
        ("funding_cap", F64, "Funding cap in funding_limit_unit.", True),
        ("funding_deadband", F64, "Rates with |rate| below this are zeroed, in funding_limit_unit.", True),
        ("funding_limit_unit", STR, "'percentage' (cap is a rate) or 'dollar' (cap is USD per contract).", True),
        ("funding_invalid_premium_handling", STR, "'dampen' (count a missing bar as 0) or 'exclude'.", True),
        ("leverage_estimates", STR, "JSON: leverage (1/margin rate) by notional size in USD.", True),
        ("long_leverage_estimates", STR, "JSON: long-side leverage by notional size.", True),
        ("short_leverage_estimates", STR, "JSON: short-side leverage by notional size.", True),
        (
            "halt_sources",
            pa.list_(pa.string()),
            "Why the market is halted ('initial_setup', 'corporate_action').",
            True,
        ),
        ("is_open", pa.bool_(), "Session state for markets with trading hours; NULL for 24/7 markets.", True),
        ("next_close", TS, "Next scheduled close for markets with trading hours.", True),
        ("minimum_quantity", F64, "Minimum order size in contracts.", True),
    ]
)


@init_single_worker
@bind_fixed_schema
class RawKalshiMarkets(_NoArgsVenue, VenueScan[RawArgs]):
    """Kalshi-only market fields."""

    VENUE = "kalshi"
    FIXED_SCHEMA: ClassVar[pa.Schema] = KALSHI_SCHEMA

    class Meta:
        """Function metadata."""

        name = "raw_kalshi_markets"
        description = (
            "Kalshi-only perps fields: per-contract marks, funding deadband and cap unit, leverage by size, halts"
        )
        tags = {
            **object_tags(
                title="Kalshi Market Internals",
                category="venue_raw",
                keywords="kalshi, leverage estimates, funding deadband, liquidation mark, corporate action, halt",
                doc_llm=(
                    "Kalshi's margin-market fields that do not normalize across venues, in Kalshi's native "
                    "per-contract USD quoting: settlement, liquidation and reference marks per contract, "
                    "`funding_deadband` and `funding_limit_unit` (a 'dollar' cap is USD per contract, not a "
                    "rate), leverage estimates by notional (JSON), `halt_sources`, session state and "
                    "`market_version` (bumps after splits). Includes inactive markets; "
                    "`symbol := 'KXBTCPERP'` keeps one."
                ),
                doc_md="## raw_kalshi_markets(symbol := NULL)\n\nKalshi-specific margin-market fields, unnormalized.",
            ),
            "vgi.result_columns_schema": result_columns(KALSHI_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT symbol, funding_deadband, funding_limit_unit FROM perps.main.raw_kalshi_markets() "
                    "WHERE asset_class = 'Crypto' ORDER BY symbol"
                ),
                description="Kalshi crypto perps' funding deadbands",
            ),
            FunctionExample(
                sql=(
                    "SELECT symbol, json_extract(leverage_estimates, '$.\"1000000\"') AS lev_at_1m "
                    "FROM perps.main.raw_kalshi_markets() WHERE status = 'active' ORDER BY lev_at_1m LIMIT 10"
                ),
                description="Kalshi markets whose leverage shrinks most at $1m notional",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[RawArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One row per market."""
        assert isinstance(adapter, KalshiAdapter)
        rows = []
        for m in sorted(adapter.raw_markets(), key=lambda m: str(m.get("ticker"))):
            if not _keep(params, m.get("ticker")):
                continue
            ref = _dict(m.get("reference_price"))
            settle = _dict(m.get("settlement_mark_price"))
            liq = _dict(m.get("liquidation_mark_price"))
            schedule = _dict(m.get("schedule"))

            def js(key: str, market: dict[str, Any] = m) -> str | None:
                value = market.get(key)
                return None if value is None else json.dumps(value, sort_keys=True)

            rows.append(
                {
                    "symbol": m.get("ticker"),
                    "market_id": m.get("market_id"),
                    "market_version": m.get("market_version"),
                    "title": m.get("title"),
                    "status": m.get("status"),
                    "asset_class": m.get("asset_class"),
                    "contract_size_native": to_float(m.get("contract_size")),
                    "underlying_multiplier": to_float(m.get("underlying_multiplier")),
                    "price_per_contract": to_float(m.get("price")),
                    "settlement_mark_per_contract": to_float(settle.get("price")),
                    "liquidation_mark_per_contract": to_float(liq.get("price")),
                    "reference_price_per_contract": to_float(ref.get("price")),
                    "reference_price_time": from_epoch_ms(ref.get("ts_ms")),
                    "funding_cap": to_float(m.get("funding_cap")),
                    "funding_deadband": to_float(m.get("funding_deadband")),
                    "funding_limit_unit": m.get("funding_limit_unit"),
                    "funding_invalid_premium_handling": m.get("funding_invalid_premium_handling"),
                    "leverage_estimates": js("leverage_estimates"),
                    "long_leverage_estimates": js("long_leverage_estimates"),
                    "short_leverage_estimates": js("short_leverage_estimates"),
                    "halt_sources": list(m.get("halt_sources") or []),
                    "is_open": schedule.get("is_open"),
                    "next_close": from_epoch_s(schedule.get("next_close_ts")),
                    "minimum_quantity": to_float(m.get("minimum_quantity")),
                }
            )
        return rows, None


# ------------------------------------------------------ raw_kalshi_premium_index

PREMIUM_INDEX_SCHEMA = build_schema(
    [
        ("symbol", STR, "Kalshi market ticker.", False),
        ("ts", TS, "The one-second bucket, UTC.", False),
        ("premium_index", F64, "Signed fraction of the index (not bps); 0 when no premium was measurable.", False),
        ("premium_bps", F64, "premium_index * 10000.", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class PremiumIndexArgs:
    """Arguments for ``raw_kalshi_premium_index(symbol, start_date, end_date)``."""

    symbol: Annotated[str, Arg(0, doc="Kalshi perps ticker, e.g. 'KXBTCPERP'.")]
    start_date: Annotated[
        datetime | None,
        Arg("start_date", default=None, arrow_type=TS, doc="Start of the window. Default: 10 minutes before end_date."),
    ]
    end_date: EndArg


@init_single_worker
@bind_fixed_schema
class RawKalshiPremiumIndex(_NoArgsVenue, VenueScan[PremiumIndexArgs]):
    """Kalshi's per-second informational premium index."""

    VENUE = "kalshi"
    FIXED_SCHEMA: ClassVar[pa.Schema] = PREMIUM_INDEX_SCHEMA

    class Meta:
        """Function metadata."""

        name = "raw_kalshi_premium_index"
        description = "Kalshi's per-second premium index (impact book vs index) for one market, windows up to one hour"
        tags = {
            **object_tags(
                title="Kalshi Per-Second Premium Index",
                category="venue_raw",
                keywords="kalshi, premium index, impact price, funding input, per second, basis",
                doc_llm=(
                    "Kalshi's informational per-second premium index for one market: "
                    "`raw_kalshi_premium_index('KXBTCPERP', start_date := now() - INTERVAL 5 MINUTE)`. The "
                    "window must be at most one hour (default: the last 10 minutes). `premium_index` is a "
                    "signed fraction (also as `premium_bps`); 0 means no measurable premium that second. "
                    "Kalshi says it may not exactly match the values used for funding."
                ),
                doc_md=(
                    "## raw_kalshi_premium_index(symbol, start_date := NULL, end_date := NULL)\n\n"
                    "Per-second premium index, at most one hour per call."
                ),
            ),
            "vgi.result_columns_schema": result_columns(PREMIUM_INDEX_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT avg(premium_bps) AS mean_bps, max(abs(premium_bps)) AS worst_bps FROM "
                    "perps.main.raw_kalshi_premium_index('KXBTCPERP', start_date := now() - INTERVAL 30 MINUTE)"
                ),
                description="Mean and worst BTC premium over the last half hour",
            ),
            FunctionExample(
                sql=(
                    "SELECT date_trunc('minute', ts) AS minute, avg(premium_bps) AS bps FROM "
                    "perps.main.raw_kalshi_premium_index('KXETHPERP') GROUP BY minute ORDER BY minute"
                ),
                description="Minute-averaged ETH premium over the last ten minutes",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[PremiumIndexArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Points in the window."""
        assert isinstance(adapter, KalshiAdapter)
        start, end = window(params.args.start_date, params.args.end_date, default=timedelta(minutes=10))
        inst = adapter.instrument(params.args.symbol)
        return [
            {"symbol": inst.symbol, "ts": ts, "premium_index": v, "premium_bps": v * 10_000.0}
            for ts, v in adapter.premium_index(inst, start, end)
        ], None


# --------------------------------------------------------- raw_coinbase_products

COINBASE_SCHEMA = build_schema(
    [
        ("symbol", STR, "Coinbase product id, e.g. 'BIP-20DEC30-CDE'.", False),
        ("display_name", STR, "Coinbase display name.", True),
        ("contract_code", STR, "Exchange contract code (BIP, ETP, ...).", True),
        ("contract_expiry", TS, "Nominal expiry of the 5-year perp-style contract.", True),
        ("contract_size_native", F64, "Coinbase contract_size, in the quoted unit (1000 SHIB for '1000SHIB').", True),
        ("contract_root_unit", STR, "Coinbase's underlying root unit.", True),
        ("intraday_long_margin_rate", F64, "FCM intraday initial margin, long.", True),
        ("intraday_short_margin_rate", F64, "FCM intraday initial margin, short.", True),
        ("overnight_long_margin_rate", F64, "FCM overnight initial margin, long.", True),
        ("overnight_short_margin_rate", F64, "FCM overnight initial margin, short.", True),
        ("settlement_price_native", F64, "Most recent official daily settlement price, native quoting.", True),
        ("index_price_native", F64, "Index price, native quoting.", True),
        ("funding_rate", F64, "Latest published hourly funding rate (decimal).", True),
        ("funding_time", TS, "Time of that funding rate.", True),
        ("session_state", STR, "FCM trading session state.", True),
        ("session_close", TS, "Current session close time.", True),
        ("twenty_four_by_seven", pa.bool_(), "True for products that trade around the clock.", True),
    ]
)


@init_single_worker
@bind_fixed_schema
class RawCoinbaseProducts(_NoArgsVenue, VenueScan[RawArgs]):
    """Coinbase-only product fields."""

    VENUE = "coinbase"
    FIXED_SCHEMA: ClassVar[pa.Schema] = COINBASE_SCHEMA

    class Meta:
        """Function metadata."""

        name = "raw_coinbase_products"
        description = (
            "Coinbase perp-style futures fields: intraday and overnight FCM margin by side, settlement, sessions"
        )
        tags = {
            **object_tags(
                title="Coinbase Perp-Style Product Internals",
                category="venue_raw",
                keywords="coinbase derivatives, fcm margin, intraday margin, overnight margin, settlement price",
                doc_llm=(
                    "Coinbase Derivatives perp-style product fields in native quoting: FCM intraday and "
                    "overnight margin rates for long and short (initial requirements; overnight is higher), "
                    "daily `settlement_price_native`, `index_price_native`, the latest hourly `funding_rate` "
                    "and `funding_time`, nominal `contract_expiry`, session state and close. "
                    "`symbol := '<id>'` keeps one."
                ),
                doc_md="## raw_coinbase_products(symbol := NULL)\n\nCoinbase-specific product fields, unnormalized.",
            ),
            "vgi.result_columns_schema": result_columns(COINBASE_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT symbol, intraday_long_margin_rate, overnight_long_margin_rate FROM "
                    "perps.main.raw_coinbase_products() ORDER BY overnight_long_margin_rate DESC LIMIT 5"
                ),
                description="Coinbase perps with the highest overnight margin",
            ),
            FunctionExample(
                sql=(
                    "SELECT symbol, overnight_short_margin_rate - overnight_long_margin_rate AS short_premium "
                    "FROM perps.main.raw_coinbase_products() ORDER BY short_premium DESC LIMIT 5"
                ),
                description="Where Coinbase charges shorts the most extra overnight margin",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[RawArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One row per perp-style product."""
        assert isinstance(adapter, CoinbaseAdapter)
        rows = []
        for p in sorted((p for p in adapter.raw_products() if is_perp_style(p)), key=lambda p: str(p["product_id"])):
            if not _keep(params, p["product_id"]):
                continue
            d = p.get("future_product_details") or {}
            intraday = d.get("intraday_margin_rate") or {}
            overnight = d.get("overnight_margin_rate") or {}
            session = p.get("fcm_trading_session_details") or {}
            rows.append(
                {
                    "symbol": p["product_id"],
                    "display_name": p.get("display_name"),
                    "contract_code": d.get("contract_code"),
                    "contract_expiry": parse_iso(d.get("contract_expiry")),
                    "contract_size_native": to_float(d.get("contract_size")),
                    "contract_root_unit": d.get("contract_root_unit"),
                    "intraday_long_margin_rate": to_float(intraday.get("long_margin_rate")),
                    "intraday_short_margin_rate": to_float(intraday.get("short_margin_rate")),
                    "overnight_long_margin_rate": to_float(overnight.get("long_margin_rate")),
                    "overnight_short_margin_rate": to_float(overnight.get("short_margin_rate")),
                    "settlement_price_native": to_float(d.get("settlement_price")),
                    "index_price_native": to_float(d.get("index_price")),
                    "funding_rate": to_float(d.get("funding_rate")),
                    "funding_time": parse_iso(d.get("funding_time")),
                    "session_state": session.get("session_state"),
                    "session_close": parse_iso(session.get("close_time")),
                    "twenty_four_by_seven": d.get("twenty_four_by_seven"),
                }
            )
        return rows, None


# ------------------------------------------------------- raw_bitnomial_products

BITNOMIAL_SCHEMA = build_schema(
    [
        ("symbol", STR, "Bitnomial product symbol, e.g. 'PBTCUCZ50'.", False),
        ("product_id", pa.int64(), "Bitnomial numeric product id.", False),
        ("product_name", STR, "Bitnomial product name.", True),
        ("base_symbol", STR, "Bitnomial base symbol (encodes the size tier, e.g. BTCUC = centi-bitcoin).", True),
        ("contract_size", F64, "Underlying units per contract.", True),
        ("price_increment", F64, "Tick size, USD per underlying unit (prices on the API are in ticks).", True),
        ("funding_rate_lower_bound", F64, "Spec lower bound on the funding rate per interval.", True),
        ("funding_rate_upper_bound", F64, "Spec upper bound on the funding rate per interval.", True),
        ("daily_first_funding_interval_end", STR, "First funding interval end of the day (Central time).", True),
        ("price_limit_percentage", F64, "Daily price limit as a fraction.", True),
        ("price_limit_upper", F64, "Upper price limit today, USD per underlying unit.", True),
        ("price_limit_lower", F64, "Lower price limit today, USD per underlying unit.", True),
        ("settlement_price", F64, "Latest settlement price, USD per underlying unit.", True),
        ("settlement_time", TS, "When that settlement ran.", True),
        (
            "product_group_margin_rate",
            F64,
            "Published margin rate; Bitnomial does not say whether initial or maintenance.",
            True,
        ),
    ]
)


@init_single_worker
@bind_fixed_schema
class RawBitnomialProducts(_NoArgsVenue, VenueScan[RawArgs]):
    """Bitnomial-only perpetual product fields."""

    VENUE = "bitnomial"
    FIXED_SCHEMA: ClassVar[pa.Schema] = BITNOMIAL_SCHEMA

    class Meta:
        """Function metadata."""

        name = "raw_bitnomial_products"
        description = (
            "Bitnomial perpetual fields: size tier, funding bounds, daily price limits, settlement, "
            "product-group margin rate"
        )
        tags = {
            **object_tags(
                title="Bitnomial Perpetual Internals",
                category="venue_raw",
                keywords="bitnomial, price limits, funding bounds, settlement price, margin rate, product group",
                doc_llm=(
                    "Bitnomial perpetual fields converted from ticks to USD per underlying unit: "
                    "`funding_rate_lower_bound` / `upper_bound`, first funding interval end (Central time), "
                    "daily price limits, latest settlement, and `product_group_margin_rate` (published without "
                    "saying whether it is initial or maintenance margin, so liquidation_estimate does not use "
                    "it). `base_symbol` encodes the size tier (BTCUC centi, BTCUR myrio). "
                    "`symbol :=` keeps one contract."
                ),
                doc_md="## raw_bitnomial_products(symbol := NULL)\n\nBitnomial-specific perpetual fields.",
            ),
            "vgi.result_columns_schema": result_columns(BITNOMIAL_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT symbol, contract_size, product_group_margin_rate FROM "
                    "perps.main.raw_bitnomial_products() ORDER BY symbol"
                ),
                description="Bitnomial perps with their size tier and margin rate",
            ),
            FunctionExample(
                sql=(
                    "SELECT symbol, price_limit_lower, settlement_price, price_limit_upper FROM "
                    "perps.main.raw_bitnomial_products() WHERE base_symbol LIKE 'BTC%' ORDER BY symbol"
                ),
                description="Today's price limits around settlement for Bitnomial's BTC perps",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[RawArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One row per perpetual, joining specs to today's product data."""
        assert isinstance(adapter, BitnomialAdapter)
        data = {str(d.get("product_id")): d for d in adapter.raw_data()}
        rows = []
        for spec in sorted(
            (s for s in adapter.raw_specs() if s.get("type") == "perpetual"), key=lambda s: str(s["symbol"])
        ):
            if not _keep(params, spec["symbol"]):
                continue
            inst = instrument_from_spec(spec)
            d = data.get(str(spec["product_id"]), {})
            rows.append(
                {
                    "symbol": spec["symbol"],
                    "product_id": int(spec["product_id"]),
                    "product_name": spec.get("product_name"),
                    "base_symbol": spec.get("base_symbol"),
                    "contract_size": to_float(spec.get("contract_size")),
                    "price_increment": to_float(spec.get("price_increment")),
                    "funding_rate_lower_bound": to_float(spec.get("funding_rate_lower_bound")),
                    "funding_rate_upper_bound": to_float(spec.get("funding_rate_upper_bound")),
                    "daily_first_funding_interval_end": spec.get("daily_first_funding_interval_end"),
                    "price_limit_percentage": to_float(spec.get("price_limit_percentage")),
                    "price_limit_upper": inst.price(to_float(d.get("price_limit_upper"))),
                    "price_limit_lower": inst.price(to_float(d.get("price_limit_lower"))),
                    "settlement_price": inst.price(to_float(d.get("settlement_price"))),
                    "settlement_time": parse_iso(d.get("settlement_time")),
                    "product_group_margin_rate": to_float(d.get("product_group_margin_rate")),
                }
            )
        return rows, None
