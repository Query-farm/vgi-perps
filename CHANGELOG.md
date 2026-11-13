# Changelog

## 0.1.0 (2026-11-13)

First version of the perps worker (catalog `perps`).

- Venues: Kalshi perps, Coinbase Derivatives perp-style futures, Bitnomial perpetuals
  (US-regulated) and Hyperliquid, Binance USD-M, Bybit (offshore benchmark via ccxt).
- Functions: `venues`, `capabilities`, `instruments`, `funding`, `marks`, `order_book`,
  `trades`, `candles`, `premium`, `funding_spread`, `depth_metrics`,
  `liquidation_estimate`, `raw_kalshi_markets`, `raw_kalshi_premium_index`,
  `raw_coinbase_products`, `raw_bitnomial_products`.
- One unit and sign convention across venues (price per underlying unit, positive
  funding = longs pay shorts, simple annualization); mappings documented in
  `docs/VENUES.md`.
- Read-only by construction: one `httpx` chokepoint (GET only) and one ccxt chokepoint
  (public `fetch_*` allowlist), with a structural guard test.
- Hermetic tests replaying recorded venue responses; `make live-check` for the real venues.
- Starter template examples (`greet`, `sequence_table`, `fetch_items`, `examples/`)
  removed.

Targets: vgi-python 0.51 · VGI protocol vgi.v3 3.3 · Haybarn 2.0.0rc11 · DuckDB 2.0.0 ·
vgi-lint-check 2.x (unpinned)
