"""Bring your own data: uploads, the self-hosted database, and their context.

    names       turn file, sheet and header names into safe SQL identifiers
    convert     CSV and Excel to a fresh SQLite file (the app's own format)
    sqlite_upload  check an uploaded SQLite file and copy it into the store
    store       per-session datasets on local disk, with size limits and expiry
    ingest      one entry point: bytes from the browser -> a stored dataset
    joins       joins inferred from column names and data, for schemas without
                foreign keys
    describe    model-drafted, user-edited table and column descriptions
    source      a data source the agent can answer from: NorthStar, an
                upload, or AGENTCREW_DATABASE_URL

Every source is read through the same layers as NorthStar: a read-only
connection (db/engine.py), the sqlglot guard (db/safety.py) and the row cap and
timeout (tools.execute_readonly_sql). Nothing here opens a writable handle
except the converter, on the new file it is creating, before the agent ever
sees it.
"""
