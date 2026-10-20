"""Self-audit: scan this repository for credentials and personal data.

This runs as part of the test suite, so a leak cannot land unnoticed. It covers
what ``scripts/scan-secrets.sh`` covers, but as an assertion that fails the
suite rather than a script a human has to remember to run.

What it looks for:
  * live-looking vendor tokens (sk-ant-, sk-or-v1-, sk-cp-, ghp_, JWTs)
  * absolute home paths belonging to a specific person
  * email addresses and personal identifiers
  * config files holding a literal credential instead of a reference

Known-safe text is allowlisted explicitly and narrowly -- e.g. the ``sk-or-v1-``
prefix in a doc sentence, or the fake tokens used inside tests.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "build", "dist", ".pytest_cache"}

# Files where a token-shaped string is expected and safe: they are the redactor
# patterns themselves, or fake fixtures used to prove the redaction works.
ALLOW = {
    "quota_rings/secrets.py",     # defines the token patterns
    "tests/test_collector.py",    # asserts the redactor masks a fake token
    "tests/test_server.py",       # asserts no fake token reaches a route
    "scripts/scan-secrets.sh",    # the scanner itself
    "tests/test_selfscan.py",     # this file
}

TOKEN_PATTERNS = {
    "anthropic-key": re.compile(r"sk-ant-(?:api|oat|usr|svc|admin)[A-Za-z0-9_\-]{20,}"),
    "openrouter-key": re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}"),
    "minimax-sub-key": re.compile(r"sk-cp-[A-Za-z0-9]{24,}"),
    "github-token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "openai-key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{40,}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
}

# Personal data. Deliberately narrow: generic words are not PII.
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HOME_PATH = re.compile(r"/home/[a-z][a-z0-9_\-]{2,}")
WINDOWS_USER = re.compile(r"C:\\\\Users\\\\[A-Za-z][A-Za-z0-9_\-]{2,}")


def tracked_files() -> list[Path]:
    """Every file git would publish, falling back to the working tree."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, timeout=15, check=True
        )
        files = [REPO / line for line in out.stdout.splitlines() if line.strip()]
        if files:
            return files
    except (subprocess.SubprocessError, OSError):
        pass
    return [
        p for p in REPO.rglob("*")
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
    ]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""  # binary asset: no text to scan


def relative(path: Path) -> str:
    return str(path.relative_to(REPO))


class RepositoryScanTests(unittest.TestCase):
    """One assertion per failure mode, so a CI log names the exact problem."""

    def setUp(self):
        self.files = [p for p in tracked_files() if relative(p) not in ALLOW]

    def test_no_vendor_tokens_anywhere(self):
        hits = []
        for path in self.files:
            text = read_text(path)
            for name, pattern in TOKEN_PATTERNS.items():
                for match in pattern.findall(text):
                    hits.append(f"{relative(path)}: {name}: {match[:12]}...")
        self.assertEqual(hits, [], "live-looking credential in the repository:\n" + "\n".join(hits))

    def test_no_email_addresses(self):
        hits = []
        for path in self.files:
            for match in EMAIL.findall(read_text(path)):
                hits.append(f"{relative(path)}: {match}")
        self.assertEqual(hits, [], "email address in the repository:\n" + "\n".join(hits))

    def test_no_personal_home_paths(self):
        hits = []
        for path in self.files:
            for match in HOME_PATH.findall(read_text(path)):
                hits.append(f"{relative(path)}: {match}")
        self.assertEqual(hits, [], "personal absolute path in the repository:\n" + "\n".join(hits))

    def test_no_windows_user_paths(self):
        hits = []
        for path in self.files:
            for match in WINDOWS_USER.findall(read_text(path)):
                hits.append(f"{relative(path)}: {match}")
        self.assertEqual(hits, [], "personal Windows path in the repository:\n" + "\n".join(hits))

    def test_config_template_holds_only_references(self):
        for line in read_text(REPO / "config.example.toml").splitlines():
            stripped = line.strip()
            if stripped.startswith(("token", "management_token")):
                value = stripped.split("=", 1)[1].strip().strip('"')
                self.assertTrue(
                    value.startswith(("env:", "file:", "command:")),
                    f"config.example.toml holds a literal credential: {stripped}",
                )

    def test_gitignore_protects_the_real_config(self):
        ignored = read_text(REPO / ".gitignore")
        self.assertIn("config.toml", ignored)
        self.assertIn(".env", ignored)

    def test_no_config_file_is_committed(self):
        for path in self.files:
            self.assertNotEqual(path.name, "config.toml", "a real config.toml must never be committed")


class GitHistoryScanTests(unittest.TestCase):
    """History is what gets published, so it gets scanned too."""

    def _history(self) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(REPO), "log", "-p", "--all"],
                capture_output=True, text=True, timeout=60, check=False,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            self.skipTest("git history unavailable")

    def test_history_has_no_vendor_tokens(self):
        history = self._history()
        if not history:
            self.skipTest("no commits yet")
        hits = []
        for name, pattern in TOKEN_PATTERNS.items():
            hits += [f"{name}: {m[:12]}..." for m in pattern.findall(history)]
        self.assertEqual(hits, [], "credential in git history:\n" + "\n".join(hits))

    def test_history_has_no_email_or_home_path(self):
        history = self._history()
        if not history:
            self.skipTest("no commits yet")
        # The committer identity is metadata, not file content: it is reported
        # rather than failed here, because changing it is an operator decision.
        self.assertEqual(
            [e for e in EMAIL.findall(history) if "example" not in e and "noreply" not in e],
            [],
            "email address in git history",
        )
        self.assertEqual(HOME_PATH.findall(history), [], "personal home path in git history")


if __name__ == "__main__":
    unittest.main()
