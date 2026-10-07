"""Every question set the runner can use.

The protocol's sets (showcase and the two frozen holdouts) are registered in
evalkit.questions, which is frozen by eval/FREEZE_v2.sha256. Development sets
that are not frozen are registered here instead, so adding one never touches
a frozen file. Their gold is built and screened by scripts/build_dev_gold.py.
"""

from __future__ import annotations

from typing import Any

import yaml

from evalkit.questions import GOLD_FILES, ROOT, SET_FILES

DEV_SET_FILES = {"dev2": ROOT / "eval" / "dev2.yaml"}
DEV_GOLD_FILES = {name: ROOT / "eval" / "gold" / f"{name}.json"
                  for name in DEV_SET_FILES}

ALL_SET_FILES = {**SET_FILES, **DEV_SET_FILES}
ALL_GOLD_FILES = {**GOLD_FILES, **DEV_GOLD_FILES}


def load_questions(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load(ALL_SET_FILES[name].read_text("utf-8")) or []
