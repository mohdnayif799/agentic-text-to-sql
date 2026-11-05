"""Dev probes stay ungraded and apart from every question set. Offline."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from agentcrew.llm import FakeClient
from evalrun.runner import RunConfig, Runner
from evalrun.sets import ALL_SET_FILES, load_questions

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location(
        "run_dev_probes_under_test", ROOT / "scripts" / "run_dev_probes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return load_script()


def words(text: str) -> set[str]:
    return {w.strip(".,?'\"").lower() for w in text.split()}


class TestApartFromTheHoldouts:
    def test_every_probe_is_marked(self, script):
        probes = script.load_probes()
        assert probes and all(p["id"].startswith("probe-") for p in probes)

    def test_an_unmarked_question_is_refused(self, script, tmp_path):
        path = tmp_path / "probes.yaml"
        path.write_text(yaml.safe_dump([{"id": "ha01", "question": "q"}]), "utf-8")
        with pytest.raises(ValueError, match="not marked"):
            script.load_probes(path)

    def test_the_file_is_not_a_question_set(self):
        assert all("dev_probes" not in str(path) for path in ALL_SET_FILES.values())

    @pytest.mark.parametrize("name", ["holdout_agent", "holdout_easy"])
    def test_no_probe_repeats_a_holdout_question(self, script, name):
        texts = [words(q["question"]) for q in load_questions(name)]
        for probe in script.load_probes():
            mine = words(probe["question"])
            for text in texts:
                overlap = len(mine & text) / len(mine | text)
                assert overlap < 0.5, probe["id"]


def test_a_probe_runs_and_keeps_its_ungraded_label(script, tmp_path, db_path):
    def respond(system, user):
        if "You check whether one query result" in system:
            return {"needs_followup": False, "reason": ""}
        if "final answer" in system:
            return {"answer": "Scripted.", "key_numbers": [], "caveats": []}
        return {"sql": "SELECT COUNT(*) AS n FROM orders", "rationale": "x",
                "expected_shape": ""}

    cfg = replace(RunConfig.load(), cache_dir=tmp_path / "cache")
    runner = Runner(cfg, live_factory=lambda s: FakeClient(responder=respond),
                    log=lambda *_: None)
    probe = {"id": "probe-x", "question": "How many orders are there?"}
    trace = script.run_probe(runner, probe, tmp_path / "run")
    saved = json.loads((tmp_path / "run" / "probe-x.json").read_text("utf-8"))
    assert trace["answer"] == "Scripted." and saved["path"] == "direct"
    assert "never part of any holdout" in saved["note"]
    assert "score" not in saved
