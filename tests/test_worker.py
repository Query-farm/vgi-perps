"""Through the worker process: cross-venue skipping, single-venue errors, empty ranges, cursors."""

from __future__ import annotations

import pytest
from conftest import run_table
from vgi.client import Client


def test_venues_reports_failures_as_rows(client: Client) -> None:
    t = run_table(client, "venues").to_pylist()
    status = {r["venue"]: r["status"] for r in t}
    assert status == {
        "kalshi": "ok",
        "coinbase": "ok",
        "bitnomial": "ok",
        "og": "ok",
        "hyperliquid": "ok",
        "binance": "error",
        "bybit": "error",
    }
    binance = next(r for r in t if r["venue"] == "binance")
    assert "451" in binance["last_error"] and binance["instrument_count"] is None


def test_cross_venue_query_skips_a_failing_venue(client: Client) -> None:
    # Binance and Bybit fail (recorded geo-blocks); the other venues still answer.
    t = run_table(client, "instruments", underlying="BTC")
    assert set(t.column("venue").to_pylist()) == {"kalshi", "coinbase", "bitnomial", "og", "hyperliquid"}


def test_single_venue_query_raises(client: Client) -> None:
    with pytest.raises(Exception, match="451"):
        run_table(client, "instruments", venue="binance")


def test_empty_range_returns_no_rows(client: Client) -> None:
    import datetime as dt

    import pyarrow as pa
    from conftest import Arguments

    start = pa.scalar(dt.datetime(2026, 11, 12, 6, tzinfo=dt.UTC), type=pa.timestamp("us", tz="UTC"))
    end = pa.scalar(dt.datetime(2026, 11, 12, 7, tzinfo=dt.UTC), type=pa.timestamp("us", tz="UTC"))
    args = Arguments(
        positional=(),
        named={"venue": pa.scalar("kalshi"), "underlying": pa.scalar("BTC"), "start_date": start, "end_date": end},
    )
    batches = list(client.table_function(function_name="funding", schema_path=["main"], arguments=args))
    assert sum(b.num_rows for b in batches) == 0


def test_static_capabilities_cover_every_venue(client: Client) -> None:
    t = run_table(client, "capabilities")
    assert t.num_rows == 7 * 10
    rows = {(r["venue"], r["capability"]): r for r in t.to_pylist()}
    assert rows[("coinbase", "funding_history")]["support"] == "partial"
    assert rows[("coinbase", "funding_history")]["auth_required"] is True
    assert rows[("bitnomial", "order_book")]["support"] == "unsupported"
    assert rows[("kalshi", "maintenance_margin")]["support"] == "unsupported"
    assert rows[("hyperliquid", "maintenance_margin")]["support"] == "supported"
    assert rows[("og", "order_book")]["support"] == "supported"
