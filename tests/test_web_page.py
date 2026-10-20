"""Browser tests: the dashboard's JavaScript, actually executed.

Why this file exists
--------------------
QA gate t_9eb75782 returned FAIL with two majors, both in the page's
JavaScript:

  MAJOR-1  the "stale reading" banner was created on failure and never removed,
           so a recovered account kept reading "stale" over fresh numbers;
  MAJOR-2  an already-elapsed ``reset_at`` kept one line of ``tick()``
           permanently true, so the page issued ``/api/usage?force=1`` every
           ~21 seconds forever, bypassing the 60s server cache and re-hitting
           every vendor ~2.9x more often than the README claims.

Both shipped because *no test in the repository executed JavaScript*.
``test_claims.py`` asserts on ``index.html`` as text, which cannot see a banner
that outlives its condition or a timer that fires three times too often.

These tests close that hole: a real Chromium, driven over CDP by
:mod:`tests.browser`, loading the real ``index.html`` from the real
``QuotaRingsServer``. Nothing here is mocked except the vendor fetch itself.

Time
----
Waiting 90 real seconds to watch a refresh interval would make the suite
useless. Each page instead gets a **virtual clock**: ``Date.now`` and
``setInterval`` are replaced before any page script runs, so a test advances
time in one-second steps and fires the page's own registered callbacks. The
``tick()`` under test is the shipped one, unmodified.

The A/B
-------
:func:`pre_fix_html` reconstructs the page QA rejected by reverting the two
fixes, and :class:`PreFixReproduction` runs the *same two scenarios* against it
and asserts they fail. A browser test that also passes on the broken page proves
nothing, so that direction is tested too, in-suite, every run.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from quota_rings.collector import Collector
from quota_rings.config import Account, Settings
from quota_rings.model import FetchResult, make_window
from quota_rings.secrets import SecretRef
from quota_rings.server import QuotaRingsServer

from .browser import Browser, BrowserUnavailable
from .helpers import fake_token

REPO = Path(__file__).resolve().parent.parent
UI_FILE = REPO / "quota_rings" / "web" / "index.html"
SECRET = fake_token()

# The commit QA reviewed. The pre-fix page is recovered from it, so the A/B is
# against the bytes that actually shipped rather than a hand-written fixture.
PRE_FIX_REF = "909dec4"

# Installed before any page script. Three jobs:
#   * every request the page makes is recorded, so "did it force?" is a fact
#     observed in the browser, not an inference from reading the source;
#   * time is virtual, so 90 seconds of page behaviour costs no wall clock;
#   * setInterval registrations are captured instead of run, so the test decides
#     when each one fires.
INIT_SCRIPT = r"""
(() => {
  const RealDate = Date;
  const vt = {
    virtual: RealDate.now(),
    requests: [],
    pending: [],
    intervals: [],
  };
  window.__vt = vt;
  vt.advance = (ms) => { vt.virtual += ms; };

  const realFetch = window.fetch;
  window.fetch = function (...args) {
    vt.requests.push(String(args[0]));
    const p = realFetch.apply(this, args);
    vt.pending.push(p.then(() => {}, () => {}));
    return p;
  };

  window.setInterval = function (fn, ms) {
    vt.intervals.push({ fn, ms: ms || 0 });
    return vt.intervals.length;
  };

  Date.now = () => vt.virtual;

  // Advance virtual time in stepMs slices, firing every interval callback when
  // its own period comes due, and draining in-flight fetches after each slice
  // so the ordering matches real time.
  vt.run = async (totalMs, stepMs) => {
    for (const i of vt.intervals) i.next = i.ms;
    let elapsed = 0;
    while (elapsed < totalMs) {
      elapsed += stepMs;
      vt.advance(stepMs);
      for (const i of vt.intervals) {
        while (i.next <= elapsed) {
          i.next += i.ms;
          try { await i.fn(); } catch (e) { /* the page's own error path */ }
        }
      }
      if (vt.pending.length) { await Promise.all(vt.pending.splice(0)); }
    }
    if (vt.pending.length) { await Promise.all(vt.pending.splice(0)); }
    return vt.requests.slice();
  };
})();
"""


# --------------------------------------------------------------------------
# the pre-fix page, for the A/B

def pre_fix_html() -> str:
    """Return the shipped page with both fixes reverted.

    Prefers the real bytes from git (commit ``909dec4``, the one QA reviewed).
    Falls back to a textual revert of the two hunks when git is unavailable, so
    the A/B still runs in an exported tarball or a wheel unpacked somewhere.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{PRE_FIX_REF}:quota_rings/web/index.html"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        old = out.stdout
        if "staleNote.remove()" not in old and "justRolled" not in old:
            return old  # the historical page, genuinely unfixed
    except (OSError, subprocess.SubprocessError):
        pass

    # Textual revert of the current file.
    html = UI_FILE.read_text(encoding="utf-8")
    html = html.replace("  } else if(!s.stale && staleNote){\n    staleNote.remove();\n  }\n", "")
    start = html.find("  // \"Next reset\" is the next one still to come.")
    end = html.find("  DATA.accounts.forEach(s=>{ const el=document.getElementById('card-'+s.id);")
    if start != -1 and end != -1 and start < end:
        html = html[:start] + (
            "  if(all[0]) $('#sNext').textContent = fmtLeft(all[0].w.reset_at);\n"
            "  const ago = Math.round((Date.now()-lastFetch)/1000);\n"
            "  if(all[0] && new Date(all[0].w.reset_at) < Date.now() - 5000 && ago > 20) load(true);\n"
        ) + html[end:]
    html = html.replace(
        "// How long after a window's reset instant it still counts as \"just rolled\n"
        "// over\" and worth an immediate re-read. Wider than one 60s poll so a window\n"
        "// that reset between two polls is still caught.\nconst RESET_GRACE_MS = 90000;\n", "")
    html = html.replace(
        "// Reset instants we have already force-refreshed for. One forced re-read per\n"
        "// instant, so a window that just rolled over refreshes promptly without the\n"
        "// page spinning at the vendor. See tick().\nconst forcedResets = new Set();\n", "")
    return html


