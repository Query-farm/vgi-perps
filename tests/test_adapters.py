"""Per-venue normalization: units, signs and contract size -> USD, against recorded and synthetic payloads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import Writer

from perps_worker.venues import VenueError
from perps_worker.venues.bitnomial import BitnomialAdapter, instrument_from_spec
from perps_worker.venues.coinbase import (
    CoinbaseAdapter,
    instrument_from_product,
    is_perp_style,
)
from perps_worker.venues.coinbase import (
    base_url as coinbase_url,
)
from perps_worker.venues.kalshi import KalshiAdapter, instrument_from_market, underlying_for
from perps_worker.venues.kalshi import base_url as kalshi_url
from perps_worker.venues.offshore import (
    BinanceAdapter,
    BybitAdapter,
    HyperliquidAdapter,
)
from perps_worker.venues.offshore import (
    instrument_from_market as ccxt_inst,
)
from perps_worker.venues.og import OgAdapter, is_funding_time, next_funding_time
from perps_worker.venues.og import base_url as og_url

T0 = datetime(2026, 11, 10, tzinfo=UTC)
T1 = datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC)

# --------------------------------------------------------------------- Kalshi


@pytest.mark.parametrize(
    ("ticker", "mult", "expected"),
    [
        ("KXBTCPERP", 1.0, ("BTC", "perpetual")),
        ("KXKSHIBPERP", 1000.0, ("SHIB", "perpetual")),
        ("KXGOLDPERP", 1.0, ("XAU", "perpetual")),
        ("KXWTIFUTURE", 1.0, ("WTI", "perp_style_future")),
        ("KXAAPLPERP", 1.0, ("AAPL", "perpetual")),
        ("KXKOPERP", 1.0, ("KO", "perpetual")),  # Coca-Cola: a leading K that is not kilo
    ],
)
def test_kalshi_underlying(ticker: str, mult: float, expected: tuple[str, str]) -> None:
    assert underlying_for(ticker, mult) == expected


def test_kalshi_per_contract_quoting_is_rescaled() -> None:
    market = {
        "ticker": "KXKSHIBPERP",
        "title": "1K kSHIB",
        "asset_class": "Crypto",
        "status": "active",
        "contract_size": "1000.000000",
        "underlying_multiplier": "1000.000000",
        "tick_size": "0.0001",
        "funding_cap": 0.02,
        "funding_limit_unit": "percentage",
    }
    inst = instrument_from_market(market)
    assert inst.contract_size == 1_000_000.0  # SHIB per contract
    assert inst.price(4.3401) == pytest.approx(4.3401e-6)  # USD per SHIB
    assert inst.tick_size == pytest.approx(1e-10)
    assert inst.funding_interval_hours == 8.0 and inst.funding_cap == 0.02
    # A "dollar" funding cap is USD per contract, not a rate, so it is not reported as one.
    assert instrument_from_market({**market, "funding_limit_unit": "dollar"}).funding_cap is None
    equity = instrument_from_market(
        {**market, "ticker": "KXAAPLPERP", "asset_class": "Equities", "underlying_multiplier": "1"}
    )
    assert equity.funding_interval_hours == 24.0 and equity.asset_class == "equity"


def test_kalshi_funding_keeps_native_sign_and_rescales_mark() -> None:
    k = KalshiAdapter()
    btc = k.instrument("KXBTCPERP")
    points = {p.funding_time: p for p in k.funding([btc], T0, T1)}
    p = points[datetime(2026, 11, 12, 21, tzinfo=UTC)]
    assert p.rate == 0.0001450796460962  # Kalshi: positive = longs pay; no flip
    assert p.mark_price == pytest.approx(71395.0)  # "7.1395" / 0.0001
    assert p.interval_hours == 8.0 and p.is_final


def test_kalshi_funding_negative_rates_survive() -> None:
    k = KalshiAdapter()
    rates = [p.rate for p in k.funding([i for i in k.instruments() if i.status == "active"], T0, T1)]
    assert min(rates) < 0 < max(rates)


def test_kalshi_book_is_sorted_best_first(synthetic: Writer) -> None:
    synthetic(f"{kalshi_url()}/margin/markets", None, {"markets": [{"ticker": "KXBTCPERP", "contract_size": "0.0001"}]})
    # Kalshi returns levels worst-first; the adapter must not trust that.
    synthetic(
        f"{kalshi_url()}/margin/markets/KXBTCPERP/orderbook",
        {"depth": 2},
        {
            "orderbook": {
                "bids": [["7.0001", "1"], ["7.0003", "2"], ["7.0002", "3"]],
                "asks": [["7.0009", "1"], ["7.0005", "2"]],
            }
        },
    )
    k = KalshiAdapter()
    bids, asks = k.order_book(k.instrument("KXBTCPERP"), 2)
    assert [b.price for b in bids] == pytest.approx([70003.0, 70002.0])
    assert [a.price for a in asks] == pytest.approx([70005.0, 70009.0])


def test_kalshi_trades_stop_on_empty_page_even_with_cursor(synthetic: Writer) -> None:
    synthetic(f"{kalshi_url()}/margin/markets", None, {"markets": [{"ticker": "KXBTCPERP", "contract_size": "0.0001"}]})
    base = {"ticker": "KXBTCPERP", "limit": 1000, "min_ts": int(T0.timestamp()), "max_ts": int(T1.timestamp())}
    synthetic(f"{kalshi_url()}/margin/trades", {**base, "cursor": None}, {"cursor": "c2", "trades": []})
    k = KalshiAdapter()
    trades, cursor = k.trades(k.instrument("KXBTCPERP"), T0, T1, "")
    assert trades == [] and cursor is None


# ------------------------------------------------------------------- Coinbase


def _product(pid: str, display: str, size: str, root: str, **details: Any) -> dict[str, Any]:
    return {
        "product_id": pid,
        "display_name": display,
        "price": "0.00435",
        "price_increment": "0.00001",
        "future_product_details": {
            "venue": "cde",
            "contract_display_name": display,
            "contract_size": size,
            "contract_root_unit": root,
            "funding_interval": "3600s",
            "futures_asset_type": "FUTURES_ASSET_TYPE_CRYPTO",
            **details,
        },
    }


def test_coinbase_1000x_products() -> None:
    inst = instrument_from_product(_product("SHP-20DEC30-CDE", "1000SHIB Perp", "10000", "SHIB"))
    assert inst.contract_size == 10_000_000.0  # 10,000 x 1000 SHIB
    assert inst.price(0.00435) == pytest.approx(4.35e-6)
    assert inst.tick_size == pytest.approx(1e-8)
    assert inst.funding_interval_hours == 1.0 and inst.funding_cap is None
    plain = instrument_from_product(_product("BIP-20DEC30-CDE", "BTC Perp", "0.01", "BTC"))
    assert plain.contract_size == 0.01 and plain.price(71030.0) == 71030.0


def test_coinbase_dated_futures_are_not_perps() -> None:
    dated = _product("BIT-27NOV26-CDE", "BTC 27-Nov", "0.01", "BTC", funding_interval="")
    assert not is_perp_style(dated)
    assert is_perp_style(_product("BIP-20DEC30-CDE", "BTC Perp", "0.01", "BTC"))


def test_coinbase_products_pagination_terminates(synthetic: Writer) -> None:
    url = f"{coinbase_url()}/market/products"
    page = [_product(f"P{i}-20DEC30-CDE", "X Perp", "1", "X") for i in range(100)]
    synthetic(url, {"product_type": "FUTURE", "limit": 100, "offset": 0}, {"products": page})
    synthetic(url, {"product_type": "FUTURE", "limit": 100, "offset": 100}, {"products": page[:3]})
    assert len(CoinbaseAdapter().raw_products()) == 103  # stops after the short page


def test_coinbase_trades_invert_maker_side_and_split_seconds(synthetic: Writer) -> None:
    c = CoinbaseAdapter()
    synthetic(
        f"{coinbase_url()}/market/products",
        {"product_type": "FUTURE", "limit": 100, "offset": 0},
        {"products": [_product("BIP-20DEC30-CDE", "BTC Perp", "0.01", "BTC")]},
    )
    inst = c.instrument("BIP-20DEC30-CDE")
    end = datetime(2026, 11, 13, 1, 0, 10, tzinfo=UTC)
    start = datetime(2026, 11, 13, 1, 0, 0, tzinfo=UTC)
    url = f"{coinbase_url()}/market/products/BIP-20DEC30-CDE/ticker"
    # A full page (1000) whose oldest trades sit in second :05 -> drop :05, resume with end = :06.
    full = [
        {
            "trade_id": str(i),
            "price": "70000",
            "size": "1",
            "time": f"2026-11-13T01:00:0{9 if i < 990 else 5}.5Z",
            "side": "BUY",
        }
        for i in range(1000)
    ]
    synthetic(url, {"limit": 1000, "start": int(start.timestamp()), "end": int(end.timestamp()) + 1}, {"trades": full})
    rest = [
        {"trade_id": f"r{i}", "price": "69990", "size": "2", "time": "2026-11-13T01:00:05.1Z", "side": "SELL"}
        for i in range(4)
    ] + [
        {"trade_id": str(i), "price": "70000", "size": "1", "time": "2026-11-13T01:00:05.5Z", "side": "BUY"}
        for i in range(990, 1000)
    ]
    synthetic(
        url, {"limit": 1000, "start": int(start.timestamp()), "end": int(start.timestamp()) + 6}, {"trades": rest}
    )
    first, cursor = c.trades(inst, start, end, "")
    assert len(first) == 990 and cursor == str(int(start.timestamp()) + 6)
    assert {t.taker_side for t in first} == {"sell"}  # maker BUY -> taker sold
    second, cursor2 = c.trades(inst, start, end, cursor)
    assert cursor2 is None and len(second) == 14  # second :05 delivered exactly once
    assert {t.taker_side for t in second} == {"buy", "sell"}
    ids = [t.trade_id for t in first + second]
    assert len(ids) == len(set(ids)) == 1004


# ------------------------------------------------------------------ Bitnomial


def test_bitnomial_ticks_and_interval() -> None:
    spec = {
        "product_id": 5614,
        "symbol": "PBTCUCZ50",
        "type": "perpetual",
        "contract_size": 0.01,
        "contract_size_unit": "BTC",
        "price_increment": 5,
        "quote_asset": "USD",
        "funding_interval_hours": 8,
        "funding_rate_upper_bound": 0.0005,
        "base_symbol": "BTCUC",
        "product_status": "active",
    }
    inst = instrument_from_spec(spec)
    assert inst.price(14450) == 72250.0 and inst.contract_size == 0.01 and inst.funding_cap == 0.0005
    b = BitnomialAdapter()
    pts = sorted(b.funding([b.instrument("PBTCUCZ50")], T0, T1), key=lambda p: p.funding_time)
    assert all(p.interval_hours == 8.0 and p.rate == 0.0001 for p in pts)
    last = pts[-1]
    assert (last.mark_price, last.index_price) == (72250.0, 71630.0)


def test_bitnomial_funding_cursor_terminates(synthetic: Writer) -> None:
    root = "https://bitnomial.com/exchange/api/v1"
    spec = {"product_id": 1, "symbol": "PX", "type": "perpetual", "contract_size_unit": "BTC", "price_increment": 1}
    synthetic(f"{root}/prod/product/specs/", {"active": "true"}, [spec])
    base = {
        "begin_time": "2026-11-10T00:00:00Z",
        "end_time": "2026-11-12T23:59:59Z",
        "order": "asc",
        "limit": 500,
        "product_id": "1",
    }
    row = {
        "product_id": 1,
        "funding_rate": 0.0001,
        "interval_start": "2026-11-11T01:00:00Z",
        "interval_end": "2026-11-11T09:00:00Z",
    }
    # Bitnomial returns a cursor even on its last page; a short page ends the walk.
    synthetic(f"{root}/funding-rates/", {**base, "cursor": None}, {"data": [row], "pagination": {"cursor": "more"}})
    b = BitnomialAdapter()
    assert len(b.funding([b.instrument("PX")], T0, T1)) == 1


def test_bitnomial_funding_cursor_is_decoded_before_reuse(synthetic: Writer) -> None:
    """Bitnomial's cursor arrives percent-encoded; sending it back as-is double-encodes it (HTTP 400)."""
    root = "https://bitnomial.com/exchange/api/v1"
    spec = {"product_id": 1, "symbol": "PX", "type": "perpetual", "contract_size_unit": "BTC", "price_increment": 1}
    synthetic(f"{root}/prod/product/specs/", {"active": "true"}, [spec])
    base = {
        "begin_time": "2026-11-10T00:00:00Z",
        "end_time": "2026-11-12T23:59:59Z",
        "order": "asc",
        "limit": 500,
        "product_id": "1",
    }

    def row(i: int) -> dict[str, object]:
        start = datetime(2026, 11, 10, tzinfo=UTC) + timedelta(minutes=i)
        return {
            "product_id": 1,
            "funding_rate": 0.0001,
            "interval_start": start.isoformat().replace("+00:00", "Z"),
            "interval_end": (start + timedelta(hours=8)).isoformat().replace("+00:00", "Z"),
        }

    full_page = {"data": [row(i) for i in range(500)], "pagination": {"cursor": "AAAAOR4AAO%2BtAA%3D"}}
    synthetic(f"{root}/funding-rates/", {**base, "cursor": None}, full_page)
    # Registered under the DECODED cursor: only a decoded cursor finds page 2.
    last_page = {"data": [row(500)], "pagination": {"cursor": "AAAAOR4AAO%2BtAB%3D"}}
    synthetic(f"{root}/funding-rates/", {**base, "cursor": "AAAAOR4AAO+tAA="}, last_page)
    b = BitnomialAdapter()
    assert len(b.funding([b.instrument("PX")], T0, T1)) == 501


