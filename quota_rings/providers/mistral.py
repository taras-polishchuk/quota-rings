"""Mistral -- honestly mostly unsupported, by design.

Research verdict: the only programmatic read path is the Admin API, and it is
Enterprise-only. A standard (non-Admin) key does not work on admin endpoints,
and no individual or pay-as-you-go balance endpoint exists. So:

* ``kind = "admin"`` reads the documented Admin endpoints (Enterprise key).
  Those response shapes come from Mistral's published Admin reference, not from
  observation -- this project has no Enterprise key to observe one with.
* ``kind = "payg"`` / ``"subscription"`` returns a **named unsupported state**
  with no window. It never draws a zero ring and never invents a percentage.

Tiers track cumulative *billed* usage, and limits are workspace-scoped, so
several keys under one workspace share one throughput budget.
"""

from __future__ import annotations

from typing import Any

from ..http import http_json
from ..model import FetchResult, VendorReset, make_window

BASE = "https://api.mistral.ai/v1/admin"
RATE_LIMIT_URL = f"{BASE}/rate-limit"
SPEND_LIMIT_URL = f"{BASE}/spend-limit"
USAGE_URL = f"{BASE}/usage"

UNSUPPORTED_NOTE = (
    "Mistral exposes no individual quota read path. The Admin API is Enterprise-only and a "
    "standard API key cannot read admin endpoints, so subscription and pay-as-you-go "
    "usage are not visible to the account holder. Nothing is guessed here."
)


def _admin_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "x-api-key": token}


def parse_rate_limit(payload: dict[str, Any]) -> FetchResult:
    """Shape from Mistral's published Admin API reference (not observed live)."""
    result = FetchResult()
    rps = payload.get("requests_per_second")
    if rps:
        result.extras.append(f"Rate limit: {rps} requests/second")

    limits = payload.get("tokens_limits_by_model") or {}
    monthly_totals: list[tuple[str, float, float]] = []
    for model, bucket in limits.items():
        if not isinstance(bucket, dict):
            continue
        per_month = bucket.get("tokens_per_month")
        per_minute = bucket.get("tokens_per_minute")
        if per_month:
            monthly_totals.append((model, float(per_month), float(bucket.get("tokens_per_minute") or 0)))

    # Only a *monthly total* can be turned into a ring; a per-minute token bucket
    # has no consumption figure in the payload, so it stays a text line.
    if monthly_totals:
        monthly_totals.sort(key=lambda item: item[1], reverse=True)
        model, per_month, per_minute = monthly_totals[0]
        result.extras.append(f"Highest monthly token cap: {int(per_month):,} on {model}"
                             + (f" ({int(per_minute):,}/min)" if per_minute else ""))
        result.extras.append("Token caps are ceilings, not consumption: no ring is drawn from them.")
    return result


def parse_spend_limit(payload: dict[str, Any]) -> FetchResult:
    result = FetchResult()
    limits = payload.get("limits") or {}
    completion = limits.get("completion") or {}
    currency = payload.get("currency") or "USD"
    monthly = completion.get("monthly_limit")
    if monthly is not None:
        result.extras.append(f"Monthly spend limit: {float(monthly):,.2f} {currency}")
    if completion.get("monthly_limit_reached"):
        result.extras.append("Monthly spend limit reached: requests are refused until the next billing month.")
    if payload.get("last_payment_failure"):
        result.extras.append("Last payment failed. HTTP 402 responses are a billing state, not a quota window.")
    return result


def fetch(account: Any) -> FetchResult:
    kind = str(account.options.get("kind", "admin")).lower()
    if kind != "admin":
        # The card renders ``vendor_reset.note`` next to "No vendor quota reset
        # here." An empty note left the user with a bare verdict and no reason,
        # while the same card carried the reason in extras. Say it in both.
        return FetchResult(
            extras=[UNSUPPORTED_NOTE],
            vendor_reset=VendorReset(
                supported=False, documented=False,
                note="Mistral publishes no quota reset for any account. A "
                     "non-admin key cannot read the Admin API that reports these "
                     "limits, so there is nothing here to reset."),
        )

    token = account.resolve_token()
    if not token:
        raise RuntimeError("no Mistral Admin API key configured for this account")

    result = FetchResult(vendor_reset=VendorReset(supported=False, documented=False,
                                                 note="Mistral publishes no reset action. Limits roll by calendar month."))
    rate_payload, _ = http_json(RATE_LIMIT_URL, headers=_admin_headers(token))
    result.extras.extend(parse_rate_limit(rate_payload).extras)
    try:
        spend_payload, _ = http_json(SPEND_LIMIT_URL, headers=_admin_headers(token))
        result.extras.extend(parse_spend_limit(spend_payload).extras)
    except Exception as exc:  # noqa: BLE001 - optional half of an optional feature
        result.extras.append(f"Spend limit unavailable ({type(exc).__name__}).")
    return result
