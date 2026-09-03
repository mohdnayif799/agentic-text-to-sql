# Interview notes

Answers to the questions this project invites. Written to be spoken, not read
aloud verbatim. Where a claim has a number behind it, the number is here.

---

### Why agents at all? Why not one prompt?

For easy questions, one prompt is correct and I say so — the evaluation
harness exists partly to prove that. The orchestration earns its place in
three specific ways:

1. **Different stages read different things.** The author reads a schema; the
   verifier reads a result set. One prompt doing both optimises for whichever
   it saw last.
2. **The verifier must not have written the query.** Asking a model "is the
   SQL you just wrote correct?" gets a yes. Giving a separate call only the
   goal and the returned rows — not the reasoning that produced them — gets a
   useful answer. That independence is the whole value of the stage.
3. **Repair needs an external signal.** Self-correction from introspection
   alone does not reliably improve outputs; self-correction against real
   execution results does. Splitting execution from authorship is what makes
   the signal external.

The cost is real and I measured it: roughly **4× the LLM calls** of the
single-agent baseline. That has to be earned back on hard questions.

### Why these specific agents, and what did you cut?

Five LLM nodes, six deterministic. I cut three things from my first design:

- **Schema Explorer agent → function.** Introspection is
  `sqlalchemy.inspect()`. A model can't do it better, only slower and
  occasionally hallucinated.
- **Planner separate from SQL Author → merged.** For a single step the plan
  *is* the query rationale. Two calls, one useful output.
- **"SQL Execution agent" → function.** It was never plausibly an agent.

The rule I applied: *agents are expensive, functions are free, so anything
decidable by code is code.*

### Why LangGraph over CrewAI?

I needed explicit state, conditional edges, and a retry budget I could
unit-test. LangGraph gives me a state machine where control flow is code.
CrewAI's role-and-task abstraction is faster to prototype but it hides the
control flow inside the framework, which is exactly the part I wanted to own
and test. LangGraph also has the larger production footprint in 2026.

### How does state work?

One `TypedDict` flows through every node. Nodes return **partial** updates,
never whole state, so each node is testable with a hand-built dict.
Non-serialisable collaborators (engine, LLM client, tracer, budget) live in a
`Deps` object injected by closure, never stored in state — that keeps state
serialisable, which is what makes checkpointing and the trace panel possible.

### Tell me about a bug you found.

Two worth telling.

**Routers can't mutate state.** My first design had routing functions that
decided the next node *and* set `stop_reason` on the way through. Four
loop-control tests failed with `stop_reason == "none"`. Root cause: LangGraph
routers receive the state mapping but any mutation is discarded — only node
return values get merged. The fix reshaped the architecture: every decision is
now computed in a node, written to `state["next_action"]`, and routers became
one-line pure projections. That's why `triage` is a node. The tests only
caught it because they asserted on `stop_reason` rather than just on final
status.

**pandas 3.0 silently disabled my charts.** String columns are `str` dtype
now, not `object`, so `df[c].dtype == object` stopped matching and the chart
heuristic quietly never fired. No exception, no error — just a missing
feature. Fixed with `pd.api.types.is_string_dtype` and pinned with a
regression test.

### How does self-correction work?

Two layers, and they catch different things:

- **Deterministic triage** classifies the outcome for free: syntax error,
  missing object, type error, timeout, empty result, oversize. No model call.
  This is why the verifier is cheap — I never pay a model to notice a syntax
  error.
- **LLM verification** only runs on queries that executed and returned rows,
  which is the only case where semantics are genuinely in question. It
  returns `pass` / `wrong_metric` / `insufficient` plus a concrete repair hint.

In the demo trace both fire in one run: attempt 1 fails on a missing column
(triage), attempt 2 runs cleanly but counts all customers instead of Q3 churn
(verifier), attempt 3 is correct.

### How do you prevent infinite loops?

Three independent mechanisms:

1. **Budgets** — attempts per step, total LLM calls, total SQL executions,
   wall clock. Breach ends the run with an explanation, never an invented
   answer.
2. **Fingerprint loop detection** — queries are normalised through sqlglot and
   hashed, so two attempts differing only in whitespace, casing or aliasing
   collide. A repeat doesn't consume a fresh retry; a second repeat aborts.
   Measured: the agent stops after **2** executions instead of burning the
   full budget on `generate → fail → regenerate identical → fail`.
3. **Monotonic progress** — a step only advances on a passing verification, so
   a clean query that answers the wrong question can't silently end the loop.

