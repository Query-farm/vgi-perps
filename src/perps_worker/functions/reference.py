"""Reference functions: venue health, the capability matrix, contract specifications."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import ProcessParams, bind_fixed_schema, init_single_worker

from .. import replay
from ..meta import object_tags
from ..venues import CAPABILITIES, Adapter
from .common import (
    F64,
    STR,
    TS,
    UnderlyingArg,
    VenueArg,
    VenueScan,
    build_schema,
    filter_instruments,
    instrument_row,
    result_columns,
)

# ---------------------------------------------------------------------- venues

VENUES_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name used by every other function's venue argument.", False),
        ("display_name", STR, "Human-readable venue and product name.", False),
        ("venue_type", STR, "'us_regulated' (CFTC-registered exchange) or 'offshore' (benchmark only).", False),
        ("regulator", STR, "Regulatory status as documented by the venue.", False),
        ("adapter_version", STR, "Version of this worker's adapter for the venue.", False),
        ("sign_convention", STR, "How the venue's funding sign maps to positive = longs pay shorts.", False),
        ("capabilities", pa.list_(pa.string()), "Capabilities with full or partial public support.", False),
        ("status", STR, "'ok' if the probe succeeded, 'error' if it failed, 'not_probed' when probe := false.", False),
        (
            "instrument_count",
            pa.int64(),
            "Active and inactive contracts the probe found; NULL unless status = 'ok'.",
            True,
        ),
        ("last_error", STR, "Error from the probe, NULL when it succeeded.", True),
        ("checked_at", TS, "When the probe ran (UTC).", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class VenuesArgs:
    """Arguments for ``venues(probe := true)``."""

    probe: Annotated[
        bool,
        Arg("probe", default=True, doc="Fetch each venue's instrument list to check health (default true)."),
    ]


@init_single_worker
@bind_fixed_schema
class Venues(VenueScan[VenuesArgs]):
    """One row per venue with a live health probe."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = VENUES_SCHEMA

    class Meta:
        """Function metadata."""

        name = "venues"
        description = "List every venue with its regulatory type, supported capabilities and live health"
        tags = {
            **object_tags(
                title="Venues and Health",
                category="reference",
                keywords=(
                    "venues, exchanges, health, status, kalshi, coinbase, bitnomial, og.com, "
                    "hyperliquid, binance, bybit"
                ),
                doc_llm=(
                    "One row per venue: `venue` (the name every other function accepts), `venue_type` "
                    "('us_regulated' or 'offshore'), `regulator`, `adapter_version`, `sign_convention`, "
                    "`capabilities` (list of capability names with full or partial support), and a live "
                    "health probe: `status` ('ok', 'error', 'not_probed'), `instrument_count`, `last_error`, "
                    "`checked_at`. Run it first: a venue with status 'error' (for example an offshore venue "
                    "geo-blocked from the worker's network) is skipped by cross-venue queries. "
                    "`probe := false` skips the network."
                ),
                doc_md=(
                    "## venues(probe := true)\n\nEvery venue, its regulatory type and capabilities, and a "
                    "live health check. Cross-venue functions skip venues whose probe fails."
                ),
            ),
            "vgi.result_columns_schema": result_columns(VENUES_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql="SELECT venue, status, instrument_count, last_error FROM perps.main.venues() ORDER BY venue",
                description="Which venues are reachable right now",
            ),
            FunctionExample(
                sql=(
                    "SELECT venue, venue_type, len(capabilities) AS n_capabilities "
                    "FROM perps.main.venues(probe := false) WHERE venue_type = 'us_regulated' ORDER BY venue"
                ),
                description="US-regulated venues and how many capabilities each supports, without network calls",
            ),
        ]

    @classmethod
    def venue_list(cls, params: ProcessParams[VenuesArgs]) -> tuple[list[Adapter], bool]:
        """Scan every venue, always."""
        from ..venues import ADAPTERS

        return list(ADAPTERS.values()), True

    @classmethod
    def produce(
        cls, params: ProcessParams[VenuesArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Probe one venue; failures become a row, never an error."""
        status, count, error = "not_probed", None, None
        if params.args.probe:
            try:
                count, status = adapter.probe(), "ok"
            except Exception as exc:
                status, error = "error", f"{type(exc).__name__}: {exc}"[:500]
        row = {
            "venue": adapter.name,
            "display_name": adapter.display_name,
            "venue_type": adapter.venue_type,
            "regulator": adapter.regulator,
            "adapter_version": adapter.adapter_version,
            "sign_convention": adapter.sign_convention,
            "capabilities": [c for c in CAPABILITIES if adapter.capabilities[c].support != "unsupported"],
            "status": status,
            "instrument_count": count,
            "last_error": error,
            "checked_at": replay.now(),
        }
        return [row], None


# ---------------------------------------------------------------- capabilities

CAPABILITIES_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("capability", STR, "Capability name, e.g. 'funding_history', 'order_book'.", False),
        ("support", STR, "'supported', 'partial' or 'unsupported' through public endpoints.", False),
        ("auth_required", pa.bool_(), "True when the venue gates this data behind API credentials.", False),
        ("history_depth", STR, "How far back the public source reaches.", False),
        ("notes", STR, "Endpoint used, or why the capability is missing or partial.", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class CapabilitiesArgs:
    """Arguments for ``capabilities(venue := NULL)``."""

    venue: VenueArg


@init_single_worker
@bind_fixed_schema
class Capabilities(VenueScan[CapabilitiesArgs]):
    """The per-venue capability matrix (static; no network)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CAPABILITIES_SCHEMA

    class Meta:
        """Function metadata."""

        name = "capabilities"
        description = "Capability matrix: which data each venue serves publicly, with auth, depth and caveats"
        tags = {
            **object_tags(
                title="Venue Capability Matrix",
                category="reference",
                keywords="capabilities, support matrix, auth, history depth, funding history, order book, coverage",
                doc_llm=(
                    "Static capability matrix, one row per (venue, capability). Capabilities: instruments, "
                    "funding_history, funding_estimate, marks, premium_index, open_interest, order_book, "
                    "trades, candles, maintenance_margin. `support` is 'supported', 'partial' or 'unsupported' "
                    "via public endpoints; `auth_required`, `history_depth` and `notes` say why. Use it to "
                    "know in advance why, say, Coinbase funding history has only the latest rate or why "
                    "Bitnomial has no order book. `venue := 'kalshi'` narrows to one venue. No network calls."
                ),
                doc_md=(
                    "## capabilities(venue := NULL)\n\nWhat each venue serves publicly, whether it needs "
                    "credentials, how far history goes, and the caveats."
                ),
            ),
            "vgi.result_columns_schema": result_columns(CAPABILITIES_SCHEMA),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Venues with a public order book",
                        "sql": (
                            "SELECT venue FROM perps.main.capabilities() WHERE capability = 'order_book' "
                            "AND support = 'supported' ORDER BY venue"
                        ),
                        "expected_result": [["binance"], ["bybit"], ["coinbase"], ["hyperliquid"], ["kalshi"]],
                    },
                    {
                        "description": "Kalshi's funding-history support",
                        "sql": (
                            "SELECT support, auth_required FROM perps.main.capabilities(venue := 'kalshi') "
                            "WHERE capability = 'funding_history'"
                        ),
                        "expected_result": [["supported", False]],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT capability, support, notes FROM perps.main.capabilities(venue := 'coinbase') "
                    "WHERE support <> 'supported' ORDER BY capability"
                ),
                description="What Coinbase Derivatives does not serve publicly, and why",
            ),
            FunctionExample(
                sql=(
                    "SELECT capability, count(*) FILTER (WHERE support = 'supported') AS venues_supported "
                    "FROM perps.main.capabilities() GROUP BY capability ORDER BY capability"
                ),
                description="How many venues fully support each capability",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[CapabilitiesArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return the static rows for one venue."""
        rows = []
        for name in CAPABILITIES:
            cap = adapter.capabilities[name]
            rows.append(
                {
                    "venue": adapter.name,
                    "capability": name,
                    "support": cap.support,
                    "auth_required": cap.auth_required,
                    "history_depth": cap.history_depth,
                    "notes": cap.notes,
                }
            )
        return rows, None


# ----------------------------------------------------------------- instruments

INSTRUMENTS_SCHEMA = build_schema(
    [
        ("venue", STR, "Venue name.", False),
        ("symbol", STR, "Venue-native contract symbol; pass it to the single-venue functions.", False),
        ("canonical_id", STR, "'<UNDERLYING>-<QUOTE>-PERP': the economic exposure, shared across venues.", False),
        ("underlying", STR, "Canonical underlying (BTC, ETH, AAPL, XAU, US500, ...); join venues on this.", False),
        ("asset_class", STR, "'crypto', 'equity', 'index' or 'commodity'.", False),
        ("quote_ccy", STR, "Quote and settlement currency: USD (US venues), USDT or USDC (offshore).", False),
        ("instrument_type", STR, "'perpetual', or 'perp_style_future' for long-dated futures with funding.", False),
        ("status", STR, "Venue status, normalized to 'active' where trading.", False),
        ("contract_size", F64, "Underlying units per contract (0.0001 = one contract is 0.0001 BTC).", False),
        ("native_price_unit", STR, "How the venue quotes prices before normalization.", False),
        ("tick_size", F64, "Minimum price increment, in quote currency per underlying unit.", True),
        ("funding_interval_hours", F64, "Hours between funding settlements (1, 8, 24).", True),
        ("funding_cap", F64, "Absolute cap on the funding rate per interval (decimal); NULL if none published.", True),
        ("max_leverage", F64, "Maximum leverage the venue publishes (1 / initial margin rate).", True),
        ("initial_margin_rate", F64, "Initial margin as a fraction of notional, where published.", True),
        ("maintenance_margin_rate", F64, "Maintenance margin fraction, only where the venue documents it.", True),
        ("margin_source", STR, "Where the margin figures come from, and their caveats.", True),
        ("listed_at", TS, "Listing date where the venue publishes it.", True),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class InstrumentsArgs:
    """Arguments for ``instruments(venue := NULL, underlying := NULL, include_inactive := false)``."""

    venue: VenueArg
    underlying: UnderlyingArg
    include_inactive: Annotated[
        bool, Arg("include_inactive", default=False, doc="Also list inactive or halted contracts (default false).")
    ]


@init_single_worker
@bind_fixed_schema
class Instruments(VenueScan[InstrumentsArgs]):
    """Normalized contract specifications."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = INSTRUMENTS_SCHEMA

    class Meta:
        """Function metadata."""

        name = "instruments"
        description = (
            "Perpetual contract specifications per venue: underlying, contract size, tick, funding "
            "interval, cap, margin"
        )
        tags = {
            **object_tags(
                title="Perpetual Contract Specifications",
                category="reference",
                keywords="instruments, contract size, multiplier, tick size, funding interval, funding cap, leverage",
                doc_llm=(
                    "One row per perpetual contract. `symbol` is venue-native (pass it to order_book, trades, "
                    "candles); `underlying` and `canonical_id` are shared across venues (join on them). "
                    "`contract_size` is underlying units per contract, so notional = contracts * contract_size "
                    "* price. `tick_size` is in quote currency per underlying unit. `funding_interval_hours`, "
                    "`funding_cap` (decimal per interval), `max_leverage`, `initial_margin_rate`, "
                    "`maintenance_margin_rate` (only where documented) and `margin_source`. Arguments: "
                    "`venue := 'kalshi'`, `underlying := 'BTC'`, `include_inactive := true`. Kalshi lists "
                    "crypto, single-stock, index, metal and energy perps; Coinbase lists perp-style futures."
                ),
                doc_md=(
                    "## instruments(venue := NULL, underlying := NULL, include_inactive := false)\n\n"
                    "Contract specifications, normalized: contract size in underlying units, tick in USD per "
                    "unit, funding interval and cap, margin where published."
                ),
            ),
            "vgi.result_columns_schema": result_columns(INSTRUMENTS_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT venue, symbol, contract_size, funding_interval_hours, funding_cap "
                    "FROM perps.main.instruments(underlying := 'BTC') ORDER BY venue, contract_size"
                ),
                description="Every BTC perp: how much BTC one contract is and how often it funds",
            ),
            FunctionExample(
                sql=(
                    "SELECT asset_class, count(*) AS contracts FROM perps.main.instruments(venue := 'kalshi') "
                    "GROUP BY asset_class ORDER BY contracts DESC"
                ),
                description="Kalshi's active perps by asset class",
            ),
        ]

    @classmethod
    def produce(
        cls, params: ProcessParams[InstrumentsArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return normalized specs for one venue."""
        rows = []
        for inst in filter_instruments(adapter, params.args.underlying, include_inactive=params.args.include_inactive):
            rows.append(
                {
                    **instrument_row(inst),
                    "asset_class": inst.asset_class,
                    "quote_ccy": inst.quote_ccy,
                    "instrument_type": inst.instrument_type,
                    "status": inst.status,
                    "contract_size": inst.contract_size,
                    "native_price_unit": inst.native_price_unit,
                    "tick_size": inst.tick_size,
                    "funding_interval_hours": inst.funding_interval_hours,
                    "funding_cap": inst.funding_cap,
                    "max_leverage": inst.max_leverage,
                    "initial_margin_rate": inst.initial_margin_rate,
                    "maintenance_margin_rate": inst.maintenance_margin_rate,
                    "margin_source": inst.margin_source,
                    "listed_at": inst.listed_at,
                }
            )
        return rows, None
