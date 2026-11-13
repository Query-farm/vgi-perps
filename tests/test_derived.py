"""Golden tests for the derived analytics, with hand-computed expectations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from perps_worker import derived
from perps_worker.model import BookLevel, FundingPoint, Instrument


def _inst(venue: str, symbol: str, underlying: str = "BTC") -> Instrument:
    return Instrument(venue, symbol, underlying, "crypto", "USD", "perpetual", "active", 1.0, 1.0, "x")


def _point(inst: Instrument, hour: int, rate: float, hours: float, final: bool = True) -> FundingPoint:
    return FundingPoint(inst, datetime(2026, 11, 12, hour, tzinfo=UTC), rate, hours, final)


def test_carry_pairs_mean_annualized_and_direction() -> None:
    a = _inst("kalshi", "KXBTCPERP")
    b = _inst("hyperliquid", "BTC")
    c = _inst("kalshi", "KXETHPERP", "ETH")
    points = [
        _point(a, 5, 0.0002, 8.0),  # 0.219
        _point(a, 13, 0.0, 8.0),  # 0.0   -> mean 0.1095
        _point(b, 1, 0.0000125, 1.0),  # 0.1095
        _point(b, 2, -0.0000125, 1.0),  # -0.1095 -> mean 0.0
        _point(b, 3, 0.5, 1.0, final=False),  # estimates never enter the mean
        _point(c, 5, 0.0001, 8.0),  # a different underlying: no pair
    ]
    rows = derived.carry_pairs(derived.summarize_carry(points))
    assert len(rows) == 1
    row = rows[0]
    # Pairs are ordered alphabetically by venue: a = hyperliquid, b = kalshi.
    assert (row["venue_a"], row["venue_b"]) == ("hyperliquid", "kalshi")
    assert row["annualized_a"] == pytest.approx(0.0)
    assert row["annualized_b"] == pytest.approx(0.1095)
    assert row["spread_annualized"] == pytest.approx(-0.1095)
    assert row["spread_bps"] == pytest.approx(-1095.0)
    assert (row["observations_a"], row["observations_b"]) == (2, 2)
    # Kalshi longs pay more, so short Kalshi / long Hyperliquid collects the spread.
    assert row["carry_trade"] == "long hyperliquid / short kalshi"


def test_carry_pairs_skip_same_venue() -> None:
    p = [
        _point(_inst("bitnomial", "PBTCUCZ50"), 1, 0.0001, 8.0),
        _point(_inst("bitnomial", "PBTCURZ50"), 1, 0.0002, 8.0),
    ]
    assert derived.carry_pairs(derived.summarize_carry(p)) == []


def test_depth_metrics_hand_computed() -> None:
    # contract_size 0.5: a level of 2 contracts at 100 holds 1 unit = 100 USD.
    bids = [BookLevel(99.0, 2.0), BookLevel(98.0, 4.0)]  # 99 USD, 196 USD
    asks = [BookLevel(101.0, 2.0), BookLevel(103.0, 2.0)]  # 101 USD, 103 USD
    m = derived.depth_metrics(bids, asks, 0.5, 150.0)
    assert m["mid"] == 100.0
    assert m["spread_bps"] == pytest.approx(200.0)
    assert m["bid_depth_usd"] == pytest.approx(295.0)
    assert m["ask_depth_usd"] == pytest.approx(204.0)
    assert m["imbalance"] == pytest.approx((295 - 204) / (295 + 204))
    # Buy 150 USD: 101 USD at 101 (1 unit) + 49 USD at 103.
    units = 1.0 + 49 / 103
    assert m["buy_cost_bps"] == pytest.approx((150 / units / 100 - 1) * 10_000)
    # Sell 150 USD: 99 USD at 99 (1 unit) + 51 USD at 98.
    units = 1.0 + 51 / 98
    assert m["sell_cost_bps"] == pytest.approx(abs(150 / units / 100 - 1) * 10_000)
    assert m["fully_filled"] is True
    assert derived.depth_metrics(bids, asks, 0.5, 1_000.0)["fully_filled"] is False


def test_depth_metrics_empty_book() -> None:
    m = derived.depth_metrics([], [], 1.0, 10.0)
    assert m["mid"] is None and m["buy_cost_bps"] is None and m["imbalance"] is None


def test_parse_weights_normalizes_and_validates() -> None:
    assert derived.parse_weights("2,4", "1,3") == [(2.0, 0.25), (4.0, 0.75)]
    assert derived.parse_weights("2,4", None) == [(2.0, 0.5), (4.0, 0.5)]
    for bad in (("2,x", "1,1"), ("2", "1,1"), ("1,2", "1,1"), ("2,3", "-1,2"), ("", "")):
        with pytest.raises(ValueError):
            derived.parse_weights(*bad)


@pytest.mark.parametrize(
    ("leverage", "mm", "side", "expected"),
    [
        (10.0, 0.05, 1, 100 * 0.9 / 0.95),  # long: E (1 - 1/L) / (1 - mm)
        (10.0, 0.05, -1, 100 * 1.1 / 1.05),  # short: E (1 + 1/L) / (1 + mm)
        (2.0, 0.0, 1, 50.0),  # no maintenance: a 2x long dies at -50%
        (40.0, 0.0125, 1, 100 * 0.975 / 0.9875),
    ],
)
def test_liquidation_price(leverage: float, mm: float, side: int, expected: float) -> None:
    assert derived.liquidation_price(100.0, leverage, mm, side) == pytest.approx(expected)


def test_liquidation_price_matches_hyperliquid_formula() -> None:
    # Hyperliquid docs: liq = price - side * margin_available / size / (1 - l * side),
    # with margin_available = initial - maintenance for an isolated position.
    entry, leverage, mm, size = 70938.0, 20.0, 0.0125, 3.0
    for side in (1, -1):
        margin_available = entry * size / leverage - mm * entry * size
        hl = entry - side * margin_available / size / (1 - mm * side)
        assert derived.liquidation_price(entry, leverage, mm, side) == pytest.approx(hl)


def test_liquidation_unreachable_leverage() -> None:
    assert derived.liquidation_price(100.0, 25.0, 0.05, 1) is None  # mm 5% >= 1/25


def test_liquidation_bands_split_open_interest_both_sides() -> None:
    rows = derived.liquidation_bands(
        mark=100.0,
        open_interest_contracts=1_000.0,
        contract_size=0.1,
        mm_rate=0.02,
        max_leverage=10.0,
        distribution=[(5.0, 0.5), (10.0, 0.3), (20.0, 0.2)],
    )
    longs = [r for r in rows if r["side"] == "long"]
    assert [r["leverage"] for r in longs] == [20.0, 10.0, 5.0]  # nearest first
    assert longs[0]["feasible"] is False  # 20x is reachable but exceeds the 10x max
    assert longs[1]["contracts"] == pytest.approx(300.0)
    assert longs[1]["notional_usd"] == pytest.approx(300 * 0.1 * 100)
    # Cumulative notional counts feasible buckets only: 10x (3000) then 5x (5000).
    assert [r["cum_notional_usd"] for r in longs] == pytest.approx([0.0, 3000.0, 8000.0])
    assert sum(r["weight"] for r in longs) == pytest.approx(1.0)
    assert len([r for r in rows if r["side"] == "short"]) == 3


# ------------------------------------------------------------ Kalshi funding


def test_weight_sum_closed_form() -> None:
    assert derived.weight_sum(1, 4) == 10.0
    assert derived.weight_sum(3, 5) == 12.0
    assert derived.weight_sum(5, 4) == 0.0
    assert derived.weight_sum(1, 28_800) == 28_800 * 28_801 / 2


def test_kalshi_nowcast_finished_window_is_the_linear_twap() -> None:
    # 4-second window, premiums 1..4 bps: sum(i * p_i) / sum(i) = (1+4+9+16)/10 = 3 bps.
    p = {1: 0.0001, 2: 0.0002, 3: 0.0003, 4: 0.0004}
    nc = derived.kalshi_funding_nowcast(p, 4, 4, handling="dampen", assumed=None, deadband=0.0001, cap=0.02)
    assert nc.accrued_rate == pytest.approx(0.0003, abs=1e-15)
    assert nc.projected_rate == pytest.approx(0.0003, abs=1e-15)
    assert nc.weight_accrued == 1.0


def test_kalshi_nowcast_dampen_counts_missing_seconds_as_zero_exclude_drops_them() -> None:
    p = {1: 0.0004, 2: 0.0, 4: 0.0004}  # second 2 reports no premium, second 3 is missing
    damp = derived.kalshi_funding_nowcast(p, 4, 4, handling="dampen", assumed=None, deadband=None, cap=None)
    excl = derived.kalshi_funding_nowcast(p, 4, 4, handling="exclude", assumed=None, deadband=None, cap=None)
    assert damp.accrued_rate == pytest.approx((1 * 0.0004 + 4 * 0.0004) / 10, abs=1e-15)  # 2 bps
    assert excl.accrued_rate == pytest.approx((1 * 0.0004 + 4 * 0.0004) / 5, abs=1e-15)  # 4 bps


def test_kalshi_nowcast_projects_the_rest_with_its_larger_weight() -> None:
    # 2 of 4 seconds seen at 1 bp; the rest assumed 3 bps: (1+2)*1 + (3+4)*3 = 24 bps*s over weight 10.
    p = {1: 0.0001, 2: 0.0001}
    nc = derived.kalshi_funding_nowcast(p, 2, 4, handling="dampen", assumed=0.0003, deadband=0.0001, cap=0.02)
    assert nc.accrued_rate == pytest.approx(0.0001, abs=1e-15)
    assert nc.weight_accrued == pytest.approx(0.3)
    assert nc.projected_raw == pytest.approx(0.00024, abs=1e-15)
    no_view = derived.kalshi_funding_nowcast(p, 2, 4, handling="dampen", assumed=None, deadband=None, cap=None)
    assert no_view.projected_raw is None and no_view.projected_rate is None


def test_kalshi_settle_deadband_then_cap() -> None:
    assert derived.settle_kalshi_rate(0.0000939, 0.0001, 0.02) == 0.0  # the Nov 11 13:00 UTC window
    assert derived.settle_kalshi_rate(-0.0000999, 0.0001, 0.02) == 0.0
    assert derived.settle_kalshi_rate(0.0001, 0.0001, 0.02) == 0.0001
    assert derived.settle_kalshi_rate(0.05, 0.0001, 0.02) == 0.02
    assert derived.settle_kalshi_rate(-0.05, 0.0001, 0.02) == -0.02
    assert derived.settle_kalshi_rate(None, 0.0001, 0.02) is None
