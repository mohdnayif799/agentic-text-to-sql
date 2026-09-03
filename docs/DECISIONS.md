# Decision log

Each entry records what was originally proposed, what the evidence said, what
was chosen, and why. Written during the build, not reconstructed afterwards.

---

## D1. Schema Explorer: agent → deterministic function

**Originally considered.** A "Schema Explorer" agent between the planner and
the SQL author, as in the initial architecture sketch.

**Evidence.** Introspection is `sqlalchemy.inspect()` — table names, column
types, nullability and foreign keys are fully determined by the database. No
model can produce a better answer, only a slower and occasionally hallucinated
one. Separately, dumping a full schema into the prompt is the usual failure
mode at scale: context cost grows linearly and precision drops as the model
picks plausible-but-wrong tables.

**Chosen.** Split the concern. Introspection is a plain function
(`agentcrew/db/introspect.py`). Selection — the part that needs judgement — is a
deterministic ranker (`agentcrew/db/selector.py`): token overlap against table and
column names, then one-hop foreign-key closure, then a cap.

**Why.** The FK-closure step is what makes this work rather than just cheap.
"Total revenue by region" names neither `orders` nor `order_items`, but the
query is impossible without them; closure reaches them. Verified by
`test_expands_across_foreign_keys`. Cost: zero tokens, microseconds.

---

## D2. Routers must be pure; `triage` promoted to a node

**Originally considered.** Routing functions that inspect state, decide the
next node, and set `stop_reason` / `repair_hint` on the way through.

**Evidence.** Four loop-control tests failed with `stop_reason == "none"`.
Root cause: **LangGraph routing functions cannot mutate state.** A router
receives the state mapping, but mutations are discarded — only node return
values are merged.

**Chosen.** Every decision is computed in a node, written to
`state["next_action"]`, and routers became one-line pure projections. The
deterministic post-execution triage therefore became a real node.

**Why.** Beyond correctness, it puts all decision logic where it can write
state *and* be traced. `triage` now emits a span with its decision, so the
trace panel shows why the agent went to `repair` rather than `verify`. The
failure was only caught because the tests asserted on `stop_reason` rather
than just on final status.

---

## D3. Structured outputs, not a model-driven tool loop

**Originally considered.** Native tool calling with the model looping over
tools until it decides it is finished.

**Evidence.** Model-driven loops are the origin of the most-reported agent
failure modes: step repetition, task derailment, and failure to recognise
termination conditions. A graph-driven loop has a bounded, enumerable state
space.

**Chosen.** The graph owns control flow. Each LLM call returns one validated
Pydantic object. Tool *use* is real — the agent inspects the schema and
executes queries against a live database — but the orchestrator sequences it.

**Why.** Testability, mostly. Because control flow is code rather than model
output, the entire repair loop, budget logic and loop detector are covered by
106 offline tests with no API key. That is not achievable when the model
decides the next step.

**Trade-off, stated honestly.** This system cannot discover a tool-use
strategy nobody anticipated. For open-ended exploration that would be a real
limitation. For "answer a question about this database" the state space is
known, and predictability is worth more than emergence.

---

## D4. MCP included, but as an adapter only

**Originally considered.** Routing all database access through MCP.

**Evidence.** In-process, MCP adds a JSON-RPC hop and a serialisation
boundary for zero additional capability. Against that: the MCP server
directory lists tens of thousands of entries, so "built an MCP server" is not
itself a differentiator.

**Chosen.** `agentcrew/tools.py` is the single implementation. The graph calls
it directly. `mcp_server.py` is an ~80-line adapter exposing the same
four functions so the tools — and their read-only guard — can be mounted in
Claude Desktop, Cursor, or any MCP client.

**Why.** Real benefit (reuse outside the process), honest cost (small), no
drift (one implementation). Verified end to end: 4 tools registered, correct
`read_only_hint` annotations, and `DROP TABLE customers` rejected through the
actual `call_tool` path.

---

## D5. SQLite, not PostgreSQL

**Chosen.** SQLite, seeded deterministically by `scripts/build_database.py`.

**Why.** Zero install, ships in the repo at 2.2 MB, byte-reproducible from a
fixed RNG seed, and — the deciding factor — SQLite is the only option here
where read-only can be enforced *at the connection level* via the
`file:...?mode=ro` URI. Postgres would need a separate role and a running
server to achieve the same guarantee, adding setup friction for a
demonstration project with no compensating benefit.

---

## D6. Seed data with planted signals

**Chosen.** Rather than uniform random noise, two signals are planted: an APAC
churn spike in Q3 2025, and a Q3 revenue decline driven by the Hardware
category and preceded by a marketing-spend cut.

