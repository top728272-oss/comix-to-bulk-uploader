"""Tests for the chapter-mapping importer (rename.py's chapter_mapping.txt).

Run from the project root:  .venv\\Scripts\\python.exe -m unittest discover tests

Covers parse_chapter_mapping, clean_mapped_title and resolve_chapter_title —
all stdlib-only, no browser needed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Allow running this file directly (`python tests/test_mapping.py`) as well as
# through unittest discovery: the engine lives in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core

REAL_SAMPLE = """Chapter Mapping (new -> original)
----------------------------------
1 -> 001 Prologue
2 -> 002 Chapter 1. About Me
10 -> 010 Chapter 9. Escape from Death
104 -> 104 光子鸡小剧场 - 盔仔之盔
138 -> 138 Chapter 133.5. Review - The Competitive Road
139 -> 139 Adventure Arc 01. Memories Deep Within
140 -> 140 Extra - Big Chicken, Big Luck: The Show Begins (Part 1)
165 -> 165 Transcendent Entity — Phantom of Time and Space
176 -> 176 Adventure Arc 34. Journey to the Future
"""


class ParseChapterMappingTests(unittest.TestCase):
    def test_parses_arrow_lines(self):
        mapping = core.parse_chapter_mapping("1 -> 001 Prologue\n2 -> 002 Hello\n")
        self.assertEqual(mapping, {"1": "001 Prologue", "2": "002 Hello"})

    def test_skips_header_divider_and_junk(self):
        text = (
            "Chapter Mapping (new -> original)\n"
            "----------------------------------\n"
            "\n"
            "not a mapping line\n"
            "-> missing left side\n"
            "3 -> 003 Title\n"
        )
        self.assertEqual(core.parse_chapter_mapping(text), {"3": "003 Title"})

    def test_zero_padded_numbers_key_like_plain_ones(self):
        mapping = core.parse_chapter_mapping("007 -> 007 Title\n")
        self.assertIn("7", mapping)

    def test_decimal_keys_survive(self):
        # parse_chapter_number would eat ".5" via Path.stem; the mapping
        # parser must not.
        mapping = core.parse_chapter_mapping("117.5 -> 117.5 Bonus\n")
        self.assertIn("117.5", mapping)

    def test_first_mapping_wins_on_duplicates(self):
        mapping = core.parse_chapter_mapping("2 -> 002 First\n2 -> 002 Second\n")
        self.assertEqual(mapping["2"], "002 First")

    def test_empty_text_gives_empty_mapping(self):
        self.assertEqual(core.parse_chapter_mapping(""), {})
        self.assertEqual(core.parse_chapter_mapping("no arrows here\n"), {})


class CleanMappedTitleTests(unittest.TestCase):
    def test_strips_counter_and_chinese_chapter_token(self):
        self.assertEqual(core.clean_mapped_title("002 1话.倒霉体质"), "倒霉体质")

    def test_strips_counter_and_english_chapter_token(self):
        self.assertEqual(
            core.clean_mapped_title("15 Chapter 14 - The Captain"), "The Captain"
        )

    def test_strips_ch_token_and_decimal_number(self):
        self.assertEqual(
            core.clean_mapped_title("138 Chapter 133.5. Review - The Road"),
            "Review - The Road",
        )

    def test_strips_dijihua_token(self):
        self.assertEqual(core.clean_mapped_title("007 第3话: 开端"), "开端")

    def test_keeps_arc_prefixes(self):
        self.assertEqual(
            core.clean_mapped_title("139 Adventure Arc 01. Memories Deep Within"),
            "Adventure Arc 01. Memories Deep Within",
        )

    def test_keeps_unnumbered_names(self):
        self.assertEqual(
            core.clean_mapped_title("142 Taking Sick Leave This Week"),
            "Taking Sick Leave This Week",
        )

    def test_strip_off_keeps_raw_text(self):
        raw = "002 Chapter 1. About Me"
        self.assertEqual(core.clean_mapped_title(raw, strip_numbers=False), raw)


class ResolveChapterTitleTests(unittest.TestCase):
    def test_mapping_hit_wins_over_pattern(self):
        params = core.UploadParams(
            title_pattern="Chapter {ch}",
            title_map={"2": "002 About Me"},
        )
        self.assertEqual(core.resolve_chapter_title(params, 2), "About Me")

    def test_mapping_miss_falls_back_to_pattern(self):
        params = core.UploadParams(
            title_pattern="Chapter {ch}",
            title_map={"2": "002 About Me"},
        )
        self.assertEqual(core.resolve_chapter_title(params, 999), "Chapter 999")

    def test_no_map_no_pattern_gives_empty(self):
        self.assertEqual(core.resolve_chapter_title(core.UploadParams(), 5), "")

    def test_pattern_still_works_without_a_map(self):
        params = core.UploadParams(title_pattern="Ch. {ch}")
        self.assertEqual(core.resolve_chapter_title(params, 3), "Ch. 3")

    def test_int_and_float_chapter_numbers_hit_the_same_key(self):
        params = core.UploadParams(title_map={"2": "002 About Me"})
        self.assertEqual(
            core.resolve_chapter_title(params, 2),
            core.resolve_chapter_title(params, 2.0),
        )

    def test_strip_toggle_changes_resolution(self):
        params = core.UploadParams(
            title_map={"2": "002 Chapter 1. About Me"},
            strip_map_numbers=False,
        )
        self.assertEqual(
            core.resolve_chapter_title(params, 2), "002 Chapter 1. About Me"
        )

    def test_end_to_end_with_rename_script_output(self):
        mapping = core.parse_chapter_mapping(REAL_SAMPLE)
        params = core.UploadParams(title_pattern="Chapter {ch}", title_map=mapping)
        expected = {
            1: "Prologue",
            2: "About Me",
            10: "Escape from Death",
            104: "光子鸡小剧场 - 盔仔之盔",
            138: "Review - The Competitive Road",
            139: "Adventure Arc 01. Memories Deep Within",
            140: "Extra - Big Chicken, Big Luck: The Show Begins (Part 1)",
            165: "Transcendent Entity — Phantom of Time and Space",
            176: "Adventure Arc 34. Journey to the Future",
        }
        for ch, title in expected.items():
            with self.subTest(chapter=ch):
                self.assertEqual(core.resolve_chapter_title(params, ch), title)
        self.assertEqual(core.resolve_chapter_title(params, 999), "Chapter 999")


if __name__ == "__main__":
    unittest.main()
