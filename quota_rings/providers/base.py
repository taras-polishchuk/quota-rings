"""Shared provider contract.

A fetcher receives a configured :class:`~quota_rings.config.Account` and returns
a :class:`~quota_rings.model.FetchResult`. It must:

* raise on failure (the collector isolates the error per account), or
* return an *empty* result when the provider is honestly out of reach -- the
  UI then shows a named "unsupported" state instead of a zero ring.

It must never return a credential, and must never fabricate a percentage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..model import FetchResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Account


class Fetcher(Protocol):
    def __call__(self, account: "Account") -> FetchResult: ...


# Human-readable support tier per provider id, used to label cards honestly.
PROVIDER_SUPPORT: dict[str, tuple[str, str]] = {
    "openrouter": (
        "supported",
        "Documented API. Per-key spend caps and account credits, each behind its own credential.",
    ),
    "claude": (
        "unstable",
        "Private endpoint used by Claude Code. No public contract; may change without notice.",
    ),
    "minimax": (
        "unstable",
        "Vendor-published in prose, but with no published schema. Field names come from observed traffic.",
    ),
    "codex": (
        "unstable",
        "Undocumented backend that the Codex CLI calls. A plain HTTP client can hit an edge challenge.",
    ),
    "copilot": (
        "unstable",
        "Undocumented internal endpoint, unsupported by GitHub. Billing regime varies by account.",
    ),
    "mistral": (
        "unsupported",
        "Admin API is Enterprise-only. Individual and pay-as-you-go accounts have no read path.",
    ),
}
