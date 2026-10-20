"""Normalized, provider-independent data model.

Everything a provider fetcher returns is converted into these structures so the
HTTP layer has exactly one shape to serialize and one place to enforce "no
secrets in the payload".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Support tiers, in the words a reader can act on.
SUPPORT_SUPPORTED = "supported"        # documented public contract
SUPPORT_UNSTABLE = "unstable"          # works, but private/undocumented: may break
SUPPORT_UNSUPPORTED = "unsupported"    # no programmatic read path with this credential

BILLING_SUBSCRIPTION = "subscription"
BILLING_PAYG = "payg"
BILLING_UNKNOWN = "unknown"


def iso(ts: float | None) -> str | None:
    """Epoch seconds -> ISO-8601 UTC string (or None)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def parse_iso(value: Any) -> float | None:
    """ISO-8601 string -> epoch seconds. Tolerant of a trailing ``Z``."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clamp_pct(value: float) -> float:
    """Clamp a percentage into 0..100, rejecting NaN/inf."""
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        raise ValueError("percentage is not a finite number")
    return max(0.0, min(100.0, float(value)))


@dataclass(frozen=True)
class Window:
    """One quota window, e.g. "5 hour session" or "calendar month".

    ``used`` is *consumed* percent (0..100). Providers that report *remaining*
    must invert before constructing this -- see ``docs/PROVIDERS.md``.
    """

    key: str
    label: str
    used: float
    reset_at: float | None = None
    window_seconds: int | None = None
    sub: str | None = None

    @property
    def remaining(self) -> float:
        return round(100.0 - self.used, 1)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "used": round(self.used, 1),
            "remaining": self.remaining,
            "reset_at": iso(self.reset_at),
            "window_seconds": self.window_seconds,
            "sub": self.sub,
        }


@dataclass
class FetchResult:
    """What a provider fetcher returns. Never contains credentials."""

    windows: list[Window] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    plan: str | None = None
    # A real vendor quota reset is only ever advertised here when the provider
    # documents one. ``banked`` is how many are left, when the payload says.
    vendor_reset: "VendorReset | None" = None

    def __bool__(self) -> bool:
        return bool(self.windows)


@dataclass(frozen=True)
class VendorReset:
    """Advertised vendor quota reset for one account.

    This is *not* a cache refresh. ``documented`` is False for endpoints the
    vendor does not publish, and the UI says so.
    """

    supported: bool
    documented: bool = False
    banked: int | None = None
    note: str = ""


@dataclass
class AccountResult:
    """One configured account's state, as rendered by the UI."""

    id: str
    provider: str
    label: str
    vendor: str
    billing: str = BILLING_UNKNOWN
    plan: str | None = None
    price: float | None = None
    currency: str = "USD"
    price_period: str = "month"
    support: str = SUPPORT_UNSTABLE
    support_note: str = ""
    windows: list[Window] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    stale: bool = False
    last_success_at: float | None = None
    ms: int = 0
    vendor_reset: VendorReset | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.label,
            "vendor": self.vendor,
            "billing": self.billing,
            "plan": self.plan,
            "price": self.price,
            "currency": self.currency,
            "price_period": self.price_period,
            "support": self.support,
            "support_note": self.support_note,
            "windows": [w.to_json() for w in self.windows],
            "extras": list(self.extras),
            "ok": self.ok,
            "error": self.error,
            "stale": self.stale,
            "last_success_at": iso(self.last_success_at),
            "ms": self.ms,
            "vendor_reset": (
                {
                    "supported": self.vendor_reset.supported,
                    "documented": self.vendor_reset.documented,
                    "banked": self.vendor_reset.banked,
                    "note": self.vendor_reset.note,
                }
                if self.vendor_reset
                else None
            ),
        }


def make_window(
    key: str,
    label: str,
    used_pct: float,
    reset_at: float | None = None,
    window_seconds: int | None = None,
    sub: str | None = None,
) -> Window:
    """Build a Window with a validated percentage."""
    return Window(
        key=key,
        label=label,
        used=clamp_pct(used_pct),
        reset_at=reset_at,
        window_seconds=int(window_seconds) if window_seconds else None,
        sub=sub,
    )
