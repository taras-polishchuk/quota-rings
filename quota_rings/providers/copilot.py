"""GitHub Copilot -- optional, off by default, undocumented endpoint.

Endpoint: ``GET https://api.github.com/copilot_internal/user``. GitHub supports
nothing here: the endpoint is internal, and the documented billing/metrics APIs
report consumed amounts, never the included allowance.

Billing regime matters and must not be assumed. Individual plans moved to **AI
Credits** (1 credit = $0.01, allowance resets 00:00 UTC on the 1st), while
premium-request billing is retained only for some legacy annual subscribers.
The payload decides which one this account is on, and the ring's sub-label says
so. When the payload withholds an entitlement, nothing is invented: the ring is
omitted and the reason is shown as text.
"""

from __future__ import annotations

from typing import Any

from ..http import http_json
from ..model import FetchResult, VendorReset, make_window, parse_iso

USER_URL = "https://api.github.com/copilot_internal/user"

MONTH_SECONDS = 30 * 86400  # Copilot's month is the UTC calendar month, not 30 days of subscription age


def parse_user(payload: dict[str, Any]) -> FetchResult:
    result = FetchResult()
    snapshots = payload.get("quota_snapshots") or {}

    # AI Credits regime (current individual plans). Key names have moved before;
    # accept the documented variants and fall through cleanly when absent.
    credits = snapshots.get("ai_credits") or snapshots.get("ai_credits_quota") or {}
    premium = snapshots.get("premium_interactions") or {}

    reset_ts = parse_iso(payload.get("quota_reset_date_utc"))

    if credits and not credits.get("unlimited"):
        entitlement = credits.get("entitlement")
        remaining = credits.get("remaining")
        pct_remaining = credits.get("percent_remaining")
        if pct_remaining is not None:
            used = 100.0 - float(pct_remaining)
            sub = None
            if entitlement and remaining is not None:
                sub = f"{remaining}/{entitlement} credit" if float(entitlement) == 1 else f"{remaining}/{entitlement} credits"
            result.windows.append(
                make_window("month", "Month", used, reset_at=reset_ts,
                            window_seconds=MONTH_SECONDS, sub=sub)
            )
        result.extras.append("Billing regime: AI credits (1 credit = $0.01).")
    elif premium and not premium.get("unlimited"):
        entitlement = premium.get("entitlement")
        remaining = premium.get("remaining")
        pct_remaining = premium.get("percent_remaining", 100)
        if pct_remaining is not None:
            used = 100.0 - float(pct_remaining)
            sub = f"{remaining}/{entitlement} premium requests" if entitlement and remaining is not None else None
            result.windows.append(
                make_window("month", "Month", used, reset_at=reset_ts,
                            window_seconds=MONTH_SECONDS, sub=sub)
            )
        result.extras.append("Billing regime: legacy premium requests. Units are requests, not credits.")
    elif premium.get("unlimited") or credits.get("unlimited"):
        result.extras.append("Unlimited on this account: no monthly quota ring to draw.")
    else:
        result.extras.append("Payload carried no quota snapshot; nothing to draw rather than a guess.")

    chat = snapshots.get("chat") or {}
    if chat.get("unlimited"):
        result.extras.append("Chat and auto-complete are unlimited.")

    overage = (credits.get("overage_count") if credits else None) or (premium.get("overage_count") or 0)
    if overage:
        result.extras.append(f"Overage: {overage} requests beyond the included allowance.")

    plan = payload.get("copilot_plan")
    if plan:
        result.plan = str(plan).replace("_", " ").title()

    result.vendor_reset = VendorReset(
        supported=False,
        documented=False,
        note="GitHub offers no quota reset. The allowance returns at 00:00 UTC on the 1st.",
    )
    return result


def fetch(account: Any) -> FetchResult:
    token = account.resolve_token()
    if not token:
        raise RuntimeError("no GitHub token configured for this account")
    payload, _ = http_json(
        USER_URL,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "Editor-Version": "vscode/1.99.0",
            "User-Agent": "GitHubCopilotChat/0.26.7",
            "X-Github-Api-Version": "2025-04-01",
        },
    )
    return parse_user(payload)
