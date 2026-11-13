# Venue research: US-regulated perps and the offshore benchmark

Researched and probed **2026-11-12/13 (UTC)** from a US network with read-only GETs
(Hyperliquid's public data is a `POST /info` read query, sent by ccxt). Everything below
is either quoted from the venue's documentation (linked) or observed in a live response;
observations are marked *probed*. `make live-check` re-verifies the load-bearing facts
(units across venues, Kalshi funding cadence, the FairX auth wall) against today's data.

## Capability matrix

`S` supported, `P` partial, `-` unsupported, `A` needs API credentials. The same matrix
is queryable: `SELECT * FROM perps.main.capabilities()`.

| Capability | Kalshi | Coinbase Derivatives | Bitnomial | OG.com | Hyperliquid | Binance USD-M | Bybit |
|---|---|---|---|---|---|---|---|
| instruments | S | S (Advanced Trade) | S | S | S | S¹ | S¹ |
| funding history | S, since listing | P: latest hourly rate only; full history A (FairX) | S, since 2026-05-16 | S, about 14 days | S | S¹ | S¹ |
| funding estimate | S (1 call/market) | - | S (1 call/product) | S (fixed one window ahead) | S | S¹ | S¹ |
| mark / index | S / S | P: mid (no public mark) / S | P: mark + index as of last funding | S / S (per minute) | S / S | S¹ | S¹ |
| premium index | S (per second, ≤1h windows) | - | - (only at each funding) | - | - | - | - |
| open interest | S (+ hourly in candles) | S | S | S | P | P¹ | P¹ |
| order book | S (depth ≤100, or all) | S | - (WebSocket only) | S (≤50) | S | S¹ | S¹ |
| trades | S (cursor) | S (time-window paging) | - (WebSocket; REST is block trades only) | S (150/page) | - (needs a wallet address) | P¹ | P¹ |
| candles | S 1m/1h/1d | S 1m/1h/1d (350/call) | P: daily, `/web` "non-stable" | S 1m/1h/1d (300/call) | S | S¹ | S¹ |
| maintenance-margin schedule | - A (portfolio VaR model) | - (initial rates only) | - (rate unlabelled) | - (max leverage only) | S (documented rule) | - A | - |

¹ Binance and Bybit are geo-blocked from US networks (*probed*: Binance `HTTP 451
"Service unavailable from a restricted location"`, Bybit `HTTP 403 "The Amazon CloudFront
distribution is configured to block access from your country"`). The adapters are wired
and unit-tested on ccxt-shaped inputs, but were **not** exercised live; `venues()` reports
them as `status = 'error'` and cross-venue queries skip them with a warning.

## Kalshi (perps, `/trade-api/v2/margin`)

- **Regulation:** CFTC DCM (KalshiEX LLC), cleared by Kalshi Klear.
- **Base:** `https://api.elections.kalshi.com/trade-api/v2` (docs list
  `external-api.kalshi.com` as "rolling out member by member"; the `elections` host
  served every public margin endpoint in the probe). Spec: `https://docs.kalshi.com/perps_openapi.yaml`.
- **Public endpoints used:** `GET /margin/markets`, `/margin/markets/{ticker}/orderbook`,
  `/margin/markets/{ticker}/candlesticks`, `/margin/trades`,
  `/margin/funding_rates/historical`, `/margin/funding_rates/estimate`,
  `/margin/funding_rates/premium_index`. All `security: []` in the spec.
- **Excluded (authenticated, account data):** `/margin/funding_history` (your own
  payments), `/margin/funding_schedule`, `/margin/estimate_maintenance_margin/metadata`,
  orders, fills, positions, balance. None is called; `tests/test_readonly_guard.py`
  forbids the paths in source.
- **Funding formula (*verified*, not documented by Kalshi):** the settled rate for a
  window is the **linear time-weighted mean of the per-second premium index**: second
  `i` of the window (1 = the first second after the previous funding time) weighs `i`,
  so `F = sum(i * p_i) / sum(i)` over the 28,800 seconds of an 8-hour window. Under
  `funding_invalid_premium_handling = dampen` (BTC) a second with no premium counts as 0;
  under `exclude` it is left out. The result is then set to exactly 0 when
  `|F| < funding_deadband` (0.0001 for BTC: no settlement has ever fallen between 0 and
  ±1 bp) and clamped to `funding_cap`. Checked 2026-11-13: ten consecutive BTC windows
  (2026-11-10 05:00 to 2026-11-13 05:00 UTC) reproduce to within 5e-17 (floating-point
  rounding), including three that averaged 0.94, 0.33 and 0.42 bp and settled at 0. A simple average misses by 5-20%. Kalshi's
  `/margin/funding_rates/estimate` is the same weighted mean over the seconds elapsed so
  far, matched to 13 significant digits. The help center only says "TWAP" and the
  changelog that the premium-index series "may differ from actual funding calculations";
  the weighting is ours. `kalshi_funding_nowcast()` implements it, and
  `tests/test_live.py::test_kalshi_funding_formula_reproduces_the_last_settlements`
  re-checks it against live settlements. `exclude` markets (single-stock perps) are not
  yet verified.
- **Listings (*probed*):** 126 markets (86 active): 94 Equities, 19 Crypto, 6 Metals,
  4 Energy, 3 Indices. **Single-stock perps are live** (AAPL, TSLA, NVDA, ... 58 with
  funding in the 8 days to 2026-11-12); their first funding is 2026-11-05, consistent with
  a 2026-11-05 launch. BTC funding history starts 2026-06-03T20:00Z.
- **Quoting:** prices are **USD per contract**; `contract_size` is in the market title's
  unit and `underlying_multiplier` is "Underlying units per contract-size unit". Price per
  base unit = `price / (contract_size * underlying_multiplier)`. *Probed*: BTC
  `7.0872 / 0.0001 = 70,872`, gold `3.8903 / 0.001 = 3,890/oz`, WTI `0.8762 / 0.01 = 87.62`,
  kSHIB `4.3401 / (1000 * 1000) = 4.34e-6/SHIB` (Coinbase 1000SHIB index: 4.338e-6). The
  `reference_price` is "Underlying reference price, scaled per contract": same factor.
- **Index perps:** `KXUS500PERP` reference ≈ 13,406 and `KXUSTECH100PERP` ≈ 35,401 per unit
  — Kalshi-specific index levels, not comparable to other venues' index perps (Coinbase
  `CDEUS5` ≈ 3,067). They keep distinct `underlying` names so they never pair.
