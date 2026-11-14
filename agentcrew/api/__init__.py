"""The REST API under /v1 (docs/API.md).

A second entry point beside the Streamlit app, on the same core: questions
go through agentcrew/service.py and SQL through agentcrew/sql_editor.py, so
the guard, the row cap, the timeout, the daily caps and the run log are the
ones the UI has. This package adds only what HTTP needs: API keys, a rate
limit per key, a body limit enforced while reading, one error envelope, and
datasets bound to the key that uploaded them.

    uvicorn agentcrew.api.app:create_app --factory --host 0.0.0.0 --port 8080
"""
