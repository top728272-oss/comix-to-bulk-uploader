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
    AccessBlockedError,
    ConfigStore,
    Control,
    UploadParams,
    VerificationRequired,
    apply_cookies_to_context,
    focus_challenge_window,
    launch_context,
    read_devtools_port,
    resolve_series_name,
    rotate_vpn_egress,
    search_site,
    set_config_cookies,
    sync_cookies_from_context,
)
from parallel import run_upload_batch_auto
import solver

try:
    import urbproxy
except ImportError:
    urbproxy = None  # local-only VPN client; the VPN button hides without it


# How many exit IPs to try before reporting a Cloudflare IP ban. Matches the
# cap urbproxy already uses for its own block-page retries.
SEARCH_MAX_ROTATIONS = 3


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
        # Tracks what the open browser was launched with: parallel uploads need
        # CDP, so it is enabled whenever concurrency > 1.
        self._cdp_on = False
        self.cdp_endpoint: str | None = None
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

        if kind == "set_cookies":
            # Manual cf_clearance paste: persist, reload, and inject into the
            # live browser so the next attempt uses it.
            values = job[1]
            changed = set_config_cookies(self.config.data, values)
            self.config.reload()
            if self.context is not None:
                apply_cookies_to_context(
                    self.context, self.config.data.get("cookies", [])
                )
            self.emit(
                "log",
                (
                    "[✓] Cookies updated and applied."
                    if changed
                    else "[i] Cookies unchanged."
                ),
            )
            return

        if kind == "solve_cf":
            # Auto-solve Cloudflare with the patchright solver. Runs on the
            # session thread, so writing config.json here is allowed.
            self.emit("log", "[i] Solving Cloudflare with the patchright solver…")
            ok = solver.solve_into_config(self.config, self._log)
            if ok:
                if self.context is not None:
                    apply_cookies_to_context(
                        self.context, self.config.data.get("cookies", [])
                    )
                self.emit("log", "[✓] Cloudflare clearance ready.")
            else:
                self.emit("cf_solve_failed", None)
            return

        if kind == "search":
            self._run_search(job[1])
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

        if kind == "vpn_on":
            self._vpn_apply(job[1], True)
            return

        if kind == "vpn_off":
            self._vpn_apply(None, False)
            return

    # -- internals ---------------------------------------------------------

    def _log(self, message: str) -> None:
        self.emit("log", str(message))

    def _run_search(self, query: str) -> None:
        """Search, solving Cloudflare and rotating a banned egress as needed.

        search_site() handles a solvable challenge on its own. An egress IP ban
        is different - no cookie clears it, and the only fix is a different
        exit IP, so the loop here rotates the VPN, relaunches Chrome (the proxy
        is a launch flag) and searches again. Bounded, then it gives up.
        """
        for attempt in range(1, SEARCH_MAX_ROTATIONS + 2):
            self._ensure_browser()
            try:
                results = search_site(
                    self.page,
                    query,
                    log=self._log,
                    cf_solver_hook=solver.make_cf_solver_hook(
                        self.config.data, self._log
                    ),
                )
                self.emit("search_results", results)
                return
            except AccessBlockedError as blocked:
                reason = ""
                if attempt <= SEARCH_MAX_ROTATIONS:
                    self._log(
                        f"[!] {blocked}. Rotating the VPN egress "
                        f"({attempt}/{SEARCH_MAX_ROTATIONS})..."
                    )
                    if rotate_vpn_egress(self._log):
                        # Chrome holds the old egress until it is relaunched.
                        self._close_context()
                        continue
                    reason = (
                        " No alternative exit IP is available - switch the VPN "
                        "off or pick another country."
                    )
                else:
                    reason = (
                        f" Already rotated {SEARCH_MAX_ROTATIONS} time(s) without "
                        "a working exit IP."
                    )
                self._log(f"[!] {blocked}{reason}")
                self.emit("job_blocked", ("search", blocked.kind))
                return
            except VerificationRequired as vreq:
                self._report_blocked_job("search", vreq)
                return

    def _report_blocked_job(self, context: str, vreq: VerificationRequired) -> None:
        """A search or name lookup ran into a verification screen.

        Unlike uploads, these jobs do not wait out the challenge - the tab is
        already sitting on it, so the user solves it in Chrome whenever they
        like and simply retries the action afterwards.
        """
        what = "search" if context == "search" else "name lookup"
        self._log(
            f"[!] The Cloudflare challenge blocked the {what}. Clear it in the "
            f"Chrome window, then retry the {what} later."
        )
        # Nothing else raises the window, so bring it forward here - otherwise
        # the user is told to clear a check they cannot see.
        focus_challenge_window(self.page, self.config.data, self._log)
        self.emit("job_blocked", (context, vreq.kind))

    def _ensure_browser(self) -> None:
        want_cdp = self.config.concurrency > 1
        if self.context is not None:
            if self._cdp_on == want_cdp:
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
            else:
                # CDP is a launch flag, so a changed concurrency needs a relaunch
                # (the profile keeps the login and clearance cookies).
                self._log("[i] Concurrency changed - restarting Chrome to match.")
                self._close_context()

        self.emit("browser", "starting")
        self.context, self.page = launch_context(
            self.playwright,
            self.config.data,
            headless=False,
            enable_cdp=want_cdp,
        )
        self._cdp_on = want_cdp
        self.cdp_endpoint = None
        if want_cdp:
            port = read_devtools_port()
            if port is None:
                self._log(
                    "[!] Chrome's debug port did not appear - parallel uploads "
                    "will fall back to sequential."
                )
            else:
                self.cdp_endpoint = f"http://127.0.0.1:{port}"
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
        self._cdp_on = False
        self.cdp_endpoint = None

    def _vpn_apply(self, country: str | None, want_on: bool) -> None:
        if urbproxy is None:
            self._log("[!] urbproxy.py not found - UrbanVPN routing is unavailable.")
            self.emit("vpn_state", False)
            return
        try:
            if want_on:
                urbproxy.activate(country, log=self._log)
            else:
                urbproxy.deactivate(log=self._log)
        except Exception as exc:
            self._log(f"[!] UrbanVPN toggle failed: {exc}")
        self.emit("vpn_state", urbproxy.is_active())
        if self.context is not None:
            # The proxy is a launch flag, so Chrome must restart to change egress.
            self._log("[i] Restarting Chrome so it uses the new route...")
            self._close_context()
            self._ensure_browser()

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

        def on_cloudflare_stuck(ch):
            self.emit("cloudflare_stuck", str(ch))

        def on_challenge_cleared(kind, ch):
            self.emit("challenge_cleared", (kind, str(ch)))

        try:
            summary = run_upload_batch_auto(
                page=self.page,
                endpoint=self.cdp_endpoint,
                config=self.config.data,
                params=params,
                log=self._log,
                on_status=on_status,
                on_chunk=on_chunk,
                control=self.control,
                on_cloudflare=on_cloudflare,
                cf_resolved_hook=self._cf_resolved,
                on_cloudflare_stuck=on_cloudflare_stuck,
                cf_solver_hook=solver.make_cf_solver_hook(self.config.data, self._log),
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
        # This is an explicit "come solve this" action, so the window should be
        # in front even if the profile last remembered it minimized.
        focus_challenge_window(page, config_store.data, log)
        log("A Chrome window is open. Pass Cloudflare / log in, then continue.")
        input("Press Enter here once Cloudflare is passed...")
        sync_cookies_from_context(context, config_store.data)
        config_store.reload()
        log("[✓] Fresh cookies saved to config.json.")
        context.close()
