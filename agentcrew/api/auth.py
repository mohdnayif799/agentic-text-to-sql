"""API keys: who is calling.

Keys come from AGENTCREW_API_KEYS (on Cloud Run, a Secret Manager secret),
separated by commas or whitespace. Three rules:

* The comparison is constant-time. The presented key and every configured
  key are hashed with SHA-256 and the digests compared with
  hmac.compare_digest, against every configured key with no early exit, so
  the time taken says nothing about how much of a key was right or which
  one matched. Hashing first also makes every comparison the same length.
* A key is never logged, stored or echoed. After start-up this module keeps
  only the digests. A caller is named in the logs by the first 12 hex
  characters of its key's digest (`Client.id`), which finds one caller's
  lines without revealing the key.
* A caller's datasets are bound to the full digest (`Client.owner`), so two
  keys can never see each other's data, and rotating a key orphans its
  datasets (they expire).

Keys shorter than MIN_KEY_CHARS are ignored: a short key can be guessed,
and its digest in the logs could be reversed. Generate one with
`openssl rand -hex 32`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

MIN_KEY_CHARS = 24
MAX_KEY_CHARS = 256
"""Longer than any real key; a longer header is refused without hashing it."""
HEADER = b"x-api-key"
ID_CHARS = 12


@dataclass(frozen=True)
class Client:
    """An authenticated caller. Holds no key."""

    id: str
    """For logs: the first 12 hex characters of the key's SHA-256."""
    owner: str
    """What the dataset store binds this caller's datasets to."""


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest()


class KeyRing:
    """The configured keys, as digests."""

    def __init__(self, raw: str | None) -> None:
        digests: list[bytes] = []
        self.ignored = 0
        """Configured keys left out for being too short."""
        for key in re.split(r"[,\s]+", raw or ""):
            if not key:
                continue
            if len(key) < MIN_KEY_CHARS:
                self.ignored += 1
                continue
            digests.append(_digest(key))
        self._digests = tuple(dict.fromkeys(digests))

    def __len__(self) -> int:
        return len(self._digests)

    def identify(self, presented: str | None) -> Client | None:
        """The caller for a presented key, or None. Constant-time in the key:
        every configured digest is compared, whether or not one matched."""
        if not presented or len(presented) > MAX_KEY_CHARS:
            return None
        digest = _digest(presented)
        matched = False
        for known in self._digests:
            matched |= hmac.compare_digest(digest, known)
        if not matched:
            return None
        text = digest.hex()
        return Client(id=text[:ID_CHARS], owner=f"api:{text}")


def presented_key(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The X-API-Key header of an ASGI request, if it was sent exactly once
    (two different values would make "which key" ambiguous: refuse)."""
    values = [value for name, value in headers if name.lower() == HEADER]
    if len(values) != 1:
        return None
    return values[0].decode("latin-1").strip()
