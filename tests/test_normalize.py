"""Golden tests for the normalization arithmetic, every value computed by hand."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from perps_worker.model import Instrument
from perps_worker.normalize import (
    annualize,
    fill_cost_bps,
    interval_from_times,
    notional,
    parse_iso,
    premium_bps,
    split_unit_prefix,
    to_float,
)


@pytest.mark.parametrize(
    ("rate", "hours", "expected"),
    [
        (0.0001, 8.0, 0.1095),  # 1 bp per 8h -> 1095 periods -> 10.95%
        (0.0000125, 1.0, 0.1095),  # Hyperliquid baseline: 0.01%/8h paid hourly
        (0.000005, 1.0, 0.0438),  # Coinbase hourly 0.0005% -> 4.38%
        (0.0002, 24.0, 0.073),  # daily 2 bp -> 365 periods -> 7.3%
        (-0.0003, 4.0, -0.657),  # sign survives: shorts pay longs
    ],
)
def test_annualize_is_simple_rate_times_periods(rate: float, hours: float, expected: float) -> None:
    assert annualize(rate, hours) == pytest.approx(expected, rel=1e-12)


def test_annualize_missing_inputs() -> None:
    assert annualize(None, 8.0) is None
    assert annualize(0.0001, None) is None
    assert annualize(0.0001, 0.0) is None


def test_premium_bps_sign_and_scale() -> None:
    assert premium_bps(101.0, 100.0) == pytest.approx(100.0)
    assert premium_bps(99.5, 100.0) == pytest.approx(-50.0)
    assert premium_bps(72250.0, 71630.0) == pytest.approx(86.55591232723719)
    assert premium_bps(None, 1.0) is None
    assert premium_bps(1.0, 0.0) is None


def test_notional() -> None:
    # 10 Kalshi BTC contracts (0.0001 BTC each) at 70,000 USD/BTC = 70 USD.
    assert notional(10, 0.0001, 70_000.0) == pytest.approx(70.0)
    # 4054 Coinbase 1000SHIB contracts (10,000,000 SHIB each) at 4.35e-6 USD/SHIB.
    assert notional(4054, 1e7, 4.35e-6) == pytest.approx(176_349.0)
    assert notional(None, 1.0, 1.0) is None


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("1000PEPE", ("PEPE", 1000.0)),
        ("1000000MOG", ("MOG", 1_000_000.0)),
        ("kPEPE", ("PEPE", 1000.0)),
        ("kSHIB", ("SHIB", 1000.0)),
        ("BTC", ("BTC", 1.0)),
        ("KAITO", ("KAITO", 1.0)),  # capital K is part of the name, not a prefix
        ("1INCH", ("1INCH", 1.0)),  # a leading digit that is not a multiplier
    ],
)
def test_split_unit_prefix(base: str, expected: tuple[str, float]) -> None:
    assert split_unit_prefix(base) == expected


def test_interval_from_times_takes_the_mode() -> None:
    t = [datetime(2026, 11, 12, h, tzinfo=UTC) for h in (0, 8, 16)] + [datetime(2026, 11, 13, 0, tzinfo=UTC)]
    assert interval_from_times(t) == 8.0
    assert interval_from_times(t[:1]) is None


def test_parse_iso_handles_nanoseconds_and_offsets() -> None:
    assert parse_iso("2026-11-13T00:46:21.852093123Z") == datetime(2026, 11, 13, 0, 46, 21, 852093, tzinfo=UTC)
    assert parse_iso("2026-11-12T19:00:00-05:00") == datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
    assert parse_iso("") is None


def test_to_float() -> None:
    assert to_float("7.0896") == 7.0896
    assert to_float("") is None
    assert to_float("nan") is None
    assert to_float(3) == 3.0


def test_instrument_price_factor_conversions() -> None:
    kalshi = Instrument("kalshi", "KXBTCPERP", "BTC", "crypto", "USD", "perpetual", "active", 0.0001, 1 / 0.0001, "x")
    assert kalshi.price(7.1395) == pytest.approx(71395.0, rel=1e-12)
    bitnomial = Instrument("bitnomial", "PBTCUCZ50", "BTC", "crypto", "USD", "perpetual", "active", 0.01, 5.0, "ticks")
    assert bitnomial.price(14450) == 72250.0
    coinbase_shib = Instrument(
        "coinbase", "SHP", "SHIB", "crypto", "USD", "perp_style_future", "active", 1e7, 1e-3, "x"
    )
    assert coinbase_shib.price(0.00435) == pytest.approx(4.35e-6)
    assert kalshi.canonical_id == "BTC-USD-PERP"


def test_fill_cost_walks_the_book() -> None:
    # mid 100; asks: 50 USD at 101, 100 USD at 102. Buying 100 USD takes 50@101 + 50@102.
    levels = [(101.0, 50.0), (102.0, 100.0)]
    cost, filled = fill_cost_bps(levels, 100.0, 100.0)
    units = 50 / 101 + 50 / 102
    assert filled == 100.0
    assert cost == pytest.approx((100.0 / units / 100.0 - 1) * 10_000)
    # Not enough liquidity: fills what is there.
    cost, filled = fill_cost_bps(levels, 1_000.0, 100.0)
    assert filled == 150.0
    assert math.isnan(fill_cost_bps([], 1.0, 100.0)[0])
