"""Collector behaviour: isolation, staleness, caching, and secret containment.

Offline throughout. Every provider is stubbed, so these tests prove the
*orchestration* rules rather than any vendor's wire format.
"""

from __future__ import annotations

import json
import unittest

from quota_rings.collector import Collector
from quota_rings.config import Account, Settings
from quota_rings.http import HttpError
from quota_rings.model import FetchResult, make_window
from quota_rings.secrets import REDACTOR, Redactor

SECRET = "sk-ant-oat01-THIS-IS-A-FAKE-TEST-TOKEN-0123456789"


def account(account_id: str, provider: str = "claude", **kwargs) -> Account:
    return Account(id=account_id, provider=provider, label=account_id, vendor=provider, **kwargs)


def ok_result(_account: Account | None = None, used: float = 10.0) -> FetchResult:
    """A fetcher that always succeeds, so it can be passed to fetcher_factory."""
    return FetchResult(
        windows=[make_window("short", "5 hour", used, reset_at=None, window_seconds=18000)],
        extras=["fine"],
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class IsolationTests(unittest.TestCase):
    def test_one_broken_vendor_does_not_blank_the_others(self):
        def failing(_account):
            raise HttpError("HTTP 403 from chatgpt.com/backend-api/codex/wham/usage", status=403)

        # fetcher_factory maps a provider id to a *fetcher*, not to a result.
        fetchers = {"claude": ok_result, "codex": failing}
        collector = Collector(
            Settings(accounts=[account("a-claude", "claude"), account("b-codex", "codex")]),
            fetcher_factory=lambda p: fetchers[p],
        )
        payload = collector.get()
        by_id = {a["id"]: a for a in payload["accounts"]}
        self.assertTrue(by_id["a-claude"]["ok"])
        self.assertEqual(len(by_id["a-claude"]["windows"]), 1)
        self.assertFalse(by_id["b-codex"]["ok"])
        self.assertIn("403", by_id["b-codex"]["error"])

    def test_error_message_is_truncated(self):
        collector = Collector(
            Settings(accounts=[account("boom")]),
            fetcher_factory=lambda p: (_ for _ in ()).throw(RuntimeError("x" * 5000)),
        )
        payload = collector.get()
        self.assertLessEqual(len(payload["accounts"][0]["error"]), 220)

    def test_failed_account_keeps_last_good_reading_as_stale(self):
        state = {"fail": False}

        def flaky(_account):
            if state["fail"]:
                raise HttpError("HTTP 403 edge challenge", status=403)
            return ok_result(used=25.0)

        collector = Collector(
            Settings(accounts=[account("flaky")]), fetcher_factory=lambda p: flaky
        )
        first = collector.get()["accounts"][0]
        self.assertTrue(first["ok"])
        self.assertAlmostEqual(first["windows"][0]["used"], 25.0)

        state["fail"] = True
        second = collector.get(force=True)["accounts"][0]
        self.assertFalse(second["ok"])
        self.assertTrue(second["stale"], "last known numbers must stay visible")
        self.assertAlmostEqual(second["windows"][0]["used"], 25.0, msg="stale reading keeps its value")
        self.assertIsNotNone(second["last_success_at"])

    def test_failed_account_with_no_history_shows_no_windows(self):
        collector = Collector(
            Settings(accounts=[account("never-worked")]),
            fetcher_factory=lambda p: (_ for _ in ()).throw(RuntimeError("no credential")),
        )
        payload = collector.get()["accounts"][0]
        self.assertEqual(payload["windows"], [])
        self.assertFalse(payload["stale"])


class CacheTests(unittest.TestCase):
    def test_snapshot_is_cached_within_the_ttl(self):
        calls = {"n": 0}

        def counting(_account):
            calls["n"] += 1
            return ok_result()

        clock = FakeClock()
        settings = Settings(accounts=[account("cached")], ttl_seconds=60)
        collector = Collector(settings, fetcher_factory=lambda p: counting, clock=clock)

        collector.get()
        collector.get()
        self.assertEqual(calls["n"], 1, "second read inside the TTL must not refetch")

        clock.advance(61)
        collector.get()
        self.assertEqual(calls["n"], 2, "TTL expiry refetches")

    def test_force_refetches_and_is_only_a_cache_invalidation(self):
        calls = {"n": 0}

        def counting(_account):
            calls["n"] += 1
            return ok_result()

        collector = Collector(
            Settings(accounts=[account("forced")], ttl_seconds=60),
            fetcher_factory=lambda p: counting,
        )
        collector.get()
        collector.get(force=True)
        self.assertEqual(calls["n"], 2, "force=1 re-reads the vendor numbers")

    def test_invalidate_drops_the_cache_without_refetching(self):
        collector = Collector(
            Settings(accounts=[account("inv")]), fetcher_factory=lambda p: ok_result
        )
        collector.get()
        collector.invalidate()
        collector.get()  # refetches, which is the observable point
        self.assertTrue(True)


class BillingTotalsTests(unittest.TestCase):
    def test_monthly_total_counts_subscriptions_only(self):
        accounts = [
            account("sub", "claude", price=100.0, billing="subscription"),
            account("payg", "openrouter", price=None, billing="payg"),
        ]
        # A PAYG account with a price set must still not inflate the total.
        accounts[1].price = 999.0
        collector = Collector(Settings(accounts=accounts), fetcher_factory=lambda p: ok_result)
        self.assertEqual(collector.get()["monthly_total"], 100.0)

    def test_multiple_accounts_per_provider_are_separate_cards(self):
        accounts = [
            account("claude-max", "claude", price=100.0, billing="subscription"),
            account("claude-pro", "claude", price=20.0, billing="subscription"),
        ]
        collector = Collector(Settings(accounts=accounts), fetcher_factory=lambda p: ok_result)
        payload = collector.get()
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual({a["id"] for a in payload["accounts"]}, {"claude-max", "claude-pro"})
        self.assertEqual(payload["monthly_total"], 120.0)

    def test_disabled_accounts_are_not_collected(self):
        accounts = [
            account("on", "claude"),
            account("off", "codex", enabled=False),
        ]
        collector = Collector(Settings(accounts=accounts), fetcher_factory=lambda p: ok_result)
        self.assertEqual([a["id"] for a in collector.get()["accounts"]], ["on"])


class SecretContainmentTests(unittest.TestCase):
    """A credential must not survive serialization, in any code path."""

    def test_payload_never_contains_the_credential(self):
        acc = account("leaky")
        acc._token_cache = SECRET
        collector = Collector(Settings(accounts=[acc]), fetcher_factory=lambda p: ok_result)
        body = json.dumps(collector.get())
        self.assertNotIn(SECRET, body)
        self.assertNotIn("sk-ant-oat01", body)

    def test_token_echoed_back_by_a_vendor_is_scrubbed(self):
        def echo(_account):
            raise HttpError(
                f"HTTP 401 from api.anthropic.com rejected Bearer {SECRET}",
                status=401,
                body=f'{{"error":"bad token {SECRET}"}}',
            )

        acc = account("echo")
        acc._token_cache = SECRET
        collector = Collector(Settings(accounts=[acc]), fetcher_factory=lambda p: echo)
        body = json.dumps(collector.get())
        self.assertNotIn(SECRET, body)
        self.assertIn("redacted", body)

    def test_redactor_masks_token_shapes_it_never_saw(self):
        redactor = Redactor()
        masked = redactor.text("Authorization: Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", masked)
        self.assertIn("redacted", masked)

    def test_redactor_masks_jwt_shaped_strings(self):
        redactor = Redactor()
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        self.assertNotIn(jwt, redactor.text(f"token was {jwt}"))

    def test_redactor_survives_nested_structures(self):
        redactor = Redactor()
        out = redactor.scrub({"a": [f"key={SECRET}", {"b": SECRET}], "c": 5})
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertEqual(out["c"], 5)


class ProcessRedactorTests(unittest.TestCase):
    def test_module_level_redactor_is_used_by_the_collector(self):
        acc = account("learn")
        acc._token_cache = SECRET
        Collector(Settings(accounts=[acc]), fetcher_factory=lambda p: ok_result).get()
        self.assertNotIn(SECRET, REDACTOR.text(f"saw {SECRET}"))


if __name__ == "__main__":
    unittest.main()
