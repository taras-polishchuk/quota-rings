"""End-to-end HTTP tests against a real server on a real socket, with stubbed
vendor fetches. Nothing here touches the network or a real credential.

The two things these tests exist to prove:
  1. No secret ever reaches the browser, on any route, including error paths.
  2. The vendor-reset write is unreachable without all of its gates.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from quota_rings.collector import Collector
from quota_rings.config import Account, Settings
from quota_rings.model import FetchResult, VendorReset, make_window
from quota_rings.secrets import SecretRef
from quota_rings.server import QuotaRingsServer

SECRET = "sk-or-v1-FAKE-TEST-KEY-0123456789abcdefghijklmnop"


def account(account_id, provider="openrouter", **kwargs):
    kwargs.setdefault("billing", "payg")
    return Account(
        id=account_id, provider=provider, label=account_id, vendor=provider,
        token_ref=SecretRef("env", "TEST_TOKEN"), _token_cache=SECRET, **kwargs
    )


def fetcher(used=30.0, reset=None, extras=None, vendor_reset=None):
    def fetch(_account):
        return FetchResult(
            windows=[make_window("short", "Day", used, reset_at=reset, window_seconds=86400)],
            extras=extras or ["nothing secret here"],
            vendor_reset=vendor_reset,
        )
    return fetch


class ServerTestCase(unittest.TestCase):
    settings_kwargs: dict = {}

    def build(self, accounts, settings_kwargs=None, fetch=None):
        settings = Settings(port=0, accounts=accounts, **(settings_kwargs or self.settings_kwargs))
        collector = Collector(settings, fetcher_factory=lambda p: (fetch or fetcher()))
        # The real server class, on an ephemeral port: the test exercises the
        # same wiring a user gets, not a reimplementation of it.
        self.server = QuotaRingsServer(settings, collector)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read().decode("utf-8")

    def raw_get(self, path):
        """Send a request line verbatim over a raw socket.

        ``urllib`` normalises the path before it reaches the wire, so a test
        written with it never actually asks the server for ``/../server.py`` --
        it asks for ``/server.py``, which is a different question. This writes
        the request line byte-for-byte so the server sees what an attacker
        would send.
        """
        host, _, port = self.base.split("//", 1)[1].partition(":")
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            sock.sendall(
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        return status, body.decode("utf-8", "replace")

    def post(self, path, body):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()


class UsageApiTests(ServerTestCase):
    def setUp(self):
        self.build([account("or-main"), account("claude-max", "claude", billing="subscription", price=100)])

    def test_usage_returns_json(self):
        status, body = self.get("/api/usage")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertIn("generated_at", payload)
        self.assertIn("ttl_seconds", payload)

    def test_multiple_accounts_each_get_a_card(self):
        _, body = self.get("/api/usage")
        ids = {a["id"] for a in json.loads(body)["accounts"]}
        self.assertEqual(ids, {"or-main", "claude-max"})

    def test_subscription_prices_are_summed_and_payg_is_not(self):
        _, body = self.get("/api/usage")
        self.assertEqual(json.loads(body)["monthly_total"], 100.0)

    def test_index_html_is_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Quota Rings", body)

    def test_force_parameter_is_accepted(self):
        status, _ = self.get("/api/usage?force=1")
        self.assertEqual(status, 200)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_path_traversal_is_refused(self):
        """Traversal probes sent byte-for-byte over a raw socket.

        urllib normalises ``/../server.py`` to ``/server.py`` before it reaches
        the wire, so the previous version of this test asserted 404 against a
        request the server never received. These do not go through any client
        library: the request line below is exactly what an attacker would type.
        """
        probes = (
            "/../server.py",
            "/../../etc/passwd",
            "/../../../../etc/passwd",
            "/..%2fserver.py",
            "/%2e%2e%2fserver.py",
            "/..;/server.py",
            "/....//server.py",
            "/web/../../server.py",
            "/favicon.svg/../../server.py",
            "/..",
        )
        for probe in probes:
            status, body = self.raw_get(probe)
            self.assertEqual(status, 404, f"{probe} -> {status} {body[:80]!r}")
            # The refusal must not echo the file it refused to serve.
            self.assertNotIn("QuotaRingsServer", body, probe)
            self.assertNotIn("root:x:", body, probe)

    def test_a_real_static_asset_is_still_served(self):
        """The control: traversal is refused, ordinary files are not."""
        status, body = self.raw_get("/favicon.svg")
        self.assertEqual(status, 200)
        self.assertIn("<svg", body)

    def test_trailing_slash_is_not_a_second_spelling_of_the_reset_route(self):
        """``/vendor-reset/`` must 404, not quietly become ``/vendor-reset``."""
        status, payload = self.post("/api/accounts/or-main/vendor-reset/",
                                    {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("error"), "not found")


class SecretLeakTests(ServerTestCase):
    def setUp(self):
        self.build([account("or-main")])

    def test_usage_endpoint_never_contains_the_credential(self):
        _, body = self.get("/api/usage")
        self.assertNotIn(SECRET, body)
        self.assertNotIn("sk-or-v1-", body)

    def test_check_endpoint_reports_sources_without_values(self):
        _, body = self.get("/api/check")
        self.assertNotIn(SECRET, body)
        report = json.loads(body)
        self.assertEqual(report["accounts"][0]["token_source"], "env:TEST_TOKEN")
        self.assertEqual(report["accounts"][0]["credential"], "resolved")

    def test_health_endpoint_has_no_credential(self):
        _, body = self.get("/api/health")
        self.assertNotIn(SECRET, body)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_meta_endpoint_has_no_credential(self):
        _, body = self.get("/api/meta")
        self.assertNotIn(SECRET, body)

    def test_a_vendor_echoing_the_token_gets_it_scrubbed(self):
        def leaky(_account):
            raise RuntimeError(f"upstream said: your key {SECRET} is invalid")

        self.build([account("or-main")], fetch=leaky)
        _, body = self.get("/api/usage")
        self.assertNotIn(SECRET, body)
        self.assertIn("redacted", body)

    def test_error_bodies_do_not_leak_either(self):
        def leaky(_account):
            raise RuntimeError(f"boom {SECRET}")

        self.build([account("or-main")], fetch=leaky)
        status, body = self.get("/api/health")
        self.assertNotIn(SECRET, body)
        self.assertEqual(status, 200)


class VendorResetGateTests(ServerTestCase):
    """The single write in the program. Every gate is tested."""

    def setUp(self):
        self.redeem_calls = []

    def _codex_settings(self, allow_account=True, enable_actions=False):
        codex = account("codex", "codex", allow_vendor_reset=allow_account, billing="subscription")
        return Settings(
            port=0,
            accounts=[codex],
            enable_vendor_actions=enable_actions,
        )

    def _patch_redeem(self, outcome):
        from quota_rings.providers import codex as codex_module

        def fake_redeem(account, force=False):
            self.redeem_calls.append(account.id)
            return outcome

        codex_module.redeem_banked_reset = fake_redeem

    def test_refused_when_server_flag_is_off(self):
        self.build([account("codex", "codex", allow_vendor_reset=True)], settings_kwargs={})
        status, body = self.post("/api/accounts/codex/vendor-reset", {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 403)
        self.assertIn("enable_vendor_actions", body["error"])

    def test_refused_when_account_does_not_opt_in(self):
        self.build([account("codex", "codex", allow_vendor_reset=False)],
                   settings_kwargs={"enable_vendor_actions": True})
        status, body = self.post("/api/accounts/codex/vendor-reset", {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 403)
        self.assertIn("allow_vendor_reset", body["error"])

    def test_refused_without_explicit_confirmation(self):
        self.build([account("codex", "codex", allow_vendor_reset=True)],
                   settings_kwargs={"enable_vendor_actions": True})
        for body in ({}, {"confirm": "yes"}, {"confirm": "reset"}, {"confirm": ""}):
            status, out = self.post("/api/accounts/codex/vendor-reset", body)
            self.assertEqual(status, 400, body)
            self.assertIn("Confirmation required", out["error"])

    def test_refused_for_a_provider_without_a_vendor_reset(self):
        self.build([account("claude", "claude", allow_vendor_reset=True)],
                   settings_kwargs={"enable_vendor_actions": True})
        status, out = self.post("/api/accounts/claude/vendor-reset", {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 400)
        self.assertIn("no programmatic quota reset", out["error"])

    def test_unknown_account_is_404(self):
        self.build([account("codex", "codex", allow_vendor_reset=True)],
                   settings_kwargs={"enable_vendor_actions": True})
        status, _ = self.post("/api/accounts/ghost/vendor-reset", {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 404)

    def test_disabled_account_is_refused(self):
        codex = account("codex", "codex", allow_vendor_reset=True, enabled=False)
        self.build([codex], settings_kwargs={"enable_vendor_actions": True})
        status, out = self.post("/api/accounts/codex/vendor-reset", {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 400)
        self.assertIn("disabled", out["error"])

    def test_all_gates_open_permits_the_write(self):
        self._patch_redeem({"ok": True, "status": "redeemed", "banked_before": 2, "banked_after": 1})
        self.build([account("codex", "codex", allow_vendor_reset=True)],
                   settings_kwargs={"enable_vendor_actions": True})
        status, out = self.post("/api/accounts/codex/vendor-reset", {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(self.redeem_calls, ["codex"])

    def test_vendor_refusal_is_reported_as_409(self):
        self._patch_redeem({"ok": False, "status": "no_credits_banked", "message": "none left"})
        self.build([account("codex", "codex", allow_vendor_reset=True)],
                   settings_kwargs={"enable_vendor_actions": True})
        status, out = self.post("/api/accounts/codex/vendor-reset", {"confirm": "redeem-banked-reset"})
        self.assertEqual(status, 409)
        self.assertFalse(out["ok"])

    def test_get_can_never_trigger_a_reset(self):
        self._patch_redeem({"ok": True})
        self.build([account("codex", "codex", allow_vendor_reset=True)],
                   settings_kwargs={"enable_vendor_actions": True})
        self.get("/api/usage")
        self.get("/api/usage?force=1")
        self.get("/api/health")
        self.assertEqual(self.redeem_calls, [], "refresh must never spend a vendor reset")


class RefreshSemanticsTests(ServerTestCase):
    def setUp(self):
        self.calls = {"n": 0}

    def counting(self, _account):
        self.calls["n"] += 1
        return FetchResult(windows=[make_window("short", "Day", 30.0)])

    def test_refresh_is_a_cache_invalidation_not_a_reset(self):
        self.build([account("or-main")], fetch=self.counting)
        self.get("/api/usage")
        self.get("/api/usage")
        self.assertEqual(self.calls["n"], 1, "TTL caches")
        self.get("/api/usage?force=1")
        self.assertEqual(self.calls["n"], 2, "force refetches the numbers")

    def test_usage_payload_carries_no_reset_capability_flag_for_refresh(self):
        self.build([account("or-main")], fetch=self.counting)
        _, body = self.get("/api/usage")
        payload = json.loads(body)
        self.assertIn("vendor_actions_enabled", payload)
        self.assertFalse(payload["vendor_actions_enabled"])


class VendorResetAdvertisingTests(ServerTestCase):
    def test_card_states_no_reset_when_provider_has_none(self):
        self.build([account("or-main")],
                   fetch=fetcher(vendor_reset=VendorReset(
                       supported=False, documented=True, note="limit_reset is a schedule, not an action.")))
        _, body = self.get("/api/usage")
        reset = json.loads(body)["accounts"][0]["vendor_reset"]
        self.assertFalse(reset["supported"])
        self.assertIn("schedule", reset["note"])

    def test_card_advertises_banked_resets_when_the_payload_reports_them(self):
        self.build([account("codex", "codex")],
                   fetch=fetcher(vendor_reset=VendorReset(
                       supported=True, documented=False, banked=2, note="really refills the windows")))
        _, body = self.get("/api/usage")
        reset = json.loads(body)["accounts"][0]["vendor_reset"]
        self.assertTrue(reset["supported"])
        self.assertEqual(reset["banked"], 2)
        self.assertFalse(reset["documented"], "an undocumented endpoint must say so")


if __name__ == "__main__":
    unittest.main()
