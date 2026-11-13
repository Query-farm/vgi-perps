"""Deterministic replay and recording of venue responses.

Every outbound request in this worker goes through one of two chokepoints:
:mod:`perps_worker.http` (``httpx``, the US venues) and
:mod:`perps_worker.offshore_client` (``ccxt``, the offshore benchmark). Both
consult this module first:

* ``PERPS_FIXTURES_DIR`` set: the request is answered from a recorded file and
  the network is never touched. A request with no recording raises
  :class:`FixtureMissingError`, so a test cannot silently go live.
* ``PERPS_RECORD_DIR`` set: the live response is also written to that directory,
  in the same format, so a probe run becomes a fixture set.

A recording is one JSON file per request::

    {"key": "GET https://host/path?a=1&b=2", "status": 200, "body": {...}}

``status`` other than 200 replays as an HTTP error, which is how the tests
simulate a venue outage. ``PERPS_NOW`` pins "now" (ISO 8601, UTC) so default
date windows resolve to the same requests the fixtures were recorded with.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIXTURES_ENV = "PERPS_FIXTURES_DIR"
RECORD_ENV = "PERPS_RECORD_DIR"
NOW_ENV = "PERPS_NOW"
#: Recording aid: in replay mode, go live (and record) for requests with no fixture.
RECORD_MISSING_ENV = "PERPS_RECORD_MISSING"


class FixtureMissingError(RuntimeError):
    """Replay mode is on and no recording matches the request."""


@dataclass(frozen=True, slots=True)
class Recording:
    """One recorded response."""

    key: str
    status: int
    body: Any


_lock = threading.Lock()
_loaded: dict[str, dict[str, Recording]] = {}


def now() -> datetime:
    """Return the current UTC time, or ``PERPS_NOW`` when it is set."""
    pinned = os.environ.get(NOW_ENV)
    if pinned:
        return datetime.fromisoformat(pinned.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def fixtures_dir() -> Path | None:
    """Return the replay directory, or ``None`` when requests go to the network."""
    value = os.environ.get(FIXTURES_ENV)
    return Path(value) if value else None


def _index(directory: Path) -> dict[str, Recording]:
    """Load every recording under ``directory`` once per process."""
    cache_key = str(directory.resolve())
    with _lock:
        if cache_key not in _loaded:
            table: dict[str, Recording] = {}
            for path in sorted(directory.rglob("*.json")):
                doc = json.loads(path.read_text())
                if not isinstance(doc, dict) or "key" not in doc:
                    continue
                table[doc["key"]] = Recording(doc["key"], int(doc.get("status", 200)), doc.get("body"))
            _loaded[cache_key] = table
        return _loaded[cache_key]


def reset_cache() -> None:
    """Forget loaded recordings (tests swap fixture directories)."""
    with _lock:
        _loaded.clear()


def lookup(key: str) -> Recording | None:
    """Return the recording for ``key`` in replay mode, ``None`` when live.

    Args:
        key: The canonical request key built by the chokepoint.

    Returns:
        The recording, or ``None`` when replay mode is off.

    Raises:
        FixtureMissingError: Replay mode is on and nothing matches ``key``.

    """
    directory = fixtures_dir()
    if directory is None:
        return None
    recording = _index(directory).get(key)
    if recording is None:
        if os.environ.get(RECORD_MISSING_ENV) == "1":
            return None
        raise FixtureMissingError(f"no recorded response for {key!r} in {directory}")
    return recording


def _slug(key: str) -> str:
    """Build a readable, collision-resistant file name for ``key``."""
    head = re.sub(r"^https?://[^/]+", "", key.split("?")[0].split(" ", 1)[-1])
    head = re.sub(r"[^A-Za-z0-9]+", "_", head).strip("_")[:70]
    return f"{head}_{hashlib.sha1(key.encode()).hexdigest()[:10]}.json"


def record(key: str, status: int, body: Any) -> None:
    """Write a live response to ``PERPS_RECORD_DIR`` when recording is on."""
    value = os.environ.get(RECORD_ENV)
    if not value:
        return
    directory = Path(value)
    directory.mkdir(parents=True, exist_ok=True)
    doc = {"key": key, "status": status, "body": body}
    (directory / _slug(key)).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
