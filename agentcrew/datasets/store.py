"""Per-session uploaded datasets on local disk.

Where uploads live
------------------
On Cloud Run the filesystem is an in-memory tmpfs: every byte written under
/tmp counts against the instance's 1 GiB, and everything vanishes when the
instance scales to zero. That is the right home for data that must last for
one session only and must never be kept: nothing is written to a bucket, a
database or a log. The layout is one folder per dataset,
``<upload_dir>/<dataset id>/data.db``.

What bounds it
--------------
* the size of each upload (ingest.py) and of all of them together
  (``upload_store_max_mb``): a new dataset that would pass the total is
  refused until others are gone;
* expiry: a dataset unused for ``dataset_ttl_seconds`` is deleted by a
  background sweeper, whether or not its tab is still open, so a closed tab
  cannot leave data behind;
* the session: replacing a dataset, "Remove my data" and the idle guard each
  delete it at once.

The store is process-wide. The live service runs at most one instance, so
one process holds every session's data; a dataset id is bound to the owner
that created it (a browser session, later an API key), and `get` refuses
anyone else.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentcrew.datasets.describe import Notes, empty_notes
from agentcrew.datasets.joins import Join

SWEEP_SECONDS = 60
DB_NAME = "data.db"


@dataclass(frozen=True)
class Dataset:
    id: str
    owner: str
    label: str
    """The file name the visitor uploaded."""
    kind: str
    """csv, excel or sqlite."""
    path: Path
    size_bytes: int
    tables: tuple[str, ...]
    notes: Notes = field(default_factory=empty_notes)
    joins: tuple[Join, ...] = ()
    revision: int = 0
    """Bumped on every edit of notes or joins, so cached context is rebuilt."""

    @property
    def key(self) -> str:
        return f"upload:{self.id}:{self.revision}"


class StoreFull(Exception):
    """All datasets together would pass the store's limit."""


class DatasetStore:
    """Thread-safe registry of datasets and their folders."""

    def __init__(self, root: Path, *, max_bytes: int, ttl_seconds: float,
                 clock: Callable[[], float] = time.monotonic,
                 sweep: bool = True) -> None:
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._datasets: dict[str, Dataset] = {}
        self._last_used: dict[str, float] = {}
        self._on_remove: list[Callable[[Dataset], None]] = []
        self.root.mkdir(parents=True, exist_ok=True)
        if sweep:
            threading.Thread(target=self._sweep_forever, name="dataset-sweeper",
                             daemon=True).start()

    # -- folders -----------------------------------------------------------
    def new_folder(self) -> tuple[str, Path]:
        """A fresh id and an empty folder for a dataset being created."""
        dataset_id = uuid.uuid4().hex
        folder = self.root / dataset_id
        folder.mkdir(parents=True)
        return dataset_id, folder

    def discard_folder(self, dataset_id: str) -> None:
        shutil.rmtree(self.root / dataset_id, ignore_errors=True)

    # -- registry ----------------------------------------------------------
    def total_bytes(self) -> int:
        with self._lock:
            return sum(d.size_bytes for d in self._datasets.values())

    def remaining_bytes(self) -> int:
        """Room left under the store's limit (never negative). ingest checks
        it before converting and uses it to cap the file being written."""
        return max(0, self.max_bytes - self.total_bytes())

    def add(self, dataset: Dataset) -> Dataset:
        """Register a converted dataset. Raises StoreFull (and deletes its
        folder) when it does not fit."""
        with self._lock:
            used = sum(d.size_bytes for d in self._datasets.values())
            if used + dataset.size_bytes > self.max_bytes:
                full = True
            else:
                full = False
                self._datasets[dataset.id] = dataset
                self._last_used[dataset.id] = self._clock()
        if full:
            self.discard_folder(dataset.id)
            raise StoreFull("the dataset store is full")
        return dataset

    def get(self, dataset_id: str | None, owner: str) -> Dataset | None:
        """The dataset if it exists and belongs to `owner`; marks it used."""
        if not dataset_id:
            return None
        with self._lock:
            found = self._datasets.get(dataset_id)
            if found is None or found.owner != owner:
                return None
            self._last_used[dataset_id] = self._clock()
            return found

    def update(self, dataset_id: str, owner: str, **changes: Any) -> Dataset | None:
        """A new Dataset with `changes` (notes, joins) and the next revision."""
        with self._lock:
            found = self._datasets.get(dataset_id)
            if found is None or found.owner != owner:
                return None
            updated = replace(found, **changes, revision=found.revision + 1)
            self._datasets[dataset_id] = updated
            self._last_used[dataset_id] = self._clock()
            return updated

    def owned_by(self, owner: str) -> list[Dataset]:
        with self._lock:
            return [d for d in self._datasets.values() if d.owner == owner]

    def remove(self, dataset_id: str | None) -> bool:
        if not dataset_id:
            return False
        with self._lock:
            found = self._datasets.pop(dataset_id, None)
            self._last_used.pop(dataset_id, None)
            hooks = list(self._on_remove)
        if found is None:
            return False
        for hook in hooks:
            try:
                hook(found)
            except Exception:  # noqa: S110 - a cache hook must not block deletion
                pass
        self.discard_folder(dataset_id)
        return True

    def remove_owner(self, owner: str) -> int:
        return sum(self.remove(d.id) for d in self.owned_by(owner))

    def on_remove(self, hook: Callable[[Dataset], None]) -> None:
        """Called with each removed dataset, before its files are deleted
        (closes engines and forgets cached context for it)."""
        with self._lock:
            self._on_remove.append(hook)

    # -- expiry ------------------------------------------------------------
    def sweep(self) -> list[str]:
        """Remove every dataset unused for longer than the ttl."""
        now = self._clock()
        with self._lock:
            stale = [i for i, t in self._last_used.items() if now - t > self.ttl]
        return [i for i in stale if self.remove(i)]

    def _sweep_forever(self) -> None:
        while True:
            time.sleep(SWEEP_SECONDS)
            try:
                self.sweep()
            except Exception:  # noqa: S110 - keep sweeping; the next pass retries
                pass


_store: DatasetStore | None = None
_store_lock = threading.Lock()
_DATASET_FOLDER = re.compile(r"[0-9a-f]{32}")


def remove_leftovers(root: Path) -> int:
    """Delete dataset folders under `root` from an earlier process. Only
    folders named like a dataset id are touched, so a misconfigured
    upload_dir (a home directory, say) loses nothing else."""
    if not root.is_dir():
        return 0
    removed = 0
    for child in root.iterdir():
        if child.is_dir() and _DATASET_FOLDER.fullmatch(child.name):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def dataset_store(settings: Any) -> DatasetStore:
    """The process-wide store, built from the first settings seen. Folders a
    previous process left behind (a restart without a cold start) are
    deleted: their sessions no longer exist."""
    global _store
    with _store_lock:
        if _store is None:
            root = Path(settings.upload_dir)
            remove_leftovers(root)
            _store = DatasetStore(
                root, max_bytes=int(settings.upload_store_max_mb * 1e6),
                ttl_seconds=settings.dataset_ttl_seconds)
        return _store


def reset_dataset_store() -> None:
    """Test helper: forget the store (its folders are left to the test)."""
    global _store
    with _store_lock:
        _store = None