**Why.** Diagnostic questions need discoverable answers. Verified margins are
decisive, not coin flips: APAC 44 Q3 churns vs 11 for the next region;
Hardware −1.87M while every other category grew. Without this, "why did
revenue fall?" has no correct answer and the evaluation is meaningless.

---

## D7. Evaluation graded by reference SQL, not hardcoded numbers

**Chosen.** Each evaluation question carries a reference SQL query. The
harness runs it against the same database and compares the agent's rows.

**Why.** Hardcoded expected values rot the moment the seed data changes, and
nobody can verify a number typed into a YAML file. Execution-based grading
stays correct through regeneration and is auditable. No LLM grades anything.

The harness also takes table checksums before and after the full run and
**asserts** they are unchanged, so every evaluation is simultaneously a safety
test.

---

## D8. Version findings (checked 2026-08-25 against PyPI, then installed)

Three of these would have broken code written from training data:

| Package | Version | Finding |
|---|---|---|
| `mcp` | 2.1.0 | **`mcp.server.fastmcp.FastMCP` was removed in 2.0.** Current entry point is `from mcp.server import MCPServer`. Also `Tool.inputSchema` → `Tool.input_schema`. |
| `pandas` | 3.0.5 | **String columns are now `str` dtype, not `object`.** `df[c].dtype == object` silently stops matching, which disabled chart rendering with no error. Fixed with `pd.api.types.is_string_dtype`; regression test added. |
| `anthropic` | 1.0.0 | Major bump, but `messages.create(model, max_tokens, system, messages)` is unchanged. New `output_config` parameter exists; not needed here. |
| `openai` | 3.3.1 | `chat.completions.create` uses `max_completion_tokens`. |
| `langgraph` | 1.2.11 | `StateGraph(state_schema)`, `compile(checkpointer=...)`. `config_schema` deprecated in favour of `context_schema`. Routers read-only (see D2). |
| `sqlglot` | 30.17.0 | `parse`, `parse_one`, `exp.*`, `.sql(normalize=True)` stable. |
| `langfuse` | 4.14.5 | Optional; isolated behind `LangfuseSink` so a breaking change cannot reach the core. |

Upper bounds are pinned in `pyproject.toml` (`<2.0`, `<4.0`, …) specifically
because two of the majors above broke real code.

---

## D9. Flat package layout instead of `src/`

**Originally built.** A `src/agentcrew/` layout with `app/streamlit_app.py`
and `mcp_server/server.py` in their own folders, plus subpackages for
`tools/`, `prompts/`, `observability/` and `llm/`.

**Evidence.** An audit of the package contents found three of those
subpackages contained exactly **one** module each:

| Package | Modules |
|---|---|
| `observability/` | `tracer.py` only |
| `prompts/` | `templates.py` only |
| `tools/` | `db_tools.py` only |
| `llm/` | `base.py` + `providers.py`, tightly coupled, 328 lines total |
| `db/` | 4 modules, 701 lines |
| `graph/` | 4 modules, 956 lines |

A package wrapping a single module is structure without benefit: it adds a
directory, an `__init__.py` and a longer import path (`agentcrew.tools.db_tools`)
in exchange for nothing. It also makes a project look larger than it is, which
is the wrong signal.

The `src/` layout is genuine best practice for **distributed libraries** - it
prevents `import agentcrew` from accidentally resolving to the working
directory instead of the installed package. This project is an application,
not a PyPI package. The protection is theoretical here, and it costs a real
thing: `pip install -e .` becomes mandatory before anything runs, which is
friction for someone who just cloned the repo to look at it.

**Chosen.** Flat layout. `app.py` and `mcp_server.py` at the repo root,
`agentcrew/` as a top-level package, and only `db/` and `graph/` kept as
subpackages because only those have multiple cohesive modules.

**Why.** Three reasons, in order:

1. **The threshold for a subpackage should be "more than one module."** `db/`
   and `graph/` clear it; the others did not.
2. **`pip install` is no longer required.** `streamlit run app.py`,
   `python scripts/run_eval.py` and `pytest` all work on a fresh clone with no
   install step and no `PYTHONPATH`.
3. **Portfolio consistency.** The other two projects in this portfolio put
   `app.py` at the root. A reviewer opening three repos should not have to
   re-orient in each one.

**Verified after the move.** 133 tests pass with no `PYTHONPATH`, lint is
clean, Streamlit serves HTTP 200 from `app.py`, the MCP server registers all
four tools and still rejects `DROP TABLE`, and the eval harness reports no
table mutation. No behaviour changed - this was purely a layout move.

