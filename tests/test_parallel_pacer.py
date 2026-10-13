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


class _UnusedVpnScaffold(unittest.TestCase):
    """Placeholder kept out of collection."""

    def test_noop(self) -> None:
        pass


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
