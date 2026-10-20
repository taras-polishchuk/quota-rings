"""Claude (Anthropic) subscription quota -- one entry per configured account.

Endpoint: ``GET https://api.anthropic.com/api/oauth/usage`` with a Claude Code
OAuth access token. This endpoint is NOT in Anthropic's public API docs, so the
support tier is ``unstable``: it is what Claude Code itself calls, it works, and
it can be removed without notice.

Two accounts (Max + Pro, or two teams) are two config entries with two different
token references. The usage payload does NOT name the plan, so ``plan`` is
operator-supplied config and is labelled as such in the UI.

A Console API key (``sk-ant-api...``) is a *different* identity: it bills
pay-as-you-go per token and has no subscription window at all. Passing one here
returns an explicit unsupported result instead of a zero ring.
"""

from __future__ import annotations

from typing import Any

from ..http import HttpError, http_json
from ..model import FetchResult, VendorReset, make_window, parse_iso

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-code/2.1.0"

SUPPORT_NOTE = (
    "Undocumented endpoint used by Claude Code. Works, but Anthropic publishes "
    "no contract for it, so it can change or disappear."
)


def _looks_like_console_key(token: str) -> bool:
    """Console/API-key families bill per token; they have no subscription window."""
    return token.startswith(("sk-ant-api", "sk-ant-usr", "sk-ant-svc", "sk-ant-admin"))


def parse_usage(payload: dict[str, Any]) -> FetchResult:
    """Normalize the /api/oauth/usage payload.

    Trap this module exists to get right: Anthropic returns ``utilization`` as a
    percent 0..100. A window that has just started reports ``utilization: 1``
    meaning *one percent used*. Code that treats a value <= 1 as a fraction
    renders that as 100% used. Never apply such a heuristic here.
    """
    result = FetchResult()

    five_hour = payload.get("five_hour") or {}
    if five_hour.get("utilization") is not None:
        result.windows.append(
            make_window(
                "short",
                "5 hour",
                five_hour["utilization"],
                reset_at=parse_iso(five_hour.get("resets_at")),
                window_seconds=5 * 3600,
            )
        )

    seven_day = payload.get("seven_day") or {}
    if seven_day.get("utilization") is not None:
        result.windows.append(
            make_window(
                "week",
                "Week",
                seven_day["utilization"],
                reset_at=parse_iso(seven_day.get("resets_at")),
                window_seconds=7 * 86400,
            )
        )

    for key, label in (("seven_day_opus", "Opus this week"), ("seven_day_sonnet", "Sonnet this week")):
        bucket = payload.get(key) or {}
        if bucket.get("utilization") is not None:
            result.extras.append(f"{label}: {float(bucket['utilization']):.0f}% used")

    # Usage credits are a separate meter with no meaningful percentage or window,
    # so they stay a text line and never become a ring.
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("used_credits") is not None and extra.get("monthly_limit"):
        currency = extra.get("currency") or "USD"
        result.extras.append(
            f"Usage credits: {float(extra['used_credits']):.2f}/{float(extra['monthly_limit']):.2f} {currency}"
        )

    # Claude's only real quota refill is the browser/Desktop "limit reset", which
    # this server cannot and must not perform.
    result.vendor_reset = VendorReset(
        supported=False,
        documented=True,
        note="Anthropic's limit reset is browser/Desktop only. No API action exists; "
        "the Refresh button here only re-reads the numbers.",
    )
    return result


def fetch(account: Any) -> FetchResult:
    token = account.resolve_token()
    if not token:
        raise RuntimeError("no Claude OAuth token configured for this account")

    if _looks_like_console_key(token):
        note = "A Console API key bills pay-as-you-go per token and has no subscription window."
        return FetchResult(
            # Named in extras so the card states the reason instead of showing
            # an empty ring.
            extras=[note, "Point this account at a Claude Code OAuth token to watch a subscription."],
            vendor_reset=VendorReset(supported=False, documented=True, note=note),
        )

    payload, _ = http_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CLAUDE_BETA,
            "User-Agent": USER_AGENT,
        },
    )
    result = parse_usage(payload)
    if not result.windows:
        raise HttpError("usage response carried no five_hour/seven_day window", status=200)
    return result
