"""Shared presentation and parameter handling for the perps walkthroughs."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from IPython.display import Markdown, display

COLORS = {
    "kalshi": "#246B50",
    "coinbase": "#426DB3",
    "bitnomial": "#B27232",
    "hyperliquid": "#25858B",
    "og": "#8B62A5",
    "binance": "#847347",
    "bybit": "#647786",
}
VENUE_NAMES = {
    "kalshi": "Kalshi",
    "coinbase": "Coinbase",
    "bitnomial": "Bitnomial",
    "hyperliquid": "Hyperliquid",
    "og": "OG.com",
    "binance": "Binance",
    "bybit": "Bybit",
}
BID, ASK = "#246B50", "#BF713D"
REPLAY_CLOCK = datetime(2026, 11, 13, 1, 20, tzinfo=UTC)
REPLAY_WINDOWS = {
    "funding": ("2026-11-10T00:00:00Z", "2026-11-13T00:00:00Z"),
    "candles": ("2026-11-12T18:00:00Z", "2026-11-13T00:00:00Z"),
    "trades": ("2026-11-13T01:00:00Z", "2026-11-13T01:01:00Z"),
    "premium": ("2026-11-13T01:00:00Z", "2026-11-13T01:01:00Z"),
}


def utc(value: str | datetime) -> datetime:
    """Parse an ISO date/time, interpreting a date or naive timestamp as UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def sql_text(value: str | datetime) -> str:
    """Quote a string or UTC datetime as a SQL literal for a JupySQL cell."""
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    return "'" + text.replace("'", "''") + "'"


def number(value: Any, digits: int = 2, suffix: str = "") -> str:
    """Format a result without converting missing data to zero."""
    if value is None or pd.isna(value):
        return "unavailable"
    return f"{value:,.{digits}f}{suffix}"


def money(value: Any, digits: int = 0) -> str:
    """Format a dollar-labelled value safely for notebook Markdown."""
    return "unavailable" if value is None or pd.isna(value) else "\\$" + number(value, digits)


def prose(text: str) -> None:
    """Render a result-derived paragraph while keeping its Python source folded."""
    display(Markdown(text))


def require_rows(frame: pd.DataFrame, description: str) -> None:
    """Stop with an actionable message if a required query returns no data."""
    if frame.empty:
        raise ValueError(
            f"No {description} returned. Inspect venue health, asset and dates; no replay fallback was used."
        )


def finite_numbers(values: Any, name: str, *, positive: bool = False) -> list[float]:
    """Validate and deduplicate user-supplied numeric scenarios."""
    result = [float(v) for v in values]
    if not result or not all(math.isfinite(v) and (not positive or v > 0) for v in result):
        raise ValueError(f"{name} must contain finite {'positive ' if positive else ''}numbers.")
    return sorted(set(result))


@dataclass(frozen=True)
class Window:
    """An analysis interval and the request interval that can supply it."""

    start: datetime
    end: datetime
    fetch_start: datetime
    fetch_end: datetime

    @property
    def label(self) -> str:
        """Return the precise half-open analysis interval for display."""
        return f"{self.start:%d %b %Y %H:%M} → {self.end:%d %b %Y %H:%M} UTC (end excluded)"