---

## D10. `requirements.txt` alongside `pyproject.toml`, and a real venv test

**Originally built.** `pyproject.toml` only, with the README instructing
`pip install -e ".[all]"` straight into whatever Python was on PATH.

**Evidence.** Two problems.

First, consistency: the other two projects in this portfolio use
`requirements.txt`. A reviewer cloning all three should not meet a different
install path on this one.

Second, and worse — the README recommended installing into the system Python
with no virtualenv, and that omission actively hid a bug. Every "fresh clone"
check passed because a leftover `pip install -e .` in the system Python was
shadowing the repo. `scripts/build_database.py` still pointed at the deleted
`src/` directory (an earlier `sed` matched the `ROOT / "src"` form in
`run_eval.py` but missed the inline
`Path(__file__).resolve().parents[1] / "src"` in this file). It only surfaced
when the project was installed into a genuinely clean virtualenv:
`ModuleNotFoundError: No module named 'agentcrew'`.

**Chosen.**

- `requirements.txt` — exact `==` pins for the runtime.
- `requirements-dev.txt` — the above plus pytest, ruff and the optional extras.
- `pyproject.toml` — kept, holding version *ranges* and the pytest/ruff config.
- README leads with `python -m venv .venv`.
- `tests/test_packaging.py` — 14 guards, including one that asserts
  `agentcrew.__file__` resolves inside this repo, and two that run the scripts
  as subprocesses with a stripped environment and no `PYTHONPATH`.

**Why.** The split between abstract ranges (pyproject) and concrete pins
(requirements.txt) is the conventional pattern and each serves a different
reader: the range says what the code is compatible with, the pin says what was
actually verified. The drift risk from declaring dependencies twice is real,
so a test asserts every pin in `requirements.txt` satisfies its pyproject
range.

Only direct dependencies are pinned. A full transitive lockfile would be more
reproducible but is disproportionate here and goes stale quickly.

**Verified.** Built a clean venv, confirmed isolation (`import streamlit`
failed before install), installed `requirements-dev.txt` with no resolver
conflicts, then ran the database build, 147 tests, ruff, the offline eval and
the MCP adapter — all green inside the venv.

---

## D11. Sanitise subprocess environments by removal, never by construction

**Originally built.** Two tests launched project scripts as subprocesses with a
hand-built environment dict:

```python
env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}   # test_packaging.py
env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}                # conftest.py
```

The intent was to strip `PYTHONPATH`, so the packaging tests would prove that
each entry point bootstraps `sys.path` itself.

**Evidence.** Both failed on Windows with
`OSError: [WinError 10106] The requested service provider could not be loaded
or initialized`, traced through `asyncio -> windows_events -> _overlapped`.

Passing `env=` to `subprocess.run` **replaces** the environment rather than
extending it, so the hand-built dict dropped `SystemRoot`. Winsock needs it to
locate its service-provider DLLs, and fails with `WSAEPROVIDERFAILEDINIT`
(10106) without it. The trigger is that **SQLAlchemy imports `asyncio` at
import time** — so even `build_database.py`, which has nothing async about it,
loads `windows_events` on Windows. On Linux `asyncio` loads `unix_events`,
needs no Winsock, and the bug is invisible.

The `conftest.py` instance was latent: it only runs when
`data/northstar.db` is missing, so it stayed hidden as long as the database had
been built beforehand.

**Chosen.** One shared helper in `conftest.py`, used by both call sites:

```python
def clean_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env
```

**Why.** Sanitise by *removing* the variables under test, never by constructing
an environment from scratch — you cannot enumerate what a platform needs.
`PYTHONNOUSERSITE` makes the check slightly stricter than before by keeping
user site-packages from masking a broken bootstrap.

**Verified that this did not weaken the test.** The original bug was
re-injected into `scripts/build_database.py` in two forms:

| Injected fault | Caught by |
|---|---|
| `sys.path.insert(0, ROOT / "src")` | static check (`test_entry_point_has_no_stale_src_path`) |
| `sys.path.insert(0, ROOT / "nowhere")` | subprocess run — `ModuleNotFoundError: No module named 'agentcrew'` |

The second is the important one: no static check can see it, and only the
subprocess test catches it.

**One caveat worth knowing.** During that verification the subtle fault
initially went *undetected*, because a stale
`__editable__.agentcrew-1.0.0.pth` left behind by an earlier `pip install -e .`
was still on `sys.path` — `pip uninstall` had removed the package but not the
`.pth` file. This is the same masking that let the original `src/` bug survive
three "fresh clone" checks. `test_imported_package_is_this_repo` is the
in-process guard against it; for the subprocess case, the defence is simply to
develop in a clean virtualenv. Removing the stale `.pth` restored detection
immediately.

