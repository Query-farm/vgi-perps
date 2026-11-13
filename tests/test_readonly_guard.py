"""Structural guard: this worker reads public market data and can never place orders.

* Exactly one module imports ``httpx`` (``http.py``) and exactly one imports
  ``ccxt`` (``offshore_client.py``): two files to audit.
* No HTTP write verb is called anywhere in the package.
* Only allowlisted public ccxt ``fetch_*`` methods can be invoked, and no ccxt
  exchange is constructed with credentials.
* No account, order or portfolio endpoint is referenced, and SQL values that
  reach a URL path are percent-encoded so they cannot escape their segment.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from perps_worker import http, offshore_client
from perps_worker.model import Instrument
from perps_worker.venues.coinbase import CoinbaseAdapter
from perps_worker.venues.kalshi import KalshiAdapter

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "perps_worker"
WRITE_VERBS = {"post", "put", "patch", "delete", "request", "stream", "send"}
#: ccxt account and trading methods: none may appear in the allowlist or the source.
CCXT_PRIVATE = ("create_order", "cancel", "fetch_balance", "fetch_positions", "fetch_my_trades", "withdraw", "transfer")


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in sorted(PACKAGE.rglob("*.py"))]


def test_no_http_write_calls() -> None:
    offenders = []
    for path, text in _sources():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_VERBS:
                offenders.append(f"{path.name}:{node.lineno} .{node.func.attr}()")
    assert offenders == []


def _importers(module: str) -> list[str]:
    users = []
    for path, text in _sources():
        for node in ast.walk(ast.parse(text)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(n == module or n.startswith(f"{module}.") for n in names):
                users.append(path.name)
    return sorted(set(users))


def test_single_http_chokepoint() -> None:
    assert _importers("httpx") == ["http.py"]


def test_single_ccxt_chokepoint() -> None:
    assert _importers("ccxt") == ["offshore_client.py"]


def test_ccxt_allowlist_is_public_market_data_only() -> None:
    assert all(m.startswith("fetch_") for m in offshore_client.ALLOWED_METHODS)
    for private in CCXT_PRIVATE:
        assert not any(private in m for m in offshore_client.ALLOWED_METHODS)
    for _, text in _sources():
        for private in CCXT_PRIVATE:
            assert f'"{private}' not in text and f".{private}(" not in text


def test_ccxt_refuses_unlisted_methods() -> None:
    with pytest.raises(offshore_client.OffshoreError, match="not an allowlisted"):
        offshore_client.call("hyperliquid", "create_order", "BTC/USDC:USDC", "market", "buy", 1)
    with pytest.raises(offshore_client.OffshoreError, match="unknown offshore venue"):
        offshore_client.call("ftx", "fetch_markets")


def test_ccxt_exchanges_carry_no_credentials() -> None:
    source = (PACKAGE / "offshore_client.py").read_text()
    for field in ("apiKey", "secret", "password", "privateKey", "walletAddress"):
        assert field not in source


def test_no_account_or_order_endpoints() -> None:
    forbidden = (
        "/portfolio",
        "/margin/orders",
        "/margin/balance",
        "/margin/positions",
        "/margin/fills",
        "/margin/funding_history",
        "/rest/positions",
        "/brokerage/orders",
        "/brokerage/accounts",
    )
    for path, text in _sources():
        for fragment in forbidden:
            assert f'"{fragment}' not in text and f"'{fragment}" not in text, f"{path.name} references {fragment}"


@pytest.mark.parametrize("value", ["../../portfolio/balance", "X?limit=1", "A/B#frag"])
def test_segment_cannot_escape(value: str) -> None:
    encoded = http.segment(value)
    assert "/" not in encoded and "?" not in encoded and "#" not in encoded


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(venue: str, url: str, params: dict[str, Any] | None = None) -> Any:
        seen.append(url)
        return {}

    monkeypatch.setattr(http, "get_json", fake)
    return seen


def test_symbols_are_encoded_in_url_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    seen = _capture(monkeypatch)
    hostile = "../../portfolio/balance"
    inst = Instrument("kalshi", hostile, "BTC", "crypto", "USD", "perpetual", "active", 1.0, 1.0, "x")
    KalshiAdapter().order_book(inst, 5)
    KalshiAdapter().candles(inst, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 1, tzinfo=UTC), "1h")
    CoinbaseAdapter().order_book(inst, 5)  # query parameter, not path
    CoinbaseAdapter().candles(inst, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 1, tzinfo=UTC), "1h")
    assert seen
    for url in seen:
        assert "/portfolio" not in url, url
    assert any("..%2F..%2Fportfolio%2Fbalance" in url for url in seen)


def test_only_get_is_issued(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live path calls ``client.get`` and nothing else."""
    source = (PACKAGE / "http.py").read_text()
    calls = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.value.__class__ is ast.Name
        and getattr(node.func.value, "id", "") == "client"
    }
    assert calls == {"get"}
