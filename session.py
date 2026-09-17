"""Background browser session.

Owns the single persistent Chrome profile and executes jobs (search, resolve,
upload batch) on a worker thread so the GUI stays responsive.
"""

from __future__ import annotations

import queue
import threading
import traceback

from playwright.sync_api import sync_playwright

from core import (
    ConfigStore,
    Control,
    UploadParams,
    VerificationRequired,
    launch_context,
    resolve_series_name,
    run_upload_batch,
    search_site,
    sync_cookies_from_context,
)


class BrowserSession(threading.Thread):
    """Worker thread owning one Playwright persistent context."""

    def __init__(self, config_store: ConfigStore, emit):
        super().__init__(daemon=True)
        self.config = config_store
        self.emit = emit  # emit(event: str, payload) -> must be thread-safe
        self.jobs: queue.Queue[tuple] = queue.Queue()
        self.control = Control()

        self.playwright = None
        self.context = None
        self.page = None
        self._running = True

    # -- public API (called from the GUI thread) ---------------------------

    def submit(self, job: tuple) -> None:
        kind = job[0]
        # Control signals must work *while* an upload is blocking the worker,
        # so they are applied straight away instead of going through the queue.
        if kind == "pause":
            if job[1]:
                self.control.pause_event.set()
                self.emit("run_state", "paused")
            else:
                self.control.pause_event.clear()
                self.emit("run_state", "running")
            return
        if kind == "stop":
            self.control.request_stop()
            self.emit("run_state", "stopping")
            return
        if kind == "resume_cf":
            self.control.cf_event.set()
            return
        if kind == "restart_cf":
            self.control.restart_browser = True
            self.control.cf_event.set()
            return
        if kind == "resume_waf":
            # Only nudges the wait loop; the browser is deliberately left alone.
            self.control.waf_event.set()
            return
        self.jobs.put(job)

    def shutdown(self) -> None:
        self._running = False
        self.control.request_stop()
        self.jobs.put(("quit",))

    @property
    def browser_open(self) -> bool:
        return self.context is not None

    # -- worker loop -------------------------------------------------------

    def run(self) -> None:
        with sync_playwright() as p:
            self.playwright = p
            while self._running:
                job = self.jobs.get()
                kind = job[0]
                try:
                    self._run_job(kind, job)
                except Exception:
                    self.emit("log", "[!] Worker error:\n" + traceback.format_exc())
                if kind == "quit":
                    break
            self._close_context()

    def _run_job(self, kind, job) -> None:
        if kind == "quit":
            return

        if kind == "open":
            self._ensure_browser()
            self.emit("browser", "ready")
            return

        if kind == "close":
            self._close_context()
            self.emit("browser", "closed")
            return

        if kind == "restart":
            self._close_context()
            self._ensure_browser()
            self.emit("log", "[✓] Browser restarted with a fresh session.")
            self.emit("browser", "ready")
            return

        if kind == "save_cookies":
            if self.context:
                if sync_cookies_from_context(self.context, self.config.data):
                    self.emit("log", "[✓] Cookies saved to config.json.")
                else:
                    self.emit("log", "[i] Cookies unchanged.")
            return

        if kind == "search":
            self._ensure_browser()
            query = job[1]
            try:
                results = search_site(self.page, query, log=self._log)
            except VerificationRequired as vreq:
                self._report_blocked_job("search", vreq)
                return
            self.emit("search_results", results)
            return

        if kind == "resolve":
            self._ensure_browser()
            hid = job[1]
            try:
                name = resolve_series_name(self.page, hid, log=self._log)
            except VerificationRequired as vreq:
                self._report_blocked_job("resolve", vreq)
                return
            self.emit("series_name", (hid, name))
            return

        if kind == "start":
            self._ensure_browser()
            self._run_upload(job[1])
            return

        if kind == "goto":
            self._ensure_browser()
            self.page.goto(job[1], wait_until="domcontentloaded", timeout=60000)
            return

    # -- internals ---------------------------------------------------------

    def _log(self, message: str) -> None:
        self.emit("log", str(message))

    def _report_blocked_job(self, context: str, vreq: VerificationRequired) -> None:
        """A search or name lookup ran into a verification screen.

        Unlike uploads, these jobs do not wait out the challenge — the tab is
        already sitting on it, so the user solves it in Chrome whenever they
        like and simply retries the action afterwards.
        """
        what = "search" if context == "search" else "name lookup"
        label = (
            "Cloudflare challenge" if vreq.kind == "cloudflare" else "security check"
        )
        self._log(
            f"[!] The {label} blocked the {what}. Solve it in the Chrome "
            f"window, then retry the {what} later."
        )
        self.emit("job_blocked", (context, vreq.kind))

    def _ensure_browser(self) -> None:
        if self.context is not None:
            try:
                if self.page is None or self.page.is_closed():
                    self.page = (
                        self.context.pages[0]
                        if self.context.pages
                        else self.context.new_page()
                    )
                return
            except Exception:
                self.context = None

        self.emit("browser", "starting")
        self.context, self.page = launch_context(
            self.playwright, self.config.data, headless=False
        )
        self.emit("browser", "ready")

    def _close_context(self) -> None:
        if self.context is not None:
            try:
                sync_cookies_from_context(self.context, self.config.data)
            except Exception:
                pass
            try:
                self.context.close()
            except Exception:
                pass
        self.context = None
        self.page = None

    def _cf_resolved(self, restart: bool):
        """Called by the upload runner once Cloudflare has cleared."""
        if restart:
            self._close_context()
            self._ensure_browser()
            self.emit("log", "[✓] Browser restarted with a fresh session.")
            self.emit("browser", "ready")
        else:
            if self.context is not None:
                sync_cookies_from_context(self.context, self.config.data)
        return self._ensure_page()

    def _waf_resolved(self):
        """Called once the user solves the security check.

        Deliberately does NOT restart the browser: the clearance is tied to the
        session we already have open, and relaunching would discard the puzzle
        mid-solve. Just persist whatever cookies the challenge handed us.
        """
        if self.context is not None:
            try:
                sync_cookies_from_context(self.context, self.config.data)
            except Exception:
                pass
        return self._ensure_page()

    def _ensure_page(self):
        """Keep the tracked page handle alive.

        The challenge hijack is free to move the tab around (or close it); the
        engine's retry needs a live page to navigate, not a handle to a ghost.
        """
        if self.context is not None:
            try:
                if self.page is None or self.page.is_closed():
                    self.page = (
                        self.context.pages[0]
                        if self.context.pages
                        else self.context.new_page()
                    )
            except Exception:
                pass
        return self.page

    def _run_upload(self, params: UploadParams) -> None:
        self.control.reset()
        self.emit("run_state", "running")

        def on_status(ch, status, detail):
            self.emit("chapter_status", (str(ch), status, detail))

        def on_chunk(ch, count):
            self.emit("chunk", (str(ch), count))

        def on_cloudflare(ch):
            self.emit("cloudflare", str(ch))

        def on_waf(ch):
            self.emit("waf", str(ch))

        def on_challenge_cleared(kind, ch):
            self.emit("challenge_cleared", (kind, str(ch)))

        try:
            summary = run_upload_batch(
                page=self.page,
                config=self.config.data,
                params=params,
                log=self._log,
                on_status=on_status,
                on_chunk=on_chunk,
                control=self.control,
                on_cloudflare=on_cloudflare,
                cf_resolved_hook=self._cf_resolved,
                on_waf=on_waf,
                waf_resolved_hook=self._waf_resolved,
                on_challenge_cleared=on_challenge_cleared,
                max_retries=self.config.max_retries,
            )
            self.emit("summary", summary)
        except Exception:
            self.emit("log", "[!] Upload crashed:\n" + traceback.format_exc())
        finally:
            if self.context is not None:
                sync_cookies_from_context(self.context, self.config.data)
            self.emit("run_state", "idle")


def open_browser_for_clearance(config_store: ConfigStore, log=print) -> None:
    """Standalone helper (CLI + GUI 'Refresh clearance' button)."""
    with sync_playwright() as p:
        context, page = launch_context(p, config_store.data, headless=False)
        page.goto("https://comix.to", wait_until="domcontentloaded")
        log("A Chrome window is open. Pass Cloudflare / log in, then continue.")
        input("Press Enter here once Cloudflare is passed...")
        sync_cookies_from_context(context, config_store.data)
        config_store.reload()
        log("[✓] Fresh cookies saved to config.json.")
        context.close()
