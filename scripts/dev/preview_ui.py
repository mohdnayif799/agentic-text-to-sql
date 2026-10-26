"""Preview the Streamlit app on real cached outcomes, with no model calls.

Development only: scripts/ is not in the Docker image (.dockerignore allows
just scripts/build_database.py).

It starts the real app.py, in this process, with the agent replaced by the
outcomes D actually produced in the evaluation runs. Every question in the
traces below gets its recorded answer, result table, steps, repairs, LLM call
count and live latency back, so the page is rendered by the app's own code
(answer -> execute -> render_reply) exactly as a visitor would see it.

  eval/results/phase5/*/traces/full/*.json   the final holdout runs (committed)
  eval/runs/phase4d-dev2/traces/full/dv01.json
      the fuzzy value search example chip. No holdout question escalated to
      search_values, so this one comes from the local dev2 run that
      tests/test_chat_ui.py pins for that chip (eval/runs/ is gitignored, so
      it is skipped when absent).

Run from the repo root (it changes into it anyway, so .streamlit/config.toml
and static/ resolve as they do for "streamlit run app.py"):

    .venv/Scripts/python.exe scripts/dev/preview_ui.py --port 8601

Then click an example chip, or type any question from those traces. A
question with no cached outcome fails the way a failed demo run does.

Keys: the .env file is never read here and the only key set is a dummy, so
the demo-mode path (server key configured, 5 runs per session) is what
renders, and nothing can reach a provider.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agentcrew.archs import ArchOutcome, app_runner  # noqa: E402
from agentcrew.archs.app_runner import AppRun  # noqa: E402
from agentcrew.archs.outcome import RepairEvent  # noqa: E402
from agentcrew.config import Settings  # noqa: E402
from agentcrew.schemas import ExecutionResult  # noqa: E402

TRACE_GLOBS = (
    "eval/results/phase5/phase5-holdout-*/traces/full/*.json",
    "eval/runs/phase4d-dev2/traces/full/dv01.json",
)

# What the Docker image sets (see the Dockerfile), plus a dummy server key so
# the app takes the demo path. The value is never sent anywhere.
PREVIEW_ENV = {
    "AGENTCREW_PROVIDER": "gemini",
    "AGENTCREW_MODEL": "gemini-3.5-flash-lite",
    "AGENTCREW_GEMINI_API_KEY": "preview-dummy-key-not-real",
}


def _result(record: dict[str, Any] | None) -> ExecutionResult | None:
    """A trace's result record. Repair records keep only the row count."""
    if not record:
        return None
    rows = record.get("rows")
    return ExecutionResult(
        ok=record["ok"],
        sql=record.get("sql", ""),
        columns=record.get("columns", []),
        rows=rows if isinstance(rows, list) else [],
        row_count=record.get("row_count",
                             rows if isinstance(rows, int) else 0),
        error_message=record.get("error"),
    )


def to_app_run(trace: dict[str, Any]) -> AppRun:
    """Rebuild what run_arch_question returned live, from its trace."""
    repairs = tuple(
        RepairEvent(step=r["step"], signal=r["signal"], detail=r["detail"],
                    before=_result(r["before"]), after=_result(r.get("after")),
                    accepted=r["accepted"], attempts=r.get("attempts", 1),
                    via=r.get("via") or "repair")
        for r in trace["repairs"]
    )
    outcome = ArchOutcome(
        arch=trace["arch"], question=trace["question"], status=trace["status"],
        answer=trace["answer"], selected=_result(trace.get("selected_result")),
        path=trace["path"], plan=tuple(trace["plan"]), steps=tuple(trace["steps"]),
        key_numbers=tuple(trace["key_numbers"]), caveats=tuple(trace["caveats"]),
        stop_reason=trace["stop_reason"], error=trace["error"], repairs=repairs,
        selected_step=trace["selected_step"],
    )
    # latency_live_s is the run's original wall time; latency_s is the replay.
    seconds = trace.get("latency_live_s") or trace["latency_s"]
    return AppRun(outcome, llm_calls=len(trace["llm_calls"]), seconds=seconds)


def load_traces() -> dict[str, dict[str, Any]]:
    """Every cached trace, by its question text."""
    traces = {}
    for pattern in TRACE_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            trace = json.loads(path.read_text(encoding="utf-8"))
            traces[trace["question"].strip()] = trace
    return traces


class _Counter:
    calls = 0


class _Runtime:
    def new_client(self) -> _Counter:
        return _Counter()


def install_fake_agent(traces: dict[str, dict[str, Any]]) -> None:
    """Replace the two entry points app.py imports from app_runner.

    app.py runs `from agentcrew.archs.app_runner import ...` on every rerun,
    which reads these module attributes, so the patch reaches the app.
    """

    def build_runtime(settings: Settings, *_: Any, **__: Any) -> _Runtime:
        return _Runtime()

    def run_arch_question(question: str, *, settings: Settings, runtime: Any,
                          client: Any = None, on_stage: Any = None) -> AppRun:
        trace = traces.get(question.strip())
        if trace is None:
            raise LookupError("no cached outcome for this question")
        run = to_app_run(trace)
        if client is not None:
            client.calls = run.llm_calls
        return run

    app_runner.build_runtime = build_runtime
    app_runner.run_arch_question = run_arch_question


def isolate_settings() -> None:
    """No .env, no developer AGENTCREW_* variables: only PREVIEW_ENV."""
    for name in list(os.environ):
        if name.startswith("AGENTCREW_"):
            del os.environ[name]
    os.environ.update(PREVIEW_ENV)
    Settings.model_config["env_file"] = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8601)
    args = parser.parse_args()

    os.chdir(ROOT)
    isolate_settings()
    traces = load_traces()
    install_fake_agent(traces)
    print(f"Cached outcomes for {len(traces)} questions.")

    from streamlit.web import cli

    # The same entry point as "streamlit run app.py". Localhost only: this is
    # a development server. No file watcher: a reload could re-import
    # app_runner and drop the fake agent.
    cli.main([
        "run", str(ROOT / "app.py"),
        "--server.address=localhost",
        f"--server.port={args.port}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
    ])


if __name__ == "__main__":
    main()
