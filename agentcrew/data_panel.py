"""The "Your data" panel of the Streamlit app.

Kept out of app.py, which is already long, and kept apart from datasets/,
which knows nothing about Streamlit. This module owns the session's side of
bring-your-own-data: which source the session answers from, the upload
widget, the description and join editors, and deleting the session's data.

Session state it uses (all per browser session):
    owner          a random id the store binds this session's datasets to
    dataset_id     the session's current upload, if any
    upload_seen    the uploader's file id last handled, so a rerun does not
                   ingest the same file twice
    uploads_used   uploads this session, for the per-session limit
    data_choice    which source the radio selects

A model call to draft descriptions is made by a callback app.py passes in,
because app.py decides which key pays and charges the caps.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, MutableMapping
from typing import Any

import pandas as pd
import streamlit as st

from agentcrew.config import Settings
from agentcrew.datasets.describe import Notes
from agentcrew.datasets.errors import UploadError
from agentcrew.datasets.ingest import EXTENSIONS, ingest
from agentcrew.datasets.joins import Join, check_join
from agentcrew.datasets.source import (
    DataSource,
    postgres_source,
    update_postgres,
    upload_source,
)
from agentcrew.datasets.store import Dataset, DatasetStore, dataset_store
from agentcrew.tools import ToolContext

OWNER = "owner"
DATASET_ID = "dataset_id"
UPLOAD_SEEN = "upload_seen"
UPLOADS_USED = "uploads_used"
DATA_CHOICE = "data_choice"

UPLOADS_PER_SESSION = 3

EXPANDER = "Your data"
NORTHSTAR = "Sample sales data (NorthStar)"
DATABASE = "Company database"
UPLOAD = "Upload a file"

#: Shown beside the uploader. Every item is something the code sends; the
#: numbers are the code's own limits (archs/answer.ANSWER_ROWS,
#: context/values.MAX_DISTINCT, datasets/describe.SAMPLE_ROWS and SAMPLE_CELL,
#: settings.dataset_ttl_seconds). tests/test_data_panel.py checks them.
PRIVACY_NOTE = (
    "**What the model sees.** Your question; the table and column names, types "
    "and row counts; every value of a text column with fewer than 200 distinct "
    "values; the earliest and latest date of each date column; the "
    "descriptions and joins; and rows of query results (up to 40) and of value "
    "lookups (up to 100) while the agent works. Once per "
    "upload, the descriptions are drafted from the same schema plus up to 3 "
    "example values per column and up to 3 rows per table, each value cut to "
    "40 characters. All of it goes to the model provider: Google Gemini on the "
    "demo key, or the provider of your own key.\n\n"
    "**What is kept.** Your file is converted and held in this server's memory "
    "for this session only. It is deleted when you remove it, upload another, "
    "leave it unused for {minutes} minutes, or the session ends. It is never "
    "written to a log, a bucket or a database.\n\n"
    "**The public demo is not for sensitive or personal data.**"
)

PROGRESS_START = "Reading the upload..."
PROGRESS_DONE = "Upload ready"
PROGRESS_FAILED = "Upload refused"

UPLOAD_LIMIT_MESSAGE = (
    f"This session has used its {UPLOADS_PER_SESSION} uploads. Reload the page to "
    "start a new session."
)
DAILY_UPLOAD_MESSAGE = (
    "The demo has reached its daily limit on uploads. Try again tomorrow."
)

Describer = Callable[[ToolContext], tuple[Notes | None, str | None]]
"""(tools) -> (drafted notes or None, a notice for the visitor or None)."""

_hooked: set[int] = set()
_hook_lock = threading.Lock()


def store(settings: Settings) -> DatasetStore:
    """The process-wide store, with the cache cleanup hook registered once."""
    from agentcrew.archs.app_runner import forget_dataset

    found = dataset_store(settings)
    with _hook_lock:
        if id(found) not in _hooked:
            found.on_remove(forget_dataset)
            _hooked.add(id(found))
    return found


def session_owner(state: MutableMapping[str, Any]) -> str:
    if OWNER not in state:
        state[OWNER] = uuid.uuid4().hex
    return state[OWNER]


def current_dataset(state: MutableMapping[str, Any],
                    settings: Settings) -> Dataset | None:
    """The session's upload, if it still exists (it may have expired)."""
    dataset_id = state.get(DATASET_ID)
    if not dataset_id:
        return None
    found = store(settings).get(dataset_id, session_owner(state))
    if found is None:
        state[DATASET_ID] = None
    return found