def test_bitnomial_has_no_rest_book() -> None:
    b = BitnomialAdapter()
    with pytest.raises(VenueError, match="WebSocket"):
        b.order_book(b.instrument("PBTCUCZ50"), 5)


# ------------------------------------------------------------------------ OG.com


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 13, 1, 20, tzinfo=UTC), datetime(2026, 11, 13, 8, tzinfo=UTC)),
        (datetime(2026, 11, 13, 8, tzinfo=UTC), datetime(2026, 11, 13, 16, tzinfo=UTC)),
        (datetime(2026, 11, 13, 23, 59, 59, tzinfo=UTC), datetime(2026, 11, 14, tzinfo=UTC)),
    ],
)
def test_og_next_funding_time_is_strictly_after(now: datetime, expected: datetime) -> None:
    assert next_funding_time(now) == expected


def test_og_funding_times() -> None:
    assert is_funding_time(datetime(2026, 11, 13, 16, tzinfo=UTC))
    assert not is_funding_time(datetime(2026, 11, 13, 15, tzinfo=UTC))
    assert not is_funding_time(datetime(2026, 11, 13, 16, 0, 1, tzinfo=UTC))


def test_og_instrument_units() -> None:
    inst = OgAdapter().instrument("btc-usd-perp")
    assert (inst.underlying, inst.contract_size, inst.price_factor, inst.tick_size) == ("BTC", 1.0, 1.0, 0.1)
    assert inst.funding_interval_hours == 8.0
    assert inst.max_leverage == pytest.approx(6.6666)
    assert inst.listed_at == datetime(2026, 9, 4, 15, 49, 32, tzinfo=UTC)
    assert inst.extra["index_symbol"] == "BTC-USD-INDEX@CdnaMargined"


