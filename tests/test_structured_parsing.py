"""Structured replies: fences and stray text are stripped before validation,
and a reply that still does not parse is retried once, counted like any call.

Visitors can bring their own key and model, and models do not all follow the
"single JSON object and nothing else" instruction. These are the shapes seen
in practice: a ```json fence, a fence left unclosed, a sentence before or
after, an inline code span in the prose, and an example object before the
real one. None of the prompts change.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agentcrew.archs.app_runner import CountingClient
from agentcrew.caps import DailyCap, DailyCapExceeded
from agentcrew.llm import (
    FakeClient,
    StructuredOutputError,
    extract_json,
    json_candidates,
)


class Draft(BaseModel):
    sql: str
    reason: str


GOOD = '{"sql": "SELECT 1", "reason": "count"}'


@pytest.mark.parametrize("reply", [
    GOOD,
    f"```json\n{GOOD}\n```",
    f"```JSON\n{GOOD}\n```",
    f"```\n{GOOD}\n```",
    f"Here is the query:\n```json\n{GOOD}\n```\nLet me know if it helps.",
    f"```json\n{GOOD}",                                   # fence never closed
    f"Sure! {GOOD} That should work.",                    # stray text both sides
    f"I'll use `/dictionary)` as noted.\n```json\n{GOOD}\n```",  # inline code first
    f"Thinking about {{region}} first.\n{GOOD}",          # a brace that is not JSON
    f"```sql\nSELECT 1\n```\n```json\n{GOOD}\n```",       # a non-JSON fence first
    f"Example: {{\"a\": 1}}\nAnswer: {GOOD}",               # an example object first
])
def test_one_call_is_enough_for_a_wrapped_reply(reply: str) -> None:
    llm = FakeClient([reply])
    draft, usage = llm.complete_structured(system="s", user="u", schema=Draft)
    assert draft == Draft(sql="SELECT 1", reason="count")
    assert usage.calls == 1 and len(llm.prompts) == 1


def test_nested_objects_and_braces_in_strings_survive() -> None:
    reply = ('Result:\n```json\n{"sql": "SELECT \'{x}\' AS t", '
             '"reason": "a {nested} brace", "extra": {"k": [1, {"v": 2}]}}\n```')
    (first, *_) = json_candidates(reply)
    assert first.startswith('{"sql"') and first.endswith("}}")
    draft, _ = FakeClient([reply]).complete_structured(system="s", user="u",
                                                       schema=Draft)
    assert draft.sql == "SELECT '{x}' AS t"


def test_extract_json_keeps_its_old_answers() -> None:
    assert extract_json(GOOD) == GOOD
    assert extract_json(f"```json\n{GOOD}\n```") == GOOD
    assert extract_json(f"note {GOOD} end") == GOOD
    assert extract_json("no json here") == "no json here"


def test_a_bad_reply_is_retried_once_then_succeeds() -> None:
    llm = FakeClient(["I could not decide.", f"```json\n{GOOD}\n```"])
    draft, usage = llm.complete_structured(system="s", user="u", schema=Draft)
    assert draft.sql == "SELECT 1"
    assert usage.calls == 2
    assert "Your previous reply was rejected." in llm.prompts[1][1]


def test_two_bad_replies_fail_with_a_structured_error() -> None:
    llm = FakeClient(["nope", "```json\n{\"sql\": \"SELECT 1\""])  # truncated
    with pytest.raises(StructuredOutputError, match="Draft"):
        llm.complete_structured(system="s", user="u", schema=Draft)
    assert len(llm.prompts) == 2, "exactly one retry"


def test_the_retry_is_counted_and_charged() -> None:
    cap = DailyCap(max_runs=0, max_llm_calls=10)
    client = CountingClient(FakeClient(["garbage", GOOD]), budget=cap)
    client.complete_structured(system="s", user="u", schema=Draft)
    assert client.calls == 2 and cap.snapshot().llm_calls == 2


def test_the_retry_stops_at_the_daily_cap() -> None:
    cap = DailyCap(max_runs=0, max_llm_calls=1)
    client = CountingClient(FakeClient(["garbage", GOOD]), budget=cap)
    with pytest.raises(DailyCapExceeded):
        client.complete_structured(system="s", user="u", schema=Draft)
    assert client.calls == 1


def test_the_instruction_and_repair_prompts_are_unchanged() -> None:
    """The exact bytes the frozen evaluation was replayed against."""
    llm = FakeClient(["bad", GOOD])
    llm.complete_structured(system="SYS", user="USER", schema=Draft)
    (system1, user1), (system2, user2) = llm.prompts
    assert system1 == system2
    assert system1.startswith(
        "SYS\n\nRespond with a single JSON object and nothing else. No prose, no "
        "code fence, no explanation outside the JSON.\n"
        "It must validate against this JSON Schema:\n")
    assert user1 == "USER"
    assert user2.startswith("USER\n\nYour previous reply was rejected.\n"
                            "--- your reply ---\nbad\n--- validation error ---\n")
    assert user2.endswith("Return corrected JSON only.")
