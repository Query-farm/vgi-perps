"""Discovery metadata: catalog and schema tags, plus the per-object tag helpers."""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "perps"

SOURCE_URL = "https://github.com/Query-farm/vgi-perps"

SCHEMA_CATEGORIES = [
    {
        "name": "reference",
        "title": "Venues and contracts",
        "description": "Which venues are reachable, what each supports, and the contract specifications.",
    },
    {
        "name": "market_data",
        "title": "Normalized market data",
        "description": "Funding, marks, order books, trades and bars in one unit and sign convention.",
    },
    {
        "name": "analytics",
        "title": "Cross-venue analytics",
        "description": "Premiums, carry spreads, depth costs and liquidation estimates computed from the data.",
    },
    {
        "name": "venue_raw",
        "title": "Venue-specific fields",
        "description": "Fields a single venue publishes that have no cross-venue equivalent.",
    },
]


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in {c["name"] for c in SCHEMA_CATEGORIES}:
        raise ValueError(f"unknown category {category!r}; add it to SCHEMA_CATEGORIES")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


CONVENTIONS_MD = (
    "**Conventions.** Prices are quote currency per one unit of the underlying (USD per BTC, per "
    "share, per troy ounce), whatever the venue's own quoting. Sizes are contracts, and "
    "`contract_size` is underlying units per contract. Funding rates are decimal fractions per "
    "interval with one sign convention: **positive means longs pay shorts**. Annualized rates are "
    "simple: `rate * 8760 / interval_hours`. Times are UTC `TIMESTAMPTZ`."
)

CATALOG_DOC_LLM = (
    "Normalized, cross-venue market data for US-regulated perpetual futures: Kalshi perps "
    "(crypto, single stocks, indices, metals, energy), Coinbase Derivatives perpetual-style "
    "futures and Bitnomial perpetuals, plus Hyperliquid, Binance USD-M and Bybit as an offshore "
    "benchmark. Every result has a `venue` column. Prices are quote currency per one unit of the "
    "underlying regardless of how the venue quotes; sizes are contracts with `contract_size` "
    "underlying units each; funding rates are decimal fractions per interval where positive means "
    "longs pay shorts, with simple annualization `rate * 8760 / interval_hours`. Join across venues "
    "on `underlying` (not on `symbol`, which is venue-native). Cross-venue queries skip a venue "
    "that fails and log a warning; check venue health and per-venue capability support before "
    "relying on a venue. Offshore venues need an `underlying` filter for per-contract data. "
    "Read-only public market data: no accounts, no orders."
)

CATALOG_DOC_MD = (
    "# Perpetual futures, normalized across venues\n\n"
    "Funding, marks, books, trades and bars for **US-regulated perpetual futures** (Kalshi, "
    "Coinbase Derivatives, Bitnomial) next to an **offshore benchmark** (Hyperliquid, Binance "
    "USD-M, Bybit), in one schema.\n\n"
    f"{CONVENTIONS_MD}\n\n"
    "Each venue sits behind its own adapter; the venue documentation in the repository records "
    "how each venue's units and signs were mapped, with sources. Read-only public market data, "
    "no account or order endpoints. Market data belongs to the venues and is subject to their "
    "terms of use."
)

SCHEMA_DOC_LLM = (
    "The `main` schema holds table functions over live venue APIs. Reference functions say which "
    "venues work right now and list contract specifications; market-data functions return "
    "normalized funding history, live marks, order books, trades and bars; analytics functions "
    "compute premiums, cross-venue funding carry spreads, the cost of filling a notional on each "
    "side of a book, and parameterized liquidation-price estimates; venue_raw functions expose "
    "fields one venue publishes that do not normalize. Named arguments use `name := value`; "
    "venue and symbol arguments of single-venue functions are positional. " + CONVENTIONS_MD
)

SCHEMA_DOC_MD = (
    "## main\n\n"
    "- **Reference**: venue health, the capability matrix, contract specifications.\n"
    "- **Market data**: funding history, live marks, order books, trades and bars.\n"
    "- **Analytics**: premiums, carry spreads, depth costs, liquidation estimates.\n"
    "- **Venue raw**: fields that only one venue publishes.\n\n" + CONVENTIONS_MD
)

