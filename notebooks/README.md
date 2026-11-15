# Perpetual Futures Across Venues — SQL notebooks

[Worker repository](https://github.com/Query-farm/vgi-perps) · [Shared notebook standard](https://github.com/Query-farm/vgi-example-notebooks/blob/main/docs/standard.md)

Read the executed notebooks directly on GitHub or use each **Open in Colab** button. Their original analytical narratives and SQL remain: venue coverage before funding comparisons, normalized quantity before fill costs, and accrued weight before a funding nowcast. Saved SVGs retain units and recorded/synthetic provenance; no account positions or orders are read or written.

| Notebook | Question | Focused experiment |
|---|---|---|
| [01 · Funding across venues](01_funding_across_venues.ipynb) | Which contracts have comparable settled funding, and what do marks and source coverage add? | Restrict the three-day window to 12 November. |
| [02 · Liquidity and market activity](02_liquidity_and_market_activity.ipynb) | How does a hypothetical order fit the recorded book, beside candles, trades and assumed liquidation bands? | Reduce `ORDER_USD` from 100,000 to 50,000. |
| [03 · Kalshi funding nowcast](03_kalshi_funding_nowcast.ipynb) | How much funding weight is undecided, and where do the deadband and cap change the answer? | Reduce assumed remaining premium from 2 to 1 basis point. |

## Setup and execution

Each notebook installs only the base shared package, pinned to **v0.3.47**, checks imported dependencies, and retrieves the immutable `perps` companion set with SHA256 checks. Colab needs no manual clone. The companion is the domain-only helper plus the original 93 JSON fixtures; the common theme, runner, branding and SQL isolation live in the shared package. Initial setup needs network access, Git and uv. The helper cannot silently download a missing venue recording.

For local publication, download `notebooks/` and run from that directory:

```sh
uv sync --locked --extra runner
uv run --locked --extra runner vgi-notebooks
```

Use the environment's Python kernel for interactive Run All. The base package supports Python 3.11–3.14; the independently launched worker requires Python 3.13 or newer, which uv can provision. The notebook lock does not alter the worker environment or root lockfile. The visible release launch is:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'perps' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-perps@v0.1.5 vgi-perps-worker');
```

This release publishes Python packages rather than native `github-auto` archives. No worker checkout, build or transitive dependency overrides are required. `connect(globals())` creates empty temporary SQL-secret storage, disables persistent secrets, establishes UTC on the connection and prevents JupySQL from loading a saved default connection. The notebooks verify the empty inventory and close the session; the helper restores its prior fixture/clock environment on cleanup.

## Data provenance

The saved examples use the bundled `fixtures/venues/`, with `PERPS_NOW` fixed at
**2026-11-13T01:20:00Z**. The worker reads those responses instead of reaching venue
APIs; Haybarn still calls the real worker through VGI and executes its adapters,
normalization, pagination and analytics. Initial dependency and VGI extension
installation requires network access. Replaying venue data does not.

- Most responses are live recordings described in the [worker README](https://github.com/Query-farm/vgi-perps#testing),
  trimmed to selected contracts and request windows. Counts are fixture coverage,
  not exchange-wide totals. Requests outside that coverage can fail.
- Binance and Bybit error responses are recorded too. Cross-venue functions may
  emit warnings and omit failed venues. Use `venues()` and `capabilities()` to
  understand missing results; do not fill missing venues with zero rates.
- The full-window Kalshi nowcast uses **synthetic premium fixtures**, clearly
  identified in notebook 03. The separate one-minute premium plot uses recorded
  data. The synthetic calculation is not validated against the separately
  recorded published estimate.
- Order books requested at different depths, marks, candles and trades were
  recorded independently. They do not represent one synchronized snapshot.
- Annualized funding is a simple unit conversion. Liquidation bands use an
  assumed leverage distribution; they are not observed positions or forecasts.

The setup explicitly disables recording and the record-missing fallback. A missing
fixture cannot silently turn into a live venue request.

## Parameters and live data

Edit the visible parameters cell and **Restart Kernel → Run All**. All defaults
save a replay run. Change `DATA_MODE = "live"` to use public APIs: no setup-code or
SQL edits are needed. Source badges, date defaults, numeric prose, plot annotations
and checks follow the selected mode. There is no automatic replay fallback.

| Notebook | Parameters | Live defaults when dates are `None` |
|---|---|---|
| Funding | `ASSET`, `START_DATE`, `END_DATE` | Previous three complete UTC days; BTC by default. |
| Liquidity | `ORDER_USD`, `ORDER_MULTIPLIERS`, candle `START_DATE` / `END_DATE`, `TRADE_START` / `TRADE_END`, `TRADE_BUCKET_SECONDS` | Previous six complete hours of candles and the most recent complete minute of trades; BTC contracts discovered from the worker. |
| Nowcast | `SYMBOL`, raw-premium `START_DATE` / `END_DATE`, `ASSUMED_PREMIUM_BPS`, `COMPARISON_PREMIUM_BPS` | Most recent complete minute for raw premiums; the current settlement window for the nowcast. |

Dates accept ISO dates/timestamps, default to UTC, and use **inclusive start,
exclusive end**. Live windows are resolved once from the run's UTC start time.
The worker clock is pinned to that instant for consistent nowcast scenarios;
public API observations can still change between calls. Restart to refresh it.
Premium requests may span at most one hour.

Replay supports BTC (`KXBTCPERP` for the nowcast) and analysis subwindows inside:

| Data | Recorded analysis interval, UTC |
|---|---|
| Funding | 2026-11-10 00:00 → 2026-11-13 00:00 |
| Candles | 2026-11-12 18:00 → 2026-11-13 00:00 |
| Trades and raw premiums | 2026-11-13 01:00 → 2026-11-13 01:01 |

Replay fetches a known recorded request and filters to the selected subwindow in
SQL. Unsupported replay dates/assets fail explicitly. Changing raw-premium dates
in notebook 03 does not change the full-window synthetic nowcast inputs.

Run each experiment into its own directory; the runner preserves the last successful default and rejects unknown parameters:

```sh
uv run --locked --extra runner vgi-notebooks 01_funding_across_venues.ipynb --parameters '{"START_DATE":"2026-11-12"}' --output-dir validation/last-day
uv run --locked --extra runner vgi-notebooks 02_liquidity_and_market_activity.ipynb --parameters '{"ORDER_USD":50000}' --output-dir validation/smaller-order
uv run --locked --extra runner vgi-notebooks 03_kalshi_funding_nowcast.ipynb --parameters '{"ASSUMED_PREMIUM_BPS":1.0}' --output-dir validation/one-basis-point
```

To request public live data explicitly, use `--parameters '{"DATA_MODE":"live"}' --output-dir validation/live` for a chosen notebook. There is no replay fallback. Live availability depends on venue access and supported contracts. Funding reports returned venue health; liquidity requires Kalshi, Coinbase and Hyperliquid BTC contracts. A required failure stops the run. Empty optional trade/premium results remain explicit; missing USD turnover stays NULL. The local nowcast curve applies only to the returned `dampen` rule; other rules retain worker scenarios without that curve or its check.

## SQL and coverage evidence

API retrieval is materialized once in visible `CREATE OR REPLACE TABLE ... AS SELECT ...` cells. Captured displays select from these local tables, avoiding JupySQL's repeated remote SELECT during result discovery. Ordinary in-memory tables deliberately survive its separate cursors. Financial transformations, comparison populations and checks remain visible in SQL. Python plots or interprets returned rows.

[coverage.json](coverage.json) and [coverage/](coverage/) record complete native function/table/view exports, source comparisons, successful cell receipts and reviewed findings. Release v0.1.5 and the captured public main both expose **17 logical functions**. These three stories meaningfully cover **12/17 (70.6%)**, exceeding the nine-name requirement. All 12 execute the exact release. Current-interface credit is reconciled from matching signatures and source evidence, not a claim that the notebooks ran current main. The five explicit gaps are `premium`, `funding_spread`, `raw_coinbase_products`, `raw_bitnomial_products`, and `raw_kalshi_markets`; the SQL pairwise spread in notebook 01 does not claim a call to `funding_spread`.

Validation includes all three defaults and separate experiments, one fresh Python 3.12 notebook-only bootstrap with already imported libraries, output/presentation checks, runner preservation guards and chart review. Hosted Colab execution is not claimed. This migration reuses successful source CI for unchanged worker code; it does not rebuild the worker or repeat transport suites. Replay analytics are independently checked. A separate anonymous live Kalshi BTC instrument lookup succeeded on 15 November 2026; this does not establish complete live coverage of all three stories.

The original [fixture provenance](https://github.com/Query-farm/vgi-perps#testing), [venue mappings](https://github.com/Query-farm/vgi-perps/blob/main/docs/VENUES.md), official local branding assets and historical references remain. [STANDARD.md](STANDARD.md) now points to the maintained central standard. No duplicated runner or theme is required.
