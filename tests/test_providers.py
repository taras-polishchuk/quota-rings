"""Provider parsing: every documented trap from the research, pinned as a test.

These are the tests that would fail if someone "simplified" a parser back into
lying about a number. Each one names the trap it guards.
"""

from __future__ import annotations

import unittest

from quota_rings.config import Account
from quota_rings.providers import claude, codex, copilot, minimax, mistral, openrouter
from quota_rings.secrets import SecretRef

from .helpers import load_fixture


def account(provider: str, token: str = "test-token", **options) -> Account:
    return Account(
        id=f"{provider}-test",
        provider=provider,
        label=provider,
        vendor=provider,
        options=options,
        token_ref=SecretRef("env", "TEST_TOKEN"),
        _token_cache=token,
    )


class ClaudeTests(unittest.TestCase):
    """TRAP: utilization is a percent 0..100, never a fraction."""

    def test_fresh_window_reading_one_percent_is_one_percent(self):
        result = claude.parse_usage(load_fixture("claude_usage.json"))
        five_hour = next(w for w in result.windows if w.key == "short")
        self.assertEqual(five_hour.used, 1.0)
        self.assertEqual(five_hour.remaining, 99.0)

    def test_weekly_window(self):
        result = claude.parse_usage(load_fixture("claude_usage.json"))
        week = next(w for w in result.windows if w.key == "week")
        self.assertEqual(week.used, 42.5)

    def test_reset_instants_are_parsed_from_iso(self):
        result = claude.parse_usage(load_fixture("claude_usage.json"))
        five_hour = next(w for w in result.windows if w.key == "short")
        self.assertIsNotNone(five_hour.reset_at)
        self.assertGreater(five_hour.reset_at, 0)

    def test_usage_credits_stay_text_not_a_ring(self):
        result = claude.parse_usage(load_fixture("claude_usage.json"))
        self.assertTrue(any("Usage credits" in e for e in result.extras))
        self.assertEqual(len(result.windows), 2, "credits must not become a third window")

    def test_no_vendor_reset_is_claimed(self):
        result = claude.parse_usage(load_fixture("claude_usage.json"))
        self.assertFalse(result.vendor_reset.supported)

    def test_console_api_key_families_are_recognised(self):
        # Only the family prefix matters; the rest is deliberately too short to
        # look like a key, so the self-scan never sees a realistic string.
        for prefix in ("sk-ant-api", "sk-ant-usr", "sk-ant-svc", "sk-ant-admin"):
            self.assertTrue(claude._looks_like_console_key(prefix + "-X"))
        self.assertFalse(claude._looks_like_console_key("sk-ant-oat01-X"))

    def test_console_api_key_gets_no_ring(self):
        result = claude.fetch(account("claude", token="sk-ant-api-X"))
        self.assertEqual(result.windows, [])
        self.assertIn("pay-as-you-go", result.extras[0])


class OpenRouterTests(unittest.TestCase):
    def test_spend_cap_becomes_a_ring(self):
        result = openrouter.parse_key_payload({}, load_fixture("openrouter_key.json")["data"])
        window = result.windows[0]
        # 42.5 of 100 remaining -> 57.5% used
        self.assertAlmostEqual(window.used, 57.5)
        self.assertAlmostEqual(window.remaining, 42.5)
        self.assertEqual(window.window_seconds, 604800, "weekly reset -> 7 day window")

    def test_limit_reset_is_a_schedule_not_an_action(self):
        result = openrouter.parse_key_payload({}, load_fixture("openrouter_key.json")["data"])
        self.assertFalse(result.vendor_reset.supported)
        self.assertIn("schedule", result.vendor_reset.note.lower())

    def test_key_without_spend_cap_gets_no_ring(self):
        result = openrouter.parse_key_payload({}, {"label": "x", "limit": None, "usage": 3.0})
        self.assertEqual(result.windows, [], "usage alone is currency, not a quota window")
        self.assertTrue(any("No spend cap" in e for e in result.extras))

    def test_monthly_limit_is_never_inferred_from_usage(self):
        result = openrouter.parse_key_payload({}, {"limit": None, "usage": 999.0})
        self.assertEqual(result.windows, [])