- **Funding:** decimal fraction per interval. "If the funding rate is positive (perp
  trading above spot): long holders pay short holders"
  ([help.kalshi.com/en/articles/15357613](https://help.kalshi.com/en/articles/15357613-how-funding-works)):
  **no sign flip**. Cap: "clamped to +2% / –2% per 8-hour interval"; `funding_cap = 0.02`
  with `funding_limit_unit = 'percentage'`; |rate| below the deadband (0.01% crypto,
  0.002% others) is zeroed. *Probed* cadence (schedule endpoint needs auth): Crypto at
  05/13/21 UTC (00/08/16 ET) = **8h**; Equities and Indices daily at 21:00 UTC, **including
  weekends**; Metals daily 15:00 UTC; WTI daily 19:30 UTC. Annualization therefore uses
  8,760/8 or 8,760/24. `KXWTIFUTURE` uses `funding_limit_unit = 'dollar'` (cap in USD per
  contract): its cap is not reported as a rate, and its rate semantics are **unverified**
  (all recent WTI rates were 0).
- **History / pagination:** funding history has no cursor; the adapter queries per ticker
  for ≤6 markets and in 7-day windows across all markets otherwise. Trades use an opaque
  cursor (`limit` ≤ 1000); the scan stops on an empty page even if a cursor is returned.
  Candles: 1/60/1440-minute periods, ≤5000 bars per call. Orderbook `depth` > 100 →
  `400 invalid_selector` (*probed*); `depth=0` returns every level. Levels come back
  worst-first (*probed*) and are re-sorted.
- **Rate limits:** token buckets; "Public endpoints charge your IP address's budget";
  429s carry no `Retry-After`; "Apply exponential backoff on 429"
  ([rate limits](https://docs.kalshi.com/getting_started/rate_limits.md)).
- **Margin:** `GET /margin/risk_parameters` (public) gives `initial_margin_multiplier`
  (1.3 = IM / MM) and liquidation margin-ratio thresholds; markets carry
  `leverage_estimates` (1/margin rate) by notional. Maintenance margin itself is a
  portfolio VaR model whose inputs are authenticated, so `liquidation_estimate` refuses
  Kalshi unless you pass `mm_rate`.

## Coinbase Derivatives (perp-style futures)

- **Regulation:** CFTC DCM (Coinbase Derivatives, LLC), cleared by Nodal Clear.
- **FairX REST is member-only:** every path on `https://api.exchange.fairx.net`,
  including the documented `GET /rest/funding-rate?symbol=BIPZ30`, returns
  `401 {"error":"missing request header: CB-ACCESS-KEY"}` (*probed*), although the
  OpenAPI spec marks it `security: []`. The rest of that API is firm/position
  administration. Funding **history** therefore needs clearing-member API keys; signing
  was not implemented because it could not be verified.
- **Public source used instead:** Advanced Trade
  `https://api.coinbase.com/api/v3/brokerage/market/{products,product_book,products/{id}/ticker,products/{id}/candles}`.
  `product_type=FUTURE` lists the 100 US futures (offset-paged); 29 are perp-style
  (`future_product_details.funding_interval = "3600s"`), e.g. `BIP-20DEC30-CDE`. Note
  `contract_expiry_type=PERPETUAL` returns the **INTX** international perps (venue
  `neptune`, not US-regulated, stale) — not used.
- **Quoting:** USD per named unit; `1000SHIB`/`1000PEPE` products quote per 1000 coins
  and `contract_size` is in those 1000-coin units (SHP: 10,000 x 1000 = 10M SHIB per
  contract). Normalized: price / 1000, size x 1000.
- **Funding:** CFTC filing #2025-32, Appendix B: "Funding Payment Calculation: Funding
  Rate * Open Position Quantity * Contract Multiplier * Futures Mark Price * -1", rate =
  `Premium * 0.75 + previous * 0.25`, `Premium = TWAP((Futures Mark - Spot Mark)/Spot Mark/24, 1h, 3min)`,
  calculated hourly, settled at the mid-day and end-of-day margin runs
  ([filing](https://www.cftc.gov/filings/ptc/ptc06262524818.pdf)). A positive rate debits
  a long: **positive = longs pay shorts, no flip**. No cap is specified (only "Fair Value
  Limits" on order prices). `funding_rate`/`funding_time` in the listing are the latest
  hourly rate; *probed* `funding_time` was the top of the current hour.
- **Mark:** the listing has `index_price` ("used for mark-to-market"), `price` (last) and
  `mid_market_price`, no live futures mark; `marks()` uses the mid and says so
  (`mark_source = 'mid'`).
- **Trades:** the public `side` is "The maker side of the trade" (Advanced Trade docs), so
  `taker_side` is its opposite. Paging walks windows backwards by `end`; a page's oldest
  second is re-requested whole so it is emitted exactly once.
- **Margin:** `intraday_margin_rate` / `overnight_margin_rate` per side are FCM initial
  requirements; no maintenance schedule is published → `liquidation_estimate` refuses
  Coinbase unless `mm_rate` is given. Raw rates are in `raw_coinbase_products()`.
- **Rate limits:** not stated on the pages read; responses carry
  `Cache-Control: public, max-age=14400` (*probed*).

## Bitnomial (perpetual futures)

- **Regulation:** CFTC DCM and DCO (Bitnomial Exchange / Bitnomial Clear).
- **Bases:** specs and data under `https://bitnomial.com/exchange/api/v1/prod/`;
  funding under `https://bitnomial.com/exchange/api/v1/funding-rates/` (the `/prod/`
  prefix 404s for funding, *probed*); daily bars under `/exchange/api/web/charts/price/{id}`
  ("non-stable", per the docs).
- **Listings (*probed*):** 34 perpetuals (`type = 'perpetual'`), all crypto, in size tiers
  (e.g. `PBTCUCZ50` 0.01 BTC, `PBTCURZ50` 0.0001 BTC).
- **Quoting:** product-data and funding prices are **ticks**; USD per unit =
  `ticks * price_increment` (*probed*: 14,450 x 5 = 72,250 USD/BTC).
- **Funding:** "If the funding rate is positive, longs pay shorts; if negative, shorts pay
  longs" ([Digital Asset Perpetual Pricing](https://bitnomial.com/exchange/docs/market-operations/settlements/digital-asset-perpetual-pricing/)):
  **no flip**. `FR = avg(P) + clamp(IR - avg(P), -0.05%, 0.05%)`, IR 0.01%. Intervals are
  Central-time aligned (7pm/3am/11am CT), so UTC hours shift with DST (*probed*: 00/08/16
  UTC in May, 01/09/17 UTC in November; the REST docs' "00:00, 08:00, 16:00 UTC" is the
  summer schedule). Spec `funding_rate_upper_bound`/`lower_bound` = ±0.0005 is reported as
  `funding_cap`. Every *probed* rate since listing was the 0.0001 floor.
- **Pagination:** `cursor` in `pagination`; the cursor is returned even on the last page
  (*probed*), so the adapter stops on a short page.
- **Marks:** product data's `mark_price` equals the mark at the last funding calculation
  (*probed*), so `marks()` stamps it with that interval's close and pairs it with that
  interval's `price_index` (`mark_source = 'last_funding_mark'`).
- **Rate limit:** "200 requests per 5 minutes. Exceeding this returns HTTP 403"
  ([REST overview](https://bitnomial.com/exchange/docs/api/rest/overview)). Per-product
  estimate calls are therefore made only for ≤6 contracts.
- **Margin:** product data has `product_group_margin_rate` (0.15/0.18), not labelled
  initial or maintenance → not used by `liquidation_estimate`.

## OG.com (crypto perpetuals; North American Derivatives Exchange)

- **Regulation:** CFTC DCM and DCO: North American Derivatives Exchange, Inc. (formerly
  Nadex; also styled "Crypto.com | Derivatives North America", CDNA), a Crypto.com
  affiliate. US crypto perps launched 2026-10-27, up to 5x for retail, distributed through
  OG.com, the Crypto.com App and the Crypto.com Exchange
  ([announcement](https://og.com/company-news/ogcom-and-cryptocom-expand-derivatives-offering-with-launch-of-regulated-crypto-perpetuals-for-the-us-market)).
  The exchange has also filed rules for perpetual single-stock security futures
  ([Federal Register 2026-20194](https://www.federalregister.gov/documents/2026/10/02/2026-20194)).
- **Base:** `https://api.crypto.com/fcm/v1/public/{method}`, the "GEN4 FCM US B2C API
  (Preview)" ([docs](https://exchange-developer.crypto.com/fcm-b2c/v1/docs/api/rest-introduction)),
  linked as "APIs" from og.com. "Public market-data endpoints can be explored before
  onboarding"; 100 requests per second per method per IP. Plain `GET` with query
  parameters works; a JSON-RPC `POST` to the public methods returns HTTP 500 (*probed*).
  This is **not** `api.crypto.com/exchange/v1`, the offshore exchange (414 perps, up to
  50x, not for US persons).
- **Listings (*probed*):** `get-instruments?inst_type=PERPETUAL_SWAP` → 6 perps: BTC, ETH,
  SOL, HYPE, SEI, ZEC (`BTC-USD-PERP`, ...), index `BTC-USD-INDEX@CdnaMargined`. Without
  `inst_type` the call returns 500 rows of event contracts and no perps.
- **Quoting:** USD per coin; quantities in coins (`contract_size` 1, `qty_tick_size`
  0.0001 BTC). `max_leverage` per instrument (BTC 6.6666, i.e. 15% initial).
- **Funding:** "Funding is paid three times a day, at 00:00, 08:00, and 16:00 UTC ... The
  rate is shown as an amount per eight-hour interval"
  ([Funding Payments](https://help.og.com/en/articles/17252978-funding-payments)). Rule
  16.14 (in the stock-perp filing): positive = longs pay shorts, **no flip**; the rate
  combines a time-weighted premium, an interest component, an asset multiplier, and an
  outer clamp to a floor and cap. *Probed*: BTC, ETH, SOL and HYPE sat at 0.0001 (10.95%/yr)
  for every hour of the 14-day history; SEI and ZEC went negative in late October and
  early November.
- **Funding history:** `get-valuations?valuation_type=funding_hist` is stamped **hourly**,
  newest first, but the value changes only in 8-hour blocks `T-7h .. T` for each funding
  time `T` (*probed*). The point stamped at `T` is taken as the settlement at `T`. About 14
  days are kept.
- **Next rate (inferred, *probed* once):** `valuation_type=estimated_funding_rate` (per
  minute, about 1000 minutes kept) drifts during a window. On 2026-11-13 the SEI estimate
  reached 0.000073951 just before 08:00 UTC, and the funding history then showed
  0.000073951 for the 09:00–15:00 stamps. So the rate settling at the next funding time
  `N` is fixed in advance: it is the last estimate before `N - 8h`. `funding_estimates()`
  reports it with `is_final = false`. Crypto.com's own documentation of these series
  refused automated fetches (HTTP 403); this mapping is an observation, not a quote.
- **Paging:** `get-trades` max `count` 150 (151 → `Invalid count`), newest first,
  `start_ts` inclusive, `end_ts` exclusive (*probed*); the adapter drops a full page's
  oldest millisecond and resumes with `end_ts` just after it. `get-candlestick` returns at
  most 300 bars, the latest in the range, so it is walked forward in 300-bar windows.
- **Margin:** isolated per position; only `max_leverage` is published. The help centre's
  example (3x, $45 maintenance on $300) implies 15%, but no schedule is published, so
  `liquidation_estimate` needs `mm_rate`.

## Offshore benchmark (ccxt 4.5.x)

- **Hyperliquid:** hourly funding; the API reports the hourly rate (`0.0000125` = the
  0.01%/8h baseline / 8, *probed*). "Funding on Hyperliquid is capped at 0.5%/hour"; when
  the contract trades above the oracle "the long position will pay the short position"
  ([funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)): no flip.
  "The maintenance margin is half of the initial margin at max leverage"
  ([liquidations](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)) →
  `mm = 0.5 / maxLeverage` (first margin tier; tiers above `lowerBound` notional are not
  modelled). Quote/settle USDC. `kPEPE`/`kSHIB` = 1000 coins. Public trades need a wallet
  address in ccxt → unsupported.
- **Hyperliquid HIP-3 (builder-deployed dexes):** loaded with ccxt's `hip3` market type
  ([HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals)).
  *Probed* 13 Nov: 328 markets, 171 active: `xyz` 122 (US stocks, ETFs, `GOLD`, `SILVER`,
  `CL`, FX, `XYZ100`, `SP500`), `para` 33 (stocks, rates `2Y`/`10Y`/`30Y`, crypto indices),
  `io` 15, `mkts` 1; the rest (`km`, `hyna`, `flx`, `vntl`, `cash`, ...) inactive. All settle
  in USDC (`collateralToken` 0) and fund hourly; ccxt's funding history, rates, tickers,
  books and OHLCV all work on them. Normalization: `symbol` is the ccxt name with its
  prefix (`xyz:NVDA`); `underlying` strips the prefix (`NVDA`) so a ticker shared with
  Kalshi joins in `funding_spread` and `premium`; `dex` names the builder dex. There is no
  further mapping, so `GOLD` and `SP500` do not join Kalshi's `XAU` and `US500`. At most two
  active markets share a stripped name, but they need not be the same asset: main-dex
  `STX` is Stacks and `para:STX` is Seagate. Builders publish no asset class
  (`asset_class = 'unknown'`), funding cap or maintenance schedule (deployer-set margin
  tables), so only `max_leverage` is reported and `liquidation_estimate` needs `mm_rate`.
  Loading costs one `POST /info` per dex plus two, so the worker loads `xyz`, `para` and
  `io` by default (`PERPS_HYPERLIQUID_DEXES=xyz,km` to choose, `none` for the main dex
  only), caches the list for 10 minutes and retries HTTP 429. ccxt's `fetch_funding_rates`
  "only supports symbols from one DEX at a time for HIP3 markets" (*probed*), so funding-rate
  and ticker calls are made once per dex.
- **Binance USD-M / Bybit linear:** ccxt unified `fundingRate`, positive = longs pay.
  Binance intervals vary per symbol and are inferred from history spacing; Bybit's
  `fundingInterval` (minutes) comes from instrument info. Maintenance brackets are
  authenticated (Binance) or not wired (Bybit) → refused. **Geo-blocked from the US**.
- Allowlisted ccxt methods: `fetch_markets`, `fetch_funding_rate_history`,
  `fetch_funding_rates`, `fetch_order_book`, `fetch_trades`, `fetch_ohlcv`,
  `fetch_tickers`. No exchange is constructed with credentials.

## Market-data terms of use (gate for any recorder / archive)

| Venue | Finding | Source |
|---|---|---|
| Kalshi | §3: "Use of Kalshi APIs is expressly limited to facilitating a members own trading on the Exchange; all other usages are disallowed". §3.1 prohibits "Collecting, caching, aggregating, or storing data or content accessed via the API except for purposes of facilitating your own trading on Kalshi. You may not share such data or content with third parties in any manner without prior written authorization from Kalshi." §3.5 also bars access "for any other benchmarking or competitive purposes". | [Kalshi Developer Agreement v1.1](https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf) (also kalshi.com/developer-agreement) |
| Bitnomial | Permitted: "internal business use, evaluation, education, compliance, and research purposes". Prohibited: "any redistribution, reproduction, rebroadcasting, sublicensing, or dissemination", "archiving", "electronically extracting, scraping, collecting, mining", and "creation of indexes, investment products, reference rates, valuation services, or end-user market data services". | [bitnomial.com/terms](https://bitnomial.com/terms) (Bitnomial Data Terms of Use) |
| Coinbase | Market Data Terms of Use (last updated 2023-02-06): "Coinbase reserves all rights in the Market Data not expressly granted in these Terms"; the license is "revocable, nonexclusive, nontransferable, non-sublicensable". Redistribution goes through named "authorized redistribution partners" (e.g. Amberdata). The full text returned HTTP 403 to automated fetches, so it was read through search excerpts only — **verify by hand**. | [coinbase.com/legal/market_data](https://www.coinbase.com/legal/market_data) |
| Binance | Public datasets are governed by the Binance Vision terms plus the general Terms of Use; restricts using the Binance name to market "your project, company, paper, or software tool without prior written consent". Not reviewed in full. | [Binance Vision Terms of Use](https://data.binance.vision/Binance_Vision-Terms_of_Use.pdf) |
| Bybit | API Terms: "you or your API Client shall not commercially exploit the APIs". | [Bybit API Terms & Conditions](https://bybit.nl/en/help-center/article/API-Terms) |
| OG.com | The FCM API docs publish no data licence and invite public market-data calls without an account. The OG.com user Terms (v1.6, 2026-01-12) bar account holders from "automated use of the Services ... such as using scripts, bots, data mining tools, or similar data gathering and extraction tools"; whether that reaches the public API is unclear. **Ask before archiving or redistributing.** | [FCM API docs](https://exchange-developer.crypto.com/fcm-b2c/v1/docs/api/rest-introduction), [OG.com Terms](https://static.og.com/assets/documents/terms-and-conditions-v1.6-20260112.pdf) |
| Hyperliquid | Not found; the interface terms exclude US persons. **Unreviewed.** | — |

**Bottom line for a recorder/archive:** Kalshi's agreement, read literally, forbids
storing API data for anything but your own trading and forbids sharing it; Bitnomial
forbids archiving and end-user data services; Coinbase reserves redistribution to
licensed partners; OG.com's user terms bar automated data gathering. An archive that is shared, sold or published needs **written
licences** from each venue first. Even this worker's on-demand use sits under Kalshi §3
(member-only, own-trading use) and §3.5 (benchmarking). This is a reading of the terms,
not legal advice. See [ARCHIVE.md](ARCHIVE.md).

## Normalization decisions (summary)

| | Kalshi | Coinbase | Bitnomial | OG.com | Hyperliquid | Binance / Bybit |
|---|---|---|---|---|---|---|
| Native price unit | USD per contract | USD per coin (per 1000 for `1000X`) | ticks | USD per coin | USDC per coin (per 1000 for `kX`) | USDT per named unit |
| Price factor to per-unit | `1 / (contract_size * multiplier)` | `1` or `1/1000` | `price_increment` | `1` | `1` or `1/1000` | `1` or `1/1000` |
| Contract size (units) | `contract_size * multiplier` | `contract_size` (x1000 for `1000X`) | `contract_size` | `contract_size` (1) | `contractSize` (x1000) | `contractSize` (x1000) |
| Funding sign | native = canonical | native = canonical | native = canonical | native = canonical | native = canonical | native = canonical |
| Interval | 8h crypto, 24h others (observed) | 1h | `interval_end - interval_start` | 8h (00/08/16 UTC) | 1h | inferred / `fundingInterval` |
| Cap | `funding_cap` if unit = percentage | none | spec bound ±0.0005 | not published | 0.005/h | not published via ccxt |
| Mark | `settlement_mark_price` | mid (no public mark) | mark at last funding | `mark_price` valuation | `markPx` | ccxt `markPrice` |
| Index | `reference_price` | `index_price` | `price_index` at last funding | `index_price` valuation | oracle | ccxt `indexPrice` |
| Taker side | `taker_side` bid→buy, ask→sell | inverse of maker `side` | n/a | `s` | n/a | ccxt `side` |

Fields that do not normalize are exposed as `raw_kalshi_markets()`,
`raw_kalshi_premium_index()`, `raw_coinbase_products()` and `raw_bitnomial_products()`.
Dropped on purpose: Kalshi `exchange_index` (documented as deprecated), Coinbase display
and UI fields (`icon_url`, `watched`, `alias`, ...), Bitnomial `cqg_symbol`, block-trade
minimums and option/spread products.
