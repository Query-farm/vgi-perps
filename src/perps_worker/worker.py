"""Assemble the perps catalog and run the worker.

Run it:

    vgi-perps-worker                      # stdio: what DuckDB launches by default
    vgi-perps-worker --http --port 8000   # HTTP: for Docker / hosted deploys

Attach it from DuckDB:

    ATTACH 'perps' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-perps-worker');
"""

from __future__ import annotations

import json
from typing import Any

from vgi import Worker
from vgi.catalog import Catalog, Schema

from .functions.analytics import DepthMetrics, FundingSpread, LiquidationEstimate, Premium
from .functions.market import Candles, Funding, Marks, OrderBook, Trades
from .functions.nowcast import KalshiFundingNowcast
from .functions.raw import RawBitnomialProducts, RawCoinbaseProducts, RawKalshiMarkets, RawKalshiPremiumIndex
from .functions.reference import Capabilities, Instruments, Venues
from .meta import (
    CATALOG_DOC_MD,
    CATALOG_NAME,
    CATALOG_TAGS,
    SCHEMA_CATEGORIES,
    SCHEMA_DOC_LLM,
    SCHEMA_DOC_MD,
    SOURCE_URL,
    keywords_array,
)

FUNCTIONS: list[type[Any]] = [
    Venues,
    Capabilities,
    Instruments,
    Funding,
    Marks,
    OrderBook,
    Trades,
    Candles,
    Premium,
    FundingSpread,
    DepthMetrics,
    LiquidationEstimate,
    KalshiFundingNowcast,
    RawKalshiMarkets,
    RawKalshiPremiumIndex,
    RawCoinbaseProducts,
    RawBitnomialProducts,
]


def _stamp_example_queries(functions: list[type[Any]]) -> list[dict[str, str]]:
    """Publish each function's examples as ``vgi.example_queries``; return one per function for the schema."""
    by_name: dict[str, list[dict[str, str]]] = {}
    for fn in functions:
        bucket = by_name.setdefault(fn.Meta.name, [])
        for ex in getattr(fn.Meta, "examples", []):
            entry = {"description": ex.description, "sql": ex.sql}
            if entry not in bucket:
                bucket.append(entry)
    for fn in functions:
        fn.Meta.tags = {**fn.Meta.tags, "vgi.example_queries": json.dumps(by_name[fn.Meta.name])}
    # The schema-level list carries one example per function: agent tools cap a tag at 4,000 chars.
    return [entries[0] for entries in by_name.values() if entries]


_ALL_EXAMPLES = _stamp_example_queries(FUNCTIONS)


class PerpsWorker(Worker):
    """The worker process: one catalog, one ``main`` schema."""

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_DOC_MD,
        source_url=SOURCE_URL,
        tags=CATALOG_TAGS,
        schemas=[
            Schema(
                path=["main"],
                comment=SCHEMA_DOC_MD,
                functions=FUNCTIONS,
                tags={
                    "vgi.title": "Perpetual Futures Functions",
                    "domain": "finance",
                    "vgi.keywords": keywords_array(
                        "perpetual futures, funding rates, basis, open interest, order book, liquidation, carry"
                    ),
                    "vgi.doc_llm": SCHEMA_DOC_LLM,
                    "vgi.doc_md": SCHEMA_DOC_MD,
                    "vgi.categories": json.dumps(SCHEMA_CATEGORIES),
                    "vgi.example_queries": json.dumps(_ALL_EXAMPLES),
                },
            )
        ],
    )


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    PerpsWorker.main()


if __name__ == "__main__":
    main()