---

## D14. Sample low-cardinality values by cardinality, not by storage class

**Originally built.** `render_schema_card` showed distinct values only for
columns whose declared type was textual:

```python
_TEXTUAL = ("char", "text", "clob", "string")
if any(k in col.type.lower() for k in _TEXTUAL):
```

The documented rationale was to stop the model writing `status = 'complete'`
when the data says `'completed'`.

**Evidence.** The first real evaluation (Gemini 3.5 Flash-Lite) failed q05,
"What was total completed revenue in Q3 2025?". Reproducing the arithmetic
against the database identified the agent's query exactly:

| expression | value |
|---|---|
| `SUM(qty * unit_price * (1 - discount))` (reference) | 2,127,780.36 |
| `SUM(qty * unit_price - discount)` (agent) | **2,240,199.32** |

The agent read `discount REAL` as a flat currency amount and subtracted it
once per line item. The SQL was valid, executed cleanly, and returned one
plausible number, so neither the deterministic triage nor the LLM verifier had
anything to catch - the verifier receives no schema, and a single scalar gives
it nothing to judge.

The root cause was the type gate. `order_items.discount` holds exactly four
distinct values - 0.0, 0.05, 0.10, 0.15 - comfortably inside the existing
`max_distinct=40` threshold. The mechanism that would have disambiguated it
already existed; it was skipped only because the column is `REAL` rather than
`TEXT`.

**Chosen.** Sample by *cardinality and semantics*, not storage class:

* text **and** numeric columns are eligible;
* primary keys and foreign keys are excluded - identifier values are arbitrary
  and carry no meaning worth spending context on;
* the `max_distinct` gate is unchanged, so high-cardinality columns
  (`customer_name`, `order_date`, `marketing_spend.amount`) are still omitted;
* numeric samples render unquoted, because `'0.05'` would imply a text column
  and defeat the purpose.

`discount` now renders as `discount REAL NOT NULL   -- values include: 0.05,
0.0, 0.1`, which is unmistakably a rate. Cost: +237 characters (~59 tokens) on
the schema card.

**Why this is not a q05 special case.** It completes a design that was already
in the codebase for the other half of the type space, and applies to any rate,
percentage, code or int-encoded flag in any schema. Nothing in the change
references revenue, discounts, or the evaluation set.

**Deliberately NOT re-measured.** The published result stands at 16/17
(AgentCrew) vs 17/17 (baseline), q15 corrected. This fix was derived by
inspecting a failure *in the evaluation set*, so re-running the same 17
questions and reporting a better score would be validating a post-hoc fix on
the data that motivated it - test-set contamination, not evidence. A valid
measurement would need held-out questions.

Guarded by `test_shows_values_for_low_cardinality_numeric_columns`, which was
confirmed to fail against the pre-fix gate.

---

## D15. Tests must be hermetic with respect to configuration

**Evidence.** Three provider tests passed on a clean checkout and failed on a
developer machine with a populated `.env`:

```
assert getattr(s, PROVIDERS[other].key_field) is None
E   AssertionError: assert 'AQ.Ab8...' is None
```

`Settings` merges init kwargs > environment variables > `.env` file, so any
field a test does not pass explicitly falls through to whatever the machine
has configured. The tests were written as assertions about construction
semantics but were actually measuring the environment.

The failure had a second, worse consequence: pytest renders the offending
object in the assertion diff, so a **live API key was printed to the
terminal**. That key had to be revoked.

**Chosen.** An autouse fixture in `tests/conftest.py` that neutralises both
configuration sources for every test:

```python
for name in list(os.environ):
    if name.startswith("AGENTCREW_"):
        monkeypatch.delenv(name, raising=False)
monkeypatch.setitem(Settings.model_config, "env_file", None)
```

Both are required - clearing the environment variables is not sufficient,
because pydantic-settings reads the `.env` file directly rather than through
`os.environ`. Verified: with only the env vars cleared, `Settings()` still
returned the key from disk.

**Why an autouse fixture rather than per-call-site `_env_file=None`.** The bug
class is "a test forgot to pin a field", so the fix has to apply to tests
nobody has written yet. Opt-out beats opt-in here.

Guarded by `TestEnvironmentIsolation`, confirmed to fail when the fixture is
removed. The suite now passes with a populated `.env` and with hostile shell
variables (`AGENTCREW_PROVIDER`, `AGENTCREW_MODEL`, `AGENTCREW_MAX_LLM_CALLS`)
set.

