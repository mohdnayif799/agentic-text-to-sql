"""On-disk LLM response cache, so reruns spend no quota.

Key: architecture, question id, model and a SHA-256 of the exact prompt
(system, user, max_tokens, temperature). One JSON file per response under
<cache_dir>/<model>/<arch>/<question id>/<prompt hash>.json. A hit returns
the stored text; a miss calls the live client (with backoff) and stores it
together with its latency, so a replayed call still reports how long the
live call took (original_latency_ms).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agentcrew.llm import LLMClient, LLMResponse, Usage
from evalrun.backoff import call_with_backoff

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class CacheMiss(RuntimeError):
    """Offline mode and the response is not cached."""


@dataclass(frozen=True)
class CallRecord:
    role: str
    prompt_hash: str
    cache_hit: bool
    latency_ms: float
    input_tokens: int
    output_tokens: int
    original_latency_ms: float | None = None
    """For a cache hit, the latency of the live call that filled the cache
    (None if it was cached before latencies were stored)."""


def prompt_hash(system: str, user: str, max_tokens: int, temperature: float) -> str:
    payload = json.dumps(
        {"system": system, "user": user, "max_tokens": max_tokens,
         "temperature": temperature},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _role(system: str) -> str:
    first = system.strip().splitlines()[0] if system.strip() else ""
    return first[:80]


class CachingClient(LLMClient):
    """Wraps a live client for one (architecture, question) pair."""

    name = "cache"

    def __init__(
        self,
        live: Callable[[], LLMClient] | None,
        *,
        cache_dir: Path,
        arch: str,
        qid: str,
        model: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._live = live
        self._dir = cache_dir / _UNSAFE.sub("_", model) / _UNSAFE.sub("_", arch) / qid
        self._model = model
        self._sleep = sleep
        self.records: list[CallRecord] = []
        self.slept_seconds = 0.0

    @property
    def calls(self) -> int:
        return len(self.records)

    @property
    def live_calls(self) -> int:
        return sum(1 for r in self.records if not r.cache_hit)

    @property
    def cache_hits(self) -> int:
        return sum(1 for r in self.records if r.cache_hit)

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        key = prompt_hash(system, user, max_tokens, temperature)
        path = self._dir / f"{key}.json"
        if path.exists():
            data = json.loads(path.read_text("utf-8"))
            self.records.append(CallRecord(
                _role(system), key, True, 0.0,
                data["input_tokens"], data["output_tokens"],
                data.get("latency_ms"),
            ))
            return LLMResponse(
                text=data["text"],
                usage=Usage(data["input_tokens"], data["output_tokens"], calls=1),
                model=data["model"],
            )
        if self._live is None:
            raise CacheMiss(f"no cached response for {self._dir.name}/{key[:12]}")

        live = self._live()
        started = time.perf_counter()
        response, slept = call_with_backoff(
            lambda: live.complete(system=system, user=user, max_tokens=max_tokens,
                                  temperature=temperature),
            sleep=self._sleep,
        )
        self.slept_seconds += slept
        latency_ms = (time.perf_counter() - started - slept) * 1000
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "model": response.model or self._model,
            "role": _role(system),
            "text": response.text,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "latency_ms": round(latency_ms, 1),
        }, indent=1), "utf-8", newline="\n")
        self.records.append(CallRecord(
            _role(system), key, False, round(latency_ms, 1),
            response.usage.input_tokens, response.usage.output_tokens,
        ))
        return response
