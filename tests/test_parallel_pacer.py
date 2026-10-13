"""Behaviour tests for the parallel upload pacer - no browser, no network.

Run from the project root:  .venv\\Scripts\\python.exe -m unittest discover tests
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parallel
from core import Control
import core


class VpnTunnelLimitTests(unittest.TestCase):
    """Parallel tabs cannot authenticate through the UrbanVPN gateway.

    Workers are driven over connect_over_cdp, which takes no proxy argument, so
    those connections carry no credentials and the gateway's 407 handshake
    stalls every worker tab on about:blank. Not a tunnel limit: four concurrent
    CONNECT tunnels on one token all succeed at the socket level.
    """

    def setUp(self) -> None:
        self._saved = core.urbproxy

    def tearDown(self) -> None:
        core.urbproxy = self._saved

    @staticmethod
    def _fake(active: bool):
        class Fake:
            @staticmethod
            def is_active():
                return active

        return Fake

    def test_gateway_forces_a_single_worker(self):
        core.urbproxy = self._fake(True)
        self.assertEqual(core.effective_concurrency({"concurrency": 5}), 1)

    def test_without_the_gateway_the_configured_count_stands(self):
        core.urbproxy = self._fake(False)
        self.assertEqual(core.effective_concurrency({"concurrency": 5}), 5)

    def test_a_plain_config_proxy_is_not_restricted(self):
        # config.proxy is an ordinary proxy, not the single-tunnel gateway.
        core.urbproxy = self._fake(False)
        cfg = {"concurrency": 4, "proxy": {"server": "http://p:3128"}}
        self.assertEqual(core.effective_concurrency(cfg), 4)

    def test_absent_module_keeps_the_configured_count(self):
        core.urbproxy = None
        self.assertEqual(core.effective_concurrency({"concurrency": 3}), 3)

    def test_invalid_concurrency_falls_back_to_one(self):
        core.urbproxy = self._fake(False)
        self.assertEqual(core.effective_concurrency({"concurrency": "nonsense"}), 1)

    def test_engine_choice_is_sequential_under_the_gateway(self):
        core.urbproxy = self._fake(True)
        logs: list = []
        calls: list = []

        def fake_sequential(page=None, config=None, params=None, **kw):
            calls.append("sequential")
            return core.RunSummary()

        saved = core.run_upload_batch
        core.run_upload_batch = fake_sequential
        try:
            parallel.run_upload_batch_auto(
                page=None,
                endpoint="http://127.0.0.1:1",  # would be used if parallel
                config={"concurrency": 5},
                params=None,
                log=logs.append,
                control=Control(),
            )
        finally:
            core.run_upload_batch = saved

        self.assertEqual(calls, ["sequential"])
        self.assertTrue(any("cannot authenticate" in m for m in logs))


class UploadPacerTests(unittest.TestCase):
    def test_starts_are_spaced_across_threads(self) -> None:
        pacer = parallel.UploadPacer(0.2)
        control = Control()
        stamps: list = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(3):
                self.assertTrue(pacer.wait(control))
                with lock:
                    stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stamps.sort()
        self.assertEqual(len(stamps), 9)
        for before, after in zip(stamps, stamps[1:]):
            self.assertGreaterEqual(after - before, 0.18)

    def test_stop_aborts_wait(self) -> None:
        pacer = parallel.UploadPacer(60)
        control = Control()
        control.request_stop()
        self.assertFalse(pacer.wait(control))

    def test_zero_delay_passes_through(self) -> None:
        pacer = parallel.UploadPacer(0)
        self.assertTrue(pacer.wait(Control()))


if __name__ == "__main__":
    unittest.main()
