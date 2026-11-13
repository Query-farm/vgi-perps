# Recorder / archive: design note (not built)

The worker answers queries on demand. Several things quants need cannot be rebuilt
later from public endpoints, so a recorder would capture them as they happen. This is a
design note only; nothing here is implemented.

## Gate: terms of use first

Read the ToS table in [VENUES.md](VENUES.md#market-data-terms-of-use-gate-for-any-recorder--archive)
before building anything. In short:

- **Kalshi** Developer Agreement §3/§3.1: API use is limited to "facilitating a members
  own trading"; "Collecting, caching, aggregating, or storing data ... except for purposes
  of facilitating your own trading" is prohibited, and sharing needs "prior written
  authorization from Kalshi". An archive for anything beyond one member's own trading
  requires a written licence (Kalshi also runs a Data Feed Program).
- **Bitnomial** Data Terms prohibit "archiving" and "end-user market data services";
  internal research use is permitted.
- **Coinbase** reserves redistribution to authorized partners.

So: a private, internal research archive is the most that is plausibly in bounds for
Bitnomial and Coinbase; Kalshi needs written permission for any archive at all. Get the
licences, then build. Keep per-venue switches so a venue can be excluded from capture,
retention or export independently.

## What to snapshot

Ordered by how irrecoverable the data is.

| Dataset | Why it must be recorded | Source | Cadence |
|---|---|---|---|
| Funding estimates (in-progress) | Only the live value is served; history keeps settled rates | `funding(include_estimates := true)` | every 5 min |
| Coinbase hourly funding | Public endpoint shows only the latest rate (history needs FairX keys) | `marks(venue := 'coinbase')` | every hour, :02 past |
| Marks, index, premium, OI, bid/ask | Snapshot-only on every venue (Bitnomial mark is per-interval) | `marks()` | every 1 min |
| Kalshi premium index | Per-second series with limited retention | `raw_kalshi_premium_index` | hourly, previous hour |
| Order books | Snapshot-only | `order_book(depth := 100)` | every 1 min for BTC/ETH/SOL, 5 min others |
| Kalshi leverage estimates, risk parameters | Change with calibration; no history | `raw_kalshi_markets()` + `/margin/risk_parameters` | daily |
| Instruments | Contract specs change (splits bump `market_version`, ticks change) | `instruments(include_inactive := true)` + raw tables | daily, and on change |
| Settled funding, trades, candles | Recoverable from history, but cheap insurance | `funding`, `trades`, `candles` | daily backfill of the previous UTC day |

Budget check: Bitnomial allows 200 requests per 5 minutes; the plan above stays under
~30 per 5 minutes for it. Kalshi estimates are one request per market, so batch them
inside the IP's read bucket and back off on 429.

## Storage layout: Parquet on R2, catalogued by DuckLake

- **Object store:** Cloudflare R2 bucket `perps-archive`, one prefix per dataset,
  Hive-partitioned by venue and UTC date:
  `s3://perps-archive/<dataset>/venue=<venue>/date=YYYY-MM-DD/part-<HHMM>-<uuid>.parquet`.
- **Files:** Parquet, zstd, row groups ≈ 128 MB; the worker's normalized schema plus
  `captured_at TIMESTAMPTZ`, `worker_version`, `adapter_version` and a `raw_json` column
  holding the venue payload, so a later normalization fix can be replayed.
- **Catalog:** DuckLake (metadata in Postgres or a DuckDB file on R2), one table per
  dataset, partitioned by `venue, date`. Snapshots give time travel and audit of
  corrections; schema evolution handles new venue fields.
- **Compaction:** minute files compacted to daily files at 01:00 UTC; DuckLake
  `merge_adjacent_files` plus expiry of superseded snapshots after 30 days.
- **Dedup keys:** funding `(venue, symbol, funding_time, is_final)`; trades
  `(venue, symbol, trade_id)`; snapshots `(venue, symbol, captured_at)`.

## Running it

A scheduled job (Cloudflare Worker cron or a small container) runs
`INSERT INTO lake.<dataset> SELECT ..., now() AS captured_at FROM perps.main.<fn>(...)`
through DuckDB with this worker attached over HTTP. Failures are per venue (the worker
skips failing venues with a warning), so the job also writes `venues()` each run as a
health log; gaps are then explainable rather than silent.
