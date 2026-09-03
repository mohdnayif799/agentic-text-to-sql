"""Tests for the UI helpers and the evaluation grader.

These exist because both contain version-sensitive logic that failed silently
once already (pandas 3.0 changed string columns from `object` to `str` dtype,
which disabled chart rendering without raising anything).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ui = _load("ui_app", ROOT / "app.py")
ev = _load("eval_runner", ROOT / "scripts" / "run_eval.py")

from agentcrew.schemas import ExecutionResult  # noqa: E402


def result(columns, rows, ok=True) -> ExecutionResult:
    return ExecutionResult(
        ok=ok, sql="x", columns=columns, rows=rows, row_count=len(rows)
    )


class TestUIHelpers:
    def test_to_frame_builds_labelled_frame(self) -> None:
        df = ui.to_frame(result(["region", "n"], [["APAC", 44]]))
        assert list(df.columns) == ["region", "n"]
        assert df.iloc[0]["region"] == "APAC"

    def test_to_frame_handles_no_columns(self) -> None:
        assert ui.to_frame(result([], [])).empty

    def test_label_detection_works_on_pandas_3(self) -> None:
        """Regression: pandas 3.0 string columns are `str`, not `object`."""
        df = pd.DataFrame([["APAC", 44], ["EMEA", 11]], columns=["region", "n"])
        labels = [
            c
            for c in df.columns
            if pd.api.types.is_string_dtype(df[c])
            and not pd.api.types.is_numeric_dtype(df[c])
        ]
        numbers = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        assert labels == ["region"]
        assert numbers == ["n"]

    def test_maybe_chart_is_safe_on_empty(self) -> None:
        ui.maybe_chart(pd.DataFrame())  # must not raise


class TestGrader:
    def test_scalar_exact_match(self) -> None:
        ok, _ = ev.grade("scalar", result(["n"], [[1400]]), result(["n"], [[1400]]))
        assert ok

    def test_scalar_respects_tolerance(self) -> None:
        agent = result(["n"], [[100.5]])
        ref = result(["n"], [[100.0]])
        assert ev.grade("scalar", agent, ref, tolerance=0.01)[0]
        assert not ev.grade("scalar", agent, ref, tolerance=0.0)[0]

    def test_scalar_finds_the_number_in_a_wider_row(self) -> None:
        agent = result(["label", "n"], [["total", 1400]])
        assert ev.grade("scalar", agent, result(["n"], [[1400]]))[0]

    def test_top_label_is_order_sensitive(self) -> None:
        ref = result(["r", "n"], [["APAC", 44], ["EMEA", 11]])
        right = result(["r", "n"], [["APAC", 44], ["EMEA", 11]])
        wrong = result(["r", "n"], [["EMEA", 11], ["APAC", 44]])
        assert ev.grade("top_label", right, ref)[0]
        assert not ev.grade("top_label", wrong, ref)[0]

    def test_label_set_allows_supersets(self) -> None:
        ref = result(["c"], [["Hardware"], ["Software"]])
        agent = result(["c"], [["Hardware"], ["Software"], ["Services"]])
        assert ev.grade("label_set", agent, ref)[0]

    def test_label_set_rejects_missing_labels(self) -> None:
        ref = result(["c"], [["Hardware"], ["Software"]])
        agent = result(["c"], [["Hardware"]])
        assert not ev.grade("label_set", agent, ref)[0]

    def test_failed_query_never_passes(self) -> None:
        bad = ExecutionResult(ok=False, sql="x", error_message="boom")
        assert not ev.grade("scalar", bad, result(["n"], [[1]]))[0]

    def test_empty_result_never_passes(self) -> None:
        assert not ev.grade("nonempty", result(["n"], []), result(["n"], [[1]]))[0]

    def test_missing_agent_result_never_passes(self) -> None:
        assert not ev.grade("scalar", None, result(["n"], [[1]]))[0]

    @pytest.mark.parametrize("check", ["scalar", "top_label", "label_set", "nonempty"])
    def test_all_checks_reject_a_none_result(self, check: str) -> None:
        assert not ev.grade(check, None, result(["n"], [[1]]))[0]
