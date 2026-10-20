"""A headless-browser driver, in the standard library only.

Why this file exists
--------------------
Both majors in the QA report lived in the page's JavaScript, and the suite had
no test that executed JavaScript at all -- ``test_claims.py`` asserts on
``index.html`` *as text*, which cannot see a banner that outlives its condition
or an interval that fires three times too often. Asserting on source is not a
weaker version of running the page; it is a different thing that cannot fail the
same way.

So this module drives a real Chromium over the DevTools Protocol. No selenium,
no playwright, no npm: the WebSocket client below is the ~100 lines of RFC 6455
that Chromium's endpoint actually needs, and everything else is ``socket`` and
``http.client``.

Scope note: this is a *test* dependency. The shipped package still installs on a
box with nothing but CPython and the standard library.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

__all__ = ["Browser", "BrowserUnavailable", "find_chromium"]


class BrowserUnavailable(RuntimeError):
    """No usable Chromium binary was found on this machine."""


# --------------------------------------------------------------------------
# locating a binary


def find_chromium() -> str | None:
    """Return a Chromium/Chrome binary path, or ``None``.

    Checked in order: the ``QUOTA_RINGS_CHROME`` override, a Playwright browser
    cache (those binaries are complete and work headless), then ``PATH``.
    """
    override = os.environ.get("QUOTA_RINGS_CHROME")
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override

    home = Path.home()
    cache = home / ".cache" / "ms-playwright"
    if cache.is_dir():
        found = sorted(
            (p for p in cache.glob("chromium*/chrome-linux*/chrome")
             if p.is_file() and os.access(p, os.X_OK)),
            reverse=True,
        )
        if found:
            return str(found[0])

    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome", "headless_shell"):
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------
# a WebSocket client, just enough for CDP


class _WebSocket:
    """RFC 6455 client: text frames, no extensions, no fragmentation out.

    CDP sends unmasked frames; we must mask. Payloads here can exceed 64 KiB
    (a ``Runtime.evaluate`` result is JSON), so all three length encodings are
    handled.
    """

    def __init__(self, url: str, timeout: float = 30.0):
        if not url.startswith("ws://"):
            raise ValueError(f"only ws:// is supported, got {url!r}")
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("browser closed the socket during the handshake")
            raw += chunk
        head, _, leftover = raw.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"handshake refused: {head.splitlines()[0]!r}")
        self._buffer = bytearray(leftover)

    # -- framing -----------------------------------------------------------

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("browser closed the socket")
            self._buffer += chunk
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])  # FIN + text
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        """Return the next text message, reassembling continuations."""
        chunks: list[bytes] = []
        while True:
            first, second = self._read(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else b""
            payload = self._read(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                raise ConnectionError("browser closed the websocket")
            if opcode == 0x9:  # ping -> pong, keep going
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0xA:  # pong
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except OSError:
            pass
        finally:
            self.sock.close()


# --------------------------------------------------------------------------
# the browser


class Browser:
    """A headless Chromium that can navigate and evaluate JavaScript."""

    def __init__(self, binary: str | None = None, timeout: float = 30.0):
        self.binary = binary or find_chromium()
        if not self.binary:
            raise BrowserUnavailable(
                "no Chromium/Chrome binary found. Set QUOTA_RINGS_CHROME to one, "
                "or install chromium, to run the browser tests."
            )
        self.timeout = timeout
        self._profile = tempfile.mkdtemp(prefix="quota-rings-chrome-")
        self._proc: subprocess.Popen | None = None
        self._ws: _WebSocket | None = None
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Browser":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def start(self) -> None:
        binary = self.binary
        assert binary is not None  # narrowed by __init__
        # A fixed empty port: the browser writes the real one into
        # DevToolsActivePort, so there is no race against a port scan.
        self._proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={self._profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-features=Translate,MediaRouter,OptimizationHints",
                # Fonts and telemetry must not reach the network: this suite is
                # offline by design and a hang there would look like a failure.
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        port_file = Path(self._profile) / "DevToolsActivePort"
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if port_file.is_file():
                content = port_file.read_text(encoding="utf-8").split("\n")
                if content and content[0].strip().isdigit():
                    self._connect(int(content[0].strip()))
                    return
            if self._proc.poll() is not None:
                raise BrowserUnavailable(
                    f"chromium exited immediately (code {self._proc.returncode})")
            time.sleep(0.05)
        raise BrowserUnavailable("chromium never reported a DevTools port")

    def _connect(self, port: int) -> None:
        deadline = time.time() + self.timeout
        target_url = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=5
                ) as response:
                    targets = json.loads(response.read().decode())
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    target_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001 - the browser may not be up yet
                pass
            time.sleep(0.1)
        if not target_url:
            raise BrowserUnavailable("chromium exposed no page target")
        self._ws = _WebSocket(target_url, timeout=self.timeout)
        self.send("Page.enable")
        self.send("Runtime.enable")

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
            self._proc = None
        shutil.rmtree(self._profile, ignore_errors=True)

    # -- protocol ----------------------------------------------------------

    def send(self, method: str, **params):
        if self._ws is None:
            raise BrowserUnavailable("browser is not started")
        self._next_id += 1
        message_id = self._next_id
        self._ws.send_text(json.dumps(
            {"id": message_id, "method": method, "params": params}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            message = json.loads(self._ws.recv_text())
            if message.get("id") != message_id:
                continue  # an event, or a reply to an earlier call
            if "error" in message:
                raise RuntimeError(f"{method} failed: {message['error']}")
            return message.get("result", {})
        raise TimeoutError(f"{method} did not answer within {self.timeout}s")

    # -- page control ------------------------------------------------------

    def add_init_script(self, source: str) -> None:
        """Run ``source`` in every document, before any page script."""
        self.send("Page.addScriptToEvaluateOnNewDocument", source=source)

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", url=url)

    def wait_for(self, expression: str, timeout: float = 20.0, poll: float = 0.05):
        """Poll ``expression`` in the page until it is truthy."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.evaluate(f"!!({expression})"):
                return True
            time.sleep(poll)
        raise TimeoutError(f"page never satisfied: {expression}")

    def evaluate(self, expression: str, await_promise: bool = True):
        """Evaluate JavaScript in the page and return its value.

        ``awaitPromise`` lets the caller pass an async IIFE, which is how the
        multi-second scenarios in the tests are driven.
        """
        result = self.send(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=await_promise,
            returnByValue=True,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise RuntimeError(f"page threw: {text}")
        return result.get("result", {}).get("value")
