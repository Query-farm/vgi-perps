"""Venue adapters and the registry that cross-venue functions iterate over."""

from __future__ import annotations

from .base import BARS, CAPABILITIES, Adapter, Capability, UnsupportedError, VenueError
from .bitnomial import BitnomialAdapter
from .coinbase import CoinbaseAdapter
from .kalshi import KalshiAdapter
from .offshore import BinanceAdapter, BybitAdapter, HyperliquidAdapter

#: Every adapter, US-regulated first. One instance per process.
ADAPTERS: dict[str, Adapter] = {
    a.name: a
    for a in (
        KalshiAdapter(),
        CoinbaseAdapter(),
        BitnomialAdapter(),
        HyperliquidAdapter(),
        BinanceAdapter(),
        BybitAdapter(),
    )
}

VENUE_NAMES = tuple(ADAPTERS)
US_VENUES = tuple(n for n, a in ADAPTERS.items() if a.venue_type == "us_regulated")


def get(venue: str) -> Adapter:
    """Look up an adapter by name (case-insensitive) with a helpful error."""
    adapter = ADAPTERS.get(venue.strip().lower())
    if adapter is None:
        raise VenueError(f"unknown venue {venue!r}; expected one of {', '.join(VENUE_NAMES)}")
    return adapter


def select(venue: str | None) -> list[Adapter]:
    """``None`` means every venue; otherwise exactly the named one."""
    return list(ADAPTERS.values()) if venue is None else [get(venue)]


__all__ = [
    "ADAPTERS",
    "BARS",
    "CAPABILITIES",
    "US_VENUES",
    "VENUE_NAMES",
    "Adapter",
    "Capability",
    "UnsupportedError",
    "VenueError",
    "get",
    "select",
]
