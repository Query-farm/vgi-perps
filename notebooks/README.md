# vgi-perps notebooks

<a href="https://query.farm/"><img src="assets/query-farm-strata-sun.svg" alt="Query.Farm — Strata Sun" width="252"></a>

<a href="https://query.farm/vgi/"><img src="assets/vgi-logo.png" alt="VGI — Vector Gateway Interface" width="280"></a>

[Query.Farm](https://query.farm/) · [VGI](https://query.farm/vgi/) · [Haybarn](https://query.farm/products/haybarn/)

These independent Python notebooks use Haybarn, the real VGI extension and the
released `vgi-perps` worker. Queries and calculations use JupySQL `%%sql` cells;
Python handles connection setup and Seaborn/Matplotlib visualizations. Executed outputs
are saved in the notebooks, including embedded PNG charts.

Each notebook follows an analytical question through the evidence, with
interpretation after the tables and charts. A visible parameters cell controls each
run; numerical explanations are generated from the returned results. Python imports, chart styling, cleanup
and plotting sources carry Jupyter collapse metadata. The VGI connection cells
(`INSTALL`, `LOAD`, `ATTACH`), their diagnostic output, analytical SQL and chart
outputs remain visible.
The opening setup guide has moved out of the reading path into this README.
Jupyter-compatible readers can use the saved collapse metadata. VS Code may keep
folding separately as editor state; if a fresh window expands everything, use
**Collapse Cell Input** on the Python setup and plotting cells. Keep VGI connection
SQL and chart outputs expanded.

| Notebook | What it demonstrates |
|---|---|
| [01 · Funding across venues](01_funding_across_venues.ipynb) | Venue health, capability matrix, contract units, funding history, pairwise funding spreads, marks and open interest. |
| [02 · Liquidity and market activity](02_liquidity_and_market_activity.ipynb) | Normalized order books, fill costs, candles, public trades, and an explicitly assumed liquidation scenario. |
| [03 · Kalshi funding nowcast](03_kalshi_funding_nowcast.ipynb) | Raw premiums, accrued versus remaining weight, worker scenarios, deadband and cap sensitivity. |

## Run in VS Code

Prerequisites: [uv](https://docs.astral.sh/uv/), Python 3.13 or 3.14, and the VS Code
Python and Jupyter extensions. `uv` can provision Python when needed.

From the downloaded **notebooks directory** (the worker source checkout is not required):

```sh
uv sync --locked
code 01_funding_across_venues.ipynb
```

Choose **Select Kernel → Python Environments → .venv/bin/python**.
If it is not listed, use the interpreter path picker. Then **Run All**. The collapsed Python setup
finds the notebook companions whether the kernel starts in the repository root or in `notebooks/`.
Each notebook has its own connection and can run without running another notebook.

The visible connection cell launches the published `v0.1.5` Git release directly:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'perps' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-perps@v0.1.5 vgi-perps-worker');
```

`uvx` downloads and caches the released Python worker; `uv`, Git and a supported Python are prerequisites. No root `.venv`, source build or worker-path parameter is needed. This worker publishes Python packages rather than the native platform archives required by `github-auto://`. The release tag keeps the saved walkthrough tied to a tested worker; upgrade the tag and rerun all cells when adopting another release. `DATA_MODE` only configures inherited fixture/clock environment variables, before the visible ATTACH. It never changes the launcher or silently changes data modes.

The notebook environment has its own `pyproject.toml` and `uv.lock`. Its Haybarn
version (`2.0.0rc11`) matches the repository's SQL test runner. This does not change
the worker's runtime, SDK constraints or root lockfile. The native Haybarn connection
is passed directly to JupySQL; no `duckdb-engine` connection is used.

## Execute and save all outputs

From the notebooks directory:

```sh
uv sync --locked
uv run --locked python execute.py
```

To execute just one:

```sh
uv run --locked python execute.py 03_kalshi_funding_nowcast.ipynb
```

The runner creates a temporary kernel specification pointing to the notebook
environment, uses a fresh kernel per notebook, fails on cell errors, and saves
each notebook only after it completes. It does not install a global kernel.
Notebook assertions check unit conversions, pagination identifiers, fill bounds and
agreement between the applicable nowcast formula and worker outputs. They do not
require a fixed ranking or trade direction. Worker checks remain `make check`.

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

Run live copies without replacing the default outputs:

```sh
uv run --locked python execute.py --mode live --output-dir /tmp/perps-live --timeout 600
```

Or execute an experiment in a separate output directory:

```sh
uv run --locked python execute.py 02_liquidity_and_market_activity.ipynb --parameters '{"ORDER_USD": 50000}' --output-dir /tmp/perps-experiment
```

The runner rejects unknown parameter names and requires `--output-dir` for
parameter overrides. Copies execute with the notebook directory as the kernel's working directory. The runner copies branding, recorded fixtures and the helper beside each experiment, with a link to this guide. Reuse the notebook environment to rerun them; no worker checkout is needed.

Live availability depends on venue access and supported contracts. The funding
notebook shows venue health and whatever supported cross-venue data is returned.
The liquidity walkthrough requires Kalshi, Coinbase and Hyperliquid BTC contracts;
a required API failure stops the run with its diagnostic. Empty optional trade or
premium plots are labelled explicitly. Missing USD turnover is never filled with
zero. The nowcast's local curve applies only to the returned `dampen` rule; other
rules retain the worker scenarios but omit that curve and its check.

## Editing and interpretation

`%%sql result <<` stores the query result in a pandas DataFrame named `result`.
Plain `%%sql` displays the final query result. `CREATE OR REPLACE TABLE`
materializes worker data in the in-memory database, so plotting and regrouping do
not repeat remote requests. Ordinary tables are intentional: JupySQL uses separate
Haybarn cursors, and cursor-local temporary tables would disappear between statements.
The closing cell releases the connection; restart and run from the top to reset.

The shared `notebook_support.py` centralizes presentation, parameter validation and
explicit data-mode setup. Domain queries stay in the notebooks. The
[reusable notebook standard](STANDARD.md) documents the structure, visibility,
provenance, charts and validation to use for subsequent VGI workers.

These examples need only `vgi-perps`. Complementary workers can be added when they
answer a specific question; this first set keeps setup limited to Python and Haybarn.

The Strata Sun + Query.Farm wordmark in `assets/query-farm-strata-sun.svg` is copied
unchanged from `query-farm-astro/public/media-kit/logo/wordmark-adaptive.svg`. It
appears at the top and bottom of every notebook, links to Query.Farm, and adapts
to light/dark color schemes. The VGI product logo remains near the top.

The logo in `assets/vgi-logo.png` is copied unchanged from the Query.Farm website's
`public/media-kit/products/vgi/vgi-logo.png` in `query-farm-astro`.
It links to the [VGI product](https://query.farm/vgi/) in every notebook header.

References: [Haybarn installation](https://query.farm/products/haybarn/install/),
[JupySQL connections](https://jupysql.ploomber.io/en/latest/connecting.html),
[worker SQL reference](https://github.com/Query-farm/vgi-perps#sql-surface), and
[venue mappings and sources](https://github.com/Query-farm/vgi-perps/blob/main/docs/VENUES.md).

---

<a href="https://query.farm/"><img src="assets/query-farm-strata-sun.svg" alt="Query.Farm — Strata Sun" width="210"></a>
