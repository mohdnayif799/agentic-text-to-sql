"""Refuse a request to change the data, before any LLM call or SQL.

The agent is read-only, and the guard and the read-only connection already make a
write impossible (agentcrew/tools.py, agentcrew/db). They stay as the second layer.
This first layer exists so a visitor who asks for a change gets a plain answer at
once, instead of a model call, a failed query and a used-up demo run.

It looks for a request, not a topic. A sentence has to open with the verb (after
polite words such as "please" or "can you"), in its base form, used as a verb. So
"delete all customers" is refused, and "how many orders were deleted?" or "why did
revenue drop?" are not. It errs towards letting a sentence through, because the
second layer catches anything it misses and a wrong refusal costs a real answer.
"""

from __future__ import annotations

import re

WRITE_REFUSAL = "I only answer questions about the data; I can't change or delete it."

#: Sentence ends. A decimal is split too, which is harmless: no verb follows it.
_SENTENCES = re.compile(r"[.?!;:\n]+\s*")
#: Where a later clause of a sentence can start.
_CLAUSES = re.compile(r",|\band then\b|\bthen\b|\band\b", re.IGNORECASE)

#: Words that can come before the verb of a request.
_LEAD_IN = re.compile(
    r"^(?:please|pls|kindly|hey|hi|hello|ok|okay|now|just|also|first|next|so|"
    r"go ahead and|can you|could you|would you|will you|can we|could we|shall we|"
    r"i want to|i want you to|i'd like to|i would like to|i'd like you to|"
    r"i would like you to|i need to|i need you to|we need to|we want to|we should|"
    r"you should|you must|you need to|let's|lets|let us|let me|help me|try to|"
    r"make sure to|be sure to|run this|execute this|run|execute|sql)\b[\s,]*",
    re.IGNORECASE,
)
#: A sentence that opens like this is a question, whatever verbs come later.
_ASKING = re.compile(
    r"^(?:how|what|which|who|whom|whose|why|when|where|did|does|do|is|are|was|were|"
    r"has|have|had|will|would|should|could|can|may|might)\b",
    re.IGNORECASE,
)

#: These words after a verb make it a noun ("delete counts", "edit history").
_NOT_A_NOUN = (r"(?!\s+(?:rates?|counts?|history|logs?|dates?|times?|frequency|volumes?"
               r"|activity|requests?|events?)\b)")
#: What a verb such as "change" or "clear" must act on to be a change to data.
_DATA_OBJECT = (r"(?:the|all|every|each|this|that|these|those|my|our|their|its|"
                r"customers?|orders?|products?|tickets?|regions?|prices?|status|"
                r"statuses|discounts?|names?|values?|tables?|rows?|records?|columns?|"
                r"data|database)\b")
_NEW_THING = (r"(?:customers?|orders?|products?|tickets?|regions?|rows?|records?|"
              r"entries|entry|columns?|tables?|index(?:es)?|users?)\b")
#: Things to read, not data: creating a chart of revenue changes nothing.
_PRESENTATION = (r"(?:charts?|graphs?|plots?|reports?|summar(?:y|ies)|breakdowns?|"
                 r"lists?|rankings?|overviews?|comparisons?|dashboards?|"
                 r"visuali[sz]ations?|histograms?|pivots?|table\s+of)\b")

_ALWAYS_WRITES = (r"(?:delete|erase|wipe|purge|truncate|destroy|overwrite|insert|"
                  r"upsert|alter|rename|modify|edit)\b(?![-'])" + _NOT_A_NOUN)
_REMOVES = (r"(?:remove\b(?![-'])" + _NOT_A_NOUN +
            r"|drop\b(?![-\s]*(?:in|off|of|from|by|during|between|after|before|rates?"
            r"|counts?)\b)|get\s+rid\s+of\b)")
_UPDATES = r"update\b(?![-'])(?!\s+(?:me|us|on|in|of)\b)" + _NOT_A_NOUN

_REQUEST = re.compile(
    "^(?:" + "|".join((
        _ALWAYS_WRITES,
        _REMOVES,
        _UPDATES,
        r"(?:change|clear|reset|cancel)\s+(?:out\s+)?" + _DATA_OBJECT,
        r"replace\b.*\bwith\b",
        r"set\b(?!\s+(?:of|up)\b).*\bto\b",
        r"mark\b.*\bas\b",
        r"add\s+(?:(?:a|an|the|new|another|one|two|three|\d+|more|some)\s+)*"
        + _NEW_THING,
        r"create\b" + _NOT_A_NOUN + r"(?!.*\b" + _PRESENTATION + ")",
    )) + ")",
    re.IGNORECASE,
)
#: "Remove the outliers and recompute": a filter for an analysis, not a deletion.
_FOR_AN_ANALYSIS = re.compile(
    r"\b(?:from\s+(?:the\s+)?(?:calculation|analysis|totals?|counts?|averages?|sum|"
    r"results?|numbers?|figures?)|(?:when|before)\s+(?:calculating|computing|counting)|"
    r"(?:and|then)\s+(?:show|compute|recompute|calculate|recalculate|give|tell|list|"
    r"report|sum|count|average|rank))\b",
    re.IGNORECASE,
)
_REMOVES_AT_START = re.compile("^" + _REMOVES, re.IGNORECASE)
#: A later clause must name what it acts on: "and delete them", not "and drop rates".
_LATER_REQUEST = re.compile(
    r"^(?:delete|erase|wipe|purge|truncate|destroy|overwrite|remove|drop|update"
    r"|get\s+rid\s+of)"
    r"\s+(?:them|it|those|these|the|all|every|each|this|that|everything)\b",
    re.IGNORECASE,
)


def _strip_lead_in(text: str) -> str:
    text = text.strip()
    while True:
        stripped = _LEAD_IN.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped


def _is_request(sentence: str) -> bool:
    sentence = _strip_lead_in(sentence)
    if len(sentence.split()) < 2:  # a verb with nothing to act on: "Edit: I meant 2025"
        return False
    if _REQUEST.match(sentence):
        return not (_REMOVES_AT_START.match(sentence)
                    and _FOR_AN_ANALYSIS.search(sentence))
    if _ASKING.match(sentence):
        return False
    return any(_LATER_REQUEST.match(_strip_lead_in(clause))
               for clause in _CLAUSES.split(sentence)[1:])


def is_write_request(question: str) -> bool:
    """True if the text asks to change the data rather than asks about it."""
    text = question.replace("’", "'")
    return any(_is_request(sentence) for sentence in _SENTENCES.split(text))