# --------------------------------------------------------------------------
# a page server that can serve either the shipped page or the pre-fix one


class _PageHandler(BaseHTTPRequestHandler):
    """Serves ``server.page_bytes`` for the dashboard and a tiny JSON API.

    Used by :class:`PreFixReproduction` to put the *old* page in front of a real
    browser. The shipped page is served by the project's own ``QuotaRingsServer``
    everywhere else -- this handler exists only for the counterfactual.
    """

    page_bytes = b""

    def log_message(self, *_args):  # keep the test output readable
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path in ("/", "/index.html"):
            self._send(200, self.page_bytes, "text/html; charset=utf-8")
        elif self.path.startswith("/api/usage"):
            self._send(200, json.dumps(self.usage_payload()).encode(),
                       "application/json")
        elif self.path.startswith("/api/meta"):
            self._send(200, b'{"providers":[]}', "application/json")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def usage_payload(self):
        return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                              time.gmtime()),
                "ttl_seconds": 60, "vendor_actions_enabled": False,
                "monthly_total": 0.0, "accounts": []}

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _PrefixedPageHandler(_PageHandler):
    """The pre-fix page, with one account whose freshness the test controls."""

    #: Flipped by the test. Defaults are set in :class:`PreFixReproduction`.
    account_ok = True
    account_stale = False
    account_used = 30.0
    reset_at = ""

    def usage_payload(self):
        used = self.account_used
        return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                              time.gmtime()),
                "ttl_seconds": 60, "vendor_actions_enabled": False,
                "monthly_total": 0.0, "accounts": [{
                    "id": "or-main", "provider": "openrouter", "name": "or-main",
                    "vendor": "openrouter", "billing": "payg", "plan": None,
                    "price": None, "currency": "USD", "price_period": "month",
                    "support": "supported", "support_note": None,
                    "windows": [{"key": "short", "label": "Day", "used": used,
                                 "remaining": round(100.0 - used, 1),
                                 "reset_at": self.reset_at,
                                 "window_seconds": 86400, "sub": None}],
                    "extras": [], "ok": self.account_ok,
                    "error": None if self.account_ok else "vendor refused",
                    "stale": self.account_stale,
                    "last_success_at": None, "ms": 1, "vendor_reset": None,
                }]}