@dataclass(frozen=True)
class Run:
    """Explicit data mode and a clock fixed once at the beginning of a run."""

    mode: str
    started: datetime
    as_of: datetime

    @property
    def replay(self) -> bool:
        """Whether all venue requests use the checked-in responses."""
        return self.mode == "replay"

    @property
    def source(self) -> str:
        """Label the data source without implying synchronized snapshots."""
        if self.replay:
            return "Replay of recorded responses · captured 13 Nov 2026 · VGI / Query Farm"
        return f"Live public APIs · run started {self.started:%d %b %Y %H:%M:%S} UTC · VGI / Query Farm"

    def validate_asset(self, asset: str) -> str:
        """Limit replay to the asset with complete walkthrough fixture coverage."""
        asset = asset.strip().upper()
        if not re.fullmatch(r"[A-Z0-9._:-]{1,32}", asset):
            raise ValueError("ASSET must be a nonempty ticker, such as BTC.")
        if self.replay and asset != "BTC":
            raise ValueError("These replay walkthroughs support ASSET='BTC'. Use DATA_MODE='live' for another asset.")
        return asset

    def window(self, kind: str, start: str | None = None, end: str | None = None) -> Window:
        """Resolve recent live defaults or a selectable subset of a recorded window."""
        if self.replay:
            default_start, default_end = map(utc, REPLAY_WINDOWS[kind])
        else:
            if kind == "funding":
                default_end = self.as_of.replace(hour=0, minute=0, second=0, microsecond=0)
                default_start = default_end - timedelta(days=3)
            elif kind == "candles":
                default_end = self.as_of.replace(minute=0, second=0, microsecond=0)
                default_start = default_end - timedelta(hours=6)
            else:
                default_end = self.as_of.replace(second=0, microsecond=0)
                default_start = default_end - timedelta(minutes=1)
        selected_start = utc(start) if start is not None else default_start
        selected_end = utc(end) if end is not None else default_end
        if selected_start >= selected_end:
            raise ValueError(f"{kind}: start must be earlier than the exclusive end.")
        if self.replay and not (default_start <= selected_start < selected_end <= default_end):
            raise ValueError(
                f"{kind}: replay dates must be within {default_start.isoformat()} to {default_end.isoformat()}."
            )
        if not self.replay and selected_end > self.as_of:
            raise ValueError(f"{kind}: END_DATE must not be later than this run's UTC start time.")
        if kind == "premium" and selected_end - selected_start > timedelta(hours=1):
            raise ValueError("A premium request can span at most one hour.")
        if self.replay:
            # Request the known recording, then filter locally to the selected interval.
            fetch_end = default_end - timedelta(seconds=1) if kind == "funding" else default_end
            return Window(selected_start, selected_end, default_start, fetch_end)
        return Window(selected_start, selected_end, selected_start, selected_end)

    def badge(self, **windows: Window) -> None:
        """Show data provenance and the effective analysis windows prominently."""
        mode = "REPLAY · local recorded responses" if self.replay else "LIVE · public venue APIs"
        text = f"**{mode}**  \nWorker clock: **{self.as_of:%Y-%m-%d %H:%M:%S} UTC**. "
        text += (
            "Live mode fixes the clock at run start for consistent request windows; API snapshots can differ in time."
        )
        if self.replay:
            text = (
                f"**{mode}**  \nRecorded worker clock: **{self.as_of:%Y-%m-%d %H:%M:%S} UTC**. "
                "Venue requests stay local."
            )
        for name, window in windows.items():
            text += f"  \n**{name.replace('_', ' ').title()}:** {window.label}"
        prose(text)


def make_run(mode: str, directory: Path) -> Run:
    """Configure the released worker's data mode before the visible SQL ATTACH."""
    if mode not in {"replay", "live"}:
        raise ValueError("DATA_MODE must be 'replay' or 'live'.")
    started = datetime.now(UTC).replace(microsecond=0)
    as_of = REPLAY_CLOCK if mode == "replay" else started
    for name in ("PERPS_FIXTURES_DIR", "PERPS_NOW", "PERPS_RECORD_DIR", "PERPS_RECORD_MISSING"):
        os.environ.pop(name, None)
    if mode == "replay":
        fixtures = directory / "fixtures/venues"
        if not fixtures.is_dir():
            raise FileNotFoundError("Keep the notebook's fixtures/ directory beside notebook_support.py.")
        os.environ["PERPS_FIXTURES_DIR"] = str(fixtures.resolve())
    os.environ["PERPS_NOW"] = as_of.isoformat()
    return Run(mode, started, as_of)


def set_theme() -> None:
    """Apply the shared notebook visual style without modifying query results."""
    sns.set_theme(
        style="whitegrid",
        context="notebook",
        palette=list(COLORS.values()),
        rc={
            "figure.figsize": (11, 4.8),
            "figure.dpi": 150,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": "#CCD5CF",
            "axes.labelcolor": "#405047",
            "text.color": "#243B2F",
            "xtick.color": "#58685F",
            "ytick.color": "#58685F",
            "grid.color": "#E8EDE9",
            "grid.linewidth": 0.7,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titlelocation": "left",
            "axes.titleweight": "semibold",
            "axes.titlesize": 12,
            "axes.labelsize": 10.5,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.frameon": False,
            "lines.linewidth": 2,
            "text.parse_math": False,
        },
    )
    pd.set_option("display.max_columns", 15)
    pd.set_option("display.precision", 5)


def finish_chart(fig: Any, title: str, subtitle: str, source: str) -> None:
    """Give each chart a readable finding, context and accurate provenance."""
    fig.text(0.08, 0.99, title, ha="left", va="top", fontsize=17, fontweight="semibold")
    fig.text(0.08, 0.91, subtitle, ha="left", va="top", fontsize=10.5, color="#58685F")
    sns.despine(fig=fig, left=True, bottom=True)
    fig.tight_layout(rect=(0, 0.08, 1, 0.84), w_pad=2.8, h_pad=1.4)
    fig.text(0.08, 0.015, source, ha="left", va="bottom", fontsize=8.5, color="#78857D")
    plt.show()
