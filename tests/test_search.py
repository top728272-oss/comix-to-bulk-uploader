"""Tests for the site search path and its Cloudflare recovery - no browser.

The regression these pin down: a live Cloudflare interstitial was classified as
an origin outage (every Cloudflare page footer carries a Ray ID), so a query
behind a flagged VPN egress came back as zero results with no error, no solver
call and no rotation.

Run: .venv\\Scripts\\python.exe -m unittest discover tests
"""

from __future__ import annotations

import sys
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
        self._page.typed.append(("type", value))


class FakeSearchPage:
    """A tab whose gate state and popup behaviour are scripted per navigation."""

    def __init__(self, states: list, block_on_query: bool = False) -> None:
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
        # "results" | "empty" | "none"
        self.popup = "results"
        # Gate states: clean | challenge | blocked | outage
        self.gate = "clean"
        # The popup's own request gets banned while the document loaded fine -
        # the shape a search-time IP ban actually takes.
        self.block_on_query = block_on_query

    def _apply(self) -> None:
        # gotos is incremented before this runs, so the first goto is index 0.
        index = min(self.gotos - 1, len(self._states) - 1)
        self.state = self._states[index]
        gate, _, popup = self.state.partition(":")
        self.gate = gate
        if popup:
            self.popup = popup
        if gate == "challenge":
            self.title_text = "Just a moment..."
            self.body_text = CHALLENGE_BODY
            self.html_extra = "challenge-platform cf-chl-opt"
        elif gate == "blocked":
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

    def visible(self, selector: str) -> bool:
        if self.gate == "challenge" and "challenge" in selector:
            return True
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
        return None

    def wait_for_selector(self, selector, timeout=None) -> None:
        if self.block_on_query:
            self.gate = "blocked"
            self.title_text = "Attention Required! | Cloudflare"
            self.body_text = (
                "Sorry, you have been blocked. You are unable to access comix.to. "
                "Cloudflare Ray ID: 8a1b2c3d4e5f6a7b"
            )
        if self.popup == "none":
            raise TimeoutError("popup never rendered")

    def eval_on_selector_all(self, selector, js):
        return [] if self.popup != "results" else list(RESULT_ROWS)

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
    def test_clean_search_returns_parsed_results(self):
        page = FakeSearchPage(["clean"])
        results = core.search_site(page, "one piece")

        self.assertEqual([r["hid"] for r in results], ["abc12", "def34"])
        self.assertEqual(results[0]["type"], "MANGA")
        self.assertEqual(results[0]["chapters"], "1100")
        self.assertEqual(results[0]["url"], "https://comix.to/title/abc12-one-piece")
        self.assertEqual(page.typed, [("fill", ""), ("type", "one piece")])

    def test_blank_query_short_circuits(self):
        page = FakeSearchPage(["clean"])
        self.assertEqual(core.search_site(page, "   "), [])
        self.assertEqual(page.gotos, 0)

    def test_challenge_triggers_the_solver_and_retries(self):
        page = FakeSearchPage(["challenge", "clean"])
        seen: list = []

        def hook(context, log=None):
            seen.append(context)
            return True

        results = core.search_site(page, "one piece", cf_solver_hook=hook)

        self.assertEqual(len(seen), 1)
        self.assertEqual(len(results), 2)

    def test_solver_refusal_re_raises_the_challenge(self):
        page = FakeSearchPage(["challenge"])
        hook = lambda context, log=None: False  # noqa: E731

        with self.assertRaises(core.VerificationRequired):
            core.search_site(page, "one piece", cf_solver_hook=hook)

    def test_no_hook_means_the_challenge_propagates(self):
        page = FakeSearchPage(["challenge"])

        with self.assertRaises(core.VerificationRequired):
            core.search_site(page, "one piece")

    def test_solver_is_not_called_after_the_last_attempt(self):
        page = FakeSearchPage(["challenge"])
        calls: list = []
        hook = lambda context, log=None: calls.append(1) or True  # noqa: E731

        with self.assertRaises(core.VerificationRequired):
            core.search_site(page, "one piece", cf_solver_hook=hook, max_attempts=2)
        # Two attempts: the first solved and retried, the second raised.
        self.assertEqual(len(calls), 1)

    def test_banned_egress_is_never_handed_to_the_solver(self):
        page = FakeSearchPage(["blocked"])
        calls: list = []
        hook = lambda context, log=None: calls.append(1) or True  # noqa: E731

        with self.assertRaises(core.AccessBlockedError):
            core.search_site(page, "one piece", cf_solver_hook=hook)
        self.assertEqual(calls, [])

    def test_block_detected_after_the_query_still_propagates(self):
        # The popup's own request can be banned while the document loaded fine.
        page = FakeSearchPage(["clean:none"], block_on_query=True)
        calls: list = []
        hook = lambda context, log=None: calls.append(1) or True  # noqa: E731

        with self.assertRaises(core.AccessBlockedError):
            core.search_site(page, "one piece", cf_solver_hook=hook)
        self.assertEqual(calls, [])

    def test_unrendered_popup_is_retried_then_reported_as_empty(self):
        page = FakeSearchPage(["clean"])
        page.popup = "none"
        logs: list = []

        results = core.search_site(page, "one piece", log=logs.append)

        self.assertEqual(results, [])
        self.assertGreater(page.gotos, 1, "a popup that renders nothing is retried")
        self.assertTrue(any("never rendered" in m for m in logs))

    def test_empty_marker_is_believed_without_retrying(self):
        page = FakeSearchPage(["clean"])
        page.popup = "empty"

        results = core.search_site(page, "nothing matches this at all")

        self.assertEqual(results, [])
        self.assertEqual(page.gotos, 1)


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


if __name__ == "__main__":
    unittest.main()