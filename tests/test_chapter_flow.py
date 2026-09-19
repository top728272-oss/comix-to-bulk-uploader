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
import unittest
from pathlib import Path
from unittest import mock

# Allow running this file directly (`python tests/test_chapter_flow.py`) as
# well as through unittest discovery: the engine lives in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402
import parallel  # noqa: E402

URL = "https://comix.to/title/1234-test-series"


class FakePage:
    """The engine only ever passes the page around; nothing is driven here."""

    def __init__(self) -> None:
        self.context = None
        self.url = "about:blank"

    def title(self) -> str:
        return ""


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "BASE_DIR": core.BASE_DIR,
            "WAF_DEBUG_DIR": core.WAF_DEBUG_DIR,
            "scan_folder": core.scan_folder,
            "upload_single_chapter": core.upload_single_chapter,
            "wait_for_challenge_clear": core.wait_for_challenge_clear,
            "run_upload_batch": core.run_upload_batch,
            "_run_parallel": parallel._run_parallel,
        }
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        core.BASE_DIR = tmp
        core.WAF_DEBUG_DIR = tmp / ".waf_debug"
        self.folder = tmp / "chapters"
        self.folder.mkdir()
        self.page = FakePage()
        self.logs: list[str] = []

    def tearDown(self) -> None:
        core.BASE_DIR = self._saved["BASE_DIR"]
        core.WAF_DEBUG_DIR = self._saved["WAF_DEBUG_DIR"]
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

    # -- sequential engine ---------------------------------------------------

    def test_success_records_history_and_summary(self):
        self._chapters(1, 2)
        core.upload_single_chapter = lambda **kw: (True, None)
        statuses: list[tuple] = []

        summary = self._run(on_status=lambda ch, st, d: statuses.append((str(ch), st)))

        self.assertEqual((summary.processed, summary.succeeded, summary.failed), (2, 2, 0))
        self.assertEqual(self._history(), {"1", "2"})
        self.assertIn(("1", "uploading"), statuses)
        self.assertIn(("2", "done"), statuses)

    def test_failure_is_recorded_and_does_not_stop_the_batch(self):
        self._chapters(1, 2)
        core.upload_single_chapter = (
            lambda **kw: (False, "boom") if kw["chapter_num"] == 1 else (True, None)
        )

        summary = self._run()

        self.assertEqual((summary.processed, summary.succeeded, summary.failed), (2, 1, 1))
        self.assertEqual(summary.failures[0][0], 1)
        self.assertEqual(summary.failures[0][2], "boom")
        self.assertEqual(self._failed(), {"1": "boom"})
        self.assertEqual(self._history(), {"2"})

    def test_already_uploaded_chapters_are_skipped(self):
        self._chapters(1, 2)
        core.save_history(core.get_history_file(URL), {"1"})
        seen: list = []
        core.upload_single_chapter = lambda **kw: (seen.append(kw["chapter_num"]), (True, None))[1]

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
        waf_calls: list = []

        def fake_upload(**kw):
            if state["first"]:
                state["first"] = False
                raise core.WafChallengeException("security check")
            return True, None

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = lambda *a, **kw: True

        summary = self._run(on_waf=lambda ch: waf_calls.append(ch))

        self.assertEqual((summary.processed, summary.succeeded), (1, 1))
        self.assertEqual(waf_calls, [1])
        self.assertEqual(self._history(), {"1"})

    def test_gate_expiry_records_nothing(self):
        self._chapters(1)
        statuses: list[tuple] = []

        def fake_upload(**kw):
            raise core.WafChallengeException("security check")

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = lambda *a, **kw: False

        summary = self._run(on_status=lambda ch, st, d: statuses.append((str(ch), st)))

        # "pending" must never be counted or persisted.
        self.assertEqual((summary.processed, summary.succeeded, summary.failed), (0, 0, 0))
        self.assertEqual(self._history(), set())
        self.assertEqual(self._failed(), {})
        self.assertIn(("1", "pending"), statuses)

    def test_repeated_gates_eventually_fail_the_chapter(self):
        self._chapters(1)

        def fake_upload(**kw):
            raise core.WafChallengeException("security check")

        core.upload_single_chapter = fake_upload
        core.wait_for_challenge_clear = lambda *a, **kw: True  # always "cleared"

        summary = self._run(config={"waf": {"max_pauses_per_chapter": 2}})

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

        tail = core.RunSummary(processed=2, succeeded=1, failed=1, failures=[(3, "3.cbz", "x")])
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

        class FakePage:
            context = FakeContext()

            def evaluate(self, expression, arg=None):
                # The probe-install script returns True; the state query
                # returns the configured state.
                return True if "comixFileProbe" in expression and "= 0" in expression else state

        return FakePage()

    def _attach(self, page, file_input):
        # Keep the poll loop instant in tests.
        with mock.patch.object(core, "FILE_PROBE_POLL_TRIES", 2), mock.patch.object(
            core, "FILE_PROBE_POLL_SECONDS", 0
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
