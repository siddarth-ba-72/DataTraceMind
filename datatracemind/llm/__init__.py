"""Model access. Application code asks for `get_provider()` and nothing else."""

from __future__ import annotations

from functools import lru_cache

from datatracemind.llm.base import Provider


@lru_cache(maxsize=1)
def get_provider() -> Provider:
    from datatracemind.llm.codex import CodexProvider

    return CodexProvider()