def drop_session_data(state: MutableMapping[str, Any], settings: Settings) -> None:
    """Delete everything this session uploaded (idle timeout, remove button)."""
    if OWNER in state:
        store(settings).remove_owner(state[OWNER])
    state[DATASET_ID] = None


def choices(settings: Settings) -> list[str]:
    first = DATABASE if settings.database_url else NORTHSTAR
    return [first, UPLOAD] if settings.uploads_enabled else [first]


def current_source(state: MutableMapping[str, Any],
                   settings: Settings) -> DataSource | None:
    """What a question in this session is answered from. None is NorthStar,
    which keeps its own, unchanged path through the app."""
    choice = state.get(DATA_CHOICE) or choices(settings)[0]
    if choice == UPLOAD:
        dataset = current_dataset(state, settings)
        return upload_source(dataset, settings) if dataset else None
    if choice == DATABASE and settings.database_url:
        return postgres_source(settings)
    return None


def handle_upload(files: list[tuple[str, bytes]], *,
                  state: MutableMapping[str, Any],
                  settings: Settings, describe: Describer,
                  allows_upload: Callable[[], bool] = lambda: True,
                  charge_upload: Callable[[], None] = lambda: None,
                  progress: Callable[[str], None] | None = None,
                  ) -> tuple[Dataset | None, str | None, str | None]:
    """Ingest one upload (one or several files) for this session. Returns
    (dataset, notice, error):
    a notice is information (descriptions could not be drafted), an error
    means nothing was stored. The session's previous upload is deleted."""
    if state.get(UPLOADS_USED, 0) >= UPLOADS_PER_SESSION:
        return None, None, UPLOAD_LIMIT_MESSAGE
    if not allows_upload():
        return None, None, DAILY_UPLOAD_MESSAGE
    owner = session_owner(state)
    the_store = store(settings)
    the_store.remove_owner(owner)  # one dataset per session
    state[DATASET_ID] = None
    try:
        dataset = ingest(the_store, owner=owner, files=files, settings=settings,
                         progress=progress)
    except UploadError as exc:
        return None, None, str(exc)
    state[DATASET_ID] = dataset.id
    state[UPLOADS_USED] = state.get(UPLOADS_USED, 0) + 1
    charge_upload()
    if progress is not None:
        progress("Drafting descriptions of the tables and columns")
    tools = upload_source(dataset, settings).tool_context(settings)
    notes, notice = describe(tools)
    if notes is not None:
        dataset = the_store.update(dataset.id, owner, notes=notes) or dataset
    return dataset, notice, None


def edited_joins(frame: pd.DataFrame, catalog: Any,
                 original: tuple[Join, ...]) -> tuple[tuple[Join, ...], list[str]]:
    """Joins from the editor: kept rows only, each checked. Returns the
    joins and the reasons any row was refused."""
    known = {j.sql(): j for j in original}
    joins: list[Join] = []
    problems: list[str] = []
    for row in frame.to_dict("records"):
        if not row.get("keep", True):
            continue
        fields = [str(row.get(k) or "").strip() for k in
                  ("from_table", "from_column", "to_table", "to_column")]
        if not all(fields):
            if any(fields):
                problems.append("A join row is incomplete and was left out.")
            continue
        candidate = Join(*fields, reason="added or edited by you", inferred=False)
        candidate = known.get(candidate.sql(), candidate)
        problem = check_join(candidate, catalog)
        if problem:
            problems.append(f"{candidate.sql()}: {problem}.")
        elif candidate not in joins:
            joins.append(candidate)
    return tuple(joins), problems


def _join_frame(joins: tuple[Join, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"keep": True, "from_table": j.from_table, "from_column": j.from_column,
          "to_table": j.to_table, "to_column": j.to_column, "why": j.reason}
         for j in joins],
        columns=["keep", "from_table", "from_column", "to_table", "to_column", "why"])


