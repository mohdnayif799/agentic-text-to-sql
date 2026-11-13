# Self-hosted mode: your own PostgreSQL database

A company can run its own copy of AgentCrew against its own PostgreSQL
database. Nothing about the public demo changes: the database URL is read
from the server's environment only, the browser can never supply one, and
the public demo does not set it.

Only PostgreSQL is supported. A MySQL or other URL is refused at startup with
a clear message. (The code keeps a dialect setting so another database can be
added later; see agentcrew/db/dialect.py.)

## 1. Create a read-only role

This is the guarantee that matters. Do it before anything else, and connect
the app as this role, never as an owner or superuser.

```sql
CREATE ROLE agentcrew_ro LOGIN PASSWORD 'choose-a-long-random-password';
-- Read-only at the role level too, as a second stop.
ALTER ROLE agentcrew_ro SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE company TO agentcrew_ro;
GRANT USAGE ON SCHEMA public TO agentcrew_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agentcrew_ro;
-- Tables created later are readable too.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agentcrew_ro;
```

Grant SELECT only on the tables people should be able to ask about. Every
table the role can read is shown to the model, as described under Privacy.

## 2. Run it

```bash
docker compose -f docker-compose.selfhosted.yml up --build
```

Put `AGENTCREW_DATABASE_URL=postgresql://agentcrew_ro:PASSWORD@host:5432/company`
and one model key (`AGENTCREW_GEMINI_API_KEY`, `AGENTCREW_ANTHROPIC_API_KEY`
or `AGENTCREW_OPENAI_API_KEY`) in a `.env` file next to the compose file.
Open http://localhost:8080. Under "Your data", "Company database" is
selected.

## 3. Describe the database once

Open "Your data" and press "Draft descriptions with the model". The model
writes one short description per table and column from the names, types and
a few sample rows; edit any that are wrong and press "Save descriptions and
joins". With `AGENTCREW_DESCRIPTIONS_PATH` set (the compose file sets it),
the descriptions are saved to a volume and loaded on the next start.

Joins are proposed from column names confirmed by the data: a key column of
the same name in two tables (`customer_id`), or a column naming another
table (`course_id` -> `courses`), where the referenced side is unique and at
least 90% of the values match. Generic columns such as `id`, `name` or
`date` are never enough. Untick a wrong join or add a missing one in the
same editor. Foreign keys the database already declares are used as they
are.

On a database with more tables than `AGENTCREW_MAX_TABLES_IN_CONTEXT`
(default 8), each question sees only the tables schema selection picks from
the question's words, the tables' names and descriptions, and the joins.

## What keeps it read-only

| Layer | Mechanism |
| --- | --- |
| Role | `agentcrew_ro` has SELECT only (step 1) |
| Session | every connection starts with `default_transaction_read_only=on` and a statement timeout (`AGENTCREW_SQL_TIMEOUT_SECONDS`, default 15) |
| Query | every query runs inside `SET TRANSACTION READ ONLY` |
| AST | the sqlglot guard allows one SELECT and refuses writes, batches, and server functions that change settings or reach outside the database (`set_config`, `pg_read_file`, `dblink`, `lo_import`, `pg_sleep`, ...) |
| Resource | a row cap on every result (`AGENTCREW_MAX_RESULT_ROWS`, default 500) |

These were checked against PostgreSQL 17 with a role that CAN write: the
write is still refused (tests/test_postgres.py, live class).

## The database password

The URL holds the password, so the app treats the whole URL as a secret. It
is stored as a secret value that prints as `**********`; a connection failure
is reported by its error type with the URL and password scrubbed from the
text, and without the driver's original error attached; the browser only
ever sees "Your data could not be loaded"; and the run log never holds error
text at all. `tests/test_database_url_secret.py` checks each of these,
including a driver that echoes the URL it was given. Percent-encode special
characters in the password (`@` as `%40`, `/` as `%2F`).

## Privacy

Everything the model sees goes to the provider of the key you configure:
table and column names and types, row counts, every value of text columns
with fewer than 200 distinct values, date ranges, the descriptions, the
question, and rows of query results (up to 40) and lookups (up to 100).
Drafting descriptions also sends up to 3 rows per table, each value cut to
40 characters. Do not grant the role tables whose names or values must not
leave your network, or use a provider your company has approved.

`AGENTCREW_TRACE_CONTENT=true` adds the question and the final SQL to the
run log on stdout (and to Langfuse if configured). Result rows are never
logged. See docs/OPERATIONS.md.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `AGENTCREW_DATABASE_URL` | unset | The PostgreSQL URL; unset means NorthStar |
| `AGENTCREW_DESCRIPTIONS_PATH` | unset | YAML file the descriptions are loaded from and saved to |
| `AGENTCREW_UPLOADS_ENABLED` | `true` | Also offer CSV, Excel and SQLite uploads |
| `AGENTCREW_MAX_TABLES_IN_CONTEXT` | 8 | Above this, each question sees selected tables only |
| `AGENTCREW_SQL_TIMEOUT_SECONDS` | 15 | Statement timeout |
| `AGENTCREW_MAX_RESULT_ROWS` | 500 | Row cap |