def test_og_settlements_are_the_8h_stamps_and_sign_is_native() -> None:
    og = OgAdapter()
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, 8, tzinfo=UTC)
    pts = sorted(og.funding([og.instrument("SEI-USD-PERP")], start, end), key=lambda p: p.funding_time)
    assert [p.funding_time.hour for p in pts] == [0, 8, 16, 0, 8]
    assert all(p.is_final and p.interval_hours == 8.0 for p in pts)
    assert pts[2].rate == pytest.approx(-0.000205987)  # negative: shorts pay longs


def test_og_mark_rate_settles_at_next_funding_time() -> None:
    """Like every venue, marks().next_funding_time is when the reported funding_rate settles."""
    og = OgAdapter()
    (mark,) = og.marks([og.instrument("BTC-USD-PERP")])
    assert mark.next_funding_time == datetime(2026, 11, 13, 8, tzinfo=UTC)  # PERPS_NOW 01:20Z
    assert mark.funding_time == datetime(2026, 11, 13, tzinfo=UTC)
    assert mark.funding_rate == 0.0001 and mark.funding_rate_is_estimate is False


def test_og_trades_page_boundary_emits_each_trade_once(synthetic: Writer) -> None:
    """A full page drops its oldest millisecond and the next page re-reads it (end_ts is exclusive)."""
    base = og_url()
    row = {"symbol": "X-USD-PERP", "inst_type": "PERPETUAL_SWAP", "base_ccy": "X", "tradable": True}
    synthetic(f"{base}/get-instruments", {"inst_type": "PERPETUAL_SWAP"}, {"result": {"data": [row]}})
    start = datetime(2026, 11, 12, tzinfo=UTC)
    end = start + timedelta(hours=1)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    m = s_ms + 1000

    def trade(i: int, ms: int) -> dict[str, Any]:
        return {"d": str(i), "t": ms, "p": "100", "q": "1", "s": "BUY"}

    # Page 1: ids 0..147 at distinct ms above m, then ids 148 and 149 at m (id 150 at m is on page 2).
    page1 = [trade(i, m + 148 - i) for i in range(148)] + [trade(148, m), trade(149, m)]
    page2 = [trade(148, m), trade(149, m), trade(150, m), trade(151, m - 1)]
    params = {"instrument_name": "X-USD-PERP", "count": 150, "start_ts": s_ms}
    synthetic(f"{base}/get-trades", {**params, "end_ts": e_ms + 1}, {"result": {"data": page1}})
    synthetic(f"{base}/get-trades", {**params, "end_ts": m + 1}, {"result": {"data": page2}})
    og = OgAdapter()
    inst = og.instrument("X-USD-PERP")
    first, cursor = og.trades(inst, start, end, "")
    assert (len(first), cursor) == (148, str(m + 1))
    second, cursor = og.trades(inst, start, end, cursor or "")
    assert cursor is None
    ids = [t.trade_id for t in first + second]
    assert sorted(ids, key=int) == [str(i) for i in range(152)]
    assert {t.taker_side for t in first} == {"buy"}


