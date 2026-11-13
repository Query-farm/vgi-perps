"""Pure normalization arithmetic, shared by every adapter and golden-tested.

Nothing here touches the network. Each function is small enough to verify by
hand; ``tests/test_normalize.py`` does exactly that.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from itertools import pairwise

HOURS_PER_YEAR = 8760.0

#: Leading multipliers venues put on small-unit coins: ``1000PEPE``,
#: ``1000000MOG``, Hyperliquid's ``kPEPE``.
_NUMERIC_PREFIX = re.compile(r"^(1000000|100000|10000|1000|100)(?=[A-Z])")
_K_PREFIX = re.compile(r"^k(?=[A-Z])")


def annualize(rate_per_interval: float | None, interval_hours: float | None) -> float | None:
    """Annualize simply (no compounding): ``rate * 8760 / interval_hours``.

    A 0.01% rate paid every 8 hours annualizes to ``0.0001 * 1095 = 0.1095``.
    """
    if rate_per_interval is None or not interval_hours:
        return None
    return rate_per_interval * HOURS_PER_YEAR / interval_hours


def premium_bps(price: float | None, reference: float | None) -> float | None:
    """Premium of ``price`` over ``reference`` in basis points; positive = rich."""
    if price is None or reference is None or reference == 0:
        return None
    return (price / reference - 1.0) * 10_000.0


def notional(contracts: float | None, contract_size: float, price: float | None) -> float | None:
    """Quote-currency notional of ``contracts`` at ``price`` (per base unit)."""
    if contracts is None or price is None:
        return None
    return contracts * contract_size * price


def split_unit_prefix(base: str) -> tuple[str, float]:
    """Split a scaled coin name into ``(underlying, units per named unit)``.

    ``1000PEPE`` -> ``("PEPE", 1000)``, ``kPEPE`` -> ``("PEPE", 1000)``,
    ``BTC`` -> ``("BTC", 1)``.
    """
    match = _NUMERIC_PREFIX.match(base)
    if match:
        return base[match.end() :], float(match.group(1))
    if _K_PREFIX.match(base):
        return base[1:], 1000.0
    return base, 1.0


def interval_from_times(times: Iterable[datetime]) -> float | None:
    """Return the most common spacing, in hours, between consecutive funding times."""
    ordered = sorted(set(times))
    gaps = [round((b - a).total_seconds() / 3600.0, 4) for a, b in pairwise(ordered)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    return Counter(gaps).most_common(1)[0][0]


def from_epoch_ms(value: float | int | str | None) -> datetime | None:
    """Epoch milliseconds -> aware UTC datetime."""
    if value is None or value == "":
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)


def from_epoch_s(value: float | int | str | None) -> datetime | None:
    """Epoch seconds -> aware UTC datetime."""
    if value is None or value == "":
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


def parse_iso(value: str | None) -> datetime | None:
    """ISO 8601 (``Z`` or offset, up to nanoseconds) -> aware UTC datetime."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Python accepts at most 6 fractional digits; venues send up to 9.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_float(value: object) -> float | None:
    """Venue numbers arrive as strings, numbers, ``""`` or ``None``; normalize."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        try:
            result = float(str(value))
        except ValueError:
            return None
    return result if math.isfinite(result) else None


def fill_cost_bps(levels: Sequence[tuple[float, float]], notional_target: float, mid: float) -> tuple[float, float]:
    """Walk one side of a book to fill ``notional_target``.

    Args:
        levels: ``(price, notional_at_level)`` from best to worst.
        notional_target: Quote-currency notional to fill.
        mid: Reference mid price.

    Returns:
        ``(cost_bps, filled_notional)``. ``cost_bps`` is the absolute distance
        of the volume-weighted fill price from ``mid`` in basis points;
        ``filled_notional`` is less than the target when the book runs out.

    """
    remaining = notional_target
    filled = 0.0
    units = 0.0
    for price, level_notional in levels:
        if remaining <= 0:
            break
        take = min(level_notional, remaining)
        filled += take
        units += take / price
        remaining -= take
    if units == 0 or mid == 0:
        return math.nan, 0.0
    vwap = filled / units
    return abs(vwap / mid - 1.0) * 10_000.0, filled
