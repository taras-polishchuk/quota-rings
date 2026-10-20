"""Config loading, secret references, and the shipped config template."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from quota_rings.config import ConfigError, load_config
from quota_rings.secrets import SecretError, SecretRef

TEMPLATE = Path(__file__).resolve().parent.parent / "config.example.toml"

MINIMAL = """
[server]
host = "127.0.0.1"
port = 8790
ttl_seconds = 60

[[account]]
id = "claude-max"
provider = "claude"
label = "Claude"
vendor = "Anthropic"
plan = "Max"
billing = "subscription"
price = 100
token = "env:CLAUDE_MAX_TOKEN"
"""


def write_config(body: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    handle.write(body)
    handle.close()
    return handle.name


class SecretRefTests(unittest.TestCase):
    def test_env_scheme_parses(self):
        ref = SecretRef.parse("env:SOME_TOKEN")
        self.assertEqual((ref.kind, ref.target), ("env", "SOME_TOKEN"))

    def test_env_scheme_tolerates_double_slash(self):
        self.assertEqual(SecretRef.parse("env://SOME_TOKEN").target, "SOME_TOKEN")

    def test_file_and_command_schemes_parse(self):
        self.assertEqual(SecretRef.parse("file:/run/secrets/k").kind, "file")
        self.assertEqual(SecretRef.parse("command:gh auth token").kind, "command")

    def test_absent_reference_is_empty_not_an_error(self):
        self.assertEqual(SecretRef.parse(None).kind, "empty")
        self.assertEqual(SecretRef.parse("").kind, "empty")

    def test_unknown_scheme_is_rejected(self):
        with self.assertRaises(SecretError):
            SecretRef.parse("vault:something/secret")

    def test_env_resolution_reads_the_environment(self):
        os.environ["QUOTA_RINGS_TEST_TOKEN"] = "value-from-env"
        try:
            self.assertEqual(SecretRef("env", "QUOTA_RINGS_TEST_TOKEN").resolve(), "value-from-env")
        finally:
            del os.environ["QUOTA_RINGS_TEST_TOKEN"]

    def test_missing_env_is_a_clear_error(self):
        with self.assertRaises(SecretError) as ctx:
            SecretRef("env", "QUOTA_RINGS_ABSENT_TOKEN").resolve()
        self.assertIn("QUOTA_RINGS_ABSENT_TOKEN", str(ctx.exception))

    def test_file_reference_reads_first_line(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("first-line-secret\nsecond\n")
            path = handle.name
        try:
            self.assertEqual(SecretRef("file", path).resolve(), "first-line-secret")
        finally:
            os.unlink(path)

    def test_describe_never_includes_a_value(self):
        self.assertEqual(SecretRef("env", "MY_TOKEN").describe(), "env:MY_TOKEN")


class ConfigTests(unittest.TestCase):
    def tearDown(self):
        for name in ("QUOTA_RINGS_TEST_TOKEN",):
            os.environ.pop(name, None)

    def test_minimal_config_loads(self):
        path = write_config(MINIMAL)
        try:
            settings = load_config(path)
            self.assertEqual(len(settings.accounts), 1)
            self.assertEqual(settings.accounts[0].id, "claude-max")
            self.assertEqual(settings.ttl_seconds, 60)
        finally:
            os.unlink(path)

    def test_duplicate_ids_are_rejected(self):
        # Two accounts, same id, different tokens: exactly the "two Claude
        # subscriptions" case, mistyped.
        body = MINIMAL + '\n[[account]]\nid = "claude-max"\nprovider = "claude"\ntoken = "env:OTHER"\n'
        path = write_config(body)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("duplicate", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_unknown_provider_is_rejected_with_the_valid_list(self):
        body = MINIMAL.replace('provider = "claude"', 'provider = "not-a-provider"')
        path = write_config(body)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("known providers", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_config_with_no_accounts_is_rejected(self):
        path = write_config('[server]\nport = 8790\n')
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_absurd_ttl_is_rejected(self):
        path = write_config(MINIMAL.replace("ttl_seconds = 60", "ttl_seconds = 0"))
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_negative_price_is_rejected(self):
        path = write_config(MINIMAL.replace("price = 100", "price = -5"))
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config("/nonexistent/quota-rings-config.toml")
        self.assertIn("not found", str(ctx.exception))


class ShippedTemplateTests(unittest.TestCase):
    """The template is the first thing a new user touches. It must be valid."""

    def setUp(self):
        self.settings = load_config(TEMPLATE)

    def test_template_parses(self):
        self.assertGreaterEqual(len(self.settings.accounts), 6)

    def test_template_defines_multiple_claude_accounts(self):
        claude_accounts = [a for a in self.settings.accounts if a.provider == "claude"]
        self.assertGreaterEqual(
            len(claude_accounts), 2, "the template must show two distinct Claude subscriptions"
        )
        self.assertEqual({a.token_ref.target for a in claude_accounts}.__len__(), len(claude_accounts))

    def test_template_covers_every_required_provider(self):
        providers = {a.provider for a in self.settings.accounts}
        for required in ("claude", "openrouter", "minimax", "mistral", "codex", "copilot"):
            self.assertIn(required, providers)

    def test_template_never_holds_a_literal_credential(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for scheme in ("env:", "file:", "command:"):
            self.assertIn(scheme, text)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("token") or stripped.startswith("management_token"):
                value = stripped.split("=", 1)[1].strip().strip('"')
                self.assertTrue(
                    value.startswith(("env:", "file:", "command:", '"env:', '"file:', '"command:')),
                    f"config holds a literal credential: {stripped}",
                )

    def test_optional_providers_are_off_by_default(self):
        for provider in ("codex", "copilot", "mistral"):
            entries = [a for a in self.settings.accounts if a.provider == provider]
            self.assertTrue(entries, f"{provider} missing from template")
            for entry in entries:
                self.assertFalse(entry.enabled, f"{provider} must ship disabled")

    def test_no_vendor_reset_is_enabled_by_default(self):
        self.assertFalse(self.settings.enable_vendor_actions)
        for account in self.settings.accounts:
            self.assertFalse(account.allow_vendor_reset)

    def test_remote_bind_is_off_by_default(self):
        self.assertFalse(self.settings.allow_remote_bind)
        self.assertEqual(self.settings.host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