# ------------------------------------------------- offshore (ccxt unified shapes)


def _ccxt_market(**over: Any) -> dict[str, Any]:
    market = {
        "id": "1000PEPEUSDT",
        "symbol": "1000PEPE/USDT:USDT",
        "base": "1000PEPE",
        "settle": "USDT",
        "quote": "USDT",
        "swap": True,
        "linear": True,
        "active": True,
        "contractSize": 1.0,
        "precision": {"price": 0.0000001},
        "limits": {"leverage": {"max": 50}},
        "info": {},
    }
    market.update(over)
    return market


def test_ccxt_binance_1000x_market() -> None:
    # Synthetic, shaped like ccxt binanceusdm output: Binance is geo-blocked from the recording host.
    inst = ccxt_inst("binance", _ccxt_market())
    assert inst is not None
    assert inst.underlying == "PEPE" and inst.contract_size == 1000.0 and inst.quote_ccy == "USDT"
    assert inst.price(0.004556) == pytest.approx(4.556e-6)
    assert inst.maintenance_margin_rate is None  # Binance brackets are authenticated: not guessed
    assert inst.funding_interval_hours is None  # inferred from history spacing instead


def test_ccxt_bybit_interval_from_instrument_info() -> None:
    inst = ccxt_inst(
        "bybit", _ccxt_market(id="BTCUSDT", symbol="BTC/USDT:USDT", base="BTC", info={"fundingInterval": "240"})
    )
    assert inst is not None and inst.funding_interval_hours == 4.0


