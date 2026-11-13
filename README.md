<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://query.farm/media-kit/products/vgi/vgi-logo.png" alt="A Query.Farm VGI worker for DuckDB." width="320">
  </a>
</p>

# Compare Perpetual Futures Funding, Basis & Depth Across Kalshi, Coinbase & Bitnomial in DuckDB

> **vgi-perps** · a [Query.Farm](https://query.farm) VGI worker · powered by public venue APIs and [ccxt](https://github.com/ccxt/ccxt)

Normalized, cross-venue market data for **US-regulated perpetual futures**, as DuckDB
table functions:

- **Kalshi** perps (CFTC DCM): crypto, **single-stock** (AAPL, TSLA, NVDA, ... live since
  2026-11-05), index, metals and energy perps.
- **Coinbase Derivatives** perpetual-style futures (BIP, ETP, ...).
- **Bitnomial** perpetual futures.
- An **offshore benchmark**: Hyperliquid, Binance USD-M and Bybit, through ccxt.

The cross-venue comparison is the point, so every unit and sign is normalized in code:

- **Prices** are quote currency per **one unit of the underlying** (USD per BTC, per
  share, per troy ounce), however the venue quotes (Kalshi per contract, Bitnomial in
  ticks, Coinbase `1000SHIB` per 1000 coins).
- **Sizes** are contracts, with `contract_size` underlying units each, so
  `contracts * contract_size * price` is notional.
- **Funding rates** are decimal fractions per interval and **positive always means longs
  pay shorts**; `rate_annualized = rate * 8760 / interval_hours`.
- **Times** are `TIMESTAMPTZ` (UTC). Every result has a `venue` column; join venues on
  `underlying`.

How each venue was mapped, with sources and the probes that confirmed it, is in
[docs/VENUES.md](docs/VENUES.md).

**Read-only, public market data.** No account, order or portfolio endpoint is reachable:
one `httpx` chokepoint (`http.py`, GET only) and one ccxt chokepoint
(`offshore_client.py`, an allowlist of public `fetch_*` methods, no credentials), enforced
by `tests/test_readonly_guard.py`. Symbols that reach a URL path are percent-encoded.

## Quick start

```sql
-- DuckDB 2.0
SET allow_extension_repositories = 'allowed';
CREATE EXTENSION REPOSITORY IF NOT EXISTS query_farm WITH PREFIX 'https://duckdb-extensions.query.farm';
INSTALL vgi FROM query_farm;
LOAD vgi FROM query_farm;

ATTACH 'perps' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-perps@v0.1.1 vgi-perps-worker');

-- Which venues are reachable from here (Binance and Bybit geo-block US networks)
SELECT venue, status, instrument_count FROM perps.main.venues() ORDER BY venue;

-- Average annualized BTC funding per venue over the last week
SELECT venue, avg(rate_annualized) AS mean_apr, count(*) AS settlements
FROM perps.main.funding(underlying := 'BTC') GROUP BY venue ORDER BY mean_apr DESC;

-- Cross-venue carry: who pays more, and which way to lean
SELECT venue_a, venue_b, spread_bps, carry_trade
FROM perps.main.funding_spread('BTC') ORDER BY abs(spread_bps) DESC;
```

`uvx` fetches and caches the worker on first use; change `@v0.1.1` to move to a newer release.

Other ways to run it: `vgi-perps-worker --http --port 8000` serves HTTP (attach with
`LOCATION 'http://127.0.0.1:8000'`), and `scripts/fixture-worker` serves the recorded
fixtures offline (no network; "now" pinned to the recording time).

Build from source: `git clone https://github.com/Query-farm/vgi-perps && cd vgi-perps && make build`
(the worker lands at `.venv/bin/vgi-perps-worker`).

## SQL surface

All functions live in `perps.main`. Arguments shown `name := value` are named and
optional; `venue`/`symbol` of single-venue functions are positional.

| Function | What it returns |
|---|---|
| `venues(probe := true)` | One row per venue: type, regulator, adapter version, sign convention, supported capabilities, live health (`status`, `last_error`). |
| `capabilities(venue := NULL)` | The capability matrix: support, auth, history depth, notes per (venue, capability). No network. |
| `instruments(venue, underlying, include_inactive := false)` | Contract specs: `canonical_id`, `underlying`, `asset_class`, `quote_ccy`, `contract_size`, `tick_size`, `funding_interval_hours`, `funding_cap`, margin where published. |
| `funding(venue, underlying, start_date, end_date, include_estimates := false)` | Funding history: `rate_per_interval`, `rate_annualized`, `rate_bps`, `interval_hours`, `mark_price`, `index_price`, `premium_bps`, `is_final`. |
| `marks(venue, underlying)` | Live snapshot: mark (`mark_source`), index, last, bid/ask, `premium_bps`, current funding, `next_funding_time`, open interest in contracts and USD, 24h volume. |
| `order_book(venue, symbol, depth := 50)` | `side`, `level`, `price`, `size_contracts`, `size_base`, `size_usd`, `cum_usd`. |
| `trades(venue, symbol, start_date, end_date, max_trades := 10000)` | Public prints with `taker_side` and `size_usd`, paged with an externalized cursor. |
| `candles(venue, symbol, start_date, end_date, bar := '1h')` | OHLCV (`1m`, `1h`, `1d`), volume in contracts and USD, Kalshi open interest. |
| `premium(venue, underlying, reference := NULL)` | Mark vs the venue index, or vs another venue's price (`reference := 'hyperliquid'`). |
| `funding_spread(underlying, start_date, end_date)` | Mean annualized funding per contract and the spread for every cross-venue pair, with the direction that collects it. |
| `depth_metrics(venue, symbol, notional_usd := 100000, depth := 200)` | Spread, bps cost to fill the notional on each side, depth and imbalance. |
| `liquidation_estimate(venue, symbol, leverages, weights, mm_rate := NULL, entry_price := NULL)` | **Estimate** of liquidation bands from open interest and an assumed leverage mix. Uses only a documented maintenance schedule (Hyperliquid); refuses other venues unless you pass `mm_rate`. Every row carries `estimate_method` and `mm_source`. |
| `raw_kalshi_markets(symbol)`, `raw_kalshi_premium_index(symbol, start_date, end_date)`, `raw_coinbase_products(symbol)`, `raw_bitnomial_products(symbol)` | Venue-specific fields that do not normalize (per-contract marks, deadbands, FCM margin by side, price limits, ...). |

Defaults: `venue := NULL` scans every venue; `underlying := NULL` keeps every underlying
but then skips the offshore venues (hundreds of contracts); `start_date` defaults to 7
days before `end_date` (`now()`); trades default to the last hour.

**Failures.** A cross-venue query skips a venue that fails and logs a warning naming it
(DuckDB prints it); `venues()` shows the failure as a row. A single-venue query raises.

## What is unsupported, and why

| Gap | Reason |
|---|---|
| Coinbase funding history beyond the latest hourly rate | The FairX `/rest/funding-rate` endpoint answers `401 missing CB-ACCESS-KEY`; it needs clearing-member keys. |
| Bitnomial order book and trades; Hyperliquid trades | WebSocket-only (Bitnomial) or a wallet address is required (Hyperliquid via ccxt). |
| Maintenance margin for Kalshi, Coinbase, Bitnomial, Binance, Bybit | Not published as a schedule (Kalshi: authenticated portfolio-VaR inputs; Coinbase: only initial FCM rates; Bitnomial: an unlabelled rate; Binance: authenticated brackets). `liquidation_estimate` refuses rather than guesses. |
| Binance, Bybit from US networks | Geo-blocked (HTTP 451 / 403). Adapters are unit-tested on ccxt-shaped inputs but were not exercised live. |

## Testing

`make check` is the definition of done: ruff, mypy `--strict`, pydoclint, pytest, the SQL
suite over stdio/launch/http, and vgi-lint at `fail_on = info`.

- **Hermetic.** Unit and SQL tests replay recorded venue responses from
  `tests/fixtures/venues` (set `PERPS_FIXTURES_DIR`; `PERPS_NOW` pins "now"). A request
  without a recording fails loudly. The fixtures were recorded from live probes on
  2026-11-13 (`PERPS_RECORD_DIR`) and trimmed with `scripts/trim_fixtures.py`;
  `tests/fixtures/venues/synthetic/` holds one hand-written, labelled pagination case.
- **Goldens.** `tests/test_normalize.py`, `tests/test_derived.py` and
  `tests/test_adapters.py` check every normalization (sign, annualization per interval,
  contract size to USD, ticks, 1000x units) and every derived function against
  hand-computed values. The SQL suite repeats the unit-critical ones as exact (1e-9)
  assertions, because the sqllogictest runner compares numbers with about 1% tolerance.
- **Live.** `make live-check` re-verifies against the real venues that BTC prices agree
  per unit across venues, Kalshi's funding cadence, SHIB scaling, and the FairX auth wall.
- **Stock DuckDB.** `vgi-worker.json` + `scripts/fixture-worker` drive the vgi-worker
  skill's `verify_worker.py` through DuckDB 2.0.0 with VGI from `query_farm`.

## Configuration

| Variable | Purpose |
|---|---|
| `PERPS_FIXTURES_DIR` | Replay recorded responses instead of calling venues (tests). |
| `PERPS_RECORD_DIR` | Also write every live response there (fixture recording). |
| `PERPS_NOW` | Pin "now" (ISO 8601) for default windows. |
| `PERPS_KALSHI_BASE_URL`, `PERPS_COINBASE_BASE_URL`, `PERPS_BITNOMIAL_ROOT_URL` | Override venue base URLs. |

No credentials are read: every endpoint used is public.

## Market data and terms of use

The data belongs to the venues and is subject to their terms; see [NOTICE](NOTICE). The
terms matter for anything that stores or shares data: Kalshi's Developer Agreement limits
API use to "facilitating a members own trading" and prohibits storing or sharing API data
without written authorization; Bitnomial prohibits archiving and redistribution; Coinbase
reserves redistribution to licensed partners. Details and quotes are in
[docs/VENUES.md](docs/VENUES.md#market-data-terms-of-use-gate-for-any-recorder--archive);
the recorder design in [docs/ARCHIVE.md](docs/ARCHIVE.md) is gated on them. Not legal
advice.

## Authorship & License

Written by [Query.Farm](https://query.farm). Released under the [MIT License](LICENSE).

Copyright 2026 Query Farm LLC - https://query.farm

This project is not affiliated with or endorsed by Kalshi, Coinbase, Bitnomial,
Hyperliquid, Binance or Bybit.
