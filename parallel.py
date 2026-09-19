"""Parallel chapter uploads on tabs of the one persistent Chrome.

How it works: the session already owns the persistent Chrome (launched with
``core.launch_context(enable_cdp=True)``). Each worker thread starts its own
Playwright instance and attaches to that same browser through the DevTools
endpoint (``core.read_devtools_port()``), then drives its own tab. Because the
workers share the browser, they share the profile, the login and the waf_pass
clearance cookie — one manual captcha solve still covers the whole batch.

Verified with a spike before this module existed:
  * ``--remote-debugging-port=0`` coexists with Playwright's internal
    ``--remote-debugging-pipe``, and the ``DevToolsActivePort`` file appears.
  * ``connect_over_cdp`` reaches the persistent default context.
  * Cookies are shared across connections. Pages are NOT visible across
    connections — each worker polls its own tab, which is all it needs
    (``_live_site_pages`` reads ``page.context.pages``, i.e. its own view).
  * Disconnecting a worker (page.close + pw.stop) leaves the browser and the
    other workers untouched.
  * Playwright's sync API is thread-affine: every playwright object is driven
    only from the thread that created it. The coordinator exchanges plain data
    with workers and never touches their pages.

Anything that goes wrong here degrades to the sequential engine with a clear
log line — parallel uploads must never be able to break a run.
"""

from __future__ import annotations

import queue
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from playwright.sync_api import sync_playwright

import core
from core import (
    DEFAULT_CONCURRENCY,
    ChapterContext,
    Control,
    HistoryRecorder,
    RunSummary,
    STEALTH_INIT_SCRIPT,
    build_chapter_context,
    install_popup_handler,
    live_waf_pass_valid,
    pending_chapters,
    wait_for_challenge_clear,
    _process_chapter,
)

# The parallel WAF wait runs in slices so it can also notice a clearance that
# was minted in a different tab (see _make_waf_wait).
WAF_WAIT_SLICE_SECONDS = 30.0


def run_upload_batch_auto(page, endpoint, config: dict, params, **kwargs) -> RunSummary:
    """Upload the batch, in parallel when possible, sequentially otherwise.

    ``endpoint`` is the DevTools URL of the shared Chrome (None when the
    browser was not launched with CDP enabled). Never raises: any problem in
    the parallel path falls back to the sequential engine.
    """
    log = kwargs.get("log") or print
    control = kwargs.get("control") or Control()
    # Same rule as the sequential engine: routine automation never raises the
    # window (config keep_window_in_background).
    control.raise_window = not bool(config.get("keep_window_in_background", True))

    try:
        n = core.validate_concurrency(config.get("concurrency", DEFAULT_CONCURRENCY))
    except core.ConfigError as ex:
        # load_config() rejects this at startup, so this only guards callers
        # that hand-build a config dict. Say exactly what is wrong, then run
        # sequentially rather than refusing to work at all.
        log(f"[!] {ex}")
        n = 1

    if n <= 1:
        return core.run_upload_batch(page=page, config=config, params=params, **kwargs)

    if not endpoint:
        log(
            "[!] Parallel uploads unavailable (no DevTools endpoint) — "
            "running sequentially."
        )
        return core.run_upload_batch(page=page, config=config, params=params, **kwargs)

    summary = RunSummary()
    try:
        stragglers, worker_failed = _run_parallel(
            endpoint, config, params, n, summary, **kwargs
        )
    except Exception:
        log(
            "[!] Parallel mode failed — finishing the batch sequentially.\n"
            + traceback.format_exc()
        )
        worker_failed, stragglers = True, []

    if (worker_failed or stragglers) and not control.stopped:
        if stragglers:
            log(
                f"[i] {len(stragglers)} chapter(s) still pending — "
                "finishing them sequentially."
            )
        tail = core.run_upload_batch(page=page, config=config, params=params, **kwargs)
        summary.processed += tail.processed
        summary.succeeded += tail.succeeded
        summary.failed += tail.failed
        summary.failures.extend(tail.failures)

    return summary


def _make_waf_wait(context, url: str, max_wait: int, poll: float, focus: bool):
    """Sliced WAF wait for parallel workers.

    Besides the normal signal ("the challenge page navigated away"), this also
    watches the LIVE waf_pass cookie. When several tabs are challenged at once,
    the user only solves one of them — the other tabs' challenge pages never
    learn about it and would sit there until the wait window expired. A fresh
    valid cookie means the clearance does exist, so the stale tab is simply
    re-opened and the chapter retried.
    """

    def waf_wait(page, control, log) -> bool:
        deadline = time.time() + max_wait
        first = True
        while time.time() < deadline:
            if control.stopped:
                return False
            slice_s = min(WAF_WAIT_SLICE_SECONDS, max(1.0, deadline - time.time()))
            cleared = wait_for_challenge_clear(
                page,
                control=control,
                kind="waf",
                log=log,
                max_wait=slice_s,
                poll=poll,
                # Only the first slice may pull the window forward; after that
                # the user has been told and may be busy elsewhere.
                focus=focus and first,
            )
            first = False
            if cleared:
                return True
            if control.stopped:
                return False
            if live_waf_pass_valid(context):
                log(
                    "[i] Security check solved in another tab — "
                    "re-opening this one."
                )
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception as ex:
                    log(f"[!] Could not re-open the upload page: {ex}")
                    return False
                return True
        return False

    return waf_wait


