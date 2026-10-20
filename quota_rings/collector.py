"""Collector: fetch every account, isolate failures, cache, serialize.

The three behaviours this module is responsible for:

**Error isolation.** Accounts are fetched concurrently and each one fails on its
own. A Cloudflare 403 from Codex cannot blank the Claude ring.

**Stale-but-honest.** When an account fails but has a previous good reading, the
last known numbers stay visible, flagged ``stale`` with their age. A Codex edge
challenge is normal, not an outage worth hiding a number for.

**Refresh is not reset.** :meth:`Collector.get` re-reads vendor numbers when the
TTL expires, or when ``force=True`` (the Refresh button / ``?force=1``). It
consumes zero quota. Nothing in this module can spend a vendor quota reset; that
lives in :mod:`quota_rings.server` behind its own confirmation.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .config import Account, Settings, enabled_accounts
from .http import HttpError
from .model import AccountResult, FetchResult, iso
from .providers import fetcher_for, support_of
from .secrets import REDACTOR

MAX_ERROR_CHARS = 200


class Collector:
    """Fetches all configured accounts with per-account failure isolation."""

    def __init__(
        self,
        settings: Settings,
        fetcher_factory: Callable[[str], Callable[[Account], FetchResult]] = fetcher_for,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.fetcher_factory = fetcher_factory
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0
        self._last_good: dict[str, AccountResult] = {}

    # -- fetching ----------------------------------------------------------

    def _fetch_one(self, account: Account) -> AccountResult:
        support, support_note = support_of(account.provider)
        result = AccountResult(
            id=account.id,
            provider=account.provider,
            label=account.label,
            vendor=account.vendor,
            billing=account.billing,
            plan=account.plan,
            price=account.price,
            currency=account.currency,
            price_period=account.price_period,
            support=support,
            support_note=support_note,
        )
        started = self.clock()
        try:
            fetched = self.fetcher_factory(account.provider)(account)
            # Register the credential with the process redactor before anything
            # derived from the response can be rendered or logged.
            REDACTOR.learn(account.resolve_token())
            REDACTOR.learn(account.resolve_secondary_token())
            result.windows = list(fetched.windows)
            result.extras = [REDACTOR.text(x) or "" for x in fetched.extras]
            result.plan = fetched.plan or account.plan
            result.vendor_reset = fetched.vendor_reset
            result.ok = True
            with self._lock:
                self._last_good[account.id] = result
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            self._record_failure(account, result, exc)
        result.ms = int((self.clock() - started) * 1000)
        return result

    def _record_failure(self, account: Account, result: AccountResult, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, HttpError) and exc.body:
            # A vendor body can echo the request context. Redact, then truncate.
            message = f"{message} ({REDACTOR.text(exc.body) or ''})"
        result.ok = False
        result.error = (REDACTOR.text(message) or "unknown error")[:MAX_ERROR_CHARS]

        previous = self._last_good.get(account.id)
        if previous and previous.windows:
            # Keep the last good numbers visible, honestly labelled as old.
            result.windows = previous.windows
            result.extras = previous.extras
            result.plan = previous.plan
            result.vendor_reset = previous.vendor_reset
            result.stale = True
            result.last_success_at = previous.last_success_at
            result.error = (result.error or "") + " (showing last successful reading)"

    def _collect(self) -> dict[str, Any]:
        accounts = enabled_accounts(self.settings)
        if not accounts:
            raise RuntimeError("no enabled accounts in config; nothing to collect")

        with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
            results = list(pool.map(self._fetch_one, accounts))

        now = self.clock()
        for result in results:
            if result.ok:
                result.last_success_at = now

        return {
            "generated_at": iso(now),
            "ttl_seconds": self.settings.ttl_seconds,
            "vendor_actions_enabled": self.settings.enable_vendor_actions,
            "accounts": [r.to_json() for r in results],
            # The monthly figure only counts configured subscription prices.
            # Pay-as-you-go spend is not a subscription and is not summed here.
            "monthly_total": round(
                sum(a.price or 0.0 for a in accounts if a.billing == "subscription"), 2
            ),
        }

    # -- cache -------------------------------------------------------------

    def get(self, force: bool = False) -> dict[str, Any]:
        """Return the current snapshot, refetching when stale or forced.

        ``force`` is a *cache* invalidation. It re-reads the vendors' numbers.
        It does not and cannot reset a provider-enforced limit.
        """
        with self._lock:
            fresh = (
                self._cache is not None
                and not force
                and (self.clock() - self._cache_at) < self.settings.ttl_seconds
            )
            if fresh:
                return self._cache  # type: ignore[return-value]

        # Collect outside the lock so a slow vendor cannot block the HTTP thread
        # that is trying to serve a cached snapshot.
        payload = self._collect()
        with self._lock:
            self._cache = payload
            self._cache_at = self.clock()
        return payload

    def invalidate(self) -> None:
        """Drop the cache without refetching."""
        with self._lock:
            self._cache = None
            self._cache_at = 0.0

    def check(self) -> list[dict[str, Any]]:
        """Credential-presence report for ``--check``. Never prints a value."""
        report = []
        for account in enabled_accounts(self.settings):
            entry: dict[str, Any] = {
                "id": account.id,
                "provider": account.provider,
                "label": account.label,
                "billing": account.billing,
                "token_source": account.token_source(),
            }
            if account.secondary_token_ref.kind != "empty":
                entry["secondary_token_source"] = account.secondary_token_ref.describe()
            try:
                account.resolve_token()
                entry["credential"] = "resolved"
            except Exception as exc:  # noqa: BLE001 - report, never raise
                entry["credential"] = f"unavailable: {exc}"
            report.append(entry)
        return report
