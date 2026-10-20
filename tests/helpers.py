"""Shared test helpers: fixture loading and a fake HTTP transport.

No test in this suite touches the network. Every provider response is a fixture
under ``tests/fixtures``, so the suite is repeatable offline and cannot leak or
require a real credential.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Read one fixture by file name."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeHTTP:
    """Stands in for :mod:`quota_rings.http`.

    Routes are matched by URL substring. Each route maps to either a payload
    dict, an :class:`~quota_rings.http.HttpError` to raise, or a callable.
    Every request is recorded so a test can assert which endpoints were hit.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str] | None = None,
                 method: str = "GET", payload: Any = None, timeout: float = 20.0):
        headers = dict(headers or {})
        self.calls.append((method, url, headers))
        for needle, response in self.routes.items():
            if needle in url:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(url, headers, method, payload)
                return response, 200
        raise AssertionError(f"unexpected URL in test: {url}")

    def headers_for(self, needle: str) -> dict[str, str]:
        """Headers of the first request whose URL contains ``needle``."""
        for _, url, headers in self.calls:
            if needle in url:
                return headers
        raise AssertionError(f"no request was made to anything containing {needle!r}")

    def urls(self) -> list[str]:
        return [url for _, url, _ in self.calls]


def fake_token(value: str = "fake-token-for-tests-0123456789") -> str:
    """An obviously fake credential. Never a real one, never a real shape."""
    return value
