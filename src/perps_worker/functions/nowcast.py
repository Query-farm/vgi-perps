"""Kalshi funding nowcast: the linear time-weighted premium, accrued and projected."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import ProcessParams, bind_fixed_schema, init_single_worker

from .. import derived, replay
from ..meta import object_tags
from ..normalize import annualize, parse_iso, to_float
from ..venues import Adapter, VenueError, get
from ..venues.kalshi import KalshiAdapter
from .common import F64, STR, TS, ArgumentError, VenueScan, build_schema, result_columns

NOWCAST_SCHEMA = build_schema(
    [
        ("symbol", STR, "Kalshi perps ticker.", False),
        ("funding_time", TS, "The funding time this window settles at, UTC.", False),
        ("window_start", TS, "Start of the window (the previous funding time), UTC.", False),
        ("as_of", TS, "End of the observed part of the window: now, or funding_time for a finished window.", False),
        ("interval_hours", F64, "Window length in hours.", False),
        ("elapsed_seconds", pa.int64(), "Seconds of the window observed.", False),
        (
            "weight_accrued",
            F64,
            "Share of the window's total weight already elapsed (second i weighs i, so the last hour of an "
            "8-hour window carries about 23%).",
            False,
        ),
        (
            "accrued_rate",
            F64,
            "Linear-weighted mean premium over the elapsed seconds, per interval, before the deadband. Equals "
            "Kalshi's own estimate before rounding.",
            True,
        ),
        (
            "assumed_premium",
            F64,
            "Premium assumed for the rest of the window (assume_premium, else the mean of the last recent_minutes).",
            True,
        ),
        ("projected_rate_raw", F64, "Projected window average before the deadband and cap.", True),
        (
            "projected_rate",
            F64,
            "Projected settlement per interval: 0 inside the deadband, clamped to the cap. Positive = longs pay.",
            True,
        ),
        ("projected_rate_annualized", F64, "projected_rate * 8760 / interval_hours.", True),
        ("kalshi_estimate_rate", F64, "Kalshi's published estimate (current window only).", True),
        ("settled_rate", F64, "The settled rate (finished windows only), to check the reconstruction.", True),
        ("funding_deadband", F64, "Zero threshold per interval.", True),
        ("funding_cap", F64, "Absolute cap per interval.", True),
        ("invalid_premium_handling", STR, "'dampen' (no-premium seconds count as 0) or 'exclude'.", True),
        ("method", STR, "Formula version: linear time weights over per-second premiums.", False),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NowcastArgs:
    """Arguments for ``kalshi_funding_nowcast(symbol, funding_time, assume_premium, recent_minutes)``."""

    symbol: Annotated[str, Arg(0, doc="Kalshi perps ticker, e.g. 'KXBTCPERP'.")]
    funding_time: Annotated[
        datetime | None,
        Arg(
            "funding_time",
            default=None,
            arrow_type=TS,
            doc="Window to compute, by its funding time. Default: the current window. A past funding time "
            "reconstructs that settlement (premium history is retained for a few days only).",
        ),
    ]
    assume_premium: Annotated[
        float | None,
        Arg(
            "assume_premium",
            default=None,
            doc="Premium (fraction, e.g. 0.00015 = 1.5 bps) assumed for the rest of the window. "
            "Default: the mean premium over the last recent_minutes.",
        ),
    ]
    recent_minutes: Annotated[
        int,
        Arg(
            "recent_minutes",
            default=15,
            doc="Minutes of recent premium averaged for the default projection, 1 to 480 (default 15).",
            ge=1,
            le=480,
        ),
    ]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@init_single_worker
@bind_fixed_schema
class KalshiFundingNowcast(VenueScan[NowcastArgs]):
    """Accrued and projected Kalshi funding for one window."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = NOWCAST_SCHEMA

    class Meta:
        """Function metadata."""

        name = "kalshi_funding_nowcast"
        description = (
            "Kalshi funding for one window, computed from the per-second premium with Kalshi's linear time "
            "weights: accrued so far, projected to settlement, or reconstructed for a past window"
        )
        tags = {
            **object_tags(
                title="Kalshi Funding Nowcast",
                category="analytics",
                keywords="funding forecast, funding estimate, premium index, twap, deadband, carry timing",
                doc_llm=(
                    "Forecast or reconstruct a Kalshi perp funding settlement: `kalshi_funding_nowcast("
                    "'KXBTCPERP')` for the current window, `funding_time := TIMESTAMPTZ '...'` for a past one. "
                    "Kalshi funding is the linear time-weighted mean of the per-second premium (second i of "
                    "the window weighs i), set to 0 when its absolute value is below `funding_deadband` and "
                    "clamped to `funding_cap`. `accrued_rate` is the weighted mean so far and equals Kalshi's "
                    "own estimate; `weight_accrued` says how much of the final weight it represents (about "
                    "1.6% after the first hour of an 8-hour window). `projected_rate` fills the rest of the "
                    "window with `assume_premium` (default: the mean of the last `recent_minutes`). For a "
                    "past window `settled_rate` shows the published settlement next to the reconstruction. "
                    "Rates are per interval; positive = longs pay."
                ),
                doc_md=(
                    "## kalshi_funding_nowcast(symbol, funding_time := NULL, assume_premium := NULL, "
                    "recent_minutes := 15)\n\nKalshi funding is `sum(i * p_i) / sum(i)` over the seconds `i` "
                    "of the window, zeroed inside the deadband and capped. This returns the accrued value, a "
                    "projection to settlement, and for a past window the settled rate."
                ),
            ),
            "vgi.result_columns_schema": result_columns(NOWCAST_SCHEMA),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT weight_accrued, accrued_rate * 1e4 AS accrued_bps, projected_rate_annualized "
                    "FROM perps.main.kalshi_funding_nowcast('KXBTCPERP')"
                ),
                description="Where the next BTC funding settlement is heading",
            ),
            FunctionExample(
                sql=(
                    "SELECT projected_rate_annualized FROM perps.main.kalshi_funding_nowcast('KXBTCPERP', "
                    "assume_premium := 0.0002)"
                ),
                description="Settlement if the BTC premium holds at 2 bps for the rest of the window",
            ),
        ]

    @classmethod
    def venue_list(cls, params: ProcessParams[NowcastArgs]) -> tuple[list[Adapter], bool]:
        """Kalshi only."""
        return [get("kalshi")], False

    @classmethod
    def produce(
        cls, params: ProcessParams[NowcastArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One row for the requested window."""
        assert isinstance(adapter, KalshiAdapter)
        args = params.args
        inst = adapter.instrument(args.symbol)
        hours = inst.funding_interval_hours or 8.0
        interval = int(hours * 3600)
        now = replay.now()
        estimate: dict[str, Any] = {}
        if args.funding_time is None or _utc(args.funding_time) > now:
            estimate = adapter.estimate(inst)
            next_time = parse_iso(estimate.get("next_funding_time"))
            if next_time is None:
                raise VenueError(f"kalshi returned no next_funding_time for {inst.symbol}")
            if args.funding_time is not None and _utc(args.funding_time) != next_time:
                raise ArgumentError(
                    f"funding_time {args.funding_time} is in the future but is not the next funding time "
                    f"({next_time.isoformat()})"
                )
            funding_time = next_time
        else:
            funding_time = _utc(args.funding_time)
        start = funding_time - timedelta(seconds=interval)
        as_of = min(now, funding_time)
        elapsed = max(0, int((as_of - start).total_seconds()))
        premiums = adapter.premium_seconds(inst, start, start + timedelta(seconds=elapsed)) if elapsed else {}
        if elapsed and not premiums:
            raise VenueError(
                f"kalshi returned no premium index for {inst.symbol} between {start.isoformat()} and "
                f"{as_of.isoformat()}; it keeps only recent history"
            )
        handling = inst.extra.get("funding_invalid_premium_handling") or "dampen"
        deadband = to_float(inst.extra.get("funding_deadband"))
        assumed = args.assume_premium
        if assumed is None and elapsed < interval and premiums:
            recent = [p for i, p in premiums.items() if i > elapsed - args.recent_minutes * 60]
            if handling == "exclude":
                recent = [p for p in recent if p != 0.0]
            assumed = sum(recent) / len(recent) if recent else None
        nowcast = derived.kalshi_funding_nowcast(
            premiums,
            elapsed,
            interval,
            handling=handling,
            assumed=assumed if elapsed < interval else None,
            deadband=deadband,
            cap=inst.funding_cap,
        )
        settled = adapter.settled_rate(inst, funding_time) if elapsed >= interval else None
        return [
            {
                "symbol": inst.symbol,
                "funding_time": funding_time,
                "window_start": start,
                "as_of": as_of,
                "interval_hours": hours,
                "elapsed_seconds": elapsed,
                "weight_accrued": nowcast.weight_accrued,
                "accrued_rate": nowcast.accrued_rate,
                "assumed_premium": assumed if elapsed < interval else None,
                "projected_rate_raw": nowcast.projected_raw,
                "projected_rate": nowcast.projected_rate,
                "projected_rate_annualized": annualize(nowcast.projected_rate, hours),
                "kalshi_estimate_rate": to_float(estimate.get("funding_rate")),
                "settled_rate": settled,
                "funding_deadband": deadband,
                "funding_cap": inst.funding_cap,
                "invalid_premium_handling": handling,
                "method": derived.KALSHI_FUNDING_METHOD,
            }
        ], None