def _iso(offset_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                         time.gmtime(time.time() + offset_seconds))


class _PageServer:
    """A ThreadingHTTPServer in front of one of the handlers above."""

    def __init__(self, handler_class):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------
# the shipped page, on the real server


def account(account_id="or-main", **kwargs):
    kwargs.setdefault("billing", "payg")
    return Account(
        id=account_id, provider="openrouter", label=account_id, vendor="openrouter",
        token_ref=SecretRef("env", "TEST_TOKEN"), _token_cache=SECRET, **kwargs
    )


class BrowserTestCase(unittest.TestCase):
    """A real server on a real socket, and a real browser pointed at it.

    Everything is offline: the collector's fetcher is a stub, no credential is
    resolved from the environment, and the browser has no route off localhost.

    Subclasses set ``reset_in`` to move the window's reset instant relative to
    now (negative = already elapsed), and use ``stub_state`` to make the vendor
    start failing.
    """

    reset_in = 3 * 86400.0  # three days out: the documented steady state

    @classmethod
    def setUpClass(cls):
        try:
            cls.browser = Browser()
            cls.browser.start()
        except BrowserUnavailable as exc:
            raise unittest.SkipTest(f"headless browser unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        browser = getattr(cls, "browser", None)
        if browser is not None:
            browser.close()
            cls.browser = None

    def setUp(self):
        state = {"mode": "ok", "used": 30.0, "fetches": 0}

        def stub(_account):
            state["fetches"] += 1
            if state["mode"] == "fail":
                raise RuntimeError("vendor refused the connection")
            return FetchResult(
                windows=[make_window("short", "Day", state["used"],
                                     reset_at=time.time() + self.reset_in,
                                     window_seconds=86400)],
                extras=["nothing secret here"],
            )

        settings = Settings(port=0, accounts=[account()])
        collector = Collector(settings, fetcher_factory=lambda _p: stub)
        self.server = QuotaRingsServer(settings, collector)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.state = state

        self.browser.add_init_script(INIT_SCRIPT)
        self.browser.navigate(self.base + "/")
        self.browser.wait_for(
            "document.querySelectorAll('.card:not(.skeleton)').length > 0")

    # -- helpers -----------------------------------------------------------

    def request_log(self):
        return self.browser.evaluate("window.__vt.requests") or []

    def usage_since(self, mark):
        return [r for r in self.request_log()[mark:] if "/api/usage" in r]

    def forced_since(self, mark):
        return [r for r in self.usage_since(mark) if "force=1" in r]

    def refresh(self):
        """The page's own load(), the same path the Refresh button takes."""
        return self.browser.evaluate("load(true)")

    def banner_count(self):
        return self.browser.evaluate("document.querySelectorAll('.stale-note').length")

    def row_text(self):
        return self.browser.evaluate(
            "(() => { const n = document.querySelector('.card .rows .meta');"
            " return n ? n.textContent.trim() : null; })()")


class StaleBannerTests(BrowserTestCase):
    """MAJOR-1: the banner must disappear when the account recovers."""

    def test_banner_appears_on_failure_and_is_removed_on_recovery(self):
        # 1. A healthy reading is not flagged.
        self.assertEqual(self.banner_count(), 0,
                         "a healthy account must never be flagged stale")

        # 2. The vendor starts failing. The account keeps its last good numbers,
        #    honestly labelled, and the banner appears.
        self.state["mode"] = "fail"
        self.refresh()
        self.browser.wait_for("document.querySelectorAll('.stale-note').length === 1")
        self.assertIn("stale reading", self.browser.evaluate(
            "document.querySelector('.stale-note').textContent"))

        # 3. The vendor recovers, and the numbers really do change.
        self.state["mode"] = "ok"
        self.state["used"] = 60.0
        self.refresh()
        self.browser.wait_for(
            "(() => { const n = document.querySelector('.card .rows .meta');"
            " return n && n.textContent.includes('60% used'); })()")
        self.assertEqual(self.banner_count(), 0,
                         "the stale banner outlived the failure: a card with "
                         "freshly-updated numbers still reads 'stale reading', "
                         "which is a false statement about data freshness")

    def test_a_repeated_failure_does_not_stack_banners(self):
        for _ in range(3):
            self.state["mode"] = "fail"
            self.refresh()
        self.browser.wait_for("document.querySelectorAll('.stale-note').length === 1")
        self.assertEqual(self.banner_count(), 1,
                         "repeated failures stacked duplicate banners")


class AutoRefreshTests(BrowserTestCase):
    """MAJOR-2: the page must poll on 60s and must not spin on ``?force=1``."""

    def run_page_for(self, seconds):
        """Let the page's own timers run for ``seconds`` of page time."""
        mark = len(self.request_log())
        self.browser.evaluate(f"window.__vt.run({seconds * 1000}, 1000)")
        return self.usage_since(mark), self.forced_since(mark)

    def test_no_forced_refresh_while_every_reset_is_in_the_future(self):
        usage, forced = self.run_page_for(90)
        self.assertEqual(forced, [],
                         "the page issued ?force=1 with every window still in "
                         "the future; the 60s poll must stand on its own")
        self.assertLessEqual(len(usage), 2,
                             f"expected the documented 60s poll, saw {len(usage)} "
                             f"requests in 90s")

    def test_the_sixty_second_poll_still_runs(self):
        """The fix must not have simply switched the refresh off."""
        usage, forced = self.run_page_for(90)
        self.assertGreaterEqual(len(usage), 1,
                                "the page stopped refreshing entirely")
        self.assertEqual(forced, [], "and it refreshed without forcing, which is right")


class ElapsedResetAutoRefreshTests(BrowserTestCase):
    """MAJOR-2, QA's subject condition: the reset instant has already passed.

    This is the state the project's own shipped fixtures are in, and the state
    any vendor produces before the collector rolls its window over. Before the
    fix the page issued a forced request every ~21s, forever.
    """

    reset_in = -3600.0  # an hour ago

    def test_elapsed_reset_does_not_start_a_force_loop(self):
        mark = len(self.request_log())
        self.browser.evaluate("window.__vt.run(92000, 1000)")
        usage = self.usage_since(mark)
        forced = [r for r in usage if "force=1" in r]
        self.assertEqual(forced, [],
                         f"an already-elapsed reset_at turned the documented 60s "
                         f"poll into a force loop: {len(forced)} forced requests "
                         f"in 92s ({usage})")
        self.assertLessEqual(len(usage), 2,
                             f"expected the documented 60s poll, saw {len(usage)} "
                             f"requests: {usage}")

    def test_the_server_is_not_asked_to_bypass_its_own_cache(self):
        """The 60s TTL only protects the vendors if no ?force=1 is sent."""
        mark = len(self.request_log())
        self.browser.evaluate("window.__vt.run(92000, 1000)")
        self.assertEqual(self.forced_since(mark), [], "the server TTL was bypassed")


class JustResetAutoRefreshTests(BrowserTestCase):
    """The branch MAJOR-2 lived in must still do its job.

    Without this test, "delete the force branch" is a passing way to fix
    MAJOR-2: no more force loop, no more prompt re-read. The feature is that a
    window which has *just* rolled over is re-read promptly. Exactly once.
    """

    reset_in = -30.0  # rolled over half a minute ago: inside the grace window

    def test_a_just_rolled_over_window_is_reread_exactly_once(self):
        mark = len(self.request_log())
        self.browser.evaluate("window.__vt.run(92000, 1000)")
        usage = self.usage_since(mark)
        forced = [r for r in usage if "force=1" in r]
        self.assertEqual(len(forced), 1,
                         f"a window that reset 30s ago should be re-read once, "
                         f"promptly, and then left alone; saw {len(forced)}: {usage}")

    def test_and_the_prompt_reread_happens_before_the_next_poll(self):
        mark = len(self.request_log())
        self.browser.evaluate("window.__vt.run(25000, 1000)")
        forced = [r for r in self.usage_since(mark) if "force=1" in r]
        self.assertEqual(len(forced), 1,
                         "the re-read did not happen promptly; the user waits a "
                         "full 60s on a number the vendor has already reset")


class PreFixReproduction(unittest.TestCase):
    """The A/B: both majors must reproduce on the page QA rejected.

    Run in-suite on every pass. If these stop reproducing, the fixture is wrong
    and the two tests above are no longer evidence of anything.
    """

    @classmethod
    def setUpClass(cls):
        try:
            cls.browser = Browser()
            cls.browser.start()
        except BrowserUnavailable as exc:
            raise unittest.SkipTest(f"headless browser unavailable: {exc}") from exc
        cls.old_html = pre_fix_html().encode("utf-8")

    @classmethod
    def tearDownClass(cls):
        browser = getattr(cls, "browser", None)
        if browser is not None:
            browser.close()

    def setUp(self):
        self.assertNotIn("staleNote.remove()", self.old_html.decode("utf-8"),
                         "the pre-fix page already contains the MAJOR-1 fix")
        _PageHandler.page_bytes = self.old_html
        self.server = _PageServer(_PrefixedPageHandler)
        self.addCleanup(self.server.close)
        self.browser.add_init_script(INIT_SCRIPT)

    # -- MAJOR-1 on the old page -------------------------------------------

    def test_major_1_reproduces_on_the_pre_fix_page(self):
        _PrefixedPageHandler.reset_at = _iso(86400)
        _PrefixedPageHandler.account_ok = False
        _PrefixedPageHandler.account_stale = True
        _PrefixedPageHandler.account_used = 90.0
        self.browser.navigate(self.server.base + "/")
        self.browser.wait_for("document.querySelectorAll('.card:not(.skeleton)').length > 0")
        self.browser.evaluate("load(false)")
        self.browser.wait_for("document.querySelectorAll('.stale-note').length === 1")

        # The account recovers and the numbers genuinely change underneath it.
        _PrefixedPageHandler.account_ok = True
        _PrefixedPageHandler.account_stale = False
        _PrefixedPageHandler.account_used = 60.0
        self.browser.evaluate("load(true)")
        self.browser.wait_for(
            "(() => { const n = document.querySelector('.card .rows .meta');"
            " return n && n.textContent.includes('60% used'); })()")
        self.assertEqual(
            self.browser.evaluate("document.querySelectorAll('.stale-note').length"), 1,
            "the pre-fix page was expected to keep the stale banner after a "
            "recovery; if it did not, this fixture no longer reproduces MAJOR-1")

    # -- MAJOR-2 on the old page -------------------------------------------

    def test_major_2_reproduces_on_the_pre_fix_page(self):
        _PrefixedPageHandler.reset_at = _iso(-86400)
        _PrefixedPageHandler.account_ok = True
        _PrefixedPageHandler.account_stale = False
        self.browser.navigate(self.server.base + "/")
        self.browser.wait_for("document.querySelectorAll('.card:not(.skeleton)').length > 0")
        mark = len(self.browser.evaluate("window.__vt.requests") or [])
        self.browser.evaluate("window.__vt.run(92000, 1000)")
        requests = [r for r in (self.browser.evaluate("window.__vt.requests") or [])[mark:]
                    if "/api/usage" in r]
        forced = [r for r in requests if "force=1" in r]
        self.assertGreaterEqual(
            len(forced), 2,
            "the pre-fix page was expected to force-refresh repeatedly on an "
            f"elapsed reset_at; it issued only {len(forced)} in 92s: {requests}. "
            "If that changed, this fixture no longer reproduces MAJOR-2")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
