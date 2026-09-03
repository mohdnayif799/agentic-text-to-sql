.PHONY: venv install db test lint app mcp eval eval-offline clean

venv:
	python -m venv .venv
	@echo "Now run: source .venv/bin/activate"

install:
	pip install -r requirements-dev.txt

db:
	python scripts/build_database.py

test:
	pytest

lint:
	ruff check agentcrew app.py mcp_server.py scripts tests

app:
	streamlit run app.py

mcp:
	python mcp_server.py

eval:
	python scripts/run_eval.py

eval-offline:
	python scripts/run_eval.py --provider fake

clean:
	rm -rf .pytest_cache .ruff_cache data/traces eval/results.json
	find . -name __pycache__ -type d -exec rm -rf {} +
