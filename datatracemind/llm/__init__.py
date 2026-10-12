"""Model access. Application code asks for `get_provider()` and nothing else.

DTM_LLM_PROVIDER picks the provider: `gemini` (Gemini through Google's ADK,
the default), `litellm` (the neatlogs LiteLLM gateway), or `codex` (the Codex
CLI).
"""

from __future__ import annotations

import os
from functools import lru_cache

from datatracemind.llm.base import Provider


@lru_cache(maxsize=1)
def get_provider() -> Provider:
    from datatracemind.config import get_settings

    get_settings()  # loads .env
    choice = (os.environ.get("DTM_LLM_PROVIDER") or "gemini").strip().lower()
    if choice == "gemini":
        from datatracemind.llm.gemini import GeminiProvider

        return GeminiProvider()
    if choice == "codex":
        from datatracemind.llm.codex import CodexProvider

        return CodexProvider()
    if choice == "litellm":
        from datatracemind.llm.litellm import LiteLLMProvider

        return LiteLLMProvider()
    raise ValueError(f"DTM_LLM_PROVIDER must be 'gemini', 'litellm', or 'codex', not {choice!r}")
