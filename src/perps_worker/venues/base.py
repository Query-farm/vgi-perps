"""The venue adapter contract, capability matrix and shared helpers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from ..model import BookLevel, Candle, FundingPoint, Instrument, Mark, Trade

#: Capability names, in the order ``capabilities()`` lists them.
CAPABILITIES = (
    "instruments",
    "funding_history",
    "funding_estimate",
    "marks",
    "premium_index",
    "open_interest",
    "order_book",
    "trades",
    "candles",
    "maintenance_margin",
)

#: Bars ``candles()`` accepts, as seconds.
BARS = {"1m": 60, "1h": 3600, "1d": 86400}

#: Instruments are reference data; cache them briefly per process.
INSTRUMENT_TTL_SECONDS = 60.0


class VenueError(RuntimeError):
    """A venue call failed, or the venue cannot serve the request."""


class UnsupportedError(VenueError):
    """The venue has no public source for the requested capability."""


@dataclass(frozen=True, slots=True)
class Capability:
    """One row of the capability matrix."""

    support: str  # supported | partial | unsupported
    auth_required: bool
    history_depth: str
    notes: str


class Adapter:
    """Base class for one venue. Subclasses override what the venue supports."""

    name: ClassVar[str]
    display_name: ClassVar[str]
    venue_type: ClassVar[str]  # us_regulated | offshore
    regulator: ClassVar[str]
    adapter_version: ClassVar[str] = "0.1.0"
    sign_convention: ClassVar[str]
    capabilities: ClassVar[dict[str, Capability]]

    def __init__(self) -> None:
        """Set up the per-process instrument cache."""
        self._lock = threading.Lock()
        self._cached: tuple[float, list[Instrument]] | None = None

    # ---------------------------------------------------------------- instruments
    def instruments(self) -> list[Instrument]:
        """All instruments, cached for :data:`INSTRUMENT_TTL_SECONDS`."""
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < INSTRUMENT_TTL_SECONDS:
                return self._cached[1]
        fresh = self.load_instruments()
        with self._lock:
            self._cached = (time.monotonic(), fresh)
        return fresh

    def load_instruments(self) -> list[Instrument]:
        """Fetch and normalize the instrument list (no cache)."""
        raise NotImplementedError

    def instrument(self, symbol: str) -> Instrument:
        """Look up one instrument by its venue-native symbol (case-insensitive)."""
        wanted = symbol.strip().upper()
        for inst in self.instruments():
            if inst.symbol.upper() == wanted:
                return inst
        raise VenueError(
            f"{self.name}: unknown symbol {symbol!r}; list symbols with "
            f"SELECT symbol FROM perps.main.instruments(venue := '{self.name}')"
        )

    # ------------------------------------------------------------------ data
    def unsupported(self, capability: str) -> UnsupportedError:
        """Build the standard error for a capability this venue lacks."""
        cap = self.capabilities.get(capability)
        reason = cap.notes if cap else "not implemented"
        return UnsupportedError(f"{self.name} does not support {capability}: {reason}")

    def funding(self, instruments: list[Instrument], start: datetime, end: datetime) -> list[FundingPoint]:
        """Settled funding in ``[start, end]`` for ``instruments``."""
        raise self.unsupported("funding_history")

    def funding_estimates(self, instruments: list[Instrument]) -> list[FundingPoint]:
        """In-progress (provisional) funding for ``instruments``."""
        raise self.unsupported("funding_estimate")

    def marks(self, instruments: list[Instrument]) -> list[Mark]:
        """Live snapshot for ``instruments``."""
        raise self.unsupported("marks")

    def order_book(self, inst: Instrument, depth: int) -> tuple[list[BookLevel], list[BookLevel]]:
        """``(bids best-first, asks best-first)`` in normalized prices."""
        raise self.unsupported("order_book")

    def trades(self, inst: Instrument, start: datetime, end: datetime, cursor: str) -> tuple[list[Trade], str | None]:
        """One page of trades in ``[start, end]``; returns the next cursor or ``None``."""
        raise self.unsupported("trades")

    def candles(self, inst: Instrument, start: datetime, end: datetime, bar: str) -> list[Candle]:
        """OHLCV bars in ``[start, end]``."""
        raise self.unsupported("candles")

    def probe(self) -> int:
        """Cheap health check: fetch instruments, return how many there are."""
        return len(self.instruments())
