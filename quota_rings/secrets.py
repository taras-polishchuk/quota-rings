"""Secret resolution and redaction.

Two jobs:

* ``SecretRef`` resolves a credential *reference* from config into a value.
  Config only ever holds a reference (``env:NAME``, ``file:/path``,
  ``keychain:service`` or ``command:...``), never a literal, so a config file is
  always safe to commit.
* ``Redactor`` scrubs anything that looks like a credential -- both the values
  this process actually resolved, and vendor token shapes we never saw -- out of
  every string that could reach a log, an HTTP error body or the browser.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MASK = "***redacted***"

# Token shapes per vendor. Used as a backstop: if a secret leaks into an error
# string from some other path, these still catch it.
TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-cp-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{12,}"),
)


class SecretError(RuntimeError):
    """A configured credential could not be resolved."""


@dataclass(frozen=True)
class SecretRef:
    """A pointer to a secret held outside the config file.

    ``kind`` is one of:

    * ``env``    -- ``env:CLAUDE_MAX_TOKEN``; read ``os.environ``.
    * ``file``   -- ``file:/run/secrets/openrouter_key``; first line, stripped.
    * ``command`` -- ``command:pass show openrouter/inference``; stdout, stripped.
      A pipe-separated argv is split with ``shlex``.
    * ``empty``  -- explicitly no credential (for optional/misconfigured entries
      that should fail closed with a clear message instead of at import time).
    """

    kind: str
    target: str = ""

    @classmethod
    def parse(cls, raw: str | None) -> "SecretRef":
        if not raw or not str(raw).strip():
            return cls("empty")
        text = str(raw).strip()
        if "://" in text:  # tolerate env://FOO / file:///path
            scheme, _, rest = text.partition("://")
            text = f"{scheme}:{rest}"
        kind, _, target = text.partition(":")
        kind = kind.strip().lower()
        target = target.strip()
        if kind not in ("env", "file", "command", "empty"):
            raise SecretError(
                f"unsupported secret reference scheme {kind!r}; use env:, file: or command:"
            )
        if kind != "empty" and not target:
            raise SecretError(f"secret reference {kind}: is missing its target")
        return cls(kind, target)

    def describe(self) -> str:
        """Human-readable, value-free description for --check output."""
        return {"empty": "not set", "env": f"env:{self.target}"}.get(self.kind, self.kind + ":")

    def resolve(self) -> str:
        if self.kind == "empty":
            return ""
        if self.kind == "env":
            value = os.environ.get(self.target, "")
            if not value.strip():
                raise SecretError(f"environment variable {self.target} is unset or empty")
            return value.strip()
        if self.kind == "file":
            path = Path(os.path.expanduser(self.target))
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise SecretError(f"cannot read secret file for {path}: {exc.strerror}") from exc
            value = text.strip().splitlines()[0].strip() if text.strip() else ""
            if not value:
                raise SecretError(f"secret file {path} is empty")
            return value
        # command
        import shlex

        argv = shlex.split(self.target)
        if not argv:
            raise SecretError("command: secret reference has no command")
        if shutil.which(argv[0]) is None:
            raise SecretError(f"command {argv[0]!r} not found on PATH")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretError(f"command {argv[0]!r} failed: {type(exc).__name__}") from exc
        value = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""
        if not value:
            raise SecretError(f"command {argv[0]!r} produced no output on stdout")
        return value


class Redactor:
    """Scrubs known secret values and token-shaped strings from text."""

    def __init__(self) -> None:
        self._literals: set[str] = set()

    def learn(self, secret: str | None) -> None:
        """Register a resolved secret value so it is scrubbed everywhere."""
        if secret and len(secret.strip()) >= 8:
            self._literals.add(secret.strip())

    def text(self, value: str | None) -> str | None:
        if value is None:
            return None
        out = value
        # Longest first, so a token that contains another is masked whole.
        for literal in sorted(self._literals, key=len, reverse=True):
            out = out.replace(literal, MASK)
        for pattern in TOKEN_PATTERNS:
            out = pattern.sub(MASK, out)
        return out

    def scrub(self, value, limit: int = 200):
        """Recursively scrub a JSON-ish structure before it leaves the process."""
        if isinstance(value, str):
            return (self.text(value) or "")[:limit]
        if isinstance(value, dict):
            return {k: self.scrub(v, limit) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v, limit) for v in value]
        return value


# Process-wide redactor. Fetchers register every credential they touch so that
# a vendor echoing the token back into an error body cannot reach the browser.
REDACTOR = Redactor()
