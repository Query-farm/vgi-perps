"""Derived analytics: carry spreads, book depth, liquidation bands. Pure, golden-tested."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from .model import BookLevel, FundingPoint
from .normalize import annualize, fill_cost_bps

#: Version tag written into every liquidation_estimate row.
LIQUIDATION_METHOD = "isolated_linear_entry_at_mark_v1"

DEFAULT_LEVERAGES = "2,3,5,10,20"
DEFAULT_LEVERAGE_WEIGHTS = "0.30,0.25,0.20,0.15,0.10"


@dataclass(frozen=True, slots=True)
class CarrySummary:
    """Mean annualized funding of one instrument over a window."""

    venue: str
    symbol: str
    underlying: str
    mean_annualized: float
    observations: int


def summarize_carry(points: Sequence[FundingPoint]) -> list[CarrySummary]:
    """Mean of per-observation annualized rates, per instrument (settled points only)."""
    groups: dict[tuple[str, str], list[FundingPoint]] = defaultdict(list)
    for p in points:
        if p.is_final:
            groups[(p.instrument.venue, p.instrument.symbol)].append(p)
    out = []
    for (venue, symbol), pts in sorted(groups.items()):
        annual = [a for p in pts if (a := annualize(p.rate, p.interval_hours)) is not None]
        if annual:
            out.append(
                CarrySummary(venue, symbol, pts[0].instrument.underlying, sum(annual) / len(annual), len(annual))
            )
    return out


def carry_pairs(summaries: Sequence[CarrySummary]) -> list[dict[str, Any]]:
    """Every cross-venue pair for the same underlying, with ``spread = a - b``.

    Positive funding means longs pay, so a short in ``a`` and a long in ``b``
    collects ``spread`` per year (before fees, basis moves and margin cost).
    """
    rows = []
    by_underlying: dict[str, list[CarrySummary]] = defaultdict(list)
    for s in summaries:
        by_underlying[s.underlying].append(s)
    for underlying, group in sorted(by_underlying.items()):
        for a, b in combinations(sorted(group, key=lambda s: (s.venue, s.symbol)), 2):
            if a.venue == b.venue:
                continue
            spread = a.mean_annualized - b.mean_annualized
            rows.append(
                {
                    "underlying": underlying,
                    "venue_a": a.venue,
                    "symbol_a": a.symbol,
                    "venue_b": b.venue,
                    "symbol_b": b.symbol,
                    "annualized_a": a.mean_annualized,
                    "annualized_b": b.mean_annualized,
                    "spread_annualized": spread,
                    "spread_bps": spread * 10_000.0,
                    "observations_a": a.observations,
                    "observations_b": b.observations,
                    "carry_trade": f"short {a.venue} / long {b.venue}"
                    if spread > 0
                    else (f"long {a.venue} / short {b.venue}" if spread < 0 else "flat"),
                }
            )
    return rows


def depth_metrics(
    bids: Sequence[BookLevel], asks: Sequence[BookLevel], contract_size: float, notional_usd: float
) -> dict[str, Any]:
    """Spread, cost to fill ``notional_usd`` per side, and book imbalance.

    ``buy_cost_bps`` walks the asks (a market buy), ``sell_cost_bps`` the bids;
    each is the distance of the fill VWAP from mid. ``imbalance`` is
    ``(bid_usd - ask_usd) / (bid_usd + ask_usd)`` over the levels fetched.
    """
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    mid = (best_bid + best_ask) / 2.0 if best_bid is not None and best_ask is not None else None
    bid_levels = [(lv.price, lv.contracts * contract_size * lv.price) for lv in bids]
    ask_levels = [(lv.price, lv.contracts * contract_size * lv.price) for lv in asks]
    bid_usd = sum(n for _, n in bid_levels)
    ask_usd = sum(n for _, n in ask_levels)
    out: dict[str, Any] = {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": None if mid is None else (best_ask - best_bid) / mid * 10_000.0,  # type: ignore[operator]
        "notional_usd": notional_usd,
        "bid_depth_usd": bid_usd,
        "ask_depth_usd": ask_usd,
        "imbalance": None if bid_usd + ask_usd == 0 else (bid_usd - ask_usd) / (bid_usd + ask_usd),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
    }
    for side, levels in (("buy", ask_levels), ("sell", bid_levels)):
        if mid is None or not levels:
            out[f"{side}_cost_bps"], out[f"{side}_filled_usd"] = None, 0.0
            continue
        cost, filled = fill_cost_bps(levels, notional_usd, mid)
        out[f"{side}_cost_bps"] = None if math.isnan(cost) else cost
        out[f"{side}_filled_usd"] = filled
    out["fully_filled"] = out["buy_filled_usd"] >= notional_usd * (1 - 1e-12) and out[
        "sell_filled_usd"
    ] >= notional_usd * (1 - 1e-12)
    return out


def parse_weights(leverages: str, weights: str | None) -> list[tuple[float, float]]:
    """``"2,5,10"`` and ``"0.5,0.3,0.2"`` -> ``[(2, .5), (5, .3), (10, .2)]``.

    Weights default to uniform and are renormalized to sum to 1.

    Args:
        leverages: Comma-separated leverage buckets.
        weights: Comma-separated weights, or ``None`` for uniform.

    Returns:
        ``(leverage, weight)`` pairs whose weights sum to 1.

    Raises:
        ValueError: Non-numeric values, leverage <= 1, negative weights, or a
            length mismatch.

    """
    try:
        levs = [float(x) for x in leverages.split(",") if x.strip()]
        ws = [float(x) for x in weights.split(",") if x.strip()] if weights else [1.0] * len(levs)
    except ValueError as exc:
        raise ValueError(f"leverages/weights must be comma-separated numbers: {exc}") from exc
    if not levs or len(ws) != len(levs):
        raise ValueError(f"got {len(levs)} leverages and {len(ws)} weights; they must match and be non-empty")
    if any(lev <= 1.0 for lev in levs) or any(w < 0 for w in ws) or sum(ws) <= 0:
        raise ValueError("every leverage must be > 1 and weights must be non-negative with a positive sum")
    total = sum(ws)
    return [(lev, w / total) for lev, w in zip(levs, ws, strict=True)]


def liquidation_price(entry: float, leverage: float, mm_rate: float, side: int) -> float | None:
    """Liquidation price of an isolated linear position opened at ``entry``.

    Equity ``entry/L + side*(X - entry)`` per unit hits maintenance ``mm*X`` at
    ``X = entry * (side - 1/L) / (side - mm)``. This equals Hyperliquid's
    documented ``price - side*margin_available/size/(1 - l*side)``. Returns
    ``None`` when ``mm >= 1/L`` (liquidated at entry: leverage not allowed).
    """
    if mm_rate >= 1.0 / leverage:
        return None
    return entry * (side - 1.0 / leverage) / (side - mm_rate)


def liquidation_bands(
    *,
    mark: float,
    open_interest_contracts: float,
    contract_size: float,
    mm_rate: float,
    max_leverage: float | None,
    distribution: Sequence[tuple[float, float]],
) -> list[dict[str, Any]]:
    """Spread open interest over the leverage distribution, both sides.

    Every open contract has one long and one short holder, so each side carries
    the full open interest, split across leverage buckets by weight. Rows are
    ordered nearest-to-mark first within each side, with cumulative notional.
    """
    rows: list[dict[str, Any]] = []
    for side_name, side in (("long", 1), ("short", -1)):
        side_rows: list[dict[str, Any]] = []
        for leverage, weight in distribution:
            liq = liquidation_price(mark, leverage, mm_rate, side)
            contracts = open_interest_contracts * weight
            feasible = liq is not None and (max_leverage is None or leverage <= max_leverage + 1e-9)
            side_rows.append(
                {
                    "side": side_name,
                    "leverage": leverage,
                    "weight": weight,
                    "liquidation_price": liq,
                    "distance_pct": None if liq is None else (liq / mark - 1.0) * 100.0,
                    "contracts": contracts,
                    "notional_usd": contracts * contract_size * mark,
                    "feasible": feasible,
                }
            )
        side_rows.sort(key=lambda r: abs(r["distance_pct"]) if r["distance_pct"] is not None else math.inf)
        running = 0.0
        for r in side_rows:
            if r["feasible"]:
                running += r["notional_usd"]
            r["cum_notional_usd"] = running
        rows += side_rows
    return rows


# ------------------------------------------------------------ Kalshi funding

#: Version tag written into every kalshi_funding_nowcast row.
KALSHI_FUNDING_METHOD = "kalshi_linear_twap_v1"


def weight_sum(first: int, last: int) -> float:
    """Sum of the integer weights ``first..last`` inclusive; 0 when ``last < first``."""
    if last < first:
        return 0.0
    return (first + last) * (last - first + 1) / 2.0


@dataclass(frozen=True, slots=True)
class FundingNowcast:
    """Kalshi funding for one window: accrued so far, and projected to the settlement."""

    #: Linear-weighted mean premium over the elapsed seconds (what Kalshi's estimate reports, before the deadband).
    accrued_rate: float | None
    #: Share of the whole window's weight already elapsed: ``elapsed(elapsed+1) / (T(T+1))``.
    weight_accrued: float
    #: Projected window average before the deadband and cap.
    projected_raw: float | None
    #: Projected settlement: 0 inside the deadband, clamped to the cap.
    projected_rate: float | None


def settle_kalshi_rate(raw: float | None, deadband: float | None, cap: float | None) -> float | None:
    """Apply Kalshi's zero threshold, then the cap. ``|raw| < deadband`` settles at exactly 0."""
    if raw is None:
        return None
    if deadband and abs(raw) < deadband:
        return 0.0
    if cap:
        return max(-cap, min(cap, raw))
    return raw