LangGraph's `recursion_limit=60` is a backstop. If it ever fires, that's a bug
in my budget logic, not a safety net working.

### How is database safety enforced?

Three independent layers, and the strongest one is not the prompt:

1. **Connection** — `file:...?mode=ro` URI plus `PRAGMA query_only=ON`. Delete
   every other check and the agent still cannot write.
2. **AST** — sqlglot parses and walks the whole tree. Blocks all DML/DDL,
   `PRAGMA`/`ATTACH`/`VACUUM`, statement batching, and writes hidden in CTEs.
3. **Resource** — injected `LIMIT`, row cap, statement timeout.

I audited it with 15 real attack payloads: **all rejected, zero row-count
change**, and `ATTACH` created no file. One payload is *correctly allowed* —
`SELECT ... /* ; DROP TABLE customers; */` — because a DROP inside a comment
is not a statement. There's an explicit test asserting that stays allowed, so
nobody later "hardens" the guard into rejecting valid SQL containing a scary
substring. A regex-based guard would fail exactly there.

Why AST instead of a keyword blocklist? Blocklists fail on comments, string
literals containing `DROP`, casing, and whitespace. Parsing doesn't.

### Why is MCP here if the agent doesn't use it?

Because in-process it adds nothing — a JSON-RPC hop and a serialisation
boundary to reach code in the same interpreter, for zero capability. So the
graph calls the tool functions directly.

What MCP does buy is reuse *outside* the process: the same four tools, with
the same read-only guard, mountable in Claude Desktop or Cursor. That's real,
so it's an ~80-line adapter that delegates to the exact functions the agent
uses. One implementation, no drift. I'd rather explain why I *limited* MCP
than pretend it was load-bearing.

### How is this evaluated?

Execution-graded against reference SQL. Each question carries a reference
query; the harness runs it and compares the agent's rows. No hardcoded
numbers (they rot when the seed data changes) and no LLM judge (not
reproducible). Four check types: `scalar` with tolerance, `top_label` which is
order-sensitive, `label_set`, and `nonempty` for open-ended diagnostics.

Two questions have no machine-checkable ground truth and are reported under
manual review rather than silently counted as passes.

The baseline is deliberately strong: same model, same schema card, same guard,
plus one retry. It lacks only what's under test. Weakening it would produce a
flattering number and teach me nothing.

Every eval run also takes table checksums before and after and **asserts**
they're unchanged — so evaluation doubles as a safety test.

### Why did you plant signals in the seed data?

Because "why did revenue fall in Q3?" needs a discoverable answer or the
question is meaningless. Two signals: an APAC churn spike in Q3 2025, and a
Hardware-driven revenue decline preceded by a marketing cut. Margins are
decisive, not coin flips — APAC 44 Q3 churns vs 11 for the next region;
Hardware −1.87M while every other category grew.

### What are the limitations?

- **SQLite only.** Porting to Postgres needs a different read-only mechanism
  (a role, not a URI flag) and sqlglot dialect changes.
- **Lexical schema selection has a ceiling.** Token overlap plus FK closure
  works at this scale. On a 500-table warehouse with names like `DIM_CUST_X1`
  it would need embeddings or a learned ranker.
- **The verifier is a judge, so it can be wrong.** It catches obvious
  wrong-metric errors. It will not catch a subtly incorrect join that produces
  plausible numbers. It reduces the error rate; it doesn't eliminate it.
- **No cross-question memory.** Deliberate — nothing in the use case justified
  it.
- **Shallow plans.** Up to 4 sequential steps, no branching.
- **Cost.** ~4× the baseline's calls. Whether that's worth it is a per-workload
  question, which is what the harness measures.

### What would you do next?

In order of value:

1. Run the full eval against a real model and publish the per-difficulty
   breakdown, including the cases where the baseline wins.
2. Swap lexical selection for embedding-based retrieval and measure whether it
   actually helps at this schema size — I suspect it doesn't, which would be a
   useful negative result.
3. Route easy questions to the baseline path and hard ones to the full graph,
   using the eval to set the threshold. That directly attacks the 4× cost.

### Why is this genuinely agentic and not "ChatGPT for SQL"?

A text-to-SQL wrapper emits a query and stops. This system takes actions
against a real system, observes the consequences, and changes what it does
next based on what actually happened — a missing column, an empty result, a
metric that doesn't match the goal. It also knows when to stop and say it
couldn't do it, which is the part most demos skip.

The honest framing: it's not a swarm of collaborating peers. It's one
orchestrated pipeline with a real feedback loop, bounded budgets and a safety
boundary. I think that's the more defensible thing to have built.
