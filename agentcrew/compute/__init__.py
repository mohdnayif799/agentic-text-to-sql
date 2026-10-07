"""Deterministic compute tools for architecture D (no generated code).

functions.py  statistics over a previous step's result table
metrics.py    metric decomposition and contribution analysis, with SQL built
              only from the data dictionary's metric definitions
"""

from agentcrew.compute.functions import ComputeError

__all__ = ["ComputeError"]
