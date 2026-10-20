"""OpenRouter -- the best-documented provider here, and the only SUPPORTED one.

Two *different* credentials, with different blast radius:

* **inference key** (``sk-or-v1-...``) -> ``GET /api/v1/key``. Per-key spend cap,
  usage counters and the reset schedule for that key.
* **management key** -> ``GET /api/v1/credits`` (account balance) and
  ``GET /api/v1/keys`` (list of keys). A management key cannot be used for
  inference and an inference key cannot read admin routes, so they are separate
  config slots and one never substitutes for the other.

What matters for honesty here:

* ``limit_reset`` is a *schedule* (daily/weekly/monthly/null), not an action.
  Nothing on this page resets an OpenRouter limit.
* Adding more keys does not buy more capacity -- limits are governed per
  account. Several keys are one wallet with several caps.
* ``rate_limit`` inside the payload is deprecated; ignored deliberately.
"""

from __future__ import annotations

from typing import Any

from ..http import HttpError, http_json
from ..model import FetchResult, VendorReset, make_window

BASE = "https://openrouter.ai/api/v1"
KEY_URL = f"{BASE}/key"
CREDITS_URL = f"{BASE}/credits"
KEYS_URL = f"{BASE}/keys"

RESET_LABELS = {"daily": "Day", "weekly": "Week", "monthly": "Month"}


def _window_seconds(limit_reset: str | None) -> int | None:
    return {"daily": 86400, "weekly": 604800, "monthly": 30 * 86400}.get(limit_reset or "")


def _midnight_utc_after(seconds: int) -> float:
    """Next UTC midnight, used as the reset instant for a window we cannot date.

    OpenRouter documents the schedule (daily 00:00 UTC, weekly Monday 00:00 UTC,
    monthly the 1st) but the per-key payload carries no reset *instant*. So the
    ring gets the window length and the schedule name, and the UI derives the
    countdown. The instant is the next matching boundary, labelled as derived.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    if seconds == 604800:  # Monday 00:00 UTC
        days_ahead = (7 - now.weekday()) % 7 or 7
        target = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # daily and monthly both roll over at 00:00 UTC
        target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return target.timestamp()


def parse_key_payload(payload: dict[str, Any], data: dict[str, Any]) -> FetchResult:
    """Build the ring from the /key envelope.

    Shape: ``{"data": {"limit", "limit_remaining", "limit_reset", "usage", ...}}``.
    A key with no spend cap (``limit`` is None) has no percentage to draw, and
    gets no ring -- usage alone is a currency number, not a quota window.
    """
    result = FetchResult()

    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    if limit is not None and remaining is not None and float(limit) > 0:
        limit_reset = data.get("limit_reset")
        seconds = _window_seconds(limit_reset)
        label = RESET_LABELS.get(limit_reset or "", "Spend cap")
        used_pct = (float(limit) - float(remaining)) / float(limit) * 100.0
        result.windows.append(
            make_window(
                "spend_cap",
                label,
                used_pct,
                reset_at=_midnight_utc_after(seconds) if seconds else None,
                window_seconds=seconds,
                sub=f"{float(remaining):.2f} of {float(limit):.2f} left",
            )
        )
    else:
        result.extras.append("No spend cap on this key: usage is billed per token, no quota window.")

    usage = data.get("usage")
    if usage is not None:
        result.extras.append(f"Usage on this key: ${float(usage):.2f}")

    for field_name, label in (("usage_daily", "today"), ("usage_weekly", "this week"), ("usage_monthly", "this month")):
        value = data.get(field_name)
        if value:
            result.extras.append(f"Usage {label}: ${float(value):.2f}")

    # The budget that actually blocks, per OpenRouter's own precedence
    # (guardrail -> workspace -> key), is named in effective_budget.
    effective = data.get("effective_budget")
    if isinstance(effective, dict):
        result.extras.append(
            f"Blocking budget: {effective.get('budget_type', 'budget')} "
            f"${float(effective.get('limit', 0)):.2f} per {effective.get('reset_period', 'period')}"
        )

    result.vendor_reset = VendorReset(
        supported=False,
        documented=True,
        note="limit_reset is a schedule (daily/weekly/monthly UTC), not an action. "
        "The cap rolls over on its own; no API call refills it.",
    )
    return result


def fetch(account: Any) -> FetchResult:
    inference_key = account.resolve_token()
    if not inference_key:
        raise RuntimeError("no OpenRouter inference key configured for this account")

    payload, _ = http_json(KEY_URL, headers={"Authorization": f"Bearer {inference_key}"})
    data = payload.get("data") or {}
    if not data:
        raise HttpError("OpenRouter /key returned no data object", status=200)

    result = parse_key_payload(payload, data)
    if label := data.get("label"):
        # Operator-chosen key name from the vendor, not an identity.
        result.extras.append(f"Key label: {label}")
    if data.get("disabled"):
        result.extras.append("This key is disabled in OpenRouter.")

    # Optional second credential: account-level balance.
    management_key = account.resolve_secondary_token()
    if management_key:
        try:
            credits, _ = http_json(CREDITS_URL, headers={"Authorization": f"Bearer {management_key}"})
            cdata = credits.get("data") or {}
            total, used = cdata.get("total_credits"), cdata.get("total_usage")
            if total is not None and used is not None:
                left = float(total) - float(used)
                result.extras.append(f"Account credits: ${left:.2f} of ${float(total):.2f} left")
        except HttpError as exc:
            # A missing management key must not cost the ring that already worked.
            result.extras.append(f"Account credits unavailable ({exc}). Inference key still valid.")
    else:
        result.extras.append("Account credits need a management key (config: management_token).")
    return result
