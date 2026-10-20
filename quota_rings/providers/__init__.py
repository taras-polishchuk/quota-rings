"""Provider registry.

Import cost matters: Codex, Copilot and Mistral are optional and off by default,
but they are plain stdlib modules, so importing the registry stays cheap.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Fetcher, PROVIDER_SUPPORT
from . import claude, codex, copilot, minimax, mistral, openrouter

FETCHERS: dict[str, Callable[[Any], Any]] = {
    "claude": claude.fetch,
    "openrouter": openrouter.fetch,
    "minimax": minimax.fetch,
    "codex": codex.fetch,
    "copilot": copilot.fetch,
    "mistral": mistral.fetch,
}

# Providers that only work with an Enterprise admin key. Enabled only when the
# operator configures them explicitly.
ENTERPRISE_ONLY = {"mistral"}

# Providers with a genuine vendor quota reset. Exactly one, and it needs both a
# server-wide flag and a per-account opt-in before server.py will call it.
VENDOR_RESET_PROVIDERS = {"codex"}

SUPPORTED_IDS = tuple(FETCHERS)


def support_of(provider: str) -> tuple[str, str]:
    return PROVIDER_SUPPORT.get(provider, ("unstable", "No support statement recorded for this provider."))


def fetcher_for(provider: str) -> Fetcher:
    try:
        return FETCHERS[provider]  # type: ignore[return-value]
    except KeyError:
        raise KeyError(
            f"unknown provider {provider!r}; known providers: {', '.join(sorted(FETCHERS))}"
        ) from None
