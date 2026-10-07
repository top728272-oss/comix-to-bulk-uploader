"""Parallel chapter uploads on tabs of the one persistent Chrome.

How it works: the session already owns the persistent Chrome (launched with
``core.launch_context(enable_cdp=True)``). Each worker thread starts its own
Playwright instance and attaches to that same browser through the DevTools
endpoint (``core.read_devtools_port()``), then drives its own tab. Because the
workers share the browser, they share the profile, the login and the Cloudflare
clearance cookie - one clearance covers the whole batch.

Verified with a spike before this module existed:
  * ``--remote-debugging-port=0`` coexists with Playwright's internal
    ``--remote-debugging-pipe``, and the ``DevToolsActivePort`` file appears.
  * ``connect_over_cdp`` reaches the persistent default context.
  * Cookies are shared across connections. Pages are NOT visible across
    connections - each worker polls its own tab, which is all it needs
    (``_live_site_pages`` reads ``page.context.pages``, i.e. its own view).
  * Disconnecting a worker (page.close + pw.stop) leaves the browser and the
    other workers untouched.
  * Playwright's sync API is thread-affine: every playwright object is driven
    only from the thread that created it. The coordinator exchanges plain data
    with workers and never touches their pages.

Anything that goes wrong here degrades to the sequential engine with a clear
log line - parallel uploads must never be able to break a run.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

import core
from core import (
    DEFAULT_CONCURRENCY,
    STEALTH_INIT_SCRIPT,
    ChapterContext,
    Control,
    HistoryRecorder,
    RunSummary,
    _process_chapter,
    build_chapter_context,
    install_popup_handler,
    pending_chapters,
)


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

    # Optional pre-flight: mint a fresh Cloudflare clearance before the batch,
    # so a run does not stall on the first chapter. Never let it break a run.
    hook = kwargs.get("cf_solver_hook")
    try:
        if hook and page is not None and core.cloudflare_config(config).get(
            "preflight_solve"
        ):
            log("[i] Checking Cloudflare clearance before starting…")
            hook(page.context, log)
    except Exception as ex:
        log(f"[i] Pre-flight Cloudflare solve skipped: {ex}")

    if n <= 1:
        return core.run_upload_batch(page=page, config=config, params=params, **kwargs)

    if not endpoint:
        log(
            "[!] Parallel uploads unavailable (no DevTools endpoint) - "
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
            "[!] Parallel mode failed - finishing the batch sequentially.\n"
            + traceback.format_exc()
        )
        worker_failed, stragglers = True, []

    if (worker_failed or stragglers) and not control.stopped and not summary.aborted:
        if stragglers:
            log(
                f"[i] {len(stragglers)} chapter(s) still pending - "
                "finishing them sequentially."
            )
        tail = core.run_upload_batch(page=page, config=config, params=params, **kwargs)
        summary.processed += tail.processed
        summary.succeeded += tail.succeeded
        summary.failed += tail.failed
        summary.failures.extend(tail.failures)
        summary.aborted = summary.aborted or tail.aborted
        if not summary.abort_reason:
            summary.abort_reason = tail.abort_reason
    elif summary.aborted:
        log("[!] Server appears down - batch stopped; nothing else was attempted.")

    return summary


def _sleep_between_chapters(delay, control: Control) -> None:
    """Per-worker pacing, same intent as the sequential inter-chapter delay."""
    for _ in range(int(delay)):
        if control.stopped or control.aborted:
            return
        time.sleep(1)


# Seconds without a heartbeat before a worker is treated as wedged.
#
# Playwright's page.title()/content()/evaluate()/newPage() carry NO timeout, so
# a tab whose renderer has wedged blocks its worker thread forever. Nothing can
# interrupt that call, so the only defence is to stop waiting for it.
#
# The floor. When the config does not set "worker_stall_timeout_seconds" the
# real budget is derived from the per-chapter idle timeout (see _stall_timeout)
# so the watchdog can never fire on a chapter that is still within its own
# "no chunk progress" window.
DEFAULT_WORKER_STALL_SECONDS = 180.0

# How many times one worker may throw away a tab and open a fresh one.
#
# A replaced tab can NOT be closed - Playwright's Page.close() takes no timeout,
# so closing a wedged tab is exactly the call that would hang. Replacements are
# therefore bounded: without a cap a worker leaks a tab per chapter, which is
# how a 243-chapter run ended up with Chrome's tab count climbing continuously.
MAX_TAB_REPLACEMENTS = 3


def _stall_timeout(config: dict) -> float:
    """Seconds without a heartbeat before a worker is treated as wedged.

    Derived from the per-chapter idle timeout unless set explicitly, so the
    watchdog always outlives a chapter's own "no chunk progress" budget - a
    chapter that is merely slow must be allowed to give up on its own rather
    than having its whole worker abandoned first.
    """
    try:
        configured = float(config.get("worker_stall_timeout_seconds") or 0)
    except (TypeError, ValueError):
        configured = 0.0
    if configured > 0:
        return max(60.0, configured)
    try:
        idle = float(config.get("timeout_seconds", core.DEFAULT_IDLE_TIMEOUT))
    except (TypeError, ValueError):
        idle = float(core.DEFAULT_IDLE_TIMEOUT)
    return max(DEFAULT_WORKER_STALL_SECONDS, idle + 60.0)


def _fresh_tab(context, config: dict):
    """Open a new worker tab and arm it (stealth + popup guard)."""
    page = context.new_page()
    page.add_init_script(STEALTH_INIT_SCRIPT)
    install_popup_handler(context, page, config)
    return page


def _tab_is_dead(page) -> bool:
    """True only when the tab fails TWO consecutive bounded liveness probes.

    One failed probe can just be a busy renderer - a 20 MB upload keeps the
    main thread occupied - and a false positive is expensive, because the tab
    is replaced and the old one cannot be closed.
    """
    if core.page_is_responsive(page):
        return False
    return not core.page_is_responsive(page)


def _chapter_label(chapter) -> str:
    return f"{chapter:g}" if isinstance(chapter, (int, float)) else str(chapter)


class WorkerHeartbeat:
    """Per-worker liveness handle shared between a worker and the coordinator.

    ``beat()`` says "still working". ``abandoned`` is set by the coordinator
    when it stops waiting for a worker, and the worker MUST check it and exit:
    its blocked call may eventually time out, and a worker that silently
    resumed would keep pulling chapters from the shared queue - uploading them
    at the same time as the sequential retry, which is what produced duplicate
    chapters and a chapter recorded as both done and failed.
    """

    def __init__(self, index: int, progress: dict, lock) -> None:
        self.index = index
        self.abandoned = False
        self._progress = progress
        self._lock = lock

    def beat(self, chapter=None) -> None:
        with self._lock:
            entry = self._progress.setdefault(
                self.index, {"chapter": None, "ts": time.time()}
            )
            if chapter is not None:
                entry["chapter"] = chapter
            entry["ts"] = time.time()


def _run_workers_with_deadline(targets, stall_timeout: float, log):
    """Run ``targets`` on daemon threads, and never wait on a wedged one.

    ``targets`` is a list of callables ``fn(heartbeat) -> list[rows]``, each
    given its own :class:`WorkerHeartbeat`. A worker that stops calling
    ``heartbeat.beat()`` for ``stall_timeout`` seconds is treated as wedged: it
    is marked ``abandoned`` (so it stops as soon as it can) and left behind.

    Daemon threads rather than a ThreadPoolExecutor on purpose: pool threads
    are non-daemon and are joined at interpreter exit, so a single wedged
    worker would freeze the batch *and* block the app from closing.

    Returns ``(rows, worker_errors, stalled_chapters)``.
    """
    progress: dict = {}
    lock = threading.Lock()
    results: list = []
    errors: list[str] = []

    def runner(state, fn):
        def run():
            try:
                rows = fn(state)
            except Exception:
                with lock:
                    errors.append(traceback.format_exc())
                return
            with lock:
                results.extend(rows)

        return run

    states = []
    threads = []
    for index, fn in enumerate(targets):
        state = WorkerHeartbeat(index, progress, lock)
        states.append(state)
        thread = threading.Thread(
            target=runner(state, fn), name=f"upload-{index + 1}", daemon=True
        )
        threads.append(thread)
        thread.start()

    last_progress = time.time()
    while any(t.is_alive() for t in threads):
        with lock:
            stamp = max((e["ts"] for e in progress.values()), default=0.0)
        last_progress = max(last_progress, stamp)
        if time.time() - last_progress > stall_timeout:
            stalled = []
            for index, thread in enumerate(threads):
                if not thread.is_alive():
                    continue
                # Tell it to stop as soon as its blocked call gives up.
                states[index].abandoned = True
                with lock:
                    chapter = (progress.get(index) or {}).get("chapter")
                stalled.append(chapter)
                log(
                    f"[!] Tab {index + 1} stopped making progress"
                    + (f" on chapter {_chapter_label(chapter)}" if chapter else "")
                    + f" ({stall_timeout:.0f}s) - abandoning it for a sequential "
                    "retry."
                )
            return results, errors, [c for c in stalled if c]
        time.sleep(0.5)

    return results, errors, []


def _run_parallel(
    endpoint: str, config: dict, params, n: int, summary: RunSummary, **kwargs
):
    """Run the batch on ``n`` worker tabs. Returns (stragglers, worker_failed).

    ``stragglers`` are chapter numbers that were left "pending" (a gate
    outlasted its window, or the user stopped); ``worker_failed`` means a
    worker could not do its job at all. Either way the caller gives what is
    left to the sequential engine, which re-scans and skips finished chapters.
    An "aborted" chapter (origin down / CF stuck) sets ``summary.aborted`` and
    is never handed to the sequential tail.
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

    challenge_cfg = core.challenge_config(config)
    challenge_max_wait = int(challenge_cfg.get("max_wait_seconds", 600))
    challenge_poll = float(challenge_cfg.get("poll_interval_seconds", 2))
    focus = bool(config.get("focus_on_challenge", True))

    # Worker contexts: no restart (it would kill the other workers' uploads)
    # and no hooks (they hand back the session's page - never a worker's).
    shared = {
        k: v
        for k, v in kwargs.items()
        if k
        in (
            "on_status",
            "on_chunk",
            "on_cloudflare",
            "on_cloudflare_stuck",
            "cf_solver_hook",
            "on_challenge_cleared",
            "max_retries",
            "idle_timeout",
        )
    }

    def make_ctx(context, state: WorkerHeartbeat) -> ChapterContext:
        """One worker's context.

        ``on_status``/``on_chunk`` are wrapped rather than passed straight
        through, so every status change and every uploaded chunk also feeds the
        stall watchdog (``state.beat``). ``on_heartbeat`` additionally lets the
        challenge wait and the upload poll loop keep beating while they are
        legitimately waiting.
        """
        outer_status = shared.get("on_status")
        outer_chunk = shared.get("on_chunk")

        def on_status(ch, status, detail):
            state.beat(ch)
            if outer_status:
                outer_status(ch, status, detail)

        def on_chunk(ch, count):
            state.beat(ch)
            if outer_chunk:
                outer_chunk(ch, count)

        extra = {k: v for k, v in shared.items() if k not in ("on_status", "on_chunk")}
        return build_chapter_context(
            config,
            params,
            **extra,
            on_status=on_status,
            on_chunk=on_chunk,
            on_heartbeat=state.beat,
            log=log,
            control=control,
            allow_restart=False,
            recorder=recorder,
            cf_resolved_hook=None,
            challenge_max_wait=challenge_max_wait,
            challenge_poll=challenge_poll,
            focus=focus,
        )

    def worker(state: WorkerHeartbeat) -> list:
        """Attach, drain the chapter queue, disconnect. Own thread only."""
        rows: list = []
        pw = None
        page = None
        replacements = 0
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            page = _fresh_tab(context, config)
            ctx = make_ctx(context, state)

            while True:
                if control.stopped or control.aborted:
                    break
                # The coordinator gave up on this worker. Stop rather than
                # carry on: resuming here would upload chapters at the same time
                # as the sequential retry, which is how the same chapter ended
                # up both uploaded twice and recorded as done AND failed.
                if state.abandoned:
                    log(f"[i] Tab {state.index + 1} was abandoned - stopping it.")
                    break
                if control.pause_event.is_set():
                    control.pause_event.wait(0.5)
                    continue
                try:
                    ch_num, file_path = work.get_nowait()
                except queue.Empty:
                    break

                # A challenge is free to move or close a tab, so keep this
                # worker's handle alive (same reason session._ensure_page
                # exists for the sequential path). A tab that stopped answering
                # is the hang case: every Playwright call on it would block, so
                # work moves to a fresh tab. Replacements are capped and the old
                # tab is left alone - Page.close() has no timeout, so closing a
                # wedged tab is itself the call that would hang.
                if page.is_closed():
                    replacements += 1
                    log("[i] A parallel tab was closed - opening a new one.")
                    page = _fresh_tab(context, config)
                elif _tab_is_dead(page):
                    if replacements >= MAX_TAB_REPLACEMENTS:
                        reason = (
                            f"tab kept going unresponsive ({replacements} "
                            "replacements) - deferred to the sequential retry"
                        )
                        log(f"[!] Tab {state.index + 1}: {reason}.")
                        rows.append((ch_num, file_path, "pending", reason))
                        break
                    replacements += 1
                    log(
                        "[i] A parallel tab stopped responding - opening a fresh "
                        f"one ({replacements}/{MAX_TAB_REPLACEMENTS})."
                    )
                    page = _fresh_tab(context, config)

                # Tell the watchdog which chapter this tab is on, once the tab
                # it will actually use has been chosen.
                state.beat(ch_num)

                status, err, _page = _process_chapter(page, ch_num, file_path, ctx)
                rows.append((ch_num, file_path, status, err))
                state.beat(ch_num)
                if status == "done" and not work.empty():
                    _sleep_between_chapters(params.delay, control)
                if control.aborted:
                    break
        except Exception:
            rows.append((None, None, "error", traceback.format_exc()))
        finally:
            # Disconnecting alone leaves this worker's tab behind in Chrome
            # (usually sitting on about:blank); close it first. Skipped when the
            # tab is the reason we are leaving - close() would block on it.
            try:
                if page is not None and not page.is_closed():
                    page.close()
            except Exception:
                pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
        return rows

    stall_timeout = _stall_timeout(config)
    raw_rows, worker_errors, stalled = _run_workers_with_deadline(
        [worker] * n, stall_timeout, log
    )

    # A row with no chapter number is a worker-level failure, not a chapter.
    results: list = []
    for ch_num, file_path, status, err in raw_rows:
        if ch_num is None:
            worker_errors.append(err)
        else:
            results.append((ch_num, file_path, status, err))

    stragglers = _aggregate_results(results, worker_errors, summary)
    # A stalled worker produced no row at all, so its chapter is named here.
    stragglers.extend(ch for ch in stalled if ch not in stragglers)

    for err in worker_errors:
        log("[!] A parallel worker failed:\n" + err)

    return stragglers, bool(worker_errors)


def _aggregate_results(results, worker_errors, summary: RunSummary) -> list:
    """Fold worker rows into ``summary``; return the straggler chapter numbers.

    An "aborted" row (origin down / CF stuck) sets ``summary.aborted`` and is
    NOT added to the stragglers - the sequential tail must not re-run it.
    """
    stragglers = []
    for ch_num, file_path, status, err in results:
        if status == "done":
            summary.processed += 1
            summary.succeeded += 1
        elif status == "failed":
            summary.processed += 1
            summary.failed += 1
            summary.failures.append((ch_num, Path(file_path).name, err))
        elif status == "aborted":
            summary.aborted = True
            summary.abort_reason = err or summary.abort_reason
        else:
            stragglers.append(ch_num)
    return stragglers
