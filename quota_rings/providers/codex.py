"""Codex (ChatGPT plan) -- optional, off by default, and the only real vendor reset.

Read path: ``GET chatgpt.com/backend-api/codex/wham/usage`` with the ChatGPT
OAuth access token. That path is undocumented (zero hits in OpenAI's published
``llms.txt`` indexes) and a plain HTTP client can receive a Cloudflare edge
challenge (HTTP 403, HTML body) instead of JSON. Both make this the flakiest ring
in the dashboard, which is why the CLI sends browser-shaped headers here.

**The banked reset is a genuine vendor quota reset**, and the only one reachable
programmatically in this project. It is deliberately kept far away from refresh:

* it is advertised only when the payload reports available credits,
* redeeming it is a POST that :mod:`quota_rings.server` exposes only with
  ``enable_vendor_actions = true`` *and* a per-account
  ``allow_vendor_reset = true``, behind an explicit confirmation field,
* it is never reachable from the refresh path, from the cache, or from GET.
"""

from __future__ import annotations

from typing import Any

from ..http import HttpError, http_json
from ..model import FetchResult, VendorReset, make_window, parse_iso

USAGE_URL = "https://chatgpt.com/backend-api/codex/wham/usage"
RESET_CREDITS_URL = "https://chatgpt.com/backend-api/codex/wham/rate-limit-reset-credits"
RESET_CONSUME_URL = "https://chatgpt.com/backend-api/codex/wham/rate-limit-reset-credits/consume"

USER_AGENT = "codex_cli_rs/0.20.0"

# Windows are labelled by their published duration, never by list position: a
# payload carrying only the weekly limit puts it in primary_window.
WINDOW_LABELS_BY_SECONDS = {18000: "Session", 604800: "Week"}


def _codex_headers(account: Any) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {account.resolve_token()}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
    }
    if account_id := account.options.get("account_id"):
        headers["ChatGPT-Account-ID"] = str(account_id)
    return headers


def _window_label(seconds: Any, fallback: str) -> str:
    try:
        return WINDOW_LABELS_BY_SECONDS.get(int(seconds), fallback)
    except (TypeError, ValueError):
        return fallback


def parse_usage(payload: dict[str, Any]) -> FetchResult:
    result = FetchResult()
    rate_limit = payload.get("rate_limit") or {}

    for key, fallback in (("primary_window", "Session"), ("secondary_window", "Week")):
        window = rate_limit.get(key) or {}
        used = window.get("used_percent")
        if used is None:
            continue
        seconds = window.get("limit_window_seconds")
        result.windows.append(
            make_window(
                "week" if _window_label(seconds, fallback) == "Week" else "short",
                _window_label(seconds, fallback),
                used,
                reset_at=parse_iso(window.get("reset_at")),
                window_seconds=seconds,
            )
        )

    if rate_limit.get("limit_reached"):
        result.extras.append("Limit reached: new requests are refused until a window resets.")

    banked = ((payload.get("rate_limit_reset_credits") or {}).get("available_count")) or 0
    plan = payload.get("plan_type")
    if plan:
        result.plan = str(plan).replace("_", " ").title()

    if banked:
        result.extras.append(
            f"{banked} banked reset{'s' if banked != 1 else ''} available. "
            "Redeeming one really does refill the windows."
        )

    # A banked reset is a real, finite, non-repeatable vendor quota reset. It is
    # surfaced only when credits are actually there, and never as a side effect
    # of reading usage.
    result.vendor_reset = VendorReset(
        supported=True,
        documented=False,
        banked=int(banked),
        note="Redeeming a banked reset really refills the Codex windows and moves the weekly "
        "reset date. It is not a dashboard refresh, and the credits are finite and do not "
        "replenish. This endpoint is undocumented, so treat the action as best-effort.",
    )
    if not banked:
        result.vendor_reset = VendorReset(
            supported=True,
            documented=False,
            banked=0,
            note="No banked reset is available. OpenAI states Support cannot reset Codex limits either.",
        )
    return result


def fetch(account: Any) -> FetchResult:
    if not account.resolve_token():
        raise RuntimeError("no Codex OAuth token configured for this account")
    try:
        payload, _ = http_json(USAGE_URL, headers=_codex_headers(account))
    except HttpError as exc:
        if exc.status == 403 and "json" not in exc.args[0]:
            raise HttpError(
                "HTTP 403 edge challenge: this endpoint expects the Codex CLI's browser-shaped "
                "headers and a client that passes the edge check",
                status=403,
            ) from exc
        raise
    result = parse_usage(payload)
    if not result.windows:
        raise HttpError("usage response carried no rate_limit windows", status=200)
    return result


def banked_reset_count(payload: dict[str, Any]) -> int:
    return int(((payload.get("rate_limit_reset_credits") or {}).get("available_count")) or 0)


def redeem_banked_reset(account: Any, force: bool = False) -> dict[str, Any]:
    """Redeem one banked Codex reset. A **write** against a finite vendor resource.

    Called only from the explicit, confirmed POST endpoint in
    :mod:`quota_rings.server`. Never from a refresh, never from a GET.
    """
    before_payload, _ = http_json(USAGE_URL, headers=_codex_headers(account))
    available = banked_reset_count(before_payload)
    if available <= 0:
        return {"ok": False, "status": "no_credits_banked", "message": "No banked reset is available."}

    rate_limit = before_payload.get("rate_limit") or {}
    used = [w.get("used_percent") for w in (rate_limit.get(k) or {} for k in ("primary_window", "secondary_window"))]
    used = [u for u in used if isinstance(u, (int, float))]
    if used and max(used) < 100 and not force:
        # Wasting a finite credit on a window that is not exhausted is the one
        # mistake worth blocking; --force is the documented escape hatch.
        return {
            "ok": False,
            "status": "not_exhausted",
            "message": "No window is fully used yet, so redeeming now would waste the credit.",
            "max_used_percent": max(used),
        }

    body, status = http_json(RESET_CONSUME_URL, headers=_codex_headers(account), method="POST", payload={})
    after_payload, _ = http_json(USAGE_URL, headers=_codex_headers(account))
    return {
        "ok": True,
        "status": "redeemed",
        "banked_before": available,
        "banked_after": banked_reset_count(after_payload),
        "http_status": status,
        "response_keys": sorted(body.keys()) if isinstance(body, dict) else [],
    }
