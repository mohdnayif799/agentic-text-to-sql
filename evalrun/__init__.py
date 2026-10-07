"""Evaluation runner for protocol v2: response cache, backoff, traces, scoring.

Outside the `agentcrew` package on purpose: it never ships in the Docker
image. Grading itself lives in the frozen `evalkit` package.
"""
