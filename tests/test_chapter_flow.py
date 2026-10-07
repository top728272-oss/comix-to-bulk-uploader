"""Behaviour tests for the upload engine — no browser, no network.

Two things are being pinned down here:

1. The ChapterContext/_process_chapter refactor kept the sequential semantics
   intact (success / failure / stop / gate-cleared / gate-expired paths).
2. parallel.run_upload_batch_auto picks the right engine and always degrades to
   the sequential one instead of failing a run.

Run from the project root:  .venv\\Scripts\\python.exe -m unittest discover tests
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# Allow running this file directly (`python tests/test_chapter_flow.py`) as
# well as through unittest discovery: the engine lives in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core
import parallel

URL = "https://comix.to/title/1234-test-series"


class FakePage:
    """The engine only ever passes the page around; nothing is driven here."""

    def __init__(self) -> None:
        self.context = None
        self.url = "about:blank"

    def title(self) -> str:
        return ""

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator()


class FakeLocator:
    """Just enough of Playwright's Locator for the bounded page reads.

    Every accessor accepts the ``timeout`` kwarg the real API takes: the whole
    point of routing the detectors through locators is that they carry a
    driver-side deadline, so a fake that ignored it would test nothing.
    """

    def __init__(self, visible: bool = False, text: str = "") -> None:
        self._visible = visible
        self._text = text

    @property
    def first(self) -> "FakeLocator":
        return self

    def is_visible(self, timeout=None) -> bool:
        return self._visible

    def inner_text(self, timeout=None) -> str:
        return self._text

    def text_content(self, timeout=None) -> str:
        return self._text

    def inner_html(self, timeout=None) -> str:
        return self._text

    def get_attribute(self, name, timeout=None):
        return None

    def evaluate(self, expression, arg=None, timeout=None):
        return None


class FakeContext:
    def __init__(self, pages: list | None = None) -> None:
        self.pages = pages or []


class FakeChallengePage:
    """Minimal page good enough for the Cloudflare detectors + wait loop."""

    def __init__(
        self,
        title: str = "",
        body: str = "",
        selectors: dict | None = None,
        closed: bool = False,
        wedged: bool = False,
    ) -> None:
        self._title = title
        self._body = body
        self._selectors = selectors or {}
        self._closed = closed
        # A wedged tab: every bounded read raises, the way a dead renderer
        # makes Playwright's locator calls time out.
        self._wedged = wedged
        self.url = "https://comix.to/user/upload/123"
        self.context = FakeContext([self])

    def title(self) -> str:
        return self._title

    def is_closed(self) -> bool:
        return self._closed

    def bring_to_front(self) -> None:  # pragma: no cover - not used with focus=False
        pass

    def wait_for_timeout(self, ms) -> None:
        pass

    def locator(self, selector: str) -> FakeLocator:
        if self._wedged:
            raise TimeoutError("locator timed out")
        if selector == "body":
            return FakeLocator(text=self._body)
        if selector == "title":
            return FakeLocator(text=self._title)
        if selector == "html":
            # The bounded HTML scan looks for challenge/Turnstile markers here,
            # so it sees the body text plus whatever the selectors stand for.
            return FakeLocator(text=f"{self._body} {' '.join(self._selectors)}")
        for key, visible in self._selectors.items():
            if key in selector:
                return FakeLocator(visible=visible)
        return FakeLocator(visible=False)


class EngineHarness:
    """Shared engine test scaffolding (not itself collected as a TestCase)."""

    def setUp(self) -> None:
        self._saved = {
            "BASE_DIR": core.BASE_DIR,
            "CHALLENGE_DEBUG_DIR": core.CHALLENGE_DEBUG_DIR,
            "scan_folder": core.scan_folder,
            "upload_single_chapter": core.upload_single_chapter,
            "wait_for_challenge_clear": core.wait_for_challenge_clear,
            "run_upload_batch": core.run_upload_batch,
            "_run_parallel": parallel._run_parallel,
        }
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        core.BASE_DIR = tmp
        core.CHALLENGE_DEBUG_DIR = tmp / ".challenge_debug"
        self.folder = tmp / "chapters"
        self.folder.mkdir()
        self.page = FakePage()
        self.logs: list[str] = []

    def tearDown(self) -> None:
        core.BASE_DIR = self._saved["BASE_DIR"]
        core.CHALLENGE_DEBUG_DIR = self._saved["CHALLENGE_DEBUG_DIR"]
        core.scan_folder = self._saved["scan_folder"]
        core.upload_single_chapter = self._saved["upload_single_chapter"]
        core.wait_for_challenge_clear = self._saved["wait_for_challenge_clear"]
        core.run_upload_batch = self._saved["run_upload_batch"]
        parallel._run_parallel = self._saved["_run_parallel"]
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def _log(self, message: str) -> None:
        self.logs.append(str(message))

    def _params(self, **kw) -> core.UploadParams:
        kw.setdefault("folder", str(self.folder))
        kw.setdefault("delay", 0)
        return core.UploadParams(url=URL, **kw)

    def _chapters(self, *nums: int) -> None:
        core.scan_folder = lambda folder: [
            (n, str(self.folder / f"{n:g}.cbz")) for n in nums
        ]

    def _run(self, config=None, control=None, **kw):
        return core.run_upload_batch(
            page=self.page,
            config=config or {},
            params=self._params(),
            log=self._log,
            control=control,
            max_retries=kw.pop("max_retries", 1),
            **kw,
        )

    def _history(self) -> set:
        return core.load_history(core.get_history_file(URL))

    def _failed(self) -> dict:
        return core.load_failed(core.get_failed_file(URL))


class EngineTestCase(EngineHarness, unittest.TestCase):
    # -- sequential engine ---------------------------------------------------

    def test_success_records_history_and_summary(self):
        self._chapters(1, 2)
        core.upload_single_chapter = lambda **kw: (True, None)
        statuses: list[tuple] = []

        summary = self._run(on_status=lambda ch, st, d: statuses.append((str(ch), st)))

        self.assertEqual(
            (summary.processed, summary.succeeded, summary.failed), (2, 2, 0)
        )
        self.assertEqual(self._history(), {"1", "2"})
        self.assertIn(("1", "uploading"), statuses)
        self.assertIn(("2", "done"), statuses)

    def test_failure_is_recorded_and_does_not_stop_the_batch(self):
        self._chapters(1, 2)
        core.upload_single_chapter = lambda **kw: (
            (False, "boom") if kw["chapter_num"] == 1 else (True, None)
        )

        summary = self._run()

        self.assertEqual(
            (summary.processed, summary.succeeded, summary.failed), (2, 1, 1)
        )
        self.assertEqual(summary.failures[0][0], 1)
        self.assertEqual(summary.failures[0][2], "boom")
        self.assertEqual(self._failed(), {"1": "boom"})
        self.assertEqual(self._history(), {"2"})

    def test_already_uploaded_chapters_are_skipped(self):
        self._chapters(1, 2)
        core.save_history(core.get_history_file(URL), {"1"})
        seen: list = []
        core.upload_single_chapter = lambda **kw: (
            seen.append(kw["chapter_num"]),
            (True, None),
        )[1]

        summary = self._run()

        self.assertEqual(seen, [2])
        self.assertEqual(summary.processed, 1)

    def test_stop_between_chapters_leaves_the_rest_alone(self):
        self._chapters(1, 2, 3)
        control = core.Control()
        seen: list = []

        def fake_upload(**kw):
            seen.append(kw["chapter_num"])
            return True, None

        def on_status(ch, status, detail):
            # Stop once chapter 1 is fully recorded — i.e. between chapters.
            if str(ch) == "1" and status == "done":
                control.request_stop()

        core.upload_single_chapter = fake_upload

        summary = self._run(control=control, on_status=on_status)

        self.assertEqual(seen, [1])
        self.assertEqual(summary.processed, 1)
        self.assertEqual(self._history(), {"1"})

    def test_stop_during_an_upload_discards_that_chapter(self):
        """Faithful to the pre-refactor engine: a stop arriving mid-upload
        leaves the chapter unrecorded (it is re-done safely on the next run)
        and ends the batch."""
        self._chapters(1, 2)
        control = core.Control()
        statuses: list[tuple] = []

        def fake_upload(**kw):
            control.request_stop()
            return True, None

        core.upload_single_chapter = fake_upload

        summary = self._run(
            control=control, on_status=lambda ch, st, d: statuses.append((str(ch), st))
        )

        self.assertEqual(summary.processed, 0)
        self.assertEqual(self._history(), set())
        self.assertIn(("1", "pending"), statuses)

    def test_gate_cleared_retries_the_same_chapter(self):
        self._chapters(1)
        state = {"first": True}
        cf_calls: list = []

        def fake_upload(**kw):
            if state["first"]:
                state["first"] = False
                raise core.CloudflareBlockException("challenge")
            return True, None

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = lambda *a, **kw: True

        summary = self._run(on_cloudflare=lambda ch: cf_calls.append(ch))

        self.assertEqual((summary.processed, summary.succeeded), (1, 1))
        self.assertEqual(cf_calls, [1])
        self.assertEqual(self._history(), {"1"})

    def test_gate_expiry_records_nothing(self):
        self._chapters(1)
        statuses: list[tuple] = []

        def fake_upload(**kw):
            raise core.CloudflareBlockException("challenge")

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = lambda *a, **kw: False

        summary = self._run(on_status=lambda ch, st, d: statuses.append((str(ch), st)))

        # "pending" must never be counted or persisted.
        self.assertEqual(
            (summary.processed, summary.succeeded, summary.failed), (0, 0, 0)
        )
        self.assertEqual(self._history(), set())
        self.assertEqual(self._failed(), {})
        self.assertIn(("1", "pending"), statuses)

    def test_repeated_gates_eventually_fail_the_chapter(self):
        self._chapters(1)

        def fake_upload(**kw):
            raise core.CloudflareBlockException("challenge")

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = lambda *a, **kw: True  # always "cleared"

        summary = self._run(config={"challenge": {"max_pauses_per_chapter": 2}})

        self.assertEqual(summary.failed, 1)
        self.assertEqual(self._failed()["1"], "Repeated verification screens")

    # -- dispatch ------------------------------------------------------------

    def test_concurrency_one_uses_the_sequential_engine(self):
        calls: list = []
        core.run_upload_batch = lambda **kw: (calls.append(kw), core.RunSummary())[1]

        out = parallel.run_upload_batch_auto(
            page=self.page,
            endpoint="http://127.0.0.1:9999",
            config={"concurrency": 1},
            params=self._params(),
            log=self._log,
        )

        self.assertIsInstance(out, core.RunSummary)
        self.assertEqual(len(calls), 1)

    def test_missing_endpoint_falls_back_with_a_warning(self):
        calls: list = []
        core.run_upload_batch = lambda **kw: (calls.append(kw), core.RunSummary())[1]

        parallel.run_upload_batch_auto(
            page=self.page,
            endpoint=None,
            config={"concurrency": 5},
            params=self._params(),
            log=self._log,
        )

        self.assertEqual(len(calls), 1)
        self.assertTrue(any("Parallel uploads unavailable" in m for m in self.logs))

    def test_out_of_range_concurrency_errors_then_runs_sequentially(self):
        calls: list = []
        core.run_upload_batch = lambda **kw: (calls.append(kw), core.RunSummary())[1]

        parallel.run_upload_batch_auto(
            page=self.page,
            endpoint="http://127.0.0.1:9999",
            config={"concurrency": 9},
            params=self._params(),
            log=self._log,
        )

        self.assertEqual(len(calls), 1)
        self.assertTrue(any("1 to 8" in m for m in self.logs))

    def test_parallel_crash_falls_back_and_merges_counts(self):
        def boom(*a, **kw):
            raise RuntimeError("worker blew up")

        parallel._run_parallel = boom

        tail = core.RunSummary(
            processed=2, succeeded=1, failed=1, failures=[(3, "3.cbz", "x")]
        )
        core.run_upload_batch = lambda **kw: tail

        out = parallel.run_upload_batch_auto(
            page=self.page,
            endpoint="http://127.0.0.1:9999",
            config={"concurrency": 5},
            params=self._params(),
            log=self._log,
        )

        self.assertEqual((out.processed, out.succeeded, out.failed), (2, 1, 1))
        self.assertEqual(out.failures, [(3, "3.cbz", "x")])
        self.assertTrue(any("Parallel mode failed" in m for m in self.logs))


class ServerDownAbortTests(EngineHarness, unittest.TestCase):
    """A dead origin stops the batch instead of looping as a Cloudflare check."""

    def test_sequential_aborts_after_retries_and_stops_batch(self):
        self._chapters(1, 2)
        attempted: list = []

        def fake_upload(**kw):
            attempted.append(kw["chapter_num"])
            raise core.ServerDownError("HTTP 502 on /api/v1/uploads")

        core.upload_single_chapter = fake_upload

        # The retry backoff is real seconds; keep the test instant.
        with mock.patch("core.time.sleep", lambda *a, **k: None):
            summary = self._run(max_retries=2)

        self.assertTrue(summary.aborted)
        self.assertTrue(summary.abort_reason)
        # Chapter 1 exhausted its retries; chapter 2 was never attempted.
        self.assertEqual(attempted, [1, 1])
        self.assertEqual(
            (summary.processed, summary.succeeded, summary.failed), (0, 0, 0)
        )
        self.assertEqual(self._history(), set())
        self.assertEqual(self._failed(), {})

    def test_auto_skips_sequential_tail_on_abort(self):
        def fake_parallel(endpoint, config, params, n, summary, **kw):
            summary.aborted = True
            summary.abort_reason = "comix server appears down"
            return [2], False

        parallel._run_parallel = fake_parallel
        tail_calls: list = []
        core.run_upload_batch = lambda **kw: (tail_calls.append(kw), core.RunSummary())[1]

        out = parallel.run_upload_batch_auto(
            page=self.page,
            endpoint="http://127.0.0.1:9999",
            config={"concurrency": 5},
            params=self._params(),
            log=self._log,
        )

        self.assertTrue(out.aborted)
        self.assertEqual(tail_calls, [], "the sequential tail must not re-run aborted work")

    def test_parallel_aggregation_marks_abort(self):
        summary = core.RunSummary()
        stragglers = parallel._aggregate_results(
            [(1, "1.cbz", "aborted", "down"), (2, "2.cbz", "done", None)],
            [],
            summary,
        )

        self.assertTrue(summary.aborted)
        self.assertEqual(summary.abort_reason, "down")
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(stragglers, [], "an aborted chapter is not a straggler")


class AccessBlockTests(EngineHarness, unittest.TestCase):
    """A Cloudflare WAF block (Error 1020) stops the batch and records nothing.

    Distinct from both a challenge (nothing to wait for, no cookie helps) and a
    dead origin (the site is up — it is this IP/path that is refused).
    """

    def test_sequential_aborts_on_the_first_block(self):
        self._chapters(1, 2)
        control = core.Control()
        statuses: list[tuple] = []
        message = (
            "Cloudflare blocked this IP on the upload path (Ray ID deadbeef1234)."
        )

        def fake_upload(**kw):
            raise core.AccessBlockedError(message, ray_id="deadbeef1234")

        core.upload_single_chapter = fake_upload

        summary = self._run(
            control=control,
            on_status=lambda ch, st, d: statuses.append((str(ch), st, d)),
        )

        self.assertTrue(summary.aborted)
        self.assertEqual(summary.abort_reason, message)
        self.assertIn("deadbeef1234", summary.abort_reason)
        # Nothing recorded, so the chapters come back on the next run.
        self.assertEqual(self._history(), set())
        self.assertEqual(self._failed(), {})
        self.assertIn(("1", "aborted", message), statuses)
        # Chapter 2 was never attempted: a block is per-IP, so it would fail too.
        self.assertEqual(len(statuses), 2, statuses)

    def test_block_raises_the_right_error_from_a_blocked_upload_page(self):
        """The upload path must raise AccessBlockedError, not ServerDownError.

        The block page has no upload form, so the form wait fails — which used
        to be misread as a dead origin.
        """
        page = FakeChallengePage(
            title="Attention Required! | Cloudflare",
            body="Sorry, you have been blocked. Cloudflare Ray ID: cafe1234beef",
        )

        with self.assertRaises(core.AccessBlockedError) as caught:
            core._raise_if_access_blocked(page)

        self.assertEqual(caught.exception.ray_id, "cafe1234beef")
        self.assertEqual(caught.exception.kind, "access_blocked")
        self.assertIn("cafe1234beef", str(caught.exception))

    def test_an_ordinary_page_does_not_raise(self):
        core._raise_if_access_blocked(FakeChallengePage(title="Upload"))


class ParallelStallTests(unittest.TestCase):
    """A wedged worker must not freeze the batch.

    Playwright's page.title() / page.content() / page.evaluate() carry no
    timeout at all, so a dead renderer blocks its worker thread forever. That
    call cannot be interrupted, so the coordinator stops waiting for it and
    hands the chapter to the sequential engine.
    """

    def test_a_stalled_worker_is_abandoned_and_named(self):
        logs: list[str] = []
        released = threading.Event()

        def finishes(state):
            state.beat(1)
            return [(1, "1.cbz", "done", None)]

        def wedges(state):
            state.beat(2)
            # Stands in for a Playwright call that never returns.
            released.wait(30)
            return []

        start = time.time()
        try:
            results, errors, stalled = parallel._run_workers_with_deadline(
                [finishes, wedges], stall_timeout=1.0, log=logs.append
            )
            elapsed = time.time() - start
        finally:
            released.set()

        self.assertLess(elapsed, 10, "must not wait for the wedged worker")
        self.assertEqual(results, [(1, "1.cbz", "done", None)])
        self.assertEqual(errors, [])
        self.assertEqual(stalled, [2], "the stalled chapter is handed back")
        self.assertTrue(any("Tab 2" in line for line in logs), logs)

    def test_an_abandoned_worker_is_told_to_stop(self):
        """A wedged worker may recover; it must not then resume the loop.

        Without this the abandoned worker keeps pulling chapters and uploading
        them concurrently with the sequential retry — which is how one chapter
        ended up uploaded twice and recorded as both done and failed.
        """
        released = threading.Event()
        saw_abandoned: list[bool] = []

        def wedges(state):
            state.beat(7)
            released.wait(30)
            # Stands in for the worker's loop re-checking the flag once its
            # blocked call finally returns.
            saw_abandoned.append(state.abandoned)
            return []

        _results, _errors, stalled = parallel._run_workers_with_deadline(
            [wedges], stall_timeout=1.0, log=lambda m: None
        )
        released.set()

        self.assertEqual(stalled, [7])
        for _ in range(50):
            if saw_abandoned:
                break
            time.sleep(0.05)
        self.assertEqual(saw_abandoned, [True], "the worker must see abandoned=True")

    def test_workers_that_all_finish_return_their_rows(self):
        def ok(state):
            state.beat(1)
            return [(1, "1.cbz", "done", None)]

        results, errors, stalled = parallel._run_workers_with_deadline(
            [ok, ok], stall_timeout=30, log=lambda m: None
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(errors, [])
        self.assertEqual(stalled, [])

    def test_a_worker_exception_is_reported_not_raised(self):
        def boom(state):
            raise RuntimeError("kaboom")

        results, errors, stalled = parallel._run_workers_with_deadline(
            [boom], stall_timeout=5, log=lambda m: None
        )

        self.assertEqual(results, [])
        self.assertEqual(stalled, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("kaboom", errors[0])


class TabReplacementTests(unittest.TestCase):
    """Replacing a worker tab must be rare, confirmed, and bounded.

    The old code opened a fresh tab on every unresponsive check and could never
    close the old one (``Page.close()`` takes no timeout, so closing a wedged
    tab is the call that would hang), so a long run leaked a tab per chapter and
    Chrome's tab count climbed continuously.
    """

    def test_one_failed_probe_is_not_enough(self):
        """A busy renderer must not cost a tab."""
        calls = {"n": 0}

        class Flaky:
            def is_closed(self):
                return False

            def locator(self, selector):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("busy")
                return FakeLocator()

        self.assertFalse(parallel._tab_is_dead(Flaky()))
        self.assertEqual(calls["n"], 2, "it must probe a second time")

    def test_two_failed_probes_mean_dead(self):
        class Wedged:
            def is_closed(self):
                return False

            def locator(self, selector):
                raise TimeoutError("wedged")

        self.assertTrue(parallel._tab_is_dead(Wedged()))

    def test_a_healthy_tab_is_never_replaced(self):
        class Healthy:
            def is_closed(self):
                return False

            def locator(self, selector):
                return FakeLocator()

        self.assertFalse(parallel._tab_is_dead(Healthy()))

    def test_replacements_are_capped(self):
        self.assertGreaterEqual(parallel.MAX_TAB_REPLACEMENTS, 1)
        self.assertLessEqual(parallel.MAX_TAB_REPLACEMENTS, 5)


class DuplicateUploadGuardTests(EngineHarness, unittest.TestCase):
    """One run must never upload the same chapter twice."""

    def test_the_tail_skips_a_chapter_finished_after_the_snapshot(self):
        """The pending list is computed once, so a parallel worker that
        finishes a chapter after that snapshot must not be re-done."""
        self._chapters(1, 2)
        uploaded: list = []

        real_build = core.build_chapter_context
        try:
            ctx = real_build({}, self._params())
            core.build_chapter_context = lambda *a, **kw: ctx

            def fake_upload(**kw):
                uploaded.append(kw["chapter_num"])
                if kw["chapter_num"] == 1:
                    # Stands in for a parallel worker finishing chapter 2.
                    ctx.recorder.record_success(2)
                return True, None

            core.upload_single_chapter = fake_upload
            summary = self._run()
        finally:
            core.build_chapter_context = real_build

        self.assertEqual(uploaded, [1], "chapter 2 was recorded mid-run")
        self.assertEqual(summary.succeeded, 1)

    def test_a_failure_never_contradicts_a_recorded_success(self):
        """A stale duplicate attempt must not leave one chapter both done and
        failed — the state seen in upload_history/upload_failed for 14-17."""
        recorder = core.HistoryRecorder(URL)
        recorder.record_success(7)
        recorder.record_failure(7, "stale duplicate attempt")

        self.assertEqual(core.load_history(core.get_history_file(URL)), {"7"})
        self.assertEqual(core.load_failed(core.get_failed_file(URL)), {})

    def test_a_genuine_failure_is_still_recorded(self):
        recorder = core.HistoryRecorder(URL)
        recorder.record_failure(9, "boom")

        self.assertEqual(core.load_failed(core.get_failed_file(URL)), {"9": "boom"})


class CloudflareDetectionTests(unittest.TestCase):
    def test_error_page_is_not_a_challenge(self):
        page = FakeChallengePage(
            title="comix.to | 522: Connection timed out",
            body="Ray ID: 8f2c1d  •  Origin is unreachable",
        )

        self.assertTrue(core.is_cloudflare_error_page(page))
        self.assertFalse(core.is_cloudflare_active(page))
        self.assertIsNone(core.detect_challenge(page))

    def test_real_interstitial_is_still_a_challenge(self):
        page = FakeChallengePage(
            title="Just a moment...",
            selectors={"#challenge-running": True},
        )

        self.assertFalse(core.is_cloudflare_error_page(page))
        self.assertTrue(core.is_cloudflare_active(page))
        self.assertEqual(core.detect_challenge(page), "cloudflare")

    def test_verifying_marker_detected(self):
        page = FakeChallengePage(
            title="Just a moment...",
            body="Verifying you are human. This may take a few seconds.",
            selectors={"#challenge-running": True},
        )
        self.assertTrue(core.is_cloudflare_verifying(page))

    def test_turnstile_markers_count_as_challenge(self):
        page = FakeChallengePage(title="", body="<div class='cf-turnstile'></div>")
        self.assertTrue(core.is_cloudflare_active(page))

    def test_upload_form_page_with_turnstile_is_not_a_challenge(self):
        # comix's own login/upload Turnstile widget must not look like a block.
        page = FakeChallengePage(
            title="Upload",
            body="<div class='cf-turnstile'></div>",
            selectors={".upage-field": True},
        )
        self.assertFalse(core.is_cloudflare_active(page))

    def test_waf_block_page_is_neither_a_challenge_nor_an_outage(self):
        page = FakeChallengePage(
            title="Attention Required! | Cloudflare",
            body=(
                "Sorry, you have been blocked. You are unable to access "
                "comix.to. Cloudflare Ray ID: 8a1b2c3d4e5f6a7b"
            ),
        )

        self.assertTrue(core.is_cloudflare_access_block(page))
        # It carries a Ray ID, which used to make it read as a dead origin and
        # abort the batch with "comix server appears down".
        self.assertFalse(core.is_cloudflare_error_page(page))
        # It shares the challenge title, so it must not be reported as a gate.
        self.assertFalse(core.is_cloudflare_active(page))
        self.assertIsNone(core.detect_challenge(page))
        self.assertEqual(core.cloudflare_ray_id(page), "8a1b2c3d4e5f6a7b")

    def test_wedged_tab_gives_a_verdict_instead_of_blocking(self):
        page = FakeChallengePage(title="Just a moment...", wedged=True)

        # Every bounded read raises TimeoutError inside; none of these may hang
        # or propagate — each detector just reports "no".
        self.assertFalse(core.page_is_responsive(page))
        self.assertFalse(core.is_cloudflare_active(page))
        self.assertFalse(core.is_cloudflare_error_page(page))
        self.assertFalse(core.is_cloudflare_access_block(page))
        self.assertIsNone(core.cloudflare_ray_id(page))


class CfSolverHookTests(EngineHarness, unittest.TestCase):
    """The engine calls cf_solver_hook before giving up on a challenge."""

    def test_solver_success_retries_the_chapter(self):
        self._chapters(1)
        state = {"first": True}
        calls: list = []

        def fake_upload(**kw):
            if state["first"]:
                state["first"] = False
                raise core.CloudflareBlockException("challenge")
            return True, None

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = lambda *a, **kw: False  # never clears

        summary = self._run(
            cf_solver_hook=lambda context, log: (calls.append(context), True)[1]
        )

        self.assertEqual((summary.processed, summary.succeeded), (1, 1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._history(), {"1"})

    def test_solver_failure_still_aborts_on_stuck(self):
        self._chapters(1)
        control = core.Control()
        statuses: list[tuple] = []

        def fake_upload(**kw):
            raise core.CloudflareBlockException("challenge")

        def fake_wait(*a, **kw):
            control.cf_stuck_event.set()
            return False

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = fake_wait

        summary = self._run(
            control=control,
            cf_solver_hook=lambda context, log: False,
            on_cloudflare_stuck=lambda ch: None,
            on_status=lambda ch, st, d: statuses.append((str(ch), st)),
        )

        self.assertTrue(summary.aborted)
        self.assertEqual(self._history(), set())
        self.assertEqual(self._failed(), {})
        self.assertIn(("1", "aborted"), statuses)


class StuckVerifyingTests(EngineHarness, unittest.TestCase):
    def _challenged_page(self) -> FakeChallengePage:
        return FakeChallengePage(
            title="Just a moment...",
            body="Verifying you are human. This may take a few seconds.",
            selectors={"#challenge-running": True},
        )

    def test_wait_sets_the_stuck_event(self):
        page = self._challenged_page()
        control = core.Control()

        cleared = core.wait_for_challenge_clear(
            page,
            control=control,
            log=self._log,
            max_wait=5,
            poll=0.01,
            settle=2,
            focus=False,
            cf_reload_after=999,
            cf_reload_every=999,
            stuck_grace=0,
        )

        self.assertFalse(cleared)
        self.assertTrue(control.cf_stuck_event.is_set())

    def test_stuck_event_aborts_the_chapter(self):
        self._chapters(1)
        control = core.Control()
        statuses: list[tuple] = []

        def fake_upload(**kw):
            raise core.CloudflareBlockException("challenge")

        def fake_wait(*a, **kw):
            control.cf_stuck_event.set()
            return False

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = fake_wait

        summary = self._run(
            control=control,
            on_status=lambda ch, st, d: statuses.append((str(ch), st)),
            on_cloudflare_stuck=lambda ch: None,
        )

        self.assertTrue(summary.aborted)
        self.assertEqual(self._history(), set())
        self.assertEqual(self._failed(), {})
        self.assertIn(("1", "aborted"), statuses)


class WafRemovedTests(unittest.TestCase):
    def test_no_waf_symbols_remain(self):
        for name in (
            "WAF_PATH",
            "WAF_TITLE",
            "WAF_SELECTORS",
            "WAF_DEBUG_DIR",
            "WAF_CLEARANCE_MARGIN_SECONDS",
            "WafChallengeException",
            "is_waf_challenge",
            "waf_clearance_seconds_left",
            "live_waf_pass_valid",
        ):
            self.assertFalse(hasattr(core, name), f"core.{name} should be gone")
        self.assertFalse(hasattr(parallel, "WAF_WAIT_SLICE_SECONDS"))
        self.assertFalse(hasattr(parallel, "_make_waf_wait"))
        self.assertNotIn("waf", core.CLEARANCE_COOKIE_HINTS)

    def test_no_waf_branches_in_core_source(self):
        src = Path(core.__file__).read_text(encoding="utf-8")
        for needle in (
            "WafChallengeException",
            "is_waf_challenge",
            "waf_pass",
            "/@waf/",
        ):
            self.assertNotIn(needle, src, f"{needle!r} should be gone from core.py")


class CookiePasteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_path = core.CONFIG_PATH
        self._tmp = tempfile.TemporaryDirectory()
        core.CONFIG_PATH = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        core.CONFIG_PATH = self._saved_path
        self._tmp.cleanup()

    def test_updates_in_place_and_appends(self):
        config = {
            "cookies": [
                {"name": "cf_clearance", "value": "old", "url": core.SITE_ROOT}
            ]
        }

        changed = core.set_config_cookies(
            config, {"cf_clearance": "new", "session": "abc"}
        )

        self.assertTrue(changed)
        by_name = {c["name"]: c for c in config["cookies"]}
        self.assertEqual(by_name["cf_clearance"]["value"], "new")
        self.assertEqual(by_name["session"]["value"], "abc")
        self.assertEqual(by_name["session"]["url"], core.SITE_ROOT)
        # Round-trips through the (patched) config file.
        self.assertEqual(core.load_config()["cookies"], config["cookies"])

    def test_blank_values_are_skipped(self):
        config = {
            "cookies": [
                {"name": "cf_clearance", "value": "keep", "url": core.SITE_ROOT}
            ]
        }

        changed = core.set_config_cookies(config, {"cf_clearance": "  ", "session": ""})

        self.assertFalse(changed)
        self.assertEqual(config["cookies"][0]["value"], "keep")

    def test_apply_cookies_to_context(self):
        class Good:
            def __init__(self):
                self.calls = []

            def add_cookies(self, cookies):
                self.calls.append(cookies)

        ctx = Good()
        self.assertTrue(
            core.apply_cookies_to_context(ctx, [{"name": "a", "value": "b"}])
        )
        self.assertEqual(len(ctx.calls), 1)

        class Bad:
            def add_cookies(self, cookies):
                raise RuntimeError("nope")

        self.assertFalse(
            core.apply_cookies_to_context(Bad(), [{"name": "a", "value": "b"}])
        )


class AttachUploadFileTests(unittest.TestCase):
    """core.attach_upload_file must use CDP, and fall back when it cannot.

    The CDP route exists because Playwright's path-based file input takes ~16 s
    on a parallel worker's connect_over_cdp connection (it goes through the
    private Playwright.grantFileReadAccess method) instead of ~0.1 s — which
    randomly exceeded the 30 s action timeout.
    """

    class FakeInput:
        def __init__(self):
            self.waited = False
            self.set_calls: list[str] = []
            self.set_timeouts: list[int] = []

        def wait_for(self, **kwargs):
            self.waited = True

        def set_input_files(self, path, timeout=None):
            self.set_calls.append(path)
            self.set_timeouts.append(timeout)

    class FakeSession:
        def __init__(self, node_id=7, fail_on=None):
            self.node_id = node_id
            self.fail_on = fail_on or ()
            self.sent: list[str] = []
            self.detached = False

        def send(self, method, params=None):
            if method in self.fail_on:
                raise RuntimeError(f"boom: {method}")
            self.sent.append(method)
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": self.node_id}
            return {}

        def detach(self):
            self.detached = True

    def _page(self, session=None, state="accepted", cdp_raises=False):
        """A page whose CDP session is the given fake (or raises on creation).

        ``state`` is what the page reports when asked whether it took the file:
        "accepted", "empty" or "gone" (see core.FILE_PROBE_STATE_JS).
        """
        fallback_session = session if session is not None else self.FakeSession()

        class FakeContext:
            def new_cdp_session(self, page):
                if cdp_raises:
                    raise RuntimeError("no cdp session")
                return fallback_session

        class FakeHandle:
            def evaluate(self, expression, arg=None, timeout=None):
                # The probe-install script returns True; the state query
                # returns the configured state. (Both run through a locator
                # handle, so the call carries a timeout.)
                return (
                    True
                    if "comixFileProbe" in expression and "= 0" in expression
                    else state
                )

        class FakeLocator:
            @property
            def first(self):
                return FakeHandle()

        class FakePage:
            context = FakeContext()

            def locator(self, selector):
                return FakeLocator()

        return FakePage()

    def _attach(self, page, file_input):
        # Keep the poll loop instant in tests.
        with (
            mock.patch.object(core, "FILE_PROBE_POLL_TRIES", 2),
            mock.patch.object(core, "FILE_PROBE_POLL_SECONDS", 0),
        ):
            core.attach_upload_file(page, file_input, "C:/x/1.zip", lambda m: None)

    def test_cdp_path_is_used_and_playwright_is_not(self):
        session = self.FakeSession()
        page = self._page(session=session)
        file_input = self.FakeInput()

        self._attach(page, file_input)

        self.assertIn("DOM.setFileInputFiles", session.sent)
        self.assertTrue(session.detached, "the CDP session must be detached")
        self.assertEqual(file_input.set_calls, [], "Playwright route must not run")
        self.assertTrue(file_input.waited, "should still wait for the input")

    def test_consumed_input_counts_as_accepted(self):
        """comix replaces the drop-zone input once it has the file — that must
        not be mistaken for a failure (it caused a pointless 30 s fallback)."""
        page = self._page(state="gone")
        file_input = self.FakeInput()

        self._attach(page, file_input)

        self.assertEqual(file_input.set_calls, [])

    def test_falls_back_when_no_input_node_found(self):
        page = self._page(session=self.FakeSession(node_id=0))
        file_input = self.FakeInput()

        self._attach(page, file_input)

        self.assertEqual(file_input.set_calls, ["C:/x/1.zip"])

    def test_falls_back_when_the_page_never_took_the_file(self):
        page = self._page(state="empty")
        file_input = self.FakeInput()

        self._attach(page, file_input)

        self.assertEqual(file_input.set_calls, ["C:/x/1.zip"])

    def test_falls_back_when_cdp_session_cannot_be_created(self):
        page = self._page(cdp_raises=True)
        file_input = self.FakeInput()

        self._attach(page, file_input)

        self.assertEqual(file_input.set_calls, ["C:/x/1.zip"])

    def test_falls_back_and_detaches_when_a_cdp_call_fails(self):
        session = self.FakeSession(fail_on=("DOM.setFileInputFiles",))
        page = self._page(session=session)
        file_input = self.FakeInput()

        self._attach(page, file_input)

        self.assertEqual(file_input.set_calls, ["C:/x/1.zip"])
        self.assertTrue(session.detached)


if __name__ == "__main__":
    unittest.main()
