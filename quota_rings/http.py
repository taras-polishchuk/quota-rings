"""Minimal JSON-over-HTTP client.

stdlib only (urllib), so the package installs with no wheels on any CPython
3.11+. Two behaviours matter for correctness here:

* ``http_json`` raises :class:`HttpError` with the *status* preserved, because a
  403 edge challenge and a 401 bad key need different operator advice.
* Nothing in this module logs a header. Credentials go into the request and
  nowhere else.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


class HttpError(RuntimeError):
    """Non-2xx response, or a transport failure. ``status`` is 0 when unknown."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def http_json(
    url: str,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
    payload: Any = None,
    timeout: float = 20.0,
) -> tuple[dict[str, Any], int]:
    """Perform a request and return ``(parsed_json, status_code)``.

    A JSON decode failure is an :class:`HttpError` too -- several vendors answer
    an edge challenge with HTML and a 403, and that is a failure, not a result.
    """
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:  # 4xx / 5xx still carry a body
        body = _safe_text(exc.read())
        raise HttpError(f"HTTP {exc.code} from {_host(url)}", status=exc.code, body=body) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(f"transport failure for {_host(url)}: {type(reason).__name__}") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpError(
            f"HTTP {status} from {_host(url)} returned a non-JSON body", status=status
        ) from exc
    if not isinstance(parsed, dict):
        raise HttpError(f"HTTP {status} from {_host(url)} returned {type(parsed).__name__}, expected an object",
                        status=status)
    return parsed, status


def _host(url: str) -> str:
    """Host (and path) only. A URL can carry a key in its query string; a full
    URL in an error message could therefore echo a credential, so strip the
    query and fragment before the string is ever rendered."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.netloc}{parts.path}" or "the provider"


def _safe_text(raw: bytes, limit: int = 400) -> str:
    return raw[:limit].decode("utf-8", "replace") if raw else ""
