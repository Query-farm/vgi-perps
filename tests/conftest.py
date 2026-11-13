"""Shared fixtures: hermetic replay for every test, a worker client, synthetic fixture dirs."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from vgi.arguments import Arguments

__all__ = ["Arguments", "run_table"]
from vgi.client import Client

from perps_worker import replay
from perps_worker.http import request_key
from perps_worker.offshore_client import call_key

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "venues"
NOW = "2026-11-13T01:20:00Z"


@pytest.fixture(autouse=True)
def _hermetic(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every non-live test replays the recorded fixtures; nothing reaches the network."""
    if request.node.get_closest_marker("live"):
        yield
        return
    monkeypatch.setenv(replay.FIXTURES_ENV, str(FIXTURES))
    monkeypatch.setenv(replay.NOW_ENV, NOW)
    monkeypatch.delenv(replay.RECORD_ENV, raising=False)
    monkeypatch.delenv(replay.RECORD_MISSING_ENV, raising=False)
    replay.reset_cache()
    yield
    replay.reset_cache()


@pytest.fixture(scope="session")
def client() -> Iterator[Client]:
    """A worker subprocess replaying the recorded fixtures, as DuckDB would launch it."""
    import os

    env = {**os.environ, replay.FIXTURES_ENV: str(FIXTURES), replay.NOW_ENV: NOW}
    old = dict(os.environ)
    os.environ.update(env)
    try:
        with Client([sys.executable, "-m", "perps_worker.worker"]) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(old)


def run_table(client: Client, name: str, *positional: Any, **named: Any) -> pa.Table:
    """Call a table function through the worker and collect the result."""
    arguments = Arguments(
        positional=tuple(pa.scalar(v) for v in positional),
        named={k: pa.scalar(v) for k, v in named.items()},
    )
    batches = list(client.table_function(function_name=name, schema_path=["main"], arguments=arguments))
    return pa.Table.from_batches(batches)


Writer = Callable[..., None]


@pytest.fixture
def synthetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Writer:
    """Point replay at an empty temp dir and return a writer for hand-made responses.

    ``write(url, params, body)`` records an HTTP response; ``write(None, None,
    body, ccxt=(exchange_id, method, args, kwargs))`` records a ccxt result.
    """
    monkeypatch.setenv(replay.FIXTURES_ENV, str(tmp_path))
    replay.reset_cache()
    counter = {"n": 0}

    def write(
        url: str | None,
        params: dict[str, Any] | None,
        body: Any,
        *,
        status: int = 200,
        ccxt: tuple[str, str, tuple[Any, ...], dict[str, Any]] | None = None,
    ) -> None:
        key = call_key(*ccxt) if ccxt else request_key(str(url), params)
        counter["n"] += 1
        (tmp_path / f"r{counter['n']:03d}.json").write_text(json.dumps({"key": key, "status": status, "body": body}))
        replay.reset_cache()

    return write
