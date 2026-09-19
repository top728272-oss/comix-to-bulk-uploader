"""Tests for where upload history is stored (and how old files get adopted).

Run from the project root:  .venv\\Scripts\\python.exe -m unittest discover tests

History moved from ".upload_history_<hid>.json" in the project root into a
gitignored ".history/" folder. The migration matters: those files are the record
of what has already been uploaded, so losing them would re-upload everything.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402

URL = "https://comix.to/title/1234-test-series"
HID = "1234-test-series"


class HistoryLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_base = core.BASE_DIR
        self._orig_checked = core._legacy_history_checked
        self._tmp = tempfile.TemporaryDirectory()
        core.BASE_DIR = Path(self._tmp.name)
        core._legacy_history_checked = False

    def tearDown(self) -> None:
        core.BASE_DIR = self._orig_base
        core._legacy_history_checked = self._orig_checked
        self._tmp.cleanup()

    def _legacy(self, name: str, payload) -> Path:
        path = core.BASE_DIR / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    # -- layout --------------------------------------------------------------

    def test_paths_live_inside_the_history_folder(self):
        history = core.get_history_file(URL)
        failed = core.get_failed_file(URL)

        self.assertEqual(history.parent, core.BASE_DIR / ".history")
        self.assertEqual(history.name, f"upload_history_{HID}.json")
        self.assertEqual(failed.parent, core.BASE_DIR / ".history")
        self.assertEqual(failed.name, f"upload_failed_{HID}.json")

    def test_folder_is_created_on_demand(self):
        self.assertFalse((core.BASE_DIR / ".history").exists())

        core.get_history_file(URL)

        self.assertTrue((core.BASE_DIR / ".history").is_dir())

    def test_history_dir_follows_base_dir(self):
        """The folder must be derived per call, not frozen at import time."""
        self.assertEqual(core.history_dir(), core.BASE_DIR / ".history")
        with tempfile.TemporaryDirectory() as other:
            core.BASE_DIR = Path(other)
            self.assertEqual(core.history_dir(), Path(other) / ".history")

    # -- migration -----------------------------------------------------------

    def test_legacy_files_are_moved_into_the_folder(self):
        old_history = self._legacy(".upload_history_abc.json", ["1", "2"])
        old_failed = self._legacy(".upload_failed_abc.json", {"3": "boom"})

        moved = core.migrate_legacy_history()

        self.assertEqual(len(moved), 2)
        self.assertFalse(old_history.exists(), "legacy history should be moved")
        self.assertFalse(old_failed.exists(), "legacy failed log should be moved")
        self.assertEqual(
            json.loads((core.BASE_DIR / ".history" / "upload_history_abc.json").read_text()),
            ["1", "2"],
        )
        self.assertEqual(
            json.loads((core.BASE_DIR / ".history" / "upload_failed_abc.json").read_text()),
            {"3": "boom"},
        )

    def test_migration_never_overwrites_a_newer_file(self):
        self._legacy(".upload_history_abc.json", ["old"])
        target = core.BASE_DIR / ".history" / "upload_history_abc.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(["new"]), encoding="utf-8")

        core.migrate_legacy_history()

        self.assertEqual(json.loads(target.read_text()), ["new"])

    def test_getters_trigger_the_sweep(self):
        self._legacy(".upload_history_abc.json", ["7"])

        core.get_history_file(URL)  # first call in the process adopts it

        self.assertFalse((core.BASE_DIR / ".upload_history_abc.json").exists())
        self.assertTrue((core.BASE_DIR / ".history" / "upload_history_abc.json").exists())

    def test_migration_is_harmless_when_there_is_nothing_to_move(self):
        self.assertEqual(core.migrate_legacy_history(), [])
        self.assertTrue((core.BASE_DIR / ".history").is_dir())

    # -- round trip ----------------------------------------------------------

    def test_save_and_load_round_trip(self):
        core.save_history(core.get_history_file(URL), {"1", "2.5", "3"})
        core.save_failed(core.get_failed_file(URL), {"4": "timeout"})

        self.assertEqual(core.load_history(core.get_history_file(URL)), {"1", "2.5", "3"})
        self.assertEqual(core.load_failed(core.get_failed_file(URL)), {"4": "timeout"})

    def test_reset_history_clears_both_files(self):
        core.save_history(core.get_history_file(URL), {"1"})
        core.save_failed(core.get_failed_file(URL), {"2": "x"})

        core.reset_history(URL)

        self.assertFalse(core.get_history_file(URL).exists())
        self.assertFalse(core.get_failed_file(URL).exists())
        self.assertEqual(core.load_history(core.get_history_file(URL)), set())

    def test_other_series_are_untouched_by_migration(self):
        self._legacy(".upload_history_abc.json", ["1"])
        self._legacy(".upload_history_xyz.json", ["2"])

        core.migrate_legacy_history()

        self.assertTrue((core.BASE_DIR / ".history" / "upload_history_abc.json").exists())
        self.assertTrue((core.BASE_DIR / ".history" / "upload_history_xyz.json").exists())


if __name__ == "__main__":
    unittest.main()
