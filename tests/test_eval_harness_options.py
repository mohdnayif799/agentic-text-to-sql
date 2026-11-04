"""Two harness options for a clean, fully costed run: --cache-dir and recorded
thinking tokens. Neither may affect a cache key or a score."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from agentcrew.llm import LLMClient, LLMResponse, Usage
from evalrun.cache import CachingClient, prompt_hash
from evalrun.runner import RUN_CONFIG, RunConfig

ROOT = Path(__file__).resolve().parents[1]


class ThinkingClient(LLMClient):
    name = "thinking"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system, user, max_tokens=2048, temperature=0.0):
        self.calls += 1
        return LLMResponse(text="reply", model="m",
                           usage=Usage(10, 5, calls=1, thoughts_tokens=7))


@pytest.fixture
def client(tmp_path):
    live = ThinkingClient()

    def make():
        return CachingClient(lambda: live, cache_dir=tmp_path, arch="full", qid="q1",
                             model="m")
    make.live = live
    return make


class TestThinkingTokens:
    def test_a_live_call_records_them(self, client):
        cached = client()
        cached.complete(system="s", user="u")
        assert cached.records[0].thoughts_tokens == 7
        assert asdict(cached.records[0])["thoughts_tokens"] == 7

    def test_a_replayed_call_reports_them_too(self, client):
        client().complete(system="s", user="u")
        replay = client()
        response = replay.complete(system="s", user="u")
        assert client.live.calls == 1
        assert replay.records[0].cache_hit
        assert replay.records[0].thoughts_tokens == 7
        assert response.usage.thoughts_tokens == 7

    def test_an_entry_saved_before_they_were_recorded_is_still_a_hit(self, tmp_path):
        """The archived Phase 5 cache has no such field; its replies stay valid."""
        key = prompt_hash("s", "u", 2048, 0.0)
        folder = tmp_path / "m" / "full" / "q1"
        folder.mkdir(parents=True)
        (folder / f"{key}.json").write_text(json.dumps(
            {"model": "m", "role": "s", "text": "old", "input_tokens": 1,
             "output_tokens": 2, "latency_ms": 3.0}), "utf-8")
        old = CachingClient(None, cache_dir=tmp_path, arch="full", qid="q1", model="m")
        assert old.complete(system="s", user="u").text == "old"
        assert old.records[0].thoughts_tokens == 0

    def test_the_cache_key_is_unchanged(self, client, tmp_path):
        """Pinned: a different key would orphan every saved reply."""
        assert prompt_hash("s", "u", 2048, 0.0) == (
            "808a05668ee3350bdc95ebf111ab03f4be1a7c94124d579d9d0eb20f4a40fbf0")
        client().complete(system="s", user="u")
        names = [p.stem for p in tmp_path.rglob("*.json")]
        assert names == [prompt_hash("s", "u", 2048, 0.0)]


def load_script():
    spec = importlib.util.spec_from_file_location(
        "run_eval_v2_under_test", ROOT / "scripts" / "run_eval_v2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCacheDir:
    def test_the_default_is_the_run_config(self):
        assert load_script().run_config(None) == RunConfig.load()

    def test_a_relative_folder_is_under_the_repo(self):
        cfg = load_script().run_config("eval/runs/some-cache")
        assert cfg == replace(RunConfig.load(),
                              cache_dir=ROOT / "eval" / "runs" / "some-cache")

    def test_an_absolute_folder_stays_as_given(self, tmp_path):
        assert load_script().run_config(str(tmp_path)).cache_dir == tmp_path

    def test_the_rest_of_the_configuration_is_untouched(self, tmp_path):
        before = RUN_CONFIG.read_bytes()
        cfg = load_script().run_config(str(tmp_path))
        assert replace(cfg, cache_dir=RunConfig.load().cache_dir) == RunConfig.load()
        assert RUN_CONFIG.read_bytes() == before

    def test_a_folder_with_no_replies_answers_nothing_from_cache(self, tmp_path):
        live = ThinkingClient()
        fresh = CachingClient(lambda: live, cache_dir=tmp_path / "empty", arch="full",
                              qid="q1", model="m")
        fresh.complete(system="s", user="u")
        assert fresh.live_calls == 1 and fresh.cache_hits == 0
