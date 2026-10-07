"""Shared context: the one prompt context every v2 architecture receives.

Architectures A (baseline), C (router) and D (full) all get exactly this
context; only their control flow differs (eval/PROTOCOL.md, protocol v2).
"""

from agentcrew.context.builder import ContextBuilder, SharedContext

__all__ = ["ContextBuilder", "SharedContext"]
