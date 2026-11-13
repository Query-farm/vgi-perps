"""Trim recorded venue responses down to a small, representative fixture set.

Recording (see docs/TESTING.md) writes whole venue payloads: 126 Kalshi markets,
100 Coinbase futures, 337 Bitnomial products, ~180 Hyperliquid markets. This
keeps a handful of contracts per venue, chosen to cover every normalization
path (per-contract quoting, 1000x units, ticks, inactive markets, dated futures
that must be filtered out), and rewrites each file under a readable name.

    uv run python scripts/trim_fixtures.py tests/fixtures/venues
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

KALSHI = {
    "KXBTCPERP",
    "KXETHPERP",
    "KXSOLPERP",
    "KXKSHIBPERP",
    "KXXRPPERP",
    "KXAAPLPERP",
    "KXTSLAPERP",
    "KXNVDAPERP",
    "KXGOLDPERP",
    "KXSILVERPERP",
    "KXUS500PERP",
    "KXWTIFUTURE",
    "KXUS2000PERP",
    "KXBRENTFUTURE",
}
COINBASE = {
    "BIP-20DEC30-CDE",
    "ETP-20DEC30-CDE",
    "SLP-20DEC30-CDE",
    "SHP-20DEC30-CDE",
    "PEP-20DEC30-CDE",
    "US5-19DEC30-CDE",
    "PAU-20DEC30-CDE",
    "XPP-20DEC30-CDE",
    "BIT-27NOV26-CDE",
    "ET-27NOV26-CDE",
}
BITNOMIAL_PERPS = {"PBTCUCZ50", "PBTCURZ50", "PETHUIZ50", "PETHULZ50", "PSOLUSZ50", "PSHBUKZ50"}
HYPERLIQUID = {"BTC", "ETH", "SOL", "kSHIB", "kPEPE", "XRP", "STX"}
#: HIP-3 builder-dex markets: gold, one ticker on two dexes, a name that collides with a main-dex coin,
#: and an inactive market that must be filtered out.
HYPERLIQUID |= {"xyz:GOLD", "xyz:NVDA", "io:NVDA", "para:STX", "km:US500"}


def slug(key: str) -> str:
    """Readable file name: host-less path plus a short hash of the full key."""
    head = key.split("?")[0].split(" ", 1)[-1]
    head = re.sub(r"^https?://[^/]+", "", head)
    head = re.sub(r"[^A-Za-z0-9]+", "_", head).strip("_")[:70]
    return f"{head}_{hashlib.sha1(key.encode()).hexdigest()[:10]}.json"


def trim(key: str, body: Any, bitnomial_ids: set[str]) -> Any:
    """Apply the keep-lists to one recorded body."""
    if not isinstance(body, dict | list):
        return body
    return _trim(key, body, bitnomial_ids)


def _trim(key: str, body: Any, bitnomial_ids: set[str]) -> Any:
    """Apply the keep-list for the endpoint ``key`` names."""
    if "/margin/markets?" in key or key.endswith("/margin/markets"):
        body["markets"] = [m for m in body["markets"] if m["ticker"] in KALSHI]
    elif "/margin/funding_rates/historical" in key and "ticker=" not in key:
        body["funding_rates"] = [r for r in body["funding_rates"] if r["market_ticker"] in KALSHI]
    elif "/brokerage/market/products?" in key:
        body["products"] = [p for p in body["products"] if p["product_id"] in COINBASE]
        body["num_products"] = len(body["products"])
    elif "/prod/product/specs/" in key:
        perps = [s for s in body if s.get("symbol") in BITNOMIAL_PERPS]
        other = [s for s in body if s.get("type") == "future"][:1] + [s for s in body if s.get("type") == "spot"][:1]
        body = perps + other
    elif "/prod/product/data/" in key:
        body = [d for d in body if str(d.get("product_id")) in bitnomial_ids]
    elif "/funding-rates/" in key and isinstance(body, dict) and "data" in body and "product_id=" not in key:
        body["data"] = [r for r in body["data"] if str(r.get("product_id")) in bitnomial_ids]
    elif key.startswith("CCXT hyperliquid.fetch_markets"):
        body = [m for m in body if (m.get("info") or {}).get("name") in HYPERLIQUID]
    return body


def main(directory: Path) -> None:
    """Trim every recording in ``directory`` in place."""
    docs = []
    for path in sorted(directory.glob("*.json")):
        docs.append((path, json.loads(path.read_text())))
    bitnomial_ids: set[str] = set()
    for _, doc in docs:
        if "/prod/product/specs/" in doc["key"] and isinstance(doc["body"], list):
            bitnomial_ids |= {str(s["product_id"]) for s in doc["body"] if s.get("symbol") in BITNOMIAL_PERPS}
    for path, doc in docs:
        doc["body"] = trim(doc["key"], doc["body"], bitnomial_ids)
        path.unlink()
        (directory / slug(doc["key"])).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