AGENT_TEST_TASKS = [
    {
        "name": "us_venue_count",
        "prompt": "Using the perps worker, how many distinct venues in the catalog are US-regulated?",
    },
    {
        "name": "btc_contract_size_kalshi",
        "prompt": "On Kalshi, how much bitcoin does one contract of the BTC perpetual represent?",
    },
    {
        "name": "funding_sign_convention",
        "prompt": "In the perps worker, if Kalshi's BTC perp shows a positive funding rate, which side pays?",
    },
    {
        "name": "kalshi_single_stock_perps",
        "prompt": "Does Kalshi list single-stock perpetual futures? Name one underlying stock if so.",
    },
    {
        "name": "kalshi_funding_forecast",
        "prompt": "Where is the next funding settlement of Kalshi's BTC perpetual heading, annualized, "
        "and how much of that window's weight has already accrued?",
    },
    {
        "name": "btc_funding_history",
        "prompt": "How many funding settlements did Kalshi's BTC perpetual have over the last three days?",
    },
    {
        "name": "eth_marks",
        "prompt": "What is the current mark price of the ETH perpetual on Coinbase Derivatives, in USD per ETH?",
    },
    {
        "name": "kalshi_book_levels",
        "prompt": "Show the five best bid levels of Kalshi's BTC perpetual order book with their USD size.",
    },
    {
        "name": "coinbase_recent_trades",
        "prompt": "How much USD notional traded on Coinbase's nano BTC perp in the last 10 minutes?",
    },
    {
        "name": "kalshi_daily_bars",
        "prompt": "Give the daily closing prices of Kalshi's ETH perpetual for the last week.",
    },
    {
        "name": "btc_premium",
        "prompt": "Which BTC perpetual trades at the highest premium to its own venue index right now?",
    },
    {
        "name": "btc_carry_spread",
        "prompt": "Across venues, which pair of BTC perpetuals had the widest annualized funding spread this week?",
    },
    {
        "name": "kalshi_fill_cost",
        "prompt": "How many basis points would it cost to market-buy $50,000 of Kalshi's BTC perpetual?",
    },
    {
        "name": "hyperliquid_liquidation_bands",
        "prompt": "Estimate where 10x long ETH positions on Hyperliquid would be liquidated.",
    },
    {
        "name": "kalshi_premium_index",
        "prompt": "Average per-second premium index of Kalshi's BTC perp over the last five minutes, in bps?",
    },
    {
        "name": "kalshi_funding_deadband",
        "prompt": "What funding deadband does Kalshi apply to its gold perpetual?",
    },
    {
        "name": "coinbase_overnight_margin",
        "prompt": "What is Coinbase's overnight long margin rate on the nano BTC perp?",
    },
    {
        "name": "bitnomial_margin_rate",
        "prompt": "What product-group margin rate does Bitnomial publish for its centi-bitcoin perpetual?",
    },
    {
        "name": "og_funding_schedule",
        "prompt": (
            "On OG.com, at what UTC times does the BTC perpetual settle funding, and what was the last settled rate?"
        ),
    },
    {
        "name": "hip3_vs_kalshi_stock_funding",
        "prompt": (
            "Over the last week, how did funding on Hyperliquid's builder-dex NVDA perps compare with "
            "Kalshi's NVDA perp?"
        ),
    },
    {
        "name": "liquidation_refusal",
        "prompt": (
            "Can the perps worker estimate liquidation bands for Kalshi's BTC perp with the venue's own "
            "maintenance-margin schedule? Explain what it reports."
        ),
    },
]

CATALOG_TAGS = {
    "vgi.title": "Perpetual Futures Across Venues",
    "vgi.subtitle": "Funding, marks, books and carry for US-regulated perps, benchmarked against offshore venues.",
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/logo/mark-512.png",
    "vgi.license": "MIT",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.keywords": keywords_array(
        "perpetual futures, perps, funding rate, kalshi, coinbase derivatives, bitnomial, og.com, crypto.com, "
        "hyperliquid, "
        "basis, carry, cftc, crypto derivatives, market data"
    ),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
