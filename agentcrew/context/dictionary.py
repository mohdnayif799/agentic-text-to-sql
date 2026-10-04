"""The YAML data dictionary: loading and rendering for prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DICTIONARY_PATH = Path(__file__).with_name("data_dictionary.yaml")


def load_dictionary(path: Path = DICTIONARY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text("utf-8"))


def _column_line(name: str, spec: Any) -> str:
    if isinstance(spec, str):
        return f"  {name}: {spec}"
    line = f"  {name}: {' '.join(spec['description'].split())}"
    if spec.get("allowed_values"):
        line += f" Allowed values: {', '.join(spec['allowed_values'])}."
    return line


def render_dictionary(dictionary: dict[str, Any]) -> str:
    lines = ["Conventions:"]
    lines += [f"- {c}" for c in dictionary.get("conventions", [])]
    lines.append("")
    lines.append("Tables:")
    for table, spec in dictionary["tables"].items():
        lines.append(f"{table}: {spec['description']}")
        lines += [_column_line(n, s) for n, s in spec["columns"].items()]
    lines.append("")
    lines.append("Join paths:")
    lines += [f"- {j}" for j in dictionary.get("joins", [])]
    lines.append("")
    lines.append("Metric definitions:")
    lines += [
        f"- {name}: {' '.join(text.split())}"
        for name, text in dictionary.get("metrics", {}).items()
    ]
    return "\n".join(lines)