def test_ccxt_filters_non_perps() -> None:
    assert ccxt_inst("binance", _ccxt_market(swap=False)) is None
    assert ccxt_inst("binance", _ccxt_market(linear=False)) is None
    assert ccxt_inst("binance", _ccxt_market(active=False)) is None


def test_ccxt_binance_funding_interval_inferred_and_paging(synthetic: Writer) -> None:
    synthetic(
        None,
        None,
        [_ccxt_market(id="BTCUSDT", symbol="BTC/USDT:USDT", base="BTC")],
        ccxt=("binanceusdm", "fetch_markets", (), {}),
    )
    since = int(T0.timestamp() * 1000)
    four_h = 4 * 3600 * 1000
    rows = [{"timestamp": since + i * four_h, "fundingRate": 0.0001 * (-1) ** i} for i in range(6)]
    synthetic(None, None, rows, ccxt=("binanceusdm", "fetch_funding_rate_history", ("BTC/USDT:USDT", since, 500), {}))
    b = BinanceAdapter()
    pts = b.funding([b.instrument("BTCUSDT")], T0, T1)
    assert len(pts) == 6 and {p.interval_hours for p in pts} == {4.0}  # from the spacing
    assert sorted(p.rate for p in pts) == [-0.0001] * 3 + [0.0001] * 3  # ccxt sign kept


def test_hyperliquid_recorded_markets_and_margin() -> None:
    h = HyperliquidAdapter()
    btc = h.instrument("BTC")
    assert btc.max_leverage == 40.0 and btc.maintenance_margin_rate == pytest.approx(0.0125)
    assert btc.funding_interval_hours == 1.0 and btc.quote_ccy == "USDC"
    pepe = h.instrument("kPEPE")
    assert pepe.underlying == "PEPE" and pepe.contract_size == 1000.0


def test_offshore_needs_an_underlying() -> None:
    h = HyperliquidAdapter()
    with pytest.raises(VenueError, match="underlying"):
        h.funding(h.instruments(), T0, T1)


def test_geo_blocked_venue_raises_venue_error() -> None:
    with pytest.raises(VenueError, match="451"):
        BinanceAdapter().instruments()
    with pytest.raises(VenueError, match="403"):
        BybitAdapter().instruments()
