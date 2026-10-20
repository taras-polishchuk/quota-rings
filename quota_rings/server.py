"""HTTP server: static UI, the read-only JSON API, and one confirmed write.

Routes
------
``GET  /``                      the dashboard
``GET  /api/usage``             snapshot. ``?force=1`` = cache invalidation only.
``GET  /api/health``            liveness + per-account freshness, no secrets
``GET  /api/check``             credential sources present/absent (no values)
``GET  /api/meta``              which providers are configured and their tier
``POST /api/accounts/<id>/vendor-reset``
                                the **only** write. Redeems a Codex banked reset.
                                Requires ``enable_vendor_actions`` in config AND
                                ``allow_vendor_reset`` on the account AND an
                                explicit confirmation in the body.

The read API returns percentages, reset instants and operator-supplied labels.
It never returns a credential, and never returns a vendor's raw payload.
"""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .collector import Collector
from .config import Settings
from .providers import VENDOR_RESET_PROVIDERS, support_of
from .secrets import REDACTOR

WEB_ROOT = Path(__file__).resolve().parent / "web"

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

CONFIRM_TOKEN = "redeem-banked-reset"
MAX_BODY_BYTES = 4096


class Handler(BaseHTTPRequestHandler):
    server_version = "quota-rings"
    sys_version = ""  # do not advertise the Python version

    # -- plumbing ----------------------------------------------------------

    @property
    def collector(self) -> Collector:
        return self.server.collector  # type: ignore[attr-defined]

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # a browser navigating away mid-response is not an error

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(REDACTOR.scrub(payload), ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # access log off: the dashboard is polled every 60s by design

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path, _, query = self.path.partition("?")

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
        elif path == "/api/usage":
            try:
                # force=1 invalidates the local cache. It re-reads the vendors.
                # It does not reset anything a vendor enforces.
                payload = self.collector.get(force="force=1" in query)
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to the browser
                self._json(503, {"error": f"{type(exc).__name__}: {exc}"})
            else:
                self._json(200, payload)
        elif path == "/api/health":
            self._health()
        elif path == "/api/check":
            self._json(200, {"accounts": self.collector.check()})
        elif path == "/api/meta":
            self._meta()
        elif path == "/favicon.ico":
            self._serve_static("favicon.svg")
        else:
            self._serve_static(path.lstrip("/"))

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0]
        parts = [p for p in path.split("/") if p]
        # /api/accounts/<id>/vendor-reset
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "accounts" and parts[3] == "vendor-reset":
            self._vendor_reset(parts[2])
            return
        self._json(404, {"error": "not found"})

    # -- handlers ----------------------------------------------------------

    def _serve_static(self, relative: str) -> None:
        # Resolve inside WEB_ROOT and refuse anything that escapes it.
        target = (WEB_ROOT / relative).resolve()
        if not str(target).startswith(str(WEB_ROOT)) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, target.read_bytes(), STATIC_TYPES.get(target.suffix, "application/octet-stream"))

    def _health(self) -> None:
        try:
            payload = self.collector.get()
            now = time.time()
            accounts = []
            for a in payload["accounts"]:
                age = None
                if a["last_success_at"]:
                    age = int(now - datetime.fromisoformat(a["last_success_at"]).timestamp())
                accounts.append({
                    "id": a["id"],
                    "provider": a["provider"],
                    "ok": a["ok"],
                    "stale": a["stale"],
                    "age_seconds": age,
                    "error": a["error"],
                })
            self._json(200, {
                "status": "ok",
                "generated_at": payload["generated_at"],
                "ttl_seconds": payload["ttl_seconds"],
                "accounts_ok": sum(1 for a in accounts if a["ok"]),
                "accounts_total": len(accounts),
                "accounts": accounts,
            })
        except Exception as exc:  # noqa: BLE001
            self._json(503, {"status": "unhealthy", "error": f"{type(exc).__name__}: {exc}"})

    def _meta(self) -> None:
        self._json(200, {
            "providers": [
                {
                    "id": a.id,
                    "enabled": a.enabled,
                    "label": a.label,
                    "vendor": a.vendor,
                    "billing": a.billing,
                    "support": support_of(a.provider)[0],
                    "vendor_reset_available": a.provider in VENDOR_RESET_PROVIDERS,
                    "allow_vendor_reset": a.allow_vendor_reset,
                }
                for a in self.settings.accounts
            ],
            "vendor_actions_enabled": self.settings.enable_vendor_actions,
        })

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("body must be JSON") from None
        return data if isinstance(data, dict) else {}

    def _vendor_reset(self, account_id: str) -> None:
        """Redeem a Codex banked reset. The single write in this program.

        Three independent gates, then an explicit confirmation. This spends a
        finite vendor resource, so a mis-click must not be able to do it.
        """
        if not self.settings.enable_vendor_actions:
            self._json(403, {
                "error": "Vendor actions are disabled. Set server.enable_vendor_actions = true "
                         "in the config to allow them.",
            })
            return

        account = next((a for a in self.settings.accounts if a.id == account_id), None)
        if account is None:
            self._json(404, {"error": f"no account with id {account_id!r}"})
            return
        if account.provider not in VENDOR_RESET_PROVIDERS:
            self._json(400, {
                "error": f"{account.provider} publishes no programmatic quota reset. "
                         "Refresh only re-reads the numbers.",
            })
            return
        if not account.enabled:
            self._json(400, {"error": f"account {account_id!r} is disabled in config"})
            return
        if not account.allow_vendor_reset:
            self._json(403, {
                "error": f"Account {account_id!r} does not opt in. "
                         "Set allow_vendor_reset = true on that [[account]] to permit it.",
            })
            return

        try:
            body = self._read_body()
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        if body.get("confirm") != CONFIRM_TOKEN:
            self._json(400, {
                "error": "Confirmation required.",
                "expected": CONFIRM_TOKEN,
                "note": "This redeems a finite banked reset. It cannot be undone.",
            })
            return

        from .providers import codex  # local import: optional provider

        try:
            outcome = codex.redeem_banked_reset(account, force=bool(body.get("force")))
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        code = 200 if outcome.get("ok") else 409
        self._json(code, outcome)
        # The snapshot is now wrong by definition; drop it so the next read is honest.
        self.collector.invalidate()


class QuotaRingsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: Settings, collector: Collector) -> None:
        super().__init__((settings.host, settings.port), Handler)
        self.settings = settings
        self.collector = collector


def is_loopback(host: str) -> bool:
    try:
        return socket.gethostbyname(host) in {"127.0.0.1", "::1"}
    except socket.gaierror:
        return host in {"localhost", "127.0.0.1", "::1"}
