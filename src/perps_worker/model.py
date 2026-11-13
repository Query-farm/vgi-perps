"""Normalized, venue-neutral records produced by the venue adapters.

Conventions every adapter must honour (the whole value of this worker):

* **Prices** are in the quote currency per **one unit of the base underlying**
  (USD per BTC, USD per share, USD per troy ounce, USD per SHIB). A venue that
  quotes per contract (Kalshi) or per 1000 units (Coinbase ``1000SHIB``,
  Hyperliquid ``kPEPE``) is rescaled with ``Instrument.price_factor``.
* **Sizes** are in contracts of that venue's instrument; ``contract_size`` is
  base units per contract, so ``contracts * contract_size * price`` is the
  quote-currency notional.
* **Funding rates** are decimal fractions per funding interval, signed so that
  **positive means longs pay shorts**.
* **Times** are timezone-aware UTC ``datetime`` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Instrument:
    """One perpetual (or perpetual-style) contract on one venue."""

    venue: str
    symbol: str
    underlying: str
    asset_class: str
    quote_ccy: str
    instrument_type: str
    status: str
    contract_size: float
    price_factor: float
    native_price_unit: str
    tick_size: float | None = None
    funding_interval_hours: float | None = None
    funding_cap: float | None = None
    max_leverage: float | None = None
    initial_margin_rate: float | None = None
    maintenance_margin_rate: float | None = None
    margin_source: str | None = None
    listed_at: datetime | None = None
    #: Venue-internal ids the adapter needs (product id, ccxt symbol, ...).
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def canonical_id(self) -> str:
        """``<UNDERLYING>-<QUOTE>-PERP``: the economic exposure, not the listing."""
        return f"{self.underlying}-{self.quote_ccy}-PERP"

    def price(self, native: float | None) -> float | None:
        """Convert a venue-native price to quote currency per base unit."""
        return None if native is None else native * self.price_factor


@dataclass(slots=True)
class FundingPoint:
    """One funding observation: a settled rate, or a provisional estimate."""

    instrument: Instrument
    funding_time: datetime
    rate: float
    interval_hours: float
    is_final: bool
    mark_price: float | None = None
    index_price: float | None = None


@dataclass(slots=True)
class Mark:
    """A live snapshot of one instrument."""

    instrument: Instrument
    as_of: datetime | None
    mark_price: float | None
    mark_source: str
    index_price: float | None
    last_price: float | None
    bid: float | None
    ask: float | None
    funding_rate: float | None
    funding_rate_is_estimate: bool | None
    funding_time: datetime | None
    next_funding_time: datetime | None
    open_interest_contracts: float | None
    open_interest_usd: float | None
    volume_24h_usd: float | None


@dataclass(slots=True)
class BookLevel:
    """One aggregated price level, in normalized units."""

    price: float
    contracts: float


@dataclass(slots=True)
class Trade:
    """One public trade."""

    trade_id: str
    ts: datetime
    price: float
    contracts: float
    taker_side: str | None
    is_block: bool | None


@dataclass(slots=True)
class Candle:
    """One OHLCV bar; ``ts`` is the bar's start."""

    ts: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume_contracts: float | None
    volume_usd: float | None
    open_interest_contracts: float | None
