"""Opt-in checks against the real venues: ``make live-check`` (``pytest -m live``).

They re-verify, against today's data, the facts the normalization depends on:
units agree across venues, Kalshi's funding cadence per asset class, and the
auth wall on Coinbase's FairX API. Read-only GETs, a few per venue.
"""

from __future__ import annotations

import itertools
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from perps_worker import http, replay
from perps_worker.venues import ADAPTERS, VenueError

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(replay.FIXTURES_ENV, raising=False)
    monkeypatch.delenv(replay.NOW_ENV, raising=False)
    replay.reset_cache()


@pytest.mark.parametrize("venue", ["kalshi", "coinbase", "bitnomial", "og", "hyperliquid"])
def test_us_and_benchmark_venues_list_btc(venue: str) -> None:
    assert any(i.underlying == "BTC" for i in ADAPTERS[venue].instruments())


def test_btc_prices_agree_across_venues_per_unit() -> None:
    """If any venue's unit scaling were wrong, its BTC price would be off by orders of magnitude."""
    prices: dict[str, float] = {}
    for venue in ("kalshi", "coinbase", "og", "hyperliquid"):
        adapter = ADAPTERS[venue]
        insts = [i for i in adapter.instruments() if i.underlying == "BTC" and i.status == "active"][:1]
        marks = adapter.marks(insts)
        price = marks[0].mark_price or marks[0].last_price
        assert price is not None, venue
        prices[venue] = price
    mid = statistics.median(prices.values())
    for venue, price in prices.items():
        assert abs(price / mid - 1) < 0.02, (venue, prices)


def test_shib_scaling_agrees_between_kalshi_and_coinbase() -> None:
    kalshi = ADAPTERS["kalshi"]
    coinbase = ADAPTERS["coinbase"]
    k = kalshi.marks([i for i in kalshi.instruments() if i.underlying == "SHIB" and i.status == "active"])
    c = coinbase.marks([i for i in coinbase.instruments() if i.underlying == "SHIB"])
    assert k and c
    kp, cp = k[0].mark_price, c[0].index_price or c[0].mark_price
    assert kp and cp and abs(kp / cp - 1) < 0.05, (kp, cp)


def test_kalshi_funding_cadence_matches_the_static_table() -> None:
    """Crypto funds every 8h, everything else daily (the schedule endpoint needs auth)."""
    kalshi = ADAPTERS["kalshi"]
    end = replay.now()
    insts = [i for i in kalshi.instruments() if i.status == "active"]
    points = kalshi.funding(insts, end - timedelta(days=4), end)
    times: dict[str, list[datetime]] = defaultdict(list)
    for p in points:
        times[p.instrument.symbol].append(p.funding_time)
    by_class: dict[str, set[float]] = defaultdict(set)
    for inst in insts:
        ts = sorted(times.get(inst.symbol, []))
        gaps = {(b - a).total_seconds() / 3600 for a, b in itertools.pairwise(ts)}
        if gaps:
            by_class[inst.asset_class] |= {min(gaps)}
            assert min(gaps) == inst.funding_interval_hours, (inst.symbol, sorted(gaps))
    assert by_class["crypto"] == {8.0}


def test_coinbase_fairx_funding_history_still_needs_keys() -> None:
    with pytest.raises(http.VenueHTTPError) as err:
        http.get_json("coinbase", "https://api.exchange.fairx.net/rest/funding-rate", {"symbol": "BIPZ30"})
    assert err.value.status == 401


@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_geo_blocked_offshore_venues_fail_cleanly(venue: str) -> None:
    try:
        assert ADAPTERS[venue].instruments()
    except VenueError as exc:  # expected from US networks: 451 / 403
        assert venue in str(exc)


def test_kalshi_funding_formula_reproduces_the_last_settlements() -> None:
    """Kalshi does not publish its formula; this re-derives the last settled BTC windows from the
    per-second premium (linear time weights, deadband, cap) and requires an exact match."""
    from perps_worker import derived
    from perps_worker.venues.kalshi import KalshiAdapter

    kalshi = ADAPTERS["kalshi"]
    assert isinstance(kalshi, KalshiAdapter)
    inst = kalshi.instrument("KXBTCPERP")
    now = replay.now()
    settled = sorted(
        (p for p in kalshi.funding([inst], now - timedelta(days=1), now) if p.is_final),
        key=lambda p: p.funding_time,
    )[-2:]
    assert settled, "no settled KXBTCPERP funding in the last day"
    interval = int((inst.funding_interval_hours or 8) * 3600)
    for point in settled:
        start = point.funding_time - timedelta(seconds=interval)
        premiums = kalshi.premium_seconds(inst, start, point.funding_time)
        nc = derived.kalshi_funding_nowcast(
            premiums,
            interval,
            interval,
            handling=inst.extra["funding_invalid_premium_handling"],
            assumed=None,
            deadband=float(inst.extra["funding_deadband"]),
            cap=inst.funding_cap,
        )
        assert nc.projected_rate == pytest.approx(point.rate, abs=1e-12), point.funding_time


def test_og_settlements_fall_on_the_8h_funding_times() -> None:
    """OG.com stamps funding hourly; the 00/08/16 UTC points must exist and the next rate must resolve."""
    og = ADAPTERS["og"]
    inst = og.instrument("BTC-USD-PERP")
    end = datetime.now(UTC)
    pts = og.funding([inst], end - timedelta(days=2), end)
    assert len(pts) >= 5 and all(p.funding_time.hour % 8 == 0 for p in pts)
    nxt = og.funding_estimates([inst])
    assert nxt and not nxt[0].is_final and nxt[0].funding_time > end


def test_hyperliquid_hip3_markets_load_and_fund() -> None:
    h = ADAPTERS["hyperliquid"]
    hip3 = [i for i in h.instruments() if i.extra.get("dex")]
    assert len(hip3) > 50 and all(":" in i.symbol and ":" not in i.underlying for i in hip3)
    nvda = [i for i in hip3 if i.symbol == "xyz:NVDA"]
    assert nvda and h.funding(nvda, datetime.now(UTC) - timedelta(days=1), datetime.now(UTC))
