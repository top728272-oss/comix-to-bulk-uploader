"""Tests for the site search path and its Cloudflare recovery - no browser.

The regression these pin down: a live Cloudflare interstitial was classified as
an origin outage (every Cloudflare page footer carries a Ray ID), so a query
behind a flagged VPN egress came back as zero results with no error, no solver
call and no rotation.

Run: .venv\\Scripts\\python.exe -m unittest discover tests
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

# Allow running this file directly (`python tests/test_search.py`) as well as
# through unittest discovery: the engine lives in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core
import session as session_mod


RESULT_ROWS = [
    {"href": "/title/abc12-one-piece", "lines": ["One Piece", "MANGA Ch. 1100 9.5"]},
    {"href": "/title/def34-one-piece-hentai", "lines": ["One piece (hentai)", "OTHER"]},
]

# A real interstitial's footer. "Ray ID" is the whole point of this fixture.
CHALLENGE_BODY = (
    "Performing security verification. Cloudflare Ray ID: a49f1369fb9e9878"
)


class FakeClock:
    """Virtual time for the popup wait loop.

    The empty-settle timer is real wall-clock work in production; here it must
    not cost minutes of test time, so wait_for_timeout() advances a counter
    instead of sleeping.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.t = 1000.0

    def time(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += float(ms) / 1000.0

    def __getattr__(self, name):
        return getattr(self._real, name)


class FakeLocator:
    def __init__(self, page: "FakeSearchPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    def is_visible(self, timeout=None) -> bool:
        return self._page.visible(self._selector)

    def text_content(self, timeout=None) -> str:
        return self._page.title_text

    def inner_text(self, timeout=None) -> str:
        return self._page.body_text

    def inner_html(self, timeout=None) -> str:
        return f"{self._page.body_text} {self._page.html_extra}"

    def get_attribute(self, name, timeout=None):
        return "en"

    def evaluate(self, expression, arg=None, timeout=None):
        return None

    def click(self, timeout=None) -> None:
        return None

    def wait_for(self, state=None, timeout=None) -> None:
        return None

    def fill(self, value="") -> None:
        self._page.typed.append(("fill", value))

    def press_sequentially(self, value, delay=None) -> None:
        # Typing costs wall-clock time in the browser, and the site's debounced
        # request only starts once the last character lands.
        self._page.typed.append(("type", value))
        self._page.clock.advance(len(value) * (delay or 0))


class FakeSearchPage:
    """A tab whose gate state and popup behaviour are scripted per navigation.

    ``popup`` scripts what the results popup does over time, which is where the
    real behaviour lives:

      results          results appear and stay
      empty            the empty marker appears and stays (genuine no matches)
      transient        empty appears first, results land ``results_after_s``
                      later - the race that made long queries return nothing
      none             neither ever renders
    """

    def __init__(
        self,
        states: list,
        block_on_query: bool = False,
        clock: FakeClock | None = None,
        results_after_s: float = 2.5,
    ) -> None:
        # Each entry is the state a single goto() lands on; the last one
        # repeats, so a script shorter than the attempt count still works.
        self._states = states or ["clean"]
        self.state = self._states[0]
        self.typed: list = []
        self.gotos = 0
        self.context = object()
        self.url = "about:blank"
        self.title_text = "Comix - Read Comics online for free"
        self.body_text = "home page"
        self.html_extra = ""
        # "results" | "empty" | "transient" | "none"
        self.popup = "results"
        self.results_after_s = results_after_s
        self.clock = clock or FakeClock(time)
        # Gate states: clean | challenge | blocked | outage
        self.gate = "clean"
        # The popup's own request gets banned while the document loaded fine -
        # the shape a search-time IP ban actually takes.
        self.block_on_query = block_on_query
        self._t0 = self.clock.time()

    def _apply(self) -> None:
        # gotos is incremented before this runs, so the first goto is index 0.
        index = min(self.gotos - 1, len(self._states) - 1)
        self.state = self._states[index]
        gate, _, popup = self.state.partition(":")
        self.gate = gate
        if popup:
            self.popup = popup
        self._t0 = self.clock.time()
        if gate == "challenge":
            self.title_text = "Just a moment..."
            self.body_text = CHALLENGE_BODY
            self.html_extra = "challenge-platform cf-chl-opt"
        if gate == "blocked":
            self.title_text = "Attention Required! | Cloudflare"
            self.body_text = (
                "Sorry, you have been blocked. You are unable to access comix.to. "
                "Cloudflare Ray ID: 8a1b2c3d4e5f6a7b"
            )
            self.html_extra = ""
        elif gate == "outage":
            self.title_text = "comix.to | 522: Connection timed out"
            self.body_text = "Origin is unreachable. Cloudflare Ray ID: 8f2c1d"
            self.html_extra = ""
        else:
            self.title_text = "Comix - Read Comics online for free"
            self.body_text = "home page"
            self.html_extra = ""

    def _popup_state(self) -> str:
        """What the popup shows right now, given virtual elapsed time."""
        if self.block_on_query:
            # The popup's own request was banned: the document still looks
            # fine, but the tab now carries the WAF block page.
            self._apply_block()
            return "blocked"
        elapsed = self.clock.time() - self._t0
        if self.popup == "results":
            return "results"
        if self.popup == "empty":
            return "empty"
        if self.popup == "transient":
            return "results" if elapsed >= self.results_after_s else "empty"
        return "none"

    def _apply_block(self) -> None:
        self.gate = "blocked"
        self.title_text = "Attention Required! | Cloudflare"
        self.body_text = (
            "Sorry, you have been blocked. You are unable to access comix.to. "
            "Cloudflare Ray ID: 8a1b2c3d4e5f6a7b"
        )
        self.html_extra = ""

    def visible(self, selector: str) -> bool:
        if self.gate == "challenge" and "challenge" in selector:
            return True
        if "search-pop__item--result" in selector:
            return self._popup_state() == "results"
        if "search-pop__empty" in selector:
            return self._popup_state() == "empty"
        if self.gate == "blocked" and "cf-error-details" in selector:
            return False
        if self.gate == "outage" and "cf-error-details" in selector:
            return True
        if "search-toggle" in selector:
            return True
        return False

    def is_closed(self) -> bool:
        return False

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.gotos += 1
        self.url = url
        self._apply()

    def wait_for_timeout(self, ms) -> None:
        self.clock.advance(ms)

    def eval_on_selector_all(self, selector, js):
        return list(RESULT_ROWS) if self._popup_state() == "results" else []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


class SearchGateDetectionTests(unittest.TestCase):
    """The misclassification that made every VPN search look like a no-match."""

    def test_interstitial_with_ray_id_is_a_challenge_not_an_outage(self):
        page = FakeSearchPage(["challenge"])
        page.goto("x")

        self.assertTrue(core.is_cloudflare_active(page))
        self.assertFalse(core.is_cloudflare_error_page(page))
        self.assertEqual(core.detect_challenge(page), "cloudflare")

    def test_ray_id_is_not_an_error_marker(self):
        self.assertNotIn("ray id", core.CF_ERROR_MARKERS)

    def test_real_5xx_still_reads_as_an_outage(self):
        page = FakeSearchPage(["outage"])
        page.goto("x")

        self.assertTrue(core.is_cloudflare_error_page(page))
        self.assertFalse(core.is_cloudflare_active(page))

    def test_banned_egress_is_neither_challenge_nor_outage(self):
        page = FakeSearchPage(["blocked"])
        page.goto("x")

        self.assertTrue(core.is_cloudflare_access_block(page))
        self.assertFalse(core.is_cloudflare_active(page))
        self.assertFalse(core.is_cloudflare_error_page(page))


class SearchAutoSolveTests(unittest.TestCase):
    """Every test drives the popup wait on a virtual clock (see FakeClock)."""

    def setUp(self) -> None:
        self.clock = FakeClock(time)
        patcher = mock.patch.object(core, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _page(self, *args, **kwargs) -> FakeSearchPage:
        kwargs.setdefault("clock", self.clock)
        return FakeSearchPage(*args, **kwargs)

    def test_clean_search_returns_parsed_results(self):
        page = self._page(["clean"])
        results = core.search_site(page, "one piece")

        self.assertEqual([r["hid"] for r in results], ["abc12", "def34"])
        self.assertEqual(results[0]["type"], "MANGA")
        self.assertEqual(results[0]["chapters"], "1100")
        self.assertEqual(results[0]["url"], "https://comix.to/title/abc12-one-piece")
        self.assertEqual(page.typed, [("fill", ""), ("type", "one piece")])

    def test_transient_empty_popup_waits_for_the_real_results(self):
        # The regression: the site renders its empty marker while a long query
        # is still being typed, then swaps in results ~800 ms later. Believing
        # the first empty render is what made long queries return nothing.
        page = self._page(["clean:transient"], results_after_s=2.5)

        results = core.search_site(page, "I Become Invincible after She Saves Me")

        self.assertEqual([r["hid"] for r in results], ["abc12", "def34"])
        self.assertEqual(page.gotos, 1)

    def test_transient_empty_longer_than_the_settle_is_confirmed_then_believed(self):
        # A popup that stays empty past the settle window still gets a second
        # pass, because the transient window widens on a slow egress.
        page = self._page(["clean:transient"], results_after_s=30.0)
        logs: list = []

        self.assertEqual(core.search_site(page, "nope", log=logs.append), [])
        self.assertEqual(page.gotos, 2)
        self.assertTrue(any("confirming" in m for m in logs))
        self.assertTrue(any("no matches" in m for m in logs))

    def test_blank_query_short_circuits(self):
        page = self._page(["clean"])
        self.assertEqual(core.search_site(page, "   "), [])
        self.assertEqual(page.gotos, 0)

    def test_challenge_triggers_the_solver_and_retries(self):
        page = self._page(["challenge", "clean"])
        seen: list = []

        def hook(context, log=None):
            seen.append(context)
            return True

        results = core.search_site(page, "one piece", cf_solver_hook=hook)

        self.assertEqual(len(seen), 1)
        self.assertEqual(len(results), 2)

    def test_solver_refusal_re_raises_the_challenge(self):
        page = self._page(["challenge"])
        hook = lambda context, log=None: False  # noqa: E731

        with self.assertRaises(core.VerificationRequired):
            core.search_site(page, "one piece", cf_solver_hook=hook)

    def test_no_hook_means_the_challenge_propagates(self):
        page = self._page(["challenge"])

        with self.assertRaises(core.VerificationRequired):
            core.search_site(page, "one piece")

    def test_solver_is_not_called_after_the_last_attempt(self):
        page = self._page(["challenge"])
        calls: list = []
        hook = lambda context, log=None: calls.append(1) or True  # noqa: E731

        with self.assertRaises(core.VerificationRequired):
            core.search_site(page, "one piece", cf_solver_hook=hook, max_attempts=2)
        # Two attempts: the first solved and retried, the second raised.
        self.assertEqual(len(calls), 1)

    def test_banned_egress_is_never_handed_to_the_solver(self):
        page = self._page(["blocked"])
        calls: list = []
        hook = lambda context, log=None: calls.append(1) or True  # noqa: E731

        with self.assertRaises(core.AccessBlockedError):
            core.search_site(page, "one piece", cf_solver_hook=hook)
        self.assertEqual(calls, [])

    def test_block_detected_after_the_query_still_propagates(self):
        # The popup's own request can be banned while the document loaded fine.
        page = self._page(["clean"], block_on_query=True)
        calls: list = []
        hook = lambda context, log=None: calls.append(1) or True  # noqa: E731

        with self.assertRaises(core.AccessBlockedError):
            core.search_site(page, "one piece", cf_solver_hook=hook)
        self.assertEqual(calls, [])

    def test_unrendered_popup_is_retried_then_reported_as_empty(self):
        page = self._page(["clean:none"])
        logs: list = []

        results = core.search_site(page, "one piece", log=logs.append)

        self.assertEqual(results, [])
        self.assertGreater(page.gotos, 1, "a popup that renders nothing is retried")
        self.assertTrue(any("never rendered" in m for m in logs))

    def test_empty_marker_is_confirmed_before_being_believed(self):
        # A genuine miss is still not taken on trust: the popup's empty marker
        # doubles as a transient state while a long query is being typed.
        page = self._page(["clean:empty"])
        logs: list = []

        results = core.search_site(page, "nothing matches this at all", log=logs.append)

        self.assertEqual(results, [])
        self.assertEqual(page.gotos, 2)
        self.assertTrue(any("confirming" in m for m in logs))


class RotateEgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = core.urbproxy

    def tearDown(self) -> None:
        core.urbproxy = self._saved

    def test_no_vpn_module_means_no_rotation(self):
        core.urbproxy = None
        self.assertFalse(core.rotate_vpn_egress(log=lambda _m: None))

    def test_inactive_vpn_means_no_rotation(self):
        class Fake:
            @staticmethod
            def is_active():
                return False

        core.urbproxy = Fake
        self.assertFalse(core.rotate_vpn_egress(log=lambda _m: None))

    def test_rotation_avoids_the_ip_that_was_banned(self):
        seen: dict = {}

        class Fake:
            @staticmethod
            def is_active():
                return True

            @staticmethod
            def current_ip():
                return "203.0.113.9"

            @staticmethod
            def rotate_egress(avoid=None, log=print):
                seen["avoid"] = avoid
                return True

        core.urbproxy = Fake
        self.assertTrue(core.rotate_vpn_egress(log=lambda _m: None))
        self.assertEqual(seen["avoid"], {"203.0.113.9"})

    def test_a_failed_rotation_is_reported_not_raised(self):
        class Fake:
            @staticmethod
            def is_active():
                return True

            @staticmethod
            def current_ip():
                return "203.0.113.9"

            @staticmethod
            def rotate_egress(avoid=None, log=print):
                raise RuntimeError("no gateway left")

        core.urbproxy = Fake
        logs: list = []
        self.assertFalse(core.rotate_vpn_egress(log=logs.append))
        self.assertTrue(any("rotate" in m.lower() for m in logs))


class SessionSearchRotationTests(unittest.TestCase):
    """The outer loop: a banned egress gets a new exit IP, or a clear error."""

    def _session(self, events: list, logs: list):
        sess = session_mod.BrowserSession.__new__(session_mod.BrowserSession)
        sess.page = None
        sess.context = None
        sess.config = mock.Mock()
        sess.config.data = {}
        sess.emit = lambda event, payload=None: events.append((event, payload))
        sess._log = logs.append
        sess._ensure_browser = lambda: None
        sess._close_context = lambda: events.append(("browser", "closed"))
        return sess

    def setUp(self) -> None:
        self._saved = {
            "search_site": session_mod.search_site,
            "rotate": session_mod.rotate_vpn_egress,
            "max_rot": session_mod.SEARCH_MAX_ROTATIONS,
        }
        session_mod.SEARCH_MAX_ROTATIONS = 2

    def tearDown(self) -> None:
        session_mod.search_site = self._saved["search_site"]
        session_mod.rotate_vpn_egress = self._saved["rotate"]
        session_mod.SEARCH_MAX_ROTATIONS = self._saved["max_rot"]

    def test_clean_search_emits_results_once(self):
        events: list = []
        logs: list = []
        sess = self._session(events, logs)
        session_mod.search_site = lambda *a, **k: [{"hid": "abc12"}]

        sess._run_search("one piece")

        self.assertEqual(events, [("search_results", [{"hid": "abc12"}])])

    def test_ban_rotates_then_succeeds(self):
        events: list = []
        logs: list = []
        sess = self._session(events, logs)
        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise core.AccessBlockedError("blocked", ray_id="8a1b")
            return [{"hid": "abc12"}]

        session_mod.search_site = flaky
        rotations: list = []
        session_mod.rotate_vpn_egress = lambda log=None: rotations.append(1) or True

        sess._run_search("one piece")

        self.assertEqual(len(rotations), 2)
        # The proxy is a launch flag, so Chrome is relaunched each rotation.
        self.assertEqual(events.count(("browser", "closed")), 2)
        self.assertEqual(events[-1], ("search_results", [{"hid": "abc12"}]))

    def test_ban_gives_up_after_the_rotation_budget(self):
        events: list = []
        logs: list = []
        sess = self._session(events, logs)

        def always_blocked(*_a, **_k):
            raise core.AccessBlockedError("blocked", ray_id="8a1b")

        session_mod.search_site = always_blocked
        rotations: list = []
        session_mod.rotate_vpn_egress = lambda log=None: rotations.append(1) or True

        sess._run_search("one piece")

        self.assertEqual(len(rotations), 2)
        self.assertEqual(events[-1], ("job_blocked", ("search", "access_blocked")))
        self.assertTrue(any("rotated" in m.lower() for m in logs))

    def test_no_alternative_egress_reports_rather_than_spinning(self):
        events: list = []
        logs: list = []
        sess = self._session(events, logs)
        attempts = {"n": 0}

        def always_blocked(*_a, **_k):
            attempts["n"] += 1
            raise core.AccessBlockedError("blocked", ray_id="8a1b")

        session_mod.search_site = always_blocked
        session_mod.rotate_vpn_egress = lambda log=None: False

        sess._run_search("one piece")

        # One try, one failed rotation, then it reports instead of retrying.
        self.assertEqual(attempts["n"], 1)
        self.assertEqual(events[-1], ("job_blocked", ("search", "access_blocked")))
        self.assertTrue(any("no alternative exit ip" in m.lower() for m in logs))

    def test_a_solvable_challenge_falls_back_to_the_manual_path(self):
        events: list = []
        logs: list = []
        sess = self._session(events, logs)
        session_mod.search_site = lambda *a, **k: (_ for _ in ()).throw(
            core.CloudflareBlockException("nope")
        )
        sess._report_blocked_job = lambda ctx, vreq: events.append(
            ("manual", ctx)
        )

        sess._run_search("one piece")

        self.assertEqual(events, [("manual", "search")])


class NoVpnClientTests(unittest.TestCase):
    """urbproxy.py is gitignored and local-only, so it may simply be absent.

    Nothing on the search path may depend on it existing.
    """

    def setUp(self) -> None:
        self._saved = core.urbproxy
        self.clock = FakeClock(time)
        patcher = mock.patch.object(core, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _page(self, *args, **kwargs) -> FakeSearchPage:
        kwargs.setdefault("clock", self.clock)
        return FakeSearchPage(*args, **kwargs)

    def tearDown(self) -> None:
        core.urbproxy = self._saved

    def test_absent_module_imports_and_searches(self):
        core.urbproxy = None
        page = self._page(["clean"])

        results = core.search_site(page, "one piece")

        self.assertEqual([r["hid"] for r in results], ["abc12", "def34"])

    def test_absent_module_makes_rotation_a_clean_no(self):
        core.urbproxy = None
        self.assertFalse(core.rotate_vpn_egress(log=lambda _m: None))

    def test_a_stale_module_without_the_new_helpers_degrades(self):
        # An older urbproxy.py in the wild has no current_ip/rotate_egress.
        # That must surface as a clean "could not rotate", never an AttributeError.
        class Stale:
            @staticmethod
            def is_active():
                return True

        core.urbproxy = Stale
        logs: list = []

        self.assertFalse(core.rotate_vpn_egress(log=logs.append))
        self.assertTrue(any("rotate" in m.lower() for m in logs))

    def test_a_ban_still_reports_cleanly_without_rotation(self):
        core.urbproxy = None
        events: list = []
        logs: list = []
        sess = session_mod.BrowserSession.__new__(session_mod.BrowserSession)
        sess.page = None
        sess.context = None
        sess.config = mock.Mock()
        sess.config.data = {}
        sess.emit = lambda event, payload=None: events.append((event, payload))
        sess._log = logs.append
        sess._ensure_browser = lambda: None
        sess._close_context = lambda: None

        def blocked(*_a, **_k):
            raise core.AccessBlockedError("Cloudflare blocked this IP.")

        with mock.patch.object(session_mod, "search_site", blocked):
            sess._run_search("one piece")

        self.assertEqual(events, [("job_blocked", ("search", "access_blocked"))])


if __name__ == "__main__":
    unittest.main()