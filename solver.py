"""Automatic Cloudflare clearance for the comix uploader.

Stock Playwright emits the CDP ``Runtime.enable`` signal on attach, which
Cloudflare's challenge detects - so a Playwright-driven Chrome hangs on
"Verifying you are human" forever. This lets a browser that does NOT emit that
signal (patchright, a Playwright fork, in a separate process) solve the
challenge, then hands the ``cf_clearance`` cookie to the uploader. It uses the
real system Chrome so its User-Agent matches, since ``cf_clearance`` is bound to
the exact UA and IP that earned it.

The module top level is stdlib-only (``import solver`` is safe in tests and
``core.py``); patchright is imported lazily inside ``mint_clearance``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import core

BASE_DIR = Path(__file__).resolve().parent
SOLVER_PROFILE_DIR = BASE_DIR / "solver_profile"

# Cookie names worth harvesting (and re-injecting) from the solved browser.
CLEARANCE_HINTS = ("cf_clearance", "__cf_bm", "cf_chl", "challenge", "clearance")

# Only fields Playwright's add_cookies() accepts; patchright's cookies() adds
# extras that would make add_cookies() raise.
COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
)

DEFAULT_MAX_WAIT = 90
DEFAULT_WINDOW_MODE = "offscreen"
DEFAULT_FOCUS_AFTER = 15.0
# A solve this fresh is reused instead of spawning another Chrome (parallel
# workers share one browser, so one solve covers all of them).
RECENT_SOLVE_SECONDS = 30.0

# The parent's chosen proxy, as JSON, passed via the environment (not argv) so
# an authenticated proxy's password never shows up in the child's command line.
PROXY_ENV_VAR = "COMIX_SOLVER_PROXY"

# Serialises solver subprocesses and remembers the last successful solve.
_SOLVE_LOCK = threading.Lock()
_last_result: dict = {"ts": 0.0, "ok": False, "cookies": []}


# --------------------------------------------------------------------------
# Pure helpers (no browser - unit-tested)
# --------------------------------------------------------------------------


def _is_clearance_cookie(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in CLEARANCE_HINTS)


def _extract_clearance(cookies: list) -> list:
    """The cookie records worth copying to the uploader's browser.

    Field-whitelisted so the result can be passed straight to
    ``context.add_cookies`` on a stock Playwright context.
    """
    out = []
    for c in cookies or []:
        if not _is_clearance_cookie(c.get("name", "")):
            continue
        out.append({k: c[k] for k in COOKIE_FIELDS if k in c})
    return out


def _clearance_value(cookies: list) -> str | None:
    for c in cookies or []:
        if (c.get("name") or "").lower() == "cf_clearance":
            return c.get("value")
    return None


def _should_escalate(elapsed: float, focus_after: float) -> bool:
    return elapsed >= focus_after


def _failure(error: str) -> dict:
    return {"ok": False, "cookies": [], "user_agent": "", "error": str(error)}


def parse_result(stdout: str) -> dict:
    """Parse the child's stdout; the last JSON object wins. Never raises."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            obj.setdefault("cookies", [])
            obj.setdefault("user_agent", "")
            obj.setdefault("error", "")
            return obj
    return _failure("no JSON result from solver")


# --------------------------------------------------------------------------
# Child side (runs in the subprocess; imports patchright lazily)
# --------------------------------------------------------------------------


def _is_solved(page) -> bool:
    """True when the challenge has visibly cleared (DOM state, not cookies)."""
    try:
        if core.is_cloudflare_error_page(page):
            return False
        if core.is_cloudflare_active(page):
            return False
        return core._page_ready_after_challenge(page)
    except Exception:
        return False


def _launch(pw, mode: str, proxy: dict | None = None):
    args = []
    if mode == "offscreen":
        # Park the window off-screen so a normal run does not disrupt the user.
        args.append("--window-position=-2400,-2400")
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(SOLVER_PROFILE_DIR),
        channel="chrome",
        headless=(mode == "headless"),
        no_viewport=True,
        args=args,
        # MUST be the uploader's own egress: cf_clearance is bound to the IP
        # that earned it, so a clearance minted elsewhere is rejected.
        proxy=proxy,
    )


def mint_clearance(
    url: str = core.SITE_ROOT,
    *,
    max_wait: float = DEFAULT_MAX_WAIT,
    window_mode: str = DEFAULT_WINDOW_MODE,
    focus_after: float = DEFAULT_FOCUS_AFTER,
    proxy: dict | None = None,
    log: Callable = print,
) -> dict:
    """Open ``url`` in a real (non-CDP-flagged) Chrome and harvest the clearance.

    Never raises: returns ``{"ok", "cookies", "user_agent", "error"}``.
    """
    try:
        from patchright.sync_api import sync_playwright
    except Exception as ex:  # not installed / import error
        return _failure(f"patchright unavailable: {ex}")

    ctx = None
    pw = None
    try:
        pw = sync_playwright().start()
        ctx = _launch(pw, window_mode, proxy)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as ex:
            log(f"[i] Solver navigation warning: {ex}")

        deadline = time.time() + float(max_wait)
        escalated = False
        while time.time() < deadline:
            if _is_solved(page) and _clearance_value(ctx.cookies()) is not None:
                cookies = _extract_clearance(ctx.cookies())
                try:
                    ua = page.evaluate("navigator.userAgent") or ""
                except Exception:
                    ua = ""
                log("[✓] Solver cleared Cloudflare.")
                return {"ok": True, "cookies": cookies, "user_agent": ua, "error": ""}

            elapsed = float(max_wait) - max(0.0, deadline - time.time())
            if (
                window_mode == "offscreen"
                and not escalated
                and _should_escalate(elapsed, focus_after)
            ):
                escalated = True
                log("[i] Solver still blocked - bringing its window to the front.")
                try:
                    import window_ctl

                    window_ctl.move_and_focus_chrome(ctx, log=log)
                except Exception as ex:
                    log(f"[i] Could not focus the solver window: {ex}")

            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1.0)

        return _failure(f"challenge did not clear within {max_wait}s")
    except Exception as ex:
        return _failure(str(ex))
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass


def _proxy_from_env() -> dict | None:
    """The proxy the parent handed down, or None.

    Read from the environment rather than argv so an authenticated proxy's
    password never appears in this process's command line.
    """
    raw = os.environ.get(PROXY_ENV_VAR)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("server"):
        return data
    return None


def main(argv=None) -> int:
    """Subprocess entry point: one JSON object on stdout, logs on stderr."""
    parser = argparse.ArgumentParser(
        description="Mint a Cloudflare cf_clearance cookie."
    )
    parser.add_argument("--url", default=core.SITE_ROOT)
    parser.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT)
    parser.add_argument(
        "--window-mode",
        choices=("offscreen", "focus", "headless"),
        default=DEFAULT_WINDOW_MODE,
    )
    parser.add_argument("--focus-after", type=float, default=DEFAULT_FOCUS_AFTER)
    # Manual runs only: the parent passes the proxy through the environment so
    # credentials stay out of the command line.
    parser.add_argument("--proxy-server", default=None)
    parser.add_argument("--proxy-user", default=None)
    parser.add_argument("--proxy-pass", default=None)
    args = parser.parse_args(argv)

    def log(message):
        print(str(message), file=sys.stderr, flush=True)

    proxy = _proxy_from_env()
    if args.proxy_server:
        proxy = {"server": args.proxy_server}
        if args.proxy_user:
            proxy["username"] = args.proxy_user
        if args.proxy_pass:
            proxy["password"] = args.proxy_pass
    if proxy:
        log(f"[i] Solver egress via {proxy.get('server')}")

    result = mint_clearance(
        args.url,
        max_wait=args.max_wait,
        window_mode=args.window_mode,
        focus_after=args.focus_after,
        proxy=proxy,
        log=log,
    )
    print(json.dumps(result), flush=True)
    return 0 if result.get("ok") else 2


# --------------------------------------------------------------------------
# Parent side (runs in the uploader process)
# --------------------------------------------------------------------------


def solve_via_subprocess(
    url: str = core.SITE_ROOT,
    *,
    max_wait: float = DEFAULT_MAX_WAIT,
    window_mode: str = DEFAULT_WINDOW_MODE,
    focus_after: float = DEFAULT_FOCUS_AFTER,
    proxy: dict | None = None,
    log: Callable = print,
) -> dict:
    """Run ``python -m solver`` and return its parsed JSON. Never raises."""
    cmd = [
        sys.executable,
        "-m",
        "solver",
        "--url",
        url,
        "--max-wait",
        str(max_wait),
        "--window-mode",
        window_mode,
        "--focus-after",
        str(focus_after),
    ]
    env = dict(os.environ)
    if proxy:
        # Environment, not argv: keeps an authenticated proxy's password out of
        # the child's command line (visible via the process list).
        env[PROXY_ENV_VAR] = json.dumps(proxy)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=float(max_wait) + 30,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _failure("solver timed out")
    except Exception as ex:
        return _failure(f"could not start solver: {ex}")

    result = parse_result(proc.stdout)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            if line.strip():
                log(line.strip())
    if not result.get("ok") and proc.returncode not in (0, 2):
        result["error"] = result.get("error") or f"solver exited {proc.returncode}"
    return result


def _solver_kwargs(cf: dict) -> dict:
    return dict(
        max_wait=float(cf.get("solve_max_wait_seconds", DEFAULT_MAX_WAIT)),
        window_mode=str(cf.get("solve_window_mode", DEFAULT_WINDOW_MODE)),
        focus_after=float(cf.get("solve_focus_after_seconds", DEFAULT_FOCUS_AFTER)),
    )


def make_cf_solver_hook(config: dict, log: Callable = print) -> Callable:
    """Build the ``cf_solver_hook(context, log) -> bool`` used by the engine.

    The hook injects cookies into the CALLER's own context (thread-affine: the
    sequential engine passes the session context, parallel workers pass their
    own), and never writes config.json - persistence happens later on the
    session thread via ``core.sync_cookies_from_context``.
    """
    cf = core.cloudflare_config(config)

    def hook(context, log_: Callable | None = None) -> bool:
        logger = log_ or log
        if not cf.get("auto_solve", True):
            return False
        with _SOLVE_LOCK:
            # Another worker just solved it (shared browser) - reuse.
            if _last_result["ok"] and (
                time.time() - _last_result["ts"] < RECENT_SOLVE_SECONDS
            ):
                core.apply_cookies_to_context(context, _last_result["cookies"])
                return True

            result = solve_via_subprocess(
                core.SITE_ROOT,
                log=logger,
                # refresh_vpn=False: match the egress the running browser holds.
                proxy=core.effective_proxy(config, refresh_vpn=False),
                **_solver_kwargs(cf),
            )
            if not result.get("ok"):
                logger(
                    f"[!] Cloudflare solver failed: {result.get('error') or 'unknown'}"
                )
                return False

            cookies = result.get("cookies", [])
            _last_result.update(ts=time.time(), ok=True, cookies=cookies)
            injected = core.apply_cookies_to_context(context, cookies)
            if not injected:
                logger("[!] Solver got a clearance but could not inject it.")
                return False
            _warn_on_ua_mismatch(context, result.get("user_agent", ""), logger)
            return True

    return hook


def _warn_on_ua_mismatch(context, solver_ua: str, log: Callable) -> None:
    """cf_clearance is UA-bound; a mismatch means the cookie will be rejected."""
    if not solver_ua:
        return
    try:
        pages = [p for p in context.pages if not p.is_closed()]
        if not pages:
            return
        browser_ua = pages[0].evaluate("navigator.userAgent") or ""
    except Exception:
        return
    if browser_ua and browser_ua != solver_ua:
        log(
            "[!] Solver User-Agent differs from the uploader's - the clearance "
            "may be rejected. Make sure both use the same Google Chrome."
        )


def solve_into_config(config_store, log: Callable = print) -> bool:
    """Solve and persist the clearance into config.json (session/GUI thread only)."""
    cf = core.cloudflare_config(config_store.data)
    result = solve_via_subprocess(
        core.SITE_ROOT,
        log=log,
        proxy=core.effective_proxy(config_store.data, refresh_vpn=False),
        **_solver_kwargs(cf),
    )
    if not result.get("ok"):
        log(f"[!] Cloudflare solver failed: {result.get('error') or 'unknown'}")
        return False
    values = {c["name"]: c.get("value", "") for c in result.get("cookies", [])}
    core.set_config_cookies(config_store.data, values)
    config_store.reload()
    _last_result.update(ts=time.time(), ok=True, cookies=result.get("cookies", []))
    log("[✓] Cloudflare clearance obtained and saved.")
    return True


if __name__ == "__main__":
    sys.exit(main())
