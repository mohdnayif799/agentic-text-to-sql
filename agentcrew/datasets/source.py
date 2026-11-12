"""A data source the agent can answer from, other than NorthStar.

Two kinds:

* ``upload``: a visitor's converted file (datasets/store.py), SQLite, read
  through the same read-only engine as NorthStar;
* ``postgres``: AGENTCREW_DATABASE_URL in self-hosted mode, through
  db/engine.postgres_read_only_engine.

NorthStar itself is not a DataSource: its path through the app is the one
the frozen evaluation measured, and it stays exactly as it was. A source
supplies the three things a run needs, built from one place so the agent,
the SQL editor and the API cannot disagree: the tools (engine, catalog with
confirmed joins, dialect, row cap and timeout), and the shared context.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Engine

from agentcrew.context import ContextBuilder
from agentcrew.datasets.describe import Notes, empty_notes, load_notes
from agentcrew.datasets.joins import Join, infer_joins, with_joins
from agentcrew.datasets.store import Dataset
from agentcrew.db import dialect as sql_dialect
from agentcrew.db.engine import (
    DatabaseUnavailableError,
    UnsupportedDatabaseError,
    postgres_read_only_engine,
    read_only_engine,
    scrub_url,
)
from agentcrew.db.introspect import Catalog, build_catalog
from agentcrew.tools import ToolContext


@dataclass(frozen=True)
class DataSource:
    key: str
    """Changes whenever anything a run depends on changes (an edit of the
    descriptions or joins), so cached context is rebuilt."""
    kind: str
    """upload or postgres."""
    label: str
    engine: Engine
    dialect: str
    catalog: Catalog
    """The database's own catalog, without the confirmed joins."""
    notes: Notes
    joins: tuple[Join, ...]

    def tool_context(self, settings: Any) -> ToolContext:
        return ToolContext.create(
            self.engine, max_rows=settings.max_result_rows,
            timeout=settings.sql_timeout_seconds, dialect=self.dialect,
            metrics=False, catalog=with_joins(self.catalog, self.joins))

    def context_builder(self, tools: ToolContext, today: date,
                        settings: Any) -> ContextBuilder:
        return ContextBuilder.from_source(
            self.engine, tools.catalog, today=today, notes=self.notes,
            joins=self.joins, max_tables=settings.max_tables_in_context)


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
_ENGINES: dict[str, Engine] = {}
_CATALOGS: dict[str, Catalog] = {}
_LOCK = threading.Lock()


def _upload_engine(dataset: Dataset, settings: Any) -> tuple[Engine, Catalog]:
    """One engine and catalog per dataset, reused across its runs."""
    with _LOCK:
        if dataset.id not in _ENGINES:
            engine = read_only_engine(dataset.path,
                                      timeout=settings.sql_timeout_seconds)
            _ENGINES[dataset.id] = engine
            _CATALOGS[dataset.id] = build_catalog(engine)
        return _ENGINES[dataset.id], _CATALOGS[dataset.id]


def upload_source(dataset: Dataset, settings: Any) -> DataSource:
    engine, catalog = _upload_engine(dataset, settings)
    return DataSource(key=dataset.key, kind="upload", label=dataset.label,
                      engine=engine, dialect=sql_dialect.SQLITE, catalog=catalog,
                      notes=dataset.notes, joins=dataset.joins)


def close_upload(dataset: Dataset) -> str | None:
    """Dispose of a removed dataset's engine; returns its URL (to forget the
    caches keyed by it), or None if it was never opened."""
    with _LOCK:
        engine = _ENGINES.pop(dataset.id, None)
        _CATALOGS.pop(dataset.id, None)
    if engine is None:
        return None
    url = str(engine.url)
    engine.dispose()
    return url


# ---------------------------------------------------------------------------
# Self-hosted PostgreSQL
# ---------------------------------------------------------------------------
@dataclass
class _Postgres:
    engine: Engine
    catalog: Catalog
    notes: Notes
    joins: tuple[Join, ...]
    revision: int = 0


_POSTGRES: dict[str, _Postgres] = {}


def _url_key(url: str) -> str:
    """A cache key for a database URL that is safe to show: this key becomes
    part of widget keys, which reach the browser. It is a hash of the URL
    with the password left out, so even the hash says nothing about it."""
    try:
        from sqlalchemy.engine import make_url

        shown = make_url(url).render_as_string(hide_password=True)
    except Exception:
        shown = "unparseable"
    return hashlib.sha256(shown.encode()).hexdigest()[:16]


def postgres_state(settings: Any) -> _Postgres:
    """The database at AGENTCREW_DATABASE_URL: connected and introspected
    once per process. Saved descriptions are loaded when
    AGENTCREW_DESCRIPTIONS_PATH points at a file."""
    url = settings.database_url_value()
    key = _url_key(url)
    with _LOCK:
        if key not in _POSTGRES:
            try:
                engine = postgres_read_only_engine(
                    url, timeout=settings.sql_timeout_seconds)
                catalog = build_catalog(engine)
                joins = tuple(infer_joins(engine, catalog))
            except UnsupportedDatabaseError:
                raise  # its message is ours and names no part of the URL
            except Exception as exc:
                # Scrubbed and raised from None: a driver's message may echo
                # the URL it was given, and a chained cause would print it in
                # a traceback even when this message is clean.
                first = (str(exc).strip().splitlines() or [""])[0][:300]
                raise DatabaseUnavailableError(
                    "could not connect to or read the database at "
                    f"AGENTCREW_DATABASE_URL: {type(exc).__name__}: "
                    f"{scrub_url(first, url)}") from None
            notes = None
            if settings.descriptions_path:
                notes = load_notes(settings.descriptions_path, catalog)
            _POSTGRES[key] = _Postgres(engine, catalog, notes or empty_notes(),
                                       joins)
        return _POSTGRES[key]


def postgres_source(settings: Any) -> DataSource:
    state = postgres_state(settings)
    url = state.engine.url
    label = f"{url.database or 'database'} on {url.host or 'localhost'}"
    return DataSource(key=f"postgres:{_url_key(settings.database_url_value())}:"
                          f"{state.revision}",
                      kind="postgres", label=label, engine=state.engine,
                      dialect=sql_dialect.POSTGRES, catalog=state.catalog,
                      notes=state.notes, joins=state.joins)


def update_postgres(settings: Any, *, notes: Notes | None = None,
                    joins: tuple[Join, ...] | None = None) -> DataSource:
    """Record edited descriptions or joins (and save the descriptions when a
    path is configured)."""
    from agentcrew.datasets.describe import save_notes

    state = postgres_state(settings)
    with _LOCK:
        if notes is not None:
            state.notes = notes
        if joins is not None:
            state.joins = joins
        state.revision += 1
    if notes is not None and settings.descriptions_path:
        save_notes(settings.descriptions_path, notes)
    return postgres_source(settings)


def reset_sources() -> None:
    """Test helper."""
    with _LOCK:
        for engine in _ENGINES.values():
            engine.dispose()
        _ENGINES.clear()
        _CATALOGS.clear()
        for state in _POSTGRES.values():
            state.engine.dispose()
        _POSTGRES.clear()
