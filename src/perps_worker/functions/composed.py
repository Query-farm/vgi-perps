"""Example queries that combine two or more of the worker's functions.

Each one is defined once here, listed on the function that drives it (within its first
five examples, which is what agent tools show) and on the schema-level list.
"""

from __future__ import annotations

from vgi.metadata import FunctionExample

FUNDING_CAP_USAGE = FunctionExample(
    sql=(
        "SELECT m.venue, m.symbol, m.funding_rate, i.funding_cap, abs(m.funding_rate) / i.funding_cap AS cap_used "
        "FROM perps.main.marks(underlying := 'BTC') AS m "
        "JOIN perps.main.instruments(underlying := 'BTC') AS i USING (venue, symbol) "
        "WHERE i.funding_cap IS NOT NULL ORDER BY cap_used DESC, m.venue, m.symbol"
    ),
    description=(
        "How much of its per-interval funding cap each BTC perp's current rate uses (marks joined to "
        "instruments; venues that publish no cap drop out)"
    ),
)

LIVE_ORDER_BOOK_VENUES = FunctionExample(
    sql=(
        "SELECT v.venue, v.status, c.notes FROM perps.main.venues() AS v "
        "JOIN perps.main.capabilities() AS c USING (venue) "
        "WHERE c.capability = 'order_book' AND c.support = 'supported' AND v.status = 'ok' ORDER BY v.venue"
    ),
    description="Venues that answer right now and serve a public order book, with the endpoint each uses",
)

DAILY_RETURN_VS_FUNDING = FunctionExample(
    sql=(
        "WITH bars AS (SELECT ts, coalesce(lead(ts) OVER (ORDER BY ts), ts + INTERVAL 1 DAY) AS bar_end, "
        "(close / open - 1) * 1e4 AS return_bps FROM perps.main.candles('kalshi', 'KXBTCPERP', bar := '1d')) "
        "SELECT b.ts, b.return_bps, sum(f.rate_bps) AS funding_bps "
        "FROM bars AS b JOIN perps.main.funding(venue := 'kalshi', underlying := 'BTC') AS f "
        "ON f.funding_time > b.ts AND f.funding_time <= b.bar_end "
        "GROUP BY b.ts, b.return_bps HAVING count(*) = 3 ORDER BY b.ts"
    ),
    description=(
        "Kalshi BTC perp, last week: each daily bar's open-to-close return next to the funding its three "
        "settlements charged longs, both in bps"
    ),
)

NOWCAST_VS_WEEK = FunctionExample(
    sql=(
        "SELECT n.funding_time, n.weight_accrued, n.projected_rate_annualized, h.week_mean_apr, "
        "n.projected_rate_annualized - h.week_mean_apr AS vs_week "
        "FROM perps.main.kalshi_funding_nowcast('KXBTCPERP') AS n "
        "CROSS JOIN (SELECT avg(rate_annualized) AS week_mean_apr "
        "FROM perps.main.funding(venue := 'kalshi', underlying := 'BTC')) AS h"
    ),
    description=("The next Kalshi BTC settlement's projected annualized rate against the past week's settled average"),
)

#: Every composed example, for the schema-level ``vgi.example_queries`` list.
COMPOSED_EXAMPLES = [FUNDING_CAP_USAGE, LIVE_ORDER_BOOK_VENUES, DAILY_RETURN_VS_FUNDING, NOWCAST_VS_WEEK]