def kalshi_funding_nowcast(
    premiums: dict[int, float],
    elapsed: int,
    interval: int,
    *,
    handling: str,
    assumed: float | None,
    deadband: float | None,
    cap: float | None,
) -> FundingNowcast:
    """Kalshi's funding formula, reverse-engineered and matched exactly against settlements.

    Second ``i`` of the window (1 = the first second after the previous funding
    time, ``interval`` = the last) has weight ``i``; funding is the weighted mean
    of the per-second premium index. Under ``dampen`` a second with no premium
    (reported as 0, or missing) counts as 0; under ``exclude`` it is left out.
    The remaining ``elapsed+1..interval`` seconds are filled with ``assumed``.

    Args:
        premiums: Premium index (fraction) by 1-based second offset in the window.
        elapsed: Seconds of the window observed so far (``interval`` for a finished window).
        interval: Seconds in the window (28,800 for an 8-hour market).
        handling: The market's ``funding_invalid_premium_handling``: 'dampen' or 'exclude'.
        assumed: Premium assumed for the unobserved rest of the window; ``None`` = no projection.
        deadband: Zero threshold per interval (``funding_deadband``).
        cap: Absolute cap per interval (``funding_cap``).

    Returns:
        The accrued and projected rates.

    """
    elapsed = max(0, min(elapsed, interval))
    included = [(i, p) for i, p in premiums.items() if 1 <= i <= elapsed and not (handling == "exclude" and p == 0.0)]
    numerator = sum(i * p for i, p in included)
    observed_weight = weight_sum(1, elapsed) if handling != "exclude" else float(sum(i for i, _ in included))
    accrued = numerator / observed_weight if observed_weight else None
    remaining = weight_sum(elapsed + 1, interval)
    if remaining == 0.0:
        raw = accrued
    elif assumed is None:
        raw = None
    else:
        raw = (numerator + assumed * remaining) / (observed_weight + remaining)
    return FundingNowcast(
        accrued_rate=accrued,
        weight_accrued=weight_sum(1, elapsed) / weight_sum(1, interval) if interval else 0.0,
        projected_raw=raw,
        projected_rate=settle_kalshi_rate(raw, deadband, cap),
    )
