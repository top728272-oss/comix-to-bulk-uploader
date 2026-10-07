"""Tests for core.install_popup_handler - no browser, no network.

Pins down popup/worker-tab discrimination: every CDP connection sees every tab,
so the handler must close only tabs WITH an opener (ad popups) and never the
opener-less tabs parallel workers create.

Run: .venv\\Scripts\\python.exe -m unittest discover tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Allow running this file directly (`python tests/test_popup_handler.py`) as
# well as through unittest discovery: the engine lives in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core


class FakePage:
    """Just enough surface for the popup handler."""

    def __init__(self, url: str = "about:blank", opener=None) -> None:
        self.url = url
        self._opener = opener
        self.closed = False
        self.brought_to_front = 0

    def opener(self):
        return self._opener

    def close(self) -> None:
        self.closed = True

    def bring_to_front(self) -> None:
        self.brought_to_front += 1


class FakeContext:
    """Captures the handler registered via context.on("page", ...)."""

    def __init__(self) -> None:
        self.handler = None

    def on(self, event: str, handler) -> None:
        assert event == "page"
        self.handler = handler


def make_handler(config: dict | None = None):
    """Returns (main_page, fire) where fire(new_page) runs the handler."""
    context = FakeContext()
    main_page = FakePage(url="https://comix.to/user/upload/abc")
    core.install_popup_handler(context, main_page, config or {})
    return main_page, context.handler


class PopupHandlerTestCase(unittest.TestCase):
    def test_ad_popup_with_opener_is_closed(self):
        main, fire = make_handler()
        popup = FakePage(url="https://ads.example.com/x", opener=main)
        fire(popup)
        self.assertTrue(popup.closed)

    def test_worker_tab_without_opener_survives(self):
        main, fire = make_handler()
        worker_tab = FakePage(url="about:blank", opener=None)
        fire(worker_tab)
        self.assertFalse(worker_tab.closed)

    def test_worker_tab_on_upload_url_survives(self):
        # A tab the worker already navigated still has no opener.
        main, fire = make_handler()
        worker_tab = FakePage(url="https://comix.to/user/upload/abc", opener=None)
        fire(worker_tab)
        self.assertFalse(worker_tab.closed)

    def test_popup_with_opener_is_closed_regardless_of_url(self):
        # The WAF tab exemption is gone with the WAF itself: any page with an
        # opener is treated as a popup.
        main, fire = make_handler()
        tab = FakePage(url="https://comix.to/@waf/123", opener=main)
        fire(tab)
        self.assertTrue(tab.closed)

    def test_main_page_is_never_closed(self):
        main, fire = make_handler()
        fire(main)
        self.assertFalse(main.closed)

    def test_no_focus_steal_when_ignoring_worker_tab(self):
        # keep_window_in_background=False normally raises the main page after
        # closing a popup; ignoring a worker tab must not raise anything.
        main, fire = make_handler({"keep_window_in_background": False})
        fire(FakePage(url="about:blank", opener=None))
        self.assertEqual(main.brought_to_front, 0)
        fire(FakePage(url="https://ads.example.com/x", opener=main))
        self.assertEqual(main.brought_to_front, 1)

    def test_noopener_ad_popup_survives_documented_tradeoff(self):
        # Popups opened with rel=noopener have no opener: closing them too
        # would reintroduce the ambiguity that kills parallel workers.
        main, fire = make_handler()
        popup = FakePage(url="https://ads.example.com/x", opener=None)
        fire(popup)
        self.assertFalse(popup.closed)


if __name__ == "__main__":
    unittest.main()