class MiniMaxTests(unittest.TestCase):
    """TRAP: HTTP 200 can carry an error envelope."""

    def test_auth_error_inside_http_200_raises(self):
        from quota_rings.providers.minimax import MiniMaxEnvelopeError

        with self.assertRaises(MiniMaxEnvelopeError) as ctx:
            minimax.parse_remains(load_fixture("minimax_auth_error.json"))
        self.assertIn("1004", str(ctx.exception))

    def test_remaining_percent_is_inverted(self):
        result = minimax.parse_remains(load_fixture("minimax_remains.json"))
        short = next(w for w in result.windows if w.key == "short")
        self.assertAlmostEqual(short.used, 25.0, msg="payload says 75 remaining -> 25 used")
        self.assertAlmostEqual(short.remaining, 75.0)

    def test_epoch_milliseconds_are_divided_by_1000(self):
        result = minimax.parse_remains(load_fixture("minimax_remains.json"))
        short = next(w for w in result.windows if w.key == "short")
        # fixture: 1763628000000 ms -> 1763628000 s
        self.assertAlmostEqual(short.reset_at, 1763628000.0)
        self.assertEqual(short.window_seconds, 18000, "5h window derived from the row itself")

    def test_video_is_text_because_it_has_no_five_hour_window(self):
        result = minimax.parse_remains(load_fixture("minimax_remains.json"))
        self.assertTrue(any("Video" in e for e in result.extras))
        keys = [w.key for w in result.windows]
        self.assertNotIn("video", keys)

    def test_payg_key_gets_no_ring(self):
        result = minimax.fetch(account("minimax", token="sk-api-not-a-plan-key"))
        self.assertEqual(result.windows, [])
        self.assertIn("pay-as-you-go", result.extras[0])


class CodexTests(unittest.TestCase):
    """TRAP: windows are labelled by duration, never by list position."""

    def test_windows_labelled_by_duration(self):
        result = codex.parse_usage(load_fixture("codex_usage.json"))
        labels = [w.label for w in result.windows]
        self.assertEqual(labels, ["Session", "Week"])

    def test_weekly_only_payload_is_not_called_a_session(self):
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 50.0,
                    "reset_at": 1764200000,
                    "limit_window_seconds": 604800,
                }
            },
            "rate_limit_reset_credits": {"available_count": 0},
        }
        result = codex.parse_usage(payload)
        self.assertEqual(result.windows[0].label, "Week")

    def test_banked_reset_is_reported_but_not_performed(self):
        result = codex.parse_usage(load_fixture("codex_usage.json"))
        self.assertTrue(result.vendor_reset.supported)
        self.assertEqual(result.vendor_reset.banked, 2)
        self.assertFalse(result.vendor_reset.documented, "endpoint is undocumented; say so")

    def test_no_banked_reset_means_zero_not_a_fake_supply(self):
        payload = load_fixture("codex_usage.json")
        payload["rate_limit_reset_credits"] = {"available_count": 0}
        result = codex.parse_usage(payload)
        self.assertEqual(result.vendor_reset.banked, 0)

    def test_deprecated_rate_limit_field_is_not_read(self):
        payload = load_fixture("codex_usage.json")
        payload["data"] = {"rate_limit": {"used": 99, "limit": 100}}
        result = codex.parse_usage(payload)
        self.assertEqual([w.used for w in result.windows], [18.0, 91.5])


class CopilotTests(unittest.TestCase):
    def test_ai_credit_regime_is_labelled(self):
        result = copilot.parse_user(load_fixture("copilot_user.json"))
        self.assertTrue(any("AI credits" in e for e in result.extras))
        window = result.windows[0]
        self.assertAlmostEqual(window.used, 78.7, places=1)
        self.assertEqual(window.window_seconds, 30 * 86400)

    def test_entitlement_comes_from_the_payload_not_a_table(self):
        result = copilot.parse_user(load_fixture("copilot_user.json"))
        self.assertIn("320/1500", result.windows[0].sub)

    def test_legacy_regime_is_labelled_differently(self):
        payload = {
            "quota_snapshots": {
                "premium_interactions": {
                    "entitlement": 300, "remaining": 42, "percent_remaining": 14.0, "unlimited": False
                }
            }
        }
        result = copilot.parse_user(payload)
        self.assertTrue(any("legacy premium requests" in e for e in result.extras))
        self.assertIn("requests", result.windows[0].sub)

    def test_unlimited_account_gets_no_fake_full_ring(self):
        result = copilot.parse_user({"quota_snapshots": {"premium_interactions": {"unlimited": True}}})
        self.assertEqual(result.windows, [])

    def test_empty_payload_yields_text_not_a_guess(self):
        result = copilot.parse_user({})
        self.assertEqual(result.windows, [])
        self.assertTrue(any("nothing to draw" in e for e in result.extras))


class MistralTests(unittest.TestCase):
    """Mistral is honest about being unreachable for individuals."""

    def test_payg_and_subscription_are_named_unsupported(self):
        for kind in ("payg", "subscription"):
            result = mistral.fetch(account("mistral", kind=kind))
            self.assertEqual(result.windows, [], f"{kind} must not draw a ring")
            self.assertIn("Enterprise-only", result.extras[0])

    def test_token_ceilings_are_not_turned_into_a_percentage(self):
        result = mistral.parse_rate_limit({
            "requests_per_second": 1,
            "tokens_limits_by_model": {"large": {"tokens_per_minute": 1000000, "tokens_per_month": 1000000000}},
        })
        self.assertEqual(result.windows, [])
        self.assertTrue(any("ceilings, not consumption" in e for e in result.extras))


if __name__ == "__main__":
    unittest.main()