def _editors(source: DataSource, *, key: str) -> tuple[Notes, tuple[Join, ...],
                                                       list[str]] | None:
    """The description and join editors; the edited values when saved."""
    st.markdown("**Descriptions** (drafted by the model, edit as needed)")
    notes_frame = st.data_editor(
        pd.DataFrame(source.notes.rows(source.catalog)), hide_index=True,
        disabled=["table", "column"], key=f"notes_{key}", width="stretch")
    st.markdown("**Joins** (found from column names and the data; untick to "
                "remove, add a row to add one)")
    joins_frame = st.data_editor(
        _join_frame(source.joins), hide_index=True, num_rows="dynamic",
        disabled=["why"], key=f"joins_{key}", width="stretch")
    if not st.button("Save descriptions and joins", key=f"save_{key}"):
        return None
    notes = Notes.from_rows(notes_frame.to_dict("records"), source.catalog)
    joins, problems = edited_joins(joins_frame, source.catalog, source.joins)
    return notes, joins, problems


def render(*, settings: Settings, describe: Describer,
           allows_upload: Callable[[], bool], charge_upload: Callable[[], None],
           ) -> DataSource | None:
    """Draw the panel in the sidebar; return the session's source."""
    state = st.session_state
    options = choices(settings)
    with st.sidebar, st.expander(EXPANDER):
        choice = st.radio("Answer questions from", options, key=DATA_CHOICE)
        if choice == UPLOAD:
            _render_upload(state, settings, describe, allows_upload, charge_upload)
        elif choice == DATABASE:
            _render_database(settings, describe)
    return current_source(state, settings)


def _render_upload(state: MutableMapping[str, Any], settings: Settings,
                   describe: Describer, allows_upload: Callable[[], bool],
                   charge_upload: Callable[[], None]) -> None:
    minutes = max(1, settings.dataset_ttl_seconds // 60)
    st.markdown(PRIVACY_NOTE.format(minutes=minutes))
    st.caption(f"One or more CSV files (one table each) or a SQLite file, up to "
               f"{settings.max_upload_mb:g} MB in all; or an Excel workbook (.xlsx, "
               f"one table per sheet) up to {settings.max_excel_upload_mb:g} MB.")
    uploaded = st.file_uploader("CSV, Excel or SQLite", type=list(EXTENSIONS),
                                key="upload", accept_multiple_files=True)
    seen = tuple(sorted(f.file_id for f in uploaded or []))
    if seen and state.get(UPLOAD_SEEN) != seen:
        state[UPLOAD_SEEN] = seen
        # A status box whose label is the step in progress: a 10 MB workbook
        # takes about 16 seconds, and a bare spinner for that long looks stuck.
        with st.status(PROGRESS_START, expanded=False) as status:
            _, notice, error = handle_upload(
                [(f.name, f.getvalue()) for f in uploaded], state=state,
                settings=settings, describe=describe, allows_upload=allows_upload,
                charge_upload=charge_upload,
                progress=lambda message: status.update(label=f"{message}..."))
            status.update(label=PROGRESS_FAILED if error else PROGRESS_DONE,
                          state="error" if error else "complete")
        if error:
            st.error(error)
        if notice:
            st.warning(notice)
    dataset = current_dataset(state, settings)
    if dataset is None:
        return
    st.success(f"Using {dataset.label}: {len(dataset.tables)} table(s), "
               f"{', '.join(dataset.tables)}.")
    source = upload_source(dataset, settings)
    saved = _editors(source, key=f"{dataset.id}_{dataset.revision}")
    if saved is not None:
        notes, joins, problems = saved
        store(settings).update(dataset.id, session_owner(state), notes=notes,
                               joins=joins)
        for problem in problems:
            st.warning(problem)
        st.rerun()
    if st.button("Remove my data", key="remove_data"):
        drop_session_data(state, settings)
        st.rerun()


def _render_database(settings: Settings, describe: Describer) -> None:
    source = postgres_source(settings)
    st.caption(f"Connected read-only to {source.label}: "
               f"{len(source.catalog.tables)} table(s).")
    if not source.notes.tables and st.button("Draft descriptions with the model",
                                             key="draft_db"):
        notes, notice = describe(source.tool_context(settings))
        if notice:
            st.warning(notice)
        if notes is not None:
            update_postgres(settings, notes=notes)
            st.rerun()
    saved = _editors(source, key=source.key.replace(":", "_"))
    if saved is not None:
        notes, joins, problems = saved
        update_postgres(settings, notes=notes, joins=joins)
        for problem in problems:
            st.warning(problem)
        st.rerun()
