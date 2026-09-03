"""Graph topology assertions.

The architecture diagram in `docs/ARCHITECTURE.md` is a claim about the code.
This file turns that claim into a test, so the two cannot drift: adding a node
or an edge without updating the docs fails here.

It also encodes two invariants that matter more than the shape:

* every non-terminal node can reach a terminal node (no dead ends);
* the only edges into `synthesize` come from `advance`, i.e. the agent cannot
  reach the answer-writing stage without committing at least one step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.graph.build import build_deps, build_graph
from agentcrew.llm import FakeClient

LLM_NODES = {"plan", "author_sql", "verify", "repair", "synthesize"}
DETERMINISTIC_NODES = {
    "select_schema",
    "execute_sql",
    "triage",
    "advance",
    "clarify",
    "fail",
}
TERMINALS = {"synthesize", "clarify", "fail"}

EXPECTED_EDGES = {
    ("__start__", "plan"),
    ("plan", "select_schema"),
    ("plan", "clarify"),
    ("plan", "fail"),
    ("select_schema", "author_sql"),
    ("author_sql", "execute_sql"),
    ("author_sql", "fail"),
    ("execute_sql", "triage"),
    ("triage", "verify"),
    ("triage", "repair"),
    ("triage", "fail"),
    ("repair", "execute_sql"),
    ("repair", "fail"),
    ("verify", "advance"),
    ("verify", "repair"),
    ("verify", "fail"),
    ("advance", "author_sql"),
    ("advance", "synthesize"),
    ("synthesize", "__end__"),
    ("clarify", "__end__"),
    ("fail", "__end__"),
}


@pytest.fixture(scope="module")
def compiled(request):
    root = Path(__file__).resolve().parents[1]
    cfg = Settings(
        provider="fake",
        database_path=root / "data" / "northstar.db",
        trace_dir=root / "data" / "traces",
    )
    deps = build_deps(cfg, llm=FakeClient())
    return build_graph(deps).get_graph()


def test_node_set_matches_documentation(compiled) -> None:
    actual = {n for n in compiled.nodes if not n.startswith("__")}
    assert actual == LLM_NODES | DETERMINISTIC_NODES


def test_node_counts_match_the_readme(compiled) -> None:
    assert len(LLM_NODES) == 5
    assert len(DETERMINISTIC_NODES) == 6


def test_edge_set_matches_the_diagram(compiled) -> None:
    actual = {(e.source, e.target) for e in compiled.edges}
    assert actual == EXPECTED_EDGES


def test_no_dead_ends(compiled) -> None:
    """Every node must have an outgoing edge, or the run could hang."""
    sources = {e.source for e in compiled.edges}
    for node in compiled.nodes:
        if node == "__end__":
            continue
        assert node in sources, f"{node} has no outgoing edge"


def test_synthesize_is_only_reachable_from_advance(compiled) -> None:
    """The agent cannot write an answer without committing a step first."""
    incoming = {e.source for e in compiled.edges if e.target == "synthesize"}
    assert incoming == {"advance"}


def test_every_terminal_reaches_end(compiled) -> None:
    for terminal in TERMINALS:
        assert (terminal, "__end__") in {
            (e.source, e.target) for e in compiled.edges
        }


def test_repair_cannot_reach_verify_directly(compiled) -> None:
    """A repaired query must be re-executed before it can be verified.

    Otherwise the verifier would judge a result that was never produced.
    """
    assert ("repair", "verify") not in {(e.source, e.target) for e in compiled.edges}
