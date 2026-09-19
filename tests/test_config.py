"""Tests for config validation (the "concurrency too large" error path).

Run from the project root:  .venv\\Scripts\\python.exe -m unittest discover tests

core.py is stdlib-only by design, so this needs no Playwright and no browser.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Allow running this file directly (`python tests/test_config.py`) as well as
# through unittest discovery: the engine lives in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402


class ConcurrencyValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_path = core.CONFIG_PATH
        self._tmp = tempfile.TemporaryDirectory()
        core.CONFIG_PATH = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        core.CONFIG_PATH = self._orig_path
        self._tmp.cleanup()

    def _write_config(self, data: dict) -> None:
        core.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    # -- validate_concurrency ------------------------------------------------

    def test_accepts_the_allowed_range(self):
        for value in (1, 2, 5, 8):
            self.assertEqual(core.validate_concurrency(value), value)

    def test_rejects_out_of_range(self):
        for value in (0, -1, core.MAX_CONCURRENCY + 1, 99):
            with self.assertRaises(core.ConfigError):
                core.validate_concurrency(value)

    def test_rejects_non_integers(self):
        # "5" is the classic JSON/typo mistake; True is the sneaky one, since
        # isinstance(True, int) is True in Python.
        for value in ("5", 5.0, True, False, None, [5], {"v": 5}):
            with self.assertRaises(core.ConfigError):
                core.validate_concurrency(value)

    def test_error_message_names_value_range_and_file(self):
        with self.assertRaises(core.ConfigError) as ctx:
            core.validate_concurrency(9)
        message = str(ctx.exception)
        self.assertIn("9", message)
        self.assertIn("1 to 8", message)
        self.assertIn(str(core.CONFIG_PATH), message)

    # -- load_config ---------------------------------------------------------

    def test_load_config_accepts_missing_key(self):
        self._write_config({"max_retries": 3})
        self.assertNotIn("concurrency", core.load_config())

    def test_load_config_rejects_too_large(self):
        self._write_config({"concurrency": 9})
        with self.assertRaises(core.ConfigError):
            core.load_config()

    def test_load_config_rejects_bool(self):
        self._write_config({"concurrency": True})
        with self.assertRaises(core.ConfigError):
            core.load_config()

    def test_load_config_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            core.load_config()

    # -- ConfigStore ---------------------------------------------------------

    def test_default_when_key_absent(self):
        self._write_config({})
        self.assertEqual(core.ConfigStore().concurrency, core.DEFAULT_CONCURRENCY)

    def test_value_from_config(self):
        self._write_config({"concurrency": 3})
        self.assertEqual(core.ConfigStore().concurrency, 3)

    def test_set_validates_before_saving(self):
        self._write_config({})
        store = core.ConfigStore()
        with self.assertRaises(core.ConfigError):
            store.set("concurrency", 99)
        # Nothing was written, and the in-memory value is untouched.
        self.assertNotIn("concurrency", json.loads(core.CONFIG_PATH.read_text("utf-8")))
        self.assertEqual(store.concurrency, core.DEFAULT_CONCURRENCY)

    def test_set_persists_a_good_value(self):
        self._write_config({})
        store = core.ConfigStore()
        store.set("concurrency", 2)
        self.assertEqual(json.loads(core.CONFIG_PATH.read_text("utf-8"))["concurrency"], 2)
        self.assertEqual(core.ConfigStore().concurrency, 2)


if __name__ == "__main__":
    unittest.main()