def _sleep_between_chapters(delay, control: Control) -> None:
    """Per-worker pacing, same intent as the sequential inter-chapter delay."""
    for _ in range(int(delay)):
        if control.stopped:
            return
        time.sleep(1)


def _run_parallel(
    endpoint: str, config: dict, params, n: int, summary: RunSummary, **kwargs
):
    """Run the batch on ``n`` worker tabs. Returns (stragglers, worker_failed).

    ``stragglers`` are chapter numbers that were left "pending" (a gate
    outlasted its window, or the user stopped); ``worker_failed`` means a
    worker could not do its job at all. Either way the caller gives what is
    left to the sequential engine, which re-scans and skips finished chapters.
    """
    log = kwargs.get("log") or print
    control = kwargs.get("control") or Control()
    recorder = HistoryRecorder(params.url)

    pending = pending_chapters(params, recorder)
    if not pending:
        log(f"[i] {len(pending)} chapter(s) queued.")
        return [], False
    log(f"[i] {len(pending)} chapter(s) queued, up to {n} at a time.")

    work: queue.Queue = queue.Queue()
    for item in pending:
        work.put(item)

    waf_cfg = config.get("waf") or {}
    waf_max_wait = int(waf_cfg.get("max_wait_seconds", 600))
    waf_poll = float(waf_cfg.get("poll_interval_seconds", 2))
    focus = bool(config.get("focus_on_challenge", True))

    # Worker contexts: no restart (it would kill the other workers' uploads)
    # and no hooks (they hand back the session's page — never a worker's).
    shared = {
        k: v
        for k, v in kwargs.items()
        if k in ("on_status", "on_chunk", "on_cloudflare", "on_waf",
                 "on_challenge_cleared", "max_retries", "idle_timeout")
    }

    def make_ctx(context) -> ChapterContext:
        return build_chapter_context(
            config,
            params,
            **shared,
            log=log,
            control=control,
            allow_restart=False,
            recorder=recorder,
            cf_resolved_hook=None,
            waf_resolved_hook=None,
            waf_wait_fn=_make_waf_wait(context, params.url, waf_max_wait, waf_poll, focus),
        )

    def worker(index: int) -> list:
        """Attach, drain the chapter queue, disconnect. Own thread only."""
        rows: list = []
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            page = context.new_page()
            page.add_init_script(STEALTH_INIT_SCRIPT)
            install_popup_handler(context, page, config)
            ctx = make_ctx(context)

            while True:
                if control.stopped:
                    break
                if control.pause_event.is_set():
                    control.pause_event.wait(0.5)
                    continue
                try:
                    ch_num, file_path = work.get_nowait()
                except queue.Empty:
                    break

                # The WAF hijack is free to move or close a tab, so keep this
                # worker's handle alive (same reason session._ensure_page
                # exists for the sequential path).
                if page.is_closed():
                    log("[i] A parallel tab was closed — opening a new one.")
                    page = context.new_page()
                    page.add_init_script(STEALTH_INIT_SCRIPT)
                    install_popup_handler(context, page, config)

                status, err, _page = _process_chapter(page, ch_num, file_path, ctx)
                rows.append((ch_num, file_path, status, err))
                if status == "done" and not work.empty():
                    _sleep_between_chapters(params.delay, control)
        except Exception:
            rows.append((None, None, "error", traceback.format_exc()))
        finally:
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
        return rows

    results: list = []
    worker_errors: list[str] = []
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="upload") as pool:
        for future in [pool.submit(worker, i) for i in range(n)]:
            for ch_num, file_path, status, err in future.result():
                if ch_num is None:
                    worker_errors.append(err)
                else:
                    results.append((ch_num, file_path, status, err))

    stragglers = []
    for ch_num, file_path, status, err in results:
        if status == "done":
            summary.processed += 1
            summary.succeeded += 1
        elif status == "failed":
            summary.processed += 1
            summary.failed += 1
            summary.failures.append((ch_num, Path(file_path).name, err))
        else:
            stragglers.append(ch_num)

    for err in worker_errors:
        log("[!] A parallel worker failed:\n" + err)

    return stragglers, bool(worker_errors)
