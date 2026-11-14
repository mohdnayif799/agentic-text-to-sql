"""Several chats in one browser session.

A session keeps up to MAX_CHATS chats, oldest first. Each chat owns its
conversation and its data source: its turns, the source its radio selects,
its uploaded dataset, the uploader's last file and the SQL editor's results
under its answers (CHAT_KEYS). Everything else in the session is shared by
all of its chats, on purpose: the demo run count, the SQL run count, the
upload count and the dataset owner id. So "New chat" can never reset a limit;
`ChatState` routes every key to exactly one of the two places.

Chats live in the session only: a reload or the idle guard ends them.

Pure Python over a MutableMapping (st.session_state in the app, a dict in
tests). Nothing here imports Streamlit or touches the dataset store: the
functions that drop chats return them, and the caller deletes their data
(data_panel.remove_chat_data).
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any

CHATS = "chats"
"""Session key: the list of chats, oldest first."""
CURRENT = "current_chat"
"""Session key: the id of the chat on screen."""
NUMBER = "chat_number"
"""Session key: the last chat number given out. Ids are never reused within
a session, so a widget keyed by a chat id cannot inherit an old chat's value."""

MAX_CHATS = 10

TURNS = "turns"
DATASET_ID = "dataset_id"
DATA_CHOICE = "data_choice"
UPLOAD_SEEN = "upload_seen"
SQL_RESULTS = "sql_results"
"""{answer slot: (sql text, EditorRun, row cap)} for the SQL editors."""

CHAT_KEYS = frozenset({TURNS, DATASET_ID, DATA_CHOICE, UPLOAD_SEEN, SQL_RESULTS})
"""The keys that belong to a chat. Every other key is the session's."""

UNTITLED = "Untitled chat"
TITLE_CHARS = 40

OLDEST_REMOVED = (
    "Only {limit} chats are kept, so the oldest one ({title}) was removed"
    "{with_data}."
)


def _fresh(state: MutableMapping[str, Any]) -> dict[str, Any]:
    number = int(state.get(NUMBER, 0)) + 1
    state[NUMBER] = number
    return {"id": f"c{number}", TURNS: [], DATASET_ID: None, DATA_CHOICE: None,
            UPLOAD_SEEN: None, SQL_RESULTS: {}}


def all_chats(state: MutableMapping[str, Any]) -> list[dict[str, Any]]:
    """The session's chats, oldest first; creates the first one if needed."""
    found = state.get(CHATS)
    if not found:
        first = _fresh(state)
        state[CHATS] = [first]
        state[CURRENT] = first["id"]
        return state[CHATS]
    return found


def current(state: MutableMapping[str, Any]) -> dict[str, Any]:
    """The chat on screen. A stale or missing id falls back to the newest."""
    found = all_chats(state)
    wanted = state.get(CURRENT)
    for chat in found:
        if chat["id"] == wanted:
            return chat
    state[CURRENT] = found[-1]["id"]
    return found[-1]


def is_empty(chat: dict[str, Any]) -> bool:
    return not chat[TURNS] and not chat[DATASET_ID]


def start_new(state: MutableMapping[str, Any]) -> list[dict[str, Any]]:
    """Open a new chat on the default data source and return the chats this
    dropped: an empty chat on screen is replaced rather than kept, and past
    MAX_CHATS the oldest goes. The caller deletes their datasets."""
    found = list(all_chats(state))
    on_screen = current(state)
    dropped: list[dict[str, Any]] = []
    if is_empty(on_screen):
        found = [c for c in found if c["id"] != on_screen["id"]]
        dropped.append(on_screen)
    fresh = _fresh(state)
    found.append(fresh)
    while len(found) > MAX_CHATS:
        dropped.append(found.pop(0))
    state[CHATS] = found
    state[CURRENT] = fresh["id"]
    return dropped


def switch(state: MutableMapping[str, Any], chat_id: str) -> bool:
    if any(c["id"] == chat_id for c in all_chats(state)):
        state[CURRENT] = chat_id
        return True
    return False


def delete(state: MutableMapping[str, Any], chat_id: str) -> dict[str, Any] | None:
    """Remove a chat and return it (None if there is no such chat). Deleting
    the chat on screen shows the newest remaining one, or a new empty chat
    when it was the last."""
    found = all_chats(state)
    gone = next((c for c in found if c["id"] == chat_id), None)
    if gone is None:
        return None
    rest = [c for c in found if c["id"] != chat_id]
    if not rest:
        rest = [_fresh(state)]
    state[CHATS] = rest
    if state.get(CURRENT) == chat_id:
        state[CURRENT] = rest[-1]["id"]
    return gone


def reset(state: MutableMapping[str, Any]) -> list[dict[str, Any]]:
    """End every chat (the idle guard) and start one empty chat. Returns the
    chats that were dropped."""
    dropped = list(state.get(CHATS) or [])
    fresh = _fresh(state)
    state[CHATS] = [fresh]
    state[CURRENT] = fresh["id"]
    return dropped


def title(chat: dict[str, Any], asking: str | None = None) -> str:
    """The chat's first question, cut to TITLE_CHARS; UNTITLED before one.
    `asking` is a question being answered right now, the title-to-be of a
    chat that has none yet."""
    first = chat[TURNS][0].question if chat[TURNS] else asking
    if not first:
        return UNTITLED
    text = " ".join(str(first).split())
    if len(text) <= TITLE_CHARS:
        return text
    return text[:TITLE_CHARS].rstrip() + "..."


def eviction_notice(dropped: list[dict[str, Any]]) -> str | None:
    """The notice for a chat that went because the session hit MAX_CHATS.
    An empty chat replaced by "New chat" needs none."""
    lost = [c for c in dropped if not is_empty(c)]
    if not lost:
        return None
    chat = lost[0]
    return OLDEST_REMOVED.format(
        limit=MAX_CHATS, title=f'"{title(chat)}"',
        with_data=", with its uploaded data" if chat[DATASET_ID] else "")


class ChatState(MutableMapping[str, Any]):
    """The session as one chat sees it: CHAT_KEYS read and write the chat on
    screen, every other key the session itself. data_panel works on this
    view, so the upload count it keeps is the session's whatever chat is
    open, while the dataset it stores is the chat's."""

    def __init__(self, state: MutableMapping[str, Any]) -> None:
        self._state = state

    def _home(self, key: str) -> MutableMapping[str, Any]:
        return current(self._state) if key in CHAT_KEYS else self._state

    def __getitem__(self, key: str) -> Any:
        return self._home(key)[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._home(key)[key] = value

    def __delitem__(self, key: str) -> None:
        if key in CHAT_KEYS:
            current(self._state)[key] = {} if key == SQL_RESULTS else (
                [] if key == TURNS else None)
        else:
            del self._state[key]

    def __iter__(self) -> Iterator[str]:
        yield from CHAT_KEYS
        yield from (k for k in list(self._state) if k not in CHAT_KEYS)

    def __len__(self) -> int:
        return len(CHAT_KEYS) + sum(1 for k in list(self._state)
                                    if k not in CHAT_KEYS)

    def __contains__(self, key: object) -> bool:
        if key in CHAT_KEYS:
            return True
        return key in self._state


def view(state: MutableMapping[str, Any]) -> ChatState:
    return ChatState(state)
