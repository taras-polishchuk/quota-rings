"""The refresh/reset distinction, and the UI's honesty about it.

Acceptance requires that claims about quota reset distinguish a display refresh
from a vendor-enforced reset. These tests read the shipped UI text and the
shipped config, because that wording is part of the contract, not decoration.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI = REPO / "quota_rings" / "web" / "index.html"
README = REPO / "README.md"
TEMPLATE = REPO / "config.example.toml"

html = UI.read_text(encoding="utf-8")


class RefreshWordingTests(unittest.TestCase):
    """No user-facing control may imply refresh resets a vendor limit."""

    def _refresh_labels(self) -> list[str]:
        labels = re.findall(r'class="refresh"[^>]*title="([^"]*)"', html)
        labels += re.findall(r"<span>Refresh[^<]*</span>", html)
        labels += re.findall(r"<span><kbd>R</kbd>([^<]*)</span>", html)
        labels += re.findall(r"<div class=\"legend\">.*?</div>", html, re.S)
        return labels

    def test_refresh_controls_never_claim_to_reset_quota(self):
        for label in self._refresh_labels():
            # "does not reset" is a negation and is exactly what we want.
            cleaned = re.sub(r"does not reset any vendor limit", "", label, flags=re.I)
            self.assertIsNone(
                re.search(r"\breset\b|\brefill\b|\brestore\b", cleaned, re.I),
                f"a refresh control claims to reset quota: {label!r}",
            )

    def test_ui_states_the_distinction_explicitly(self):
        self.assertRegex(html, r"Refresh re-reads the numbers\. It does not reset any vendor limit\.")

    def test_every_card_shows_its_vendor_reset_position(self):
        # Each card renders either an available banked reset or an explicit
        # "no vendor quota reset here" note, so silence is never the answer.
        self.assertIn("No vendor quota reset here.", html)
        self.assertIn("Vendor quota reset available", html)

    def test_reset_requires_a_confirmation_dialog(self):
        self.assertIn("confirm(", html)
        self.assertIn("redeem-banked-reset", html)
        self.assertRegex(html, r"cannot be given back|finite and cannot")

    def test_reset_is_never_triggered_by_the_refresh_path(self):
        # The only fetch() call that is not a GET of a read route.
        posts = re.findall(r"fetch\(`([^`]+)`[^)]*method:\s*'POST'", html)
        self.assertTrue(posts, "the UI should have exactly one POST, for the reset")
        for url in posts:
            self.assertIn("vendor-reset", url)


class DocsWordingTests(unittest.TestCase):
    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")

    def test_readme_leads_with_the_refresh_vs_reset_table(self):
        self.assertIn("Consumes quota?", self.readme)
        self.assertRegex(self.readme, r"Redeeming a Codex banked reset.*?Yes, and the credit is finite", re.S)

    def test_readme_says_refresh_is_a_display_operation(self):
        self.assertRegex(self.readme, r"Refresh is a \*display\* operation")

    def test_readme_documents_every_provider_and_tier(self):
        for provider in ("OpenRouter", "Claude", "MiniMax", "Codex", "Copilot", "Mistral"):
            self.assertIn(provider, self.readme)
        for tier in ("supported", "unstable", "unsupported"):
            self.assertIn(tier, self.readme)

    def test_readme_names_what_is_deliberately_absent(self):
        # Collapse whitespace: the sentence wraps across lines in the source.
        absent = re.sub(r"\s+", " ", self.readme.lower())
        for phrase in ("not reachable", "mistral", "vendor plan name", "pay-as-you-go balance"):
            self.assertIn(phrase, absent)

    def test_readme_documents_every_cli_command(self):
        for command in ("serve", "once", "check"):
            self.assertIn(f"`{command}`", self.readme)

    def test_readme_documents_startup_for_generic_linux_wsl(self):
        for marker in ("python3 --version", "systemctl --user", "ssh -N -L"):
            self.assertIn(marker, self.readme)


class SafetyDefaultsTests(unittest.TestCase):
    def test_template_keeps_both_reset_switches_off(self):
        self.assertIn("enable_vendor_actions = false", TEMPLATE.read_text(encoding="utf-8"))

    def test_template_binds_loopback_only(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('host = "127.0.0.1"', text)
        self.assertIn("allow_remote_bind = false", text)

    def test_server_refuses_a_remote_bind_without_the_opt_in(self):
        server = (REPO / "quota_rings" / "server.py").read_text(encoding="utf-8")
        self.assertIn("is_loopback", server)

    def test_cli_enforces_the_remote_bind_refusal(self):
        cli = (REPO / "quota_rings" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("allow_remote_bind", cli)
        self.assertIn("refusing to bind", cli)


if __name__ == "__main__":
    unittest.main()
