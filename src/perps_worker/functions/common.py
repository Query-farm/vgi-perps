"""Shared plumbing for the table functions: types, arguments, the venue scan loop."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.table_function import OutputCollector, ProcessParams, TableFunctionGenerator
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.log import Level

from .. import replay
from ..meta import column
from ..model import Instrument
from ..venues import VENUE_NAMES, Adapter, select

log = logging.getLogger("perps_worker")

TS = pa.timestamp("us", tz="UTC")
F64 = pa.float64()
STR = pa.string()

#: Default history window when ``start_date`` is omitted.
DEFAULT_LOOKBACK = timedelta(days=7)

VenueArg = Annotated[
    str | None,
    Arg(
        "venue",
        default=None,
        doc=f"Venue to query: one of {', '.join(VENUE_NAMES)}. NULL (default) queries every venue.",
        choices=[*VENUE_NAMES],
    ),
]
RequiredVenueArg = Annotated[
    str, Arg(0, doc=f"Venue to query: one of {', '.join(VENUE_NAMES)}.", choices=[*VENUE_NAMES])
]
SymbolArg = Annotated[
    str, Arg(1, doc="Venue-native contract symbol, as in instruments().symbol, e.g. 'KXBTCPERP' or 'BIP-20DEC30-CDE'.")
]
UnderlyingArg = Annotated[
    str | None,
    Arg(
        "underlying",
        default=None,
        doc="Canonical underlying to filter on, as in instruments().underlying. NULL (default) keeps every "
        "underlying; offshore venues are scanned only when it is set, because they list hundreds of contracts.",
    ),
]
StartArg = Annotated[
    datetime | None,
    Arg(
        "start_date",
        default=None,
        arrow_type=TS,
        doc="Start of the time window, inclusive. Default: 7 days before end_date.",
    ),
]
EndArg = Annotated[
    datetime | None,
    Arg("end_date", default=None, arrow_type=TS, doc="End of the time window, inclusive. Default: now."),
]


def window(
    start: datetime | None, end: datetime | None, default: timedelta = DEFAULT_LOOKBACK
) -> tuple[datetime, datetime]:
    """Resolve the optional window arguments to aware UTC datetimes."""
    end_ = _utc(end) if end is not None else replay.now()
    start_ = _utc(start) if start is not None else end_ - default
    if start_ > end_:
        raise ArgumentError(f"start_date {start_.isoformat()} is after end_date {end_.isoformat()}")
    return start_, end_


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def filter_instruments(adapter: Adapter, underlying: str | None, *, include_inactive: bool = False) -> list[Instrument]:
    """Instruments of one venue, optionally narrowed to an underlying."""
    wanted = underlying.strip().upper() if underlying else None
    return [
        i
        for i in adapter.instruments()
        if (include_inactive or i.status == "active") and (wanted is None or i.underlying.upper() == wanted)
    ]


def result_columns(schema: pa.Schema) -> str:
    """``vgi.result_columns_schema`` JSON from an Arrow schema with comments."""
    names = {
        pa.string(): "VARCHAR",
        pa.float64(): "DOUBLE",
        pa.int64(): "BIGINT",
        pa.int32(): "INTEGER",
        pa.bool_(): "BOOLEAN",
        TS: "TIMESTAMP WITH TIME ZONE",
        pa.list_(pa.string()): "VARCHAR[]",
    }
    return json.dumps(
        [
            {
                "name": f.name,
                "type": names.get(f.type, str(f.type)),
                "description": (f.metadata or {}).get(b"comment", b"").decode(),
            }
            for f in schema
        ]
    )


def build_schema(fields: Iterable[tuple[str, pa.DataType, str, bool]]) -> pa.Schema:
    """``[(name, type, comment, nullable)]`` -> schema with column comments."""
    return pa.schema([column(n, t, c, nullable=nullable) for n, t, c, nullable in fields])


class ArgumentError(ValueError):
    """A bad argument value: always raised, even in a cross-venue scan."""


@dataclass(kw_only=True)
class ScanState(ArrowSerializableDataclass):
    """Externalized cursor: which work item we are on, and its page cursor.

    It travels with every HTTP continuation, so a scan resumes exactly where the
    previous batch stopped, whichever process serves it.
    """

    step: int = 0
    cursor: str = ""
    emitted: int = 0


def emit_rows(out: OutputCollector, params: ProcessParams[Any], rows: list[dict[str, Any]]) -> None:
    """Emit dict rows as one batch with the bound schema.

    Every non-final tick must emit a batch, so an empty work item emits an
    empty batch rather than nothing.
    """
    out.emit(pa.RecordBatch.from_pylist(rows, schema=params.output_schema))


def warn(out: OutputCollector, message: str) -> None:
    """Surface a skipped-venue warning to the client and the worker log."""
    log.warning(message)
    try:
        out.client_log(Level.WARN, message)
    except Exception:  # pragma: no cover - a logging failure must not fail the query
        log.debug("client_log failed", exc_info=True)


class VenueScan[TArgs](TableFunctionGenerator[TArgs, ScanState]):
    """One venue per work item; cross-venue scans skip failing venues with a warning.

    Subclasses implement :meth:`produce`. A single-venue call (``venue`` given)
    lets the error propagate; a cross-venue call (``venue`` NULL) logs a warning
    naming the venue and continues, and ``venues()`` reports the failure too.
    """

    FIXED_SCHEMA: ClassVar[pa.Schema]

    @classmethod
    def venue_list(cls, params: ProcessParams[TArgs]) -> tuple[list[Adapter], bool]:
        """Return the adapters to scan and whether this is a cross-venue call."""
        venue = getattr(params.args, "venue", None)
        return select(venue), venue is None

    @classmethod
    def produce(
        cls, params: ProcessParams[TArgs], adapter: Adapter, cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Rows for one venue (one page); return a next cursor to stay on this venue."""
        raise NotImplementedError

    @classmethod
    def initial_state(cls, params: ProcessParams[TArgs]) -> ScanState:
        """Start at the first venue."""
        return ScanState()

    @classmethod
    def process(cls, params: ProcessParams[TArgs], state: ScanState, out: OutputCollector) -> None:
        """Produce one work item (or one page of it) per call."""
        adapters, cross = cls.venue_list(params)
        if state.step >= len(adapters):
            out.finish()
            return
        adapter = adapters[state.step]
        try:
            rows, next_cursor = cls.produce(params, adapter, state.cursor)
        except ArgumentError:
            raise
        except Exception as exc:
            if not cross:
                raise
            warn(out, f"perps: skipped venue {adapter.name}: {exc}")
            rows, next_cursor = [], None
        cap = getattr(params.args, "max_trades", None)
        if cap is not None and state.emitted + len(rows) >= cap:
            rows, next_cursor = rows[: cap - state.emitted], None
            state.step = len(adapters) - 1  # this is the last work item
        # Advance the cursor BEFORE emitting, so a continuation never repeats rows.
        if next_cursor:
            state.cursor = next_cursor
        else:
            state.step += 1
            state.cursor = ""
        state.emitted += len(rows)
        emit_rows(out, params, rows)


def instrument_row(inst: Instrument) -> dict[str, Any]:
    """Identity columns shared by most result sets."""
    return {
        "venue": inst.venue,
        "symbol": inst.symbol,
        "canonical_id": inst.canonical_id,
        "underlying": inst.underlying,
    }


Producer = Callable[[ProcessParams[Any], Adapter, str], tuple[list[dict[str, Any]], str | None]]
