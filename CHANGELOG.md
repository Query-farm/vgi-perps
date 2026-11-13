# Changelog

## 0.1.4 (2026-11-13)

- Fix: `marks(venue := 'og')` reported `next_funding_time` one window late (the rate
  shown settles at the next 00/08/16 UTC, not 8 hours after it). Regression test added.

## 0.1.3 (2026-11-13)

- New venue `og`: OG.com crypto perpetuals (North American Derivatives Exchange, Inc.,
  a Crypto.com affiliate; CFTC DCM + DCO; live since 2026-10-27), through Crypto.com's
  public FCM market-data API (`api.crypto.com/fcm/v1/public`, no key). Instruments,
  funding history (8-hour settlements at 00/08/16 UTC, about 14 days kept), the next
  rate (fixed one window ahead), marks with mark and index, open interest, order book,
  trades and 1m/1h/1d candles. Every cross-venue function picks it up.
- docs/VENUES.md: OG.com section, capability matrix, normalization and terms rows.

## 0.1.2 (2026-11-13)

- New: `kalshi_funding_nowcast(symbol, funding_time, assume_premium, recent_minutes)`.
  Kalshi's funding is the linear time-weighted mean of the per-second premium
  (second i of the window weighs i), zeroed inside the deadband and capped. This was
  reverse-engineered and verified against ten consecutive BTC settlements (to 5e-17) and
  Kalshi's own estimate (docs/VENUES.md). The function returns the accrued rate, the
  share of the window's weight already elapsed, a projection to settlement, and for a past
  window the settled rate beside the reconstruction.
- Kalshi instruments carry `funding_deadband` and `funding_invalid_premium_handling`.
- Live check `test_kalshi_funding_formula_reproduces_the_last_settlements`.

## 0.1.1 (2026-11-13)

- Fix: Bitnomial funding history longer than one page failed with HTTP 400. Bitnomial returns its pagination cursor already percent-encoded and it was encoded a second time; cross-venue queries silently dropped Bitnomial. The cursor is now decoded before reuse (regression test added).

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
