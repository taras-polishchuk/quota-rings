"""MiniMax Token Plan (M Plan) -- subscription quota on a Subscription Key.

Endpoint: ``GET https://api.minimax.io/v1/token_plan/remains``.

Three traps this module exists to get right, all of which produce a
plausible-looking wrong ring if ignored:

1. **HTTP 200 is not success.** Auth failures arrive as ``200`` with
   ``base_resp.status_code`` 1004 (no key) or 2049 (invalid key). The envelope is
   checked before ``model_remains`` is read.
2. **The percentages are *remaining*, not consumed.** They are inverted for a
   used-% ring.
3. **Timestamps are epoch milliseconds**, and the window length is per row
   (``general`` rolls 5 h, ``video`` rolls 24 h) rather than a constant.

A pay-as-you-go API key has no quota-meter endpoint at all. Passing one yields a
named "not a plan key" state instead of a zero ring.
"""

from __future__ import annotations

from typing import Any

from ..http import http_json
from ..model import FetchResult, VendorReset, make_window

REMAINS_URL = "https://api.minimax.io/v1/token_plan/remains"

# MiniMax error codes seen in the envelope (documented in the payload, not in docs).
ERR_MESSAGES = {
    1004: "no API key was sent",
    1008: "insufficient balance",
    2013: "invalid API key",
    2049: "invalid API key",
}


class MiniMaxEnvelopeError(RuntimeError):
    """HTTP 200 whose body carries base_resp.status_code != 0."""


def check_envelope(payload: dict[str, Any]) -> None:
    """Raise unless the body-level envelope reports success."""
    base = payload.get("base_resp") or {}
    code = base.get("status_code")
    if code in (0, None):
        return
    message = base.get("status_msg") or ERR_MESSAGES.get(code, "unknown error")
    raise MiniMaxEnvelopeError(f"HTTP 200 but API error {code}: {message}")


def parse_remains(payload: dict[str, Any]) -> FetchResult:
    """Normalize ``model_remains`` into windows + extras."""
    check_envelope(payload)
    result = FetchResult()

    for row in payload.get("model_remains") or []:
        if not isinstance(row, dict):
            continue
        model = row.get("model_name")
        if model == "general":
            start_ms, end_ms = row.get("start_time"), row.get("end_time")
            if start_ms is None or end_ms is None:
                continue
            seconds = int((float(end_ms) - float(start_ms)) / 1000)
            remaining_pct = row.get("current_interval_remaining_percent")
            if remaining_pct is not None:
                result.windows.append(
                    make_window(
                        "short",
                        f"{round(seconds / 3600)} hour",
                        100.0 - float(remaining_pct),  # inverted: payload is remaining
                        reset_at=float(end_ms) / 1000.0,  # epoch ms -> seconds
                        window_seconds=seconds,
                    )
                )
            w_start, w_end = row.get("weekly_start_time"), row.get("weekly_end_time")
            weekly_remaining = row.get("current_weekly_remaining_percent")
            if weekly_remaining is not None and w_end is not None:
                wseconds = int((float(w_end) - float(w_start or 0)) / 1000) if w_start else 7 * 86400
                result.windows.append(
                    make_window(
                        "week",
                        "Week",
                        100.0 - float(weekly_remaining),
                        reset_at=float(w_end) / 1000.0,
                        window_seconds=wseconds,
                    )
                )
        elif model == "video":
            # Video has a weekly window only -- no 5-hour window exists for it.
            used = row.get("current_interval_usage_count")
            total = row.get("current_interval_total_count")
            used_w = row.get("current_weekly_usage_count")
            total_w = row.get("current_weekly_total_count")
            start_ms, end_ms = row.get("start_time"), row.get("end_time")
            hours = round((float(end_ms or 0) - float(start_ms or 0)) / 3600000) if start_ms and end_ms else None
            if used is not None and total is not None:
                window = f"per {hours} hour window" if hours else "per window"
                result.extras.append(f"Video: {used}/{total} {window}, {used_w}/{total_w} this week")
            elif weekly_remaining := row.get("current_weekly_remaining_percent"):
                result.extras.append(f"Video this week: {100 - float(weekly_remaining):.0f}% used")

    if not result.windows and not result.extras:
        raise RuntimeError("response carried no model_remains rows")

    result.vendor_reset = VendorReset(
        supported=False,
        documented=False,
        note="MiniMax documents no reset action for either plan window. Refresh only re-reads.",
    )
    return result


def fetch(account: Any) -> FetchResult:
    token = account.resolve_token()
    if not token:
        raise RuntimeError("no MiniMax subscription key configured for this account")
    if token.startswith("sk-api-"):
        # Pay-as-you-go key: bills the account balance, spends no plan quota and
        # has no meter endpoint. Naming that beats drawing an empty ring.
        return FetchResult(
            extras=[
                "This is a pay-as-you-go API key. It bills the account balance and has no plan quota meter."
            ],
            vendor_reset=VendorReset(supported=False, documented=False),
        )

    payload, _ = http_json(REMAINS_URL, headers={"Authorization": f"Bearer {token}"})
    return parse_remains(payload)
