"""Shared engine for the Comix uploader (used by both the CLI and the GUI)."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
PROFILE_DIR = (BASE_DIR / "browser_profile").resolve()
LIBRARY_PATH = BASE_DIR / "series_library.json"

ARCHIVE_EXTS = {".zip", ".cbz", ".cbr", ".rar", ".7z"}
SITE_ROOT = "https://comix.to"

# How many chapters may upload at once (each in its own tab of the same
# Chrome window). 1 = the classic strictly-sequential behaviour.
DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 8

# comix runs its own WAF next to Cloudflare: a rotate-the-circle captcha served
# from /@waf/. It hijacks the tab (same tab, no popup) and redirects back to the
# original URL by itself once solved, so "clearance" simply means "the challenge
# page navigated away".
WAF_PATH = "/@waf/"
WAF_TITLE = "security check"
WAF_SELECTORS = "#dragLayer, #angleReadout, #thumbBlock"
WAF_DEBUG_DIR = BASE_DIR / ".waf_debug"

# Cookie names that can carry a challenge clearance worth persisting.
CLEARANCE_COOKIE_HINTS = ("clearance", "waf", "challenge")

# The WAF likes to re-challenge up to about a minute before waf_pass really
# expires, so anything under this margin should be treated as "expect a check".
WAF_CLEARANCE_MARGIN_SECONDS = 120

# The upload form's file input. Used both as a Playwright locator and as a
# plain CSS selector for the CDP file attach (see attach_upload_file).
FILE_INPUT_SELECTOR = "input.upage-drop__input, input[type='file']"

# Installed just before the file is attached. The page's own `change` event is
# the only trustworthy proof that it received the file: uploaders routinely
# read the selected file and then swap the drop zone out (or reset the input),
# so the input's own state is worthless as evidence. Captured at the document
# level, in the capture phase, so this runs before the site's own handler.
#
# Re-bound on every call on purpose: a document can be replaced without a new
# window (document.write / client-side re-render), which would leave a previous
# listener attached to a dead document — and then the probe would silently
# never fire again.
FILE_PROBE_INSTALL_JS = """
() => {
  window.__comixFileProbe = 0;
  if (window.__comixFileProbeHandler) {
    document.removeEventListener('change', window.__comixFileProbeHandler, true);
  }
  window.__comixFileProbeHandler = (event) => {
    const target = event.target;
    if (target && target.type === 'file') window.__comixFileProbe = 1;
  };
  document.addEventListener('change', window.__comixFileProbeHandler, true);
  return true;
}
"""

# "accepted" (the page got the file) | "empty" (input still there, no file) |
# "gone" (the form consumed the input — typically means it took the file).
FILE_PROBE_STATE_JS = """
(sel) => {
  if (window.__comixFileProbe === 1) return 'accepted';
  const input = document.querySelector(sel);
  if (!input) return 'gone';
  if (input.files && input.files.length > 0) return 'accepted';
  return 'empty';
}
"""

# How long to wait for the page to acknowledge the file.
FILE_PROBE_POLL_TRIES = 10
FILE_PROBE_POLL_SECONDS = 0.2


class VerificationRequired(Exception):
    """Raised when the site demands a human check before it will continue.

    ``kind`` is either "cloudflare" (the standard interstitial) or "waf"
    (comix's own /@waf/ rotate captcha). The two need different recovery: the
    Cloudflare one is fixed by refreshing the cf_clearance cookie, the WAF one
    by the user dragging the circle in the very browser we already have open.
    """

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message)
        self.kind = kind


class CloudflareBlockException(VerificationRequired):
    """Raised when a Cloudflare interstitial is detected."""

    def __init__(self, message: str = "") -> None:
        super().__init__("cloudflare", message)


class WafChallengeException(VerificationRequired):
    """Raised when comix's /@waf/ rotate-captcha is detected."""

    def __init__(self, message: str = "") -> None:
        super().__init__("waf", message)


class StopRequested(Exception):
    """Raised when the user aborts a running job."""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class ConfigError(ValueError):
    """A config.json value is invalid (wrong type or out of range)."""


def validate_concurrency(value) -> int:
    """Return the value if it is an allowed concurrency setting, else raise.

    ``bool`` is rejected explicitly: JSON ``true`` loads as Python ``True``,
    which would otherwise pass ``isinstance(value, int)``.
    """
    if type(value) is not int or not (1 <= value <= MAX_CONCURRENCY):
        raise ConfigError(
            f'Invalid "concurrency" value {value!r} in {CONFIG_PATH}: '
            f"it must be a whole number from 1 to {MAX_CONCURRENCY}. "
            "Edit the file and start again."
        )
    return value


def validate_config(cfg: dict) -> dict:
    """Validate the config values we know about; raise ConfigError if not.

    Unknown keys are deliberately left alone (forward compatibility), and a
    missing "concurrency" simply means "use the default".
    """
    if "concurrency" in cfg:
        validate_concurrency(cfg["concurrency"])
    return cfg


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Configuration file {CONFIG_PATH} not found.\n"
            "Copy config.example.json to config.json and fill in your cookies."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return validate_config(json.load(f))


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def sync_cookies_from_context(context, config: dict) -> bool:
    """Persist refreshed session/cf_clearance cookies back into config.json."""
    try:
        current_cookies = context.cookies()
    except Exception:
        return False

    cookie_map = {c["name"]: c["value"] for c in current_cookies}
    updated = False

    for cfg_cookie in config.get("cookies", []):
        name = cfg_cookie["name"]
        if name in cookie_map and cfg_cookie.get("value") != cookie_map[name]:
            cfg_cookie["value"] = cookie_map[name]
            updated = True

    known_names = {c["name"] for c in config.get("cookies", [])}
    for c in current_cookies:
        name = c["name"]
        if name in known_names:
            continue
        lowered = name.lower()
        if (
            name == "session"
            or name.startswith("remember_web_")
            or any(hint in lowered for hint in CLEARANCE_COOKIE_HINTS)
        ):
            config.setdefault("cookies", []).append(
                {"name": name, "value": c["value"], "url": SITE_ROOT}
            )
            updated = True

    if updated:
        save_config(config)
    return updated


def waf_clearance_seconds_left(config: dict) -> float | None:
    """Seconds left on the persisted waf_pass clearance, or None when unknown.

    The cookie value is "<unix-expiry>.<token>": the embedded timestamp is the
    EXPIRY, not the mint time (mint + Max-Age of 3600, confirmed from a network
    capture — a capture also showed the WAF re-challenging ~1 min early, hence
    the margin constant above). None means "no cookie or unreadable"; callers
    should stay silent in that case rather than guess.
    """
    for cookie in config.get("cookies", []):
        if cookie.get("name") != "waf_pass":
            continue
        ts_part = str(cookie.get("value", "")).split(".", 1)[0]
        try:
            expiry = int(ts_part)
        except (TypeError, ValueError):
            return None
        return expiry - time.time()
    return None


def live_waf_pass_valid(context) -> bool:
    """True when the LIVE browser session holds an unexpired waf_pass.

    Unlike ``waf_clearance_seconds_left`` (which reads the persisted config),
    this reads cookies straight from the open context, so a clearance minted
    seconds ago in ANY tab of the shared browser counts. Parallel workers use
    it to notice that another tab already solved the puzzle while their own
    tab still shows the stale challenge page.
    """
    try:
        cookies = context.cookies() or []
    except Exception:
        return False
    for cookie in cookies:
        if cookie.get("name") != "waf_pass":
            continue
        ts_part = str(cookie.get("value", "")).split(".", 1)[0]
        try:
            expiry = int(ts_part)
        except (TypeError, ValueError):
            continue
        if expiry - time.time() > WAF_CLEARANCE_MARGIN_SECONDS:
            return True
    return False


class ConfigStore:
    """Small wrapper so the GUI/CLI read and write the same config object."""

    def __init__(self) -> None:
        self.data = load_config()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        with self._lock:
            # Validate the merged result so a bad value can never reach disk.
            validate_config({**self.data, key: value})
            self.data[key] = value
            save_config(self.data)

    def reload(self) -> dict:
        self.data = load_config()
        return self.data

    @property
    def group(self) -> str:
        return self.data.get("default_group", "")

    @property
    def delay(self) -> int:
        return int(self.data.get("default_delay_seconds", 6))

    @property
    def max_retries(self) -> int:
        return int(self.data.get("max_retries", 3))

    @property
    def concurrency(self) -> int:
        return int(self.data.get("concurrency", DEFAULT_CONCURRENCY))


# --------------------------------------------------------------------------
# Series library (remembers series + folders + per-series settings)
# --------------------------------------------------------------------------


def load_library() -> dict:
    if LIBRARY_PATH.exists():
        try:
            with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("series", [])
            data.setdefault("recent_folders", [])
            data.setdefault("defaults", {})
            return data
        except Exception:
            pass
    return {"series": [], "recent_folders": [], "defaults": {}}


def save_library(library: dict) -> None:
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(library, f, indent=2)


def upsert_series(library: dict, hid: str, name: str, **settings) -> dict:
    for entry in library["series"]:
        if entry["hid"] == hid:
            entry["name"] = name or entry.get("name", "")
            entry.update(settings)
            entry["last_used"] = time.time()
            save_library(library)
            return entry

    entry = {
        "hid": hid,
        "name": name,
        "last_used": time.time(),
        "folder": "",
        "group": "",
        "title_pattern": "",
        "official": False,
        "delay": 0,
    }
    entry.update(settings)
    library["series"].insert(0, entry)
    save_library(library)
    return entry


def remember_folder(library: dict, folder: str) -> None:
    folder = str(folder)
    recent = [f for f in library.get("recent_folders", []) if f != folder]
    recent.insert(0, folder)
    library["recent_folders"] = recent[:15]
    save_library(library)


# --------------------------------------------------------------------------
# Chapter discovery / numbering
# --------------------------------------------------------------------------


def natural_sort_key(s: str):
    return [
        int(text) if text.isdigit() else text.lower()
        for text in re.split(r"(\d+)", str(s))
    ]


def parse_chapter_number(filename: str):
    base = Path(filename).stem
    match = re.search(r"(\d+(\.\d+)?)", base)
    if not match:
        raise ValueError(f"Could not parse chapter number from filename: {filename}")
    num_str = match.group(1)
    if "." in num_str:
        return float(num_str) if not num_str.endswith(".0") else int(float(num_str))
    return int(num_str)


def generate_next_safe_chapter_number(base_num: float, used_numbers: set):
    base_int = int(float(base_num))
    if isinstance(base_num, int) or float(base_num).is_integer():
        candidate = base_int + 0.5
    else:
        if round(float(base_num) + 0.1, 4) >= base_int + 1.0:
            candidate = round(float(base_num) + 0.01, 4)
        else:
            candidate = round(float(base_num) + 0.1, 4)

    step = 0.1 if candidate < base_int + 0.9 else 0.01
    while candidate in used_numbers:
        next_cand = round(candidate + step, 4)
        if next_cand >= base_int + 1.0:
            step = 0.001 if step == 0.01 else 0.01
            next_cand = round(candidate + step, 4)
        candidate = next_cand

    if float(candidate).is_integer():
        return int(candidate)
    return round(candidate, 4)


def chapter_file_sort_key(file_path: Path):
    name = file_path.name
    try:
        ch_num = parse_chapter_number(name)
    except ValueError:
        return (float("inf"), 1, float("inf"), natural_sort_key(name))

    stem = file_path.stem.strip()
    num_str = f"{ch_num:g}"
    is_clean = bool(
        re.fullmatch(
            rf"(chapter\s*|ch\.?\s*|v\d+\s*ch\.?\s*)?0*{re.escape(num_str)}",
            stem,
            re.IGNORECASE,
        )
    )
    clean_priority = 0 if is_clean else 1
    return (ch_num, clean_priority, len(stem), natural_sort_key(name))


def resolve_duplicate_chapters(files: list[Path]) -> list[tuple]:
    parsed_files = []
    initial_numbers = set()

    for f in sorted(files, key=chapter_file_sort_key):
        try:
            ch_num = parse_chapter_number(f.name)
            parsed_files.append((ch_num, f))
            initial_numbers.add(ch_num)
        except ValueError:
            continue

    chapter_items = []
    used_numbers = set()

    for orig_ch, f_path in parsed_files:
        if orig_ch not in used_numbers:
            used_numbers.add(orig_ch)
            chapter_items.append((orig_ch, f_path))
        else:
            new_ch = generate_next_safe_chapter_number(
                orig_ch, used_numbers | initial_numbers
            )
            used_numbers.add(new_ch)
            chapter_items.append((new_ch, f_path))

    chapter_items.sort(key=lambda x: x[0])
    return chapter_items


def scan_folder(folder: str | Path) -> list[tuple]:
    target = Path(folder)
    if not target.exists() or not target.is_dir():
        return []
    files = [
        f for f in target.iterdir() if f.is_file() and f.suffix.lower() in ARCHIVE_EXTS
    ]
    return resolve_duplicate_chapters(files)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def series_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def upload_url_for(hid: str) -> str:
    return f"{SITE_ROOT}/user/upload/{hid}"


def history_dir() -> Path:
    """Folder holding upload history/failed state.

    Computed per call rather than as a module constant so tests can point
    BASE_DIR at a temp folder. The folder is gitignored: it is per-machine
    state, and the root of the project is tidier without a dozen dotfiles.
    """
    return BASE_DIR / ".history"


# Legacy-file sweep runs once per process (the getters are called often).
_legacy_history_checked = False


def _ensure_history_dir() -> Path:
    """Create .history/ and, once per process, adopt any pre-.history files."""
    global _legacy_history_checked
    folder = history_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if not _legacy_history_checked:
        _legacy_history_checked = True
        try:
            migrate_legacy_history()
        except Exception:
            pass
    return folder


def migrate_legacy_history() -> list[Path]:
    """Move upload state written before .history/ existed into the folder.

    The old layout kept ``.upload_history_<hid>.json`` in the project root.
    Those files are the record of what has already been uploaded, so they are
    moved rather than abandoned — losing them would re-upload everything.
    A file already present in .history/ is never overwritten.
    """
    folder = history_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []

    moved: list[Path] = []
    for legacy in sorted(BASE_DIR.glob(".upload_*.json")):
        target = folder / legacy.name.lstrip(".")
        if target.exists():
            continue
        try:
            legacy.replace(target)
            moved.append(target)
        except Exception:
            pass  # locked or already gone: the old file simply stays put
    return moved


def get_history_file(url: str) -> Path:
    return _ensure_history_dir() / f"upload_history_{series_id_from_url(url)}.json"


def get_failed_file(url: str) -> Path:
    return _ensure_history_dir() / f"upload_failed_{series_id_from_url(url)}.json"


def load_history(history_file: Path) -> set:
    if history_file.exists():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_history(history_file: Path, history: set) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(
            sorted(
                history,
                key=lambda x: float(x) if str(x).replace(".", "", 1).isdigit() else 0,
            ),
            f,
            indent=2,
        )


def load_failed(failed_file: Path) -> dict:
    if failed_file.exists():
        try:
            with open(failed_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_failed(failed_file: Path, failed_dict: dict) -> None:
    failed_file.parent.mkdir(parents=True, exist_ok=True)
    with open(failed_file, "w", encoding="utf-8") as f:
        json.dump(failed_dict, f, indent=2)


def reset_history(url: str) -> None:
    for f in (get_history_file(url), get_failed_file(url)):
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass


class HistoryRecorder:
    """Thread-safe uploaded/failed bookkeeping for one series.

    The sequential runner touches this from a single thread; parallel workers
    touch it from several — hence the lock around every mutate-and-save pair.
    """

    def __init__(self, url: str) -> None:
        self.history_file = get_history_file(url)
        self.failed_file = get_failed_file(url)
        self._lock = threading.Lock()
        self.history = load_history(self.history_file)
        self.failed_history = load_failed(self.failed_file)

    def already_done(self, ch_num) -> bool:
        with self._lock:
            return str(ch_num) in self.history

    def record_success(self, ch_num) -> None:
        with self._lock:
            self.history.add(str(ch_num))
            save_history(self.history_file, self.history)
            if str(ch_num) in self.failed_history:
                del self.failed_history[str(ch_num)]
                save_failed(self.failed_file, self.failed_history)

    def record_failure(self, ch_num, err: str | None) -> None:
        with self._lock:
            self.failed_history[str(ch_num)] = err or "Unknown error"
            save_failed(self.failed_file, self.failed_history)


# --------------------------------------------------------------------------
# Browser helpers
# --------------------------------------------------------------------------


# Injected into every page (including parallel workers' pages) so the site
# does not see a webdriver flag.
STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
)


def launch_context(
    playwright_instance,
    config: dict,
    headless: bool = False,
    enable_cdp: bool = False,
):
    """Launch the one persistent Chrome this app drives.

    ``enable_cdp`` adds a local DevTools endpoint (random port) so parallel
    upload workers can attach to this same browser from their own threads —
    they then share the profile, the login and the WAF clearance cookie.
    """
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-infobars",
    ]
    if enable_cdp:
        args.append("--remote-debugging-port=0")

    context = playwright_instance.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=headless,
        args=args,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 900},
    )

    page = context.pages[0] if context.pages else context.new_page()
    page.add_init_script(STEALTH_INIT_SCRIPT)
    install_popup_handler(context, page, config)
    try:
        context.add_cookies(config.get("cookies", []))
    except Exception:
        pass
    return context, page


def install_popup_handler(context, page, config: dict) -> None:
    """Auto-close popups on ``context``, except the WAF challenge tab.

    Extracted from launch_context so parallel workers can guard the pages
    they create on their own CDP connection.
    """

    def handle_popup(new_page):
        if new_page == page:
            return
        try:
            # The security check must be left alone — closing it would throw
            # away the puzzle the user is about to solve.
            if WAF_PATH in (new_page.url or ""):
                return
            new_page.close()
            # Closing a popup does not require raising the opener, and raising
            # it yanks the user off whatever they were doing.
            if not config.get("keep_window_in_background", True):
                page.bring_to_front()
        except Exception:
            pass

    context.on("page", handle_popup)


def read_devtools_port(timeout: float = 5.0) -> int | None:
    """Port from the profile's DevToolsActivePort file, or None if absent.

    Chrome writes the file once the debugging endpoint from
    ``launch_context(enable_cdp=True)`` is up. None means "parallel uploads
    are unavailable" — callers fall back to sequential and never crash.
    """
    path = PROFILE_DIR / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return int(path.read_text(encoding="utf-8").splitlines()[0].strip())
        except Exception:
            time.sleep(0.25)
    return None


def is_cloudflare_active(page) -> bool:
    try:
        title = page.title().strip().lower()
        if (
            title.startswith("just a moment")
            or "attention required! | cloudflare" in title
        ):
            return True
        if page.locator(
            "#challenge-stage, #challenge-running, div#cf-wrapper"
        ).first.is_visible():
            return True
    except Exception:
        pass
    return False


def is_waf_challenge(page) -> bool:
    """True while the tab sits on comix's /@waf/ rotate-the-circle captcha.

    Three independent signals, because the URL is the only one guaranteed to be
    correct the instant the navigation commits, while the DOM hooks only exist
    once the challenge page has rendered.
    """
    try:
        if page.is_closed():
            return False
        if WAF_PATH in (page.url or ""):
            return True
        if (page.title() or "").strip().lower() == WAF_TITLE:
            return True
        if page.locator(WAF_SELECTORS).first.is_visible():
            return True
    except Exception:
        pass
    return False


def detect_challenge(page) -> str | None:
    """Return "cloudflare", "waf", or None for the tab's current state."""
    if is_cloudflare_active(page):
        return "cloudflare"
    if is_waf_challenge(page):
        return "waf"
    return None


def _kind_label(kind: str) -> str:
    return "Security check" if kind == "waf" else "Cloudflare challenge"


# --------------------------------------------------------------------------
# Window focus
# --------------------------------------------------------------------------
# The policy: routine automation never raises the Chrome window — draining a
# queue is not a reason to steal the desktop. The one exception is a
# verification gate, which is unsolvable without a human looking at it. These
# helpers are that exception, and they are the only focus code left on the
# automation path.


def focus_challenge_window(page, config: dict | None = None, log: Callable = print) -> bool:
    """Restore + foreground the Chrome window holding a verification gate.

    Returns False when disabled, off Windows, or when the window can't be
    found — never raises, so a focus failure cannot break a run.
    """
    if config is not None and not config.get("focus_on_challenge", True):
        return False
    if page is None:
        return False
    try:
        from window_ctl import restore_and_focus_chrome
    except Exception:
        return False
    try:
        return restore_and_focus_chrome(page.context, log=log)
    except Exception:
        return False


# Parallel workers can hit a verification gate at the same time; raising the
# Chrome window more than once every few seconds just yanks the user around.
_FOCUS_LOCK = threading.Lock()
_last_focus_ts = 0.0


def _focus_for_challenge(page, challenged, log: Callable) -> None:
    """Make sure the challenged tab is the visible one, then raise the window."""
    global _last_focus_ts
    target = next((p for p in challenged if p == page), challenged[0])
    with _FOCUS_LOCK:
        now = time.time()
        if now - _last_focus_ts < 5.0:
            return
        _last_focus_ts = now
    try:
        # Tab-level activation (CDP), not window raising: the puzzle has to be
        # the tab the human lands on.
        target.bring_to_front()
    except Exception:
        pass
    focus_challenge_window(target, log=log)


def _exception_for(kind: str, message: str = "") -> VerificationRequired:
    if kind == "waf":
        return WafChallengeException(message or "Security check is active.")
    return CloudflareBlockException(message or "Cloudflare challenge screen is active.")


def require_no_challenge(page, message: str = "") -> None:
    """Raise the matching VerificationRequired if a challenge is showing."""
    kind = detect_challenge(page)
    if kind:
        raise _exception_for(kind, message)


def require_no_cloudflare(
    page, message: str = "Cloudflare challenge screen is active."
) -> None:
    """Kept for back-compat; require_no_challenge is the general form."""
    require_no_challenge(page, message)


def _confirm_block(page, grace: float = 3.0) -> bool:
    """Wait briefly for a blocked API call to surface a challenge in the DOM.

    A bare HTTP 403/503 is ambiguous — it can be an expired session rather than
    a challenge. Pausing the whole run on a captcha that is never going to
    appear is worse than failing the chapter, so the DOM has the final say.
    """
    deadline = time.time() + grace
    while time.time() < deadline:
        if detect_challenge(page):
            return True
        time.sleep(0.5)
    return False


def save_challenge_evidence(
    page, kind: str, tracker: dict | None = None, chapter=None
) -> Path | None:
    """Dump whatever is on screen so an unfamiliar challenge can be identified.

    Best-effort only: every step is guarded, because this runs at the worst
    possible moment (a page we do not understand, possibly mid-navigation).
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = WAF_DEBUG_DIR / f"{stamp}_{kind}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    meta = {
        "kind": kind,
        "chapter": None if chapter is None else str(chapter),
        "captured_at": stamp,
        "url": None,
        "title": None,
        "frames": [],
    }
    try:
        meta["url"] = page.url
        meta["title"] = page.title()
        meta["frames"] = [f.url for f in page.frames]
    except Exception:
        pass
    if tracker:
        try:
            meta["tracker"] = dict(tracker)
        except Exception:
            pass

    for name, write in (
        ("page.html", lambda p: p.write_text(page.content(), encoding="utf-8")),
        (
            "meta.json",
            lambda p: p.write_text(json.dumps(meta, indent=2), encoding="utf-8"),
        ),
    ):
        try:
            write(folder / name)
        except Exception:
            pass
    try:
        page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
    except Exception:
        pass

    return folder


def _live_site_pages(page) -> list | None:
    """Return live site tabs, or None when the context cannot be observed."""
    try:
        pages = [p for p in page.context.pages if not p.is_closed()]
    except Exception:
        return None
    try:
        if not page.is_closed() and page not in pages:
            pages.append(page)
    except Exception:
        pass
    return [
        p
        for p in pages
        if urlsplit(p.url).netloc == urlsplit(SITE_ROOT).netloc
        and urlsplit(p.url).scheme == urlsplit(SITE_ROOT).scheme
    ]


def _page_ready_after_challenge(page) -> bool:
    """True when a tab shows a real page — not about:blank, not an error dump.

    Kept deliberately loose: the gate detectors above decide what counts as a
    challenge, this only stops a blank tab or a 502 corpse from being read as
    "the challenge is gone".
    """
    try:
        if (page.url or "").strip() in ("", "about:blank"):
            return False
        title = page.title().strip().lower()
        body = page.locator("body").inner_text(timeout=1000).strip().lower()
        errors = (
            "bad gateway",
            "gateway time-out",
            "gateway timeout",
            "service unavailable",
            "internal server error",
        )
        if any(marker in title or marker in body for marker in errors):
            return False
        return bool(body) and page.evaluate("document.readyState") in (
            "interactive",
            "complete",
        )
    except Exception:
        return False


def _pump_browser(page, duration: float) -> None:
    # Sync Playwright delivers navigation events only while its dispatcher runs.
    try:
        pages = page.context.pages
        target = next((p for p in pages if not p.is_closed()), None)
        if target is not None:
            target.wait_for_timeout(duration * 1000)
            return
    except Exception:
        pass
    time.sleep(duration)


def wait_for_challenge_clear(
    page,
    control: Control | None = None,
    kind: str = "waf",
    log: Callable = print,
    max_wait: int = 600,
    poll: float = 2.0,
    settle: int = 2,
    cf_reload_after: float = 30.0,
    cf_reload_every: float = 15.0,
    focus: bool = True,
) -> bool:
    """Block until the gate is gone from every tab, or the wait window expires.

    The human does the solving in the visible Chrome window; all this does is
    notice it and say so. Polls every tab on the site, not just the tracked
    page: the /@waf/ hijack is free to move the challenge to another tab, and
    a clearance nobody is watching never ends the wait. A read only counts as
    clean once every tab shows real content — a blank or 502 page is not
    "cleared", it is just another thing to wait out. ``settle`` consecutive
    clean reads are required so a mid-redirect sample cannot end the wait
    early. ``kind`` also decides how pushy the wait is: "waf" never touches
    the page (reloading would reset the puzzle), while "cloudflare" reloads
    the check when it sits still — a stalled check never mints its clearance,
    and the fresh visit is what produces it.
    """
    check = is_waf_challenge if kind == "waf" else is_cloudflare_active
    manual = None
    if control is not None:
        manual = control.waf_event if kind == "waf" else control.cf_event

    deadline = time.time() + max_wait
    clean = 0
    challenge_since: float | None = None
    last_reload = 0.0

    while time.time() < deadline:
        if control is not None:
            if control.stopped:
                return False
            if control.pause_event.is_set():
                time.sleep(0.25)
                continue
            if manual is not None and manual.is_set():
                manual.clear()
                log("[✓] Resuming on your confirmation.")
                return True

        pages = _live_site_pages(page)
        if pages is None:
            # The browser went away mid-wait. Nothing to observe and nothing
            # to reload; hold here so the retry surfaces the real problem.
            clean = 0
        else:
            challenged = [p for p in pages if check(p)]
            if challenged:
                clean = 0
                now = time.time()
                if challenge_since is None:
                    challenge_since = now
                    # First sighting of this gate: surface the window so the
                    # puzzle is actually solvable. Fires once per episode.
                    if focus:
                        _focus_for_challenge(page, challenged, log)
                elif (
                    kind == "cloudflare"
                    and now - challenge_since > cf_reload_after
                    and now - last_reload > cf_reload_every
                ):
                    # A Cloudflare check that just spins (or dies with a 502)
                    # never finishes on its own; reload the stuck tab. Prefer
                    # the tracked page so the retry lands on a live tab.
                    target = next((p for p in challenged if p == page), challenged[0])
                    try:
                        log("[i] The Cloudflare check seems stuck — reloading it.")
                        target.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    last_reload = time.time()
                    challenge_since = time.time()
            elif pages and all(
                not detect_challenge(p) and _page_ready_after_challenge(p)
                for p in pages
            ):
                challenge_since = None
                clean += 1
                if clean >= settle:
                    return True
            else:
                challenge_since = None
                clean = 0

        slept = 0.0
        while slept < poll:
            if control is not None and (
                control.stopped or (manual is not None and manual.is_set())
            ):
                break
            # Pump Playwright's dispatcher while sleeping: page.url only
            # advances when the sync API gets processing time, so a pure
            # sleep would keep reading the pre-redirect URL forever.
            _pump_browser(page, 0.25)
            slept += 0.25

    return False


# --------------------------------------------------------------------------
# Site search (runs through the real browser: the API needs a per-request token)
# --------------------------------------------------------------------------


def search_site(page, query: str, limit: int = 15, log: Callable | None = None):
    """Use the site's own search box and scrape the rendered result list.

    Returns a list of dicts: {hid, name, type, chapters, rating, url}
    """
    query = (query or "").strip()
    if not query:
        return []

    page.goto(SITE_ROOT, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)
    require_no_challenge(
        page, "A verification screen blocked the site while searching."
    )

    toggle = page.locator("button.search-toggle").first
    try:
        if toggle.is_visible():
            toggle.click(timeout=2000)
            page.wait_for_timeout(300)
    except Exception:
        pass

    inp = page.locator(
        "input[placeholder*='Search' i], .search-pop input, input[type='search']"
    ).first
    inp.wait_for(state="visible", timeout=10000)
    try:
        inp.click(timeout=3000)
    except Exception:
        pass
    inp.fill("")
    inp.press_sequentially(query, delay=45)

    try:
        page.wait_for_selector(
            ".search-pop__item--result, .search-pop__empty", timeout=10000
        )
    except Exception:
        kind = detect_challenge(page)
        if kind:
            raise _exception_for(
                kind, "A verification screen blocked the site while searching."
            )
    page.wait_for_timeout(600)

    raw = page.eval_on_selector_all(
        ".search-pop__item--result",
        """els => els.map(e => {
            const a = e.querySelector('a');
            const lines = (e.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            return { href: a ? a.getAttribute('href') : null, lines: lines };
        })""",
    )

    results = []
    seen = set()
    for item in raw:
        href = item.get("href") or ""
        m = re.match(r"/title/([A-Za-z0-9]+)", href)
        if not m:
            continue
        hid = m.group(1)
        if hid in seen:
            continue
        seen.add(hid)

        lines = item.get("lines") or []
        name = lines[0] if lines else hid
        meta = " ".join(lines[1:])
        ch_match = re.search(r"Ch\.?\s*([\d.]+)", meta, re.IGNORECASE)
        results.append(
            {
                "hid": hid,
                "name": name,
                "type": (
                    re.search(r"\b(MANGA|MANHWA|MANHUA|OTHER|NOVEL)\b", meta)
                    or [None, ""]
                )[1]
                if re.search(r"\b(MANGA|MANHWA|MANHUA|OTHER|NOVEL)\b", meta)
                else "",
                "chapters": ch_match.group(1) if ch_match else "",
                "rating": (re.search(r"([\d.]+)\s*$", meta) or [None, ""])[1],
                "meta": meta,
                "url": f"{SITE_ROOT}{href}",
                "upload_url": upload_url_for(hid),
            }
        )
        if len(results) >= limit:
            break

    if log:
        log(f"Search '{query}': {len(results)} result(s).")
    return results


def resolve_series_name(page, hid: str, log: Callable | None = None) -> str:
    """Resolve a bare series id (e.g. 'nn6ny') to its display name."""
    page.goto(f"{SITE_ROOT}/title/{hid}", wait_until="domcontentloaded", timeout=60000)
    require_no_challenge(
        page, "A verification screen blocked the site while resolving the title."
    )
    title = (page.title() or "").strip()
    title = re.sub(r"\s*[-|]\s*Comix.*$", "", title, flags=re.IGNORECASE).strip()
    if log:
        log(f"Resolved {hid} -> {title or '(unknown)'}")
    return title or hid


# --------------------------------------------------------------------------
# Uploading
# --------------------------------------------------------------------------


@dataclass
class UploadParams:
    url: str = ""
    folder: str = ""
    title_pattern: str = ""
    group: str = ""
    official: bool = False
    delay: int = 6
    start: float | None = None
    end: float | None = None
    selected: list | None = None  # chapter numbers to upload (None = all pending)


@dataclass
class Control:
    stop_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    cf_event: threading.Event = field(default_factory=threading.Event)
    waf_event: threading.Event = field(default_factory=threading.Event)
    restart_browser: bool = False
    # Off by default: draining a queue is not a reason to steal the desktop.
    # Only a verification challenge is (see focus_challenge_window).
    raise_window: bool = False

    def reset(self) -> None:
        self.stop_event.clear()
        self.pause_event.clear()
        self.cf_event.clear()
        self.waf_event.clear()
        self.restart_browser = False

    def request_stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.cf_event.set()
        self.waf_event.set()

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()


def attach_upload_file(page, file_input, file_path, log: Callable = print) -> None:
    """Attach the archive to the upload form's file input.

    Why not just ``file_input.set_input_files(path)``? Because Playwright
    handles path-based file inputs through a private CDP method
    (``Playwright.grantFileReadAccess``) that is only efficient on the
    connection that launched the browser. On a parallel worker's
    ``connect_over_cdp`` connection the same call still works but takes ~16 s
    instead of ~0.1 s — which randomly exceeds the 30 s action timeout, giving
    the intermittent "Locator.set_input_files: Timeout 30000ms exceeded" that
    only shows up in parallel mode.

    So the file is attached with plain CDP (``DOM.setFileInputFiles``) on the
    page's own connection: instant on both connections, fires the same
    ``input``/``change`` events, and works for hidden inputs. Playwright's own
    call remains as a fallback, so nothing regresses if CDP misbehaves.

    Success is judged by the page's own ``change`` event rather than by reading
    the input back: comix replaces the drop-zone input as soon as it has the
    file, so a re-query afterwards finds a fresh, empty element and would look
    like a failure (that false negative cost a pointless 30 s fallback before).
    """
    # Same waiting behaviour the locator action had: the form may still be
    # rendering when we get here.
    try:
        file_input.wait_for(state="attached", timeout=30000)
    except Exception:
        pass  # the attach attempts below will produce the real error

    if _attach_via_cdp(page, file_path, log):
        return
    # The standard route is the slow one on a worker connection (~16 s), so it
    # gets a longer budget than the default 30 s instead of a coin flip.
    log("[i] Falling back to Playwright's file input handling.")
    file_input.set_input_files(str(file_path), timeout=60000)


def _attach_via_cdp(page, file_path, log: Callable) -> bool:
    """Set the file input with ``DOM.setFileInputFiles``.

    True when the page took the file (or the form consumed the input, which
    means it did). False means "the standard route should be tried instead".
    """
    session = None
    try:
        try:
            page.evaluate(FILE_PROBE_INSTALL_JS)
        except Exception:
            pass  # probing is best-effort; the input check below still works

        session = page.context.new_cdp_session(page)
        doc = session.send("DOM.getDocument", {"depth": 0})
        node = session.send(
            "DOM.querySelector",
            {"nodeId": doc["root"]["nodeId"], "selector": FILE_INPUT_SELECTOR},
        )
        node_id = node.get("nodeId") if node else 0
        if not node_id:
            return False
        session.send(
            "DOM.setFileInputFiles",
            {"files": [str(file_path)], "nodeId": node_id},
        )

        # Wait for the page to acknowledge it. Reading the input back is not
        # enough: the site replaces its drop-zone input the moment it has the
        # file, and then a re-query finds a fresh, empty element.
        gone_reads = 0
        state = ""
        for _ in range(FILE_PROBE_POLL_TRIES):
            state = page.evaluate(FILE_PROBE_STATE_JS, FILE_INPUT_SELECTOR)
            if state == "accepted":
                return True
            if state == "gone":
                # Require two consecutive reads so a mid-render moment cannot
                # be mistaken for "the form consumed it".
                gone_reads += 1
                if gone_reads >= 2:
                    log(
                        "[i] The upload form replaced its file input — "
                        "treating the file as accepted."
                    )
                    return True
            else:
                gone_reads = 0
            time.sleep(FILE_PROBE_POLL_SECONDS)
        log(f"[!] The upload form did not take the file via CDP ({state}).")
        return False
    except Exception as ex:
        log(f"[i] CDP file attach failed ({ex}) — trying the standard route.")
        return False
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def _fill_group(
    page, group_name: str, log: Callable, allow_raise: bool = False
) -> None:
    if not group_name:
        return

    group_input = page.locator(
        ".upage-field input[placeholder*='group'], input[placeholder*='Search a group']"
    ).first

    for _attempt in range(1, 4):
        picked_el = page.locator(".upage-group--picked")
        try:
            if picked_el.is_visible():
                if group_name.lower() in picked_el.inner_text().lower():
                    return
                clear_btn = page.locator(".upage-group__clear")
                if clear_btn.is_visible():
                    clear_btn.click(force=True)
                    page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            # Playwright drives the dropdown over CDP; it does not need the OS
            # foreground, so the raise is opt-in only.
            if allow_raise:
                page.bring_to_front()
            page.evaluate(
                '() => document.querySelectorAll(\'a[href="#"][target="_blank"]\')'
                ".forEach(e => e.remove())"
            )
            group_input.click(force=True)
            group_input.fill("")
            page.wait_for_timeout(300)
            group_input.press_sequentially(group_name, delay=90)

            group_item = (
                page.locator("button.upage-group__item")
                .filter(has_text=group_name)
                .first
            )
            group_item.wait_for(state="visible", timeout=7000)
            try:
                group_item.click(force=True, timeout=3000)
            except Exception:
                group_item.dispatch_event("click")

            picked_el.wait_for(state="visible", timeout=4000)
            if group_name.lower() in picked_el.inner_text().lower():
                log(f"[✓] Group '{group_name}' selected.")
                return
        except Exception:
            page.wait_for_timeout(1000)

    page.evaluate(
        """(name) => {
            const btns = Array.from(document.querySelectorAll('button.upage-group__item'));
            const match = btns.find(b => b.textContent.toLowerCase().includes(name));
            if (match) match.click();
        }""",
        group_name.lower(),
    )
    page.wait_for_timeout(1000)
    try:
        if page.locator(".upage-group--picked").is_visible():
            log(f"[✓] Group '{group_name}' selected via DOM fallback.")
            return
    except Exception:
        pass
    log(f"[!] Warning: group '{group_name}' was not confirmed picked.")


def upload_single_chapter(
    page,
    url: str,
    chapter_num,
    title: str,
    file_path,
    group_name: str,
    mark_official: bool,
    idle_timeout: int = 90,
    log: Callable = print,
    on_chunk=None,
    control: Control | None = None,
):
    """Upload one chapter. Returns (success, error). Raises CloudflareBlockException."""

    def check_stop():
        if control is not None and control.stopped:
            raise StopRequested()

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    log(
        f"\n[+] Chapter {chapter_num:g} — {Path(file_path).name} ({file_size_mb:.1f} MB)"
    )

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)
    check_stop()
    require_no_challenge(page)

    try:
        page.wait_for_selector(".upage-field input", timeout=15000)
    except Exception as ex:
        kind = detect_challenge(page)
        if kind:
            raise _exception_for(
                kind, "A verification screen blocked the upload form."
            ) from ex
        raise

    page.wait_for_timeout(500)
    try:
        page.locator("body").click(position={"x": 5, "y": 5}, force=True, timeout=2000)
        page.wait_for_timeout(1000)
    except Exception:
        pass
    if control is not None and control.raise_window:
        page.bring_to_front()

    chapter_input = page.locator(
        ".upage-field input[placeholder*='42'], input[placeholder*='42']"
    ).first
    title_input = page.locator(
        ".upage-field input[placeholder*='Walk Home'], input[placeholder*='Walk Home']"
    ).first
    file_input = page.locator(FILE_INPUT_SELECTOR).first
    official_checkbox = page.locator("input[type='checkbox']").first
    submit_btn = page.locator(
        "button[type='submit'], button:has-text('Submit upload')"
    ).first

    chapter_input.fill(str(chapter_num))
    if title:
        title_input.fill(title)

    _fill_group(
        page, group_name, log, allow_raise=bool(control and control.raise_window)
    )
    check_stop()

    try:
        if mark_official and not official_checkbox.is_checked():
            official_checkbox.check(force=True)
        elif not mark_official and official_checkbox.is_checked():
            official_checkbox.uncheck(force=True)
    except Exception:
        pass

    attach_upload_file(page, file_input, file_path, log)
    page.wait_for_timeout(1500)

    tracker = {
        "finalized": False,
        "error": None,
        "blocked": False,
        "waf_suspect": False,
        "last_activity": time.time(),
        "chunks_done": 0,
    }

    def on_response(res):
        res_url = res.url
        if "/upload/chunk" in res_url and res.status == 200:
            tracker["last_activity"] = time.time()
            tracker["chunks_done"] += 1
            if on_chunk:
                on_chunk(tracker["chunks_done"])
        elif WAF_PATH in res_url:
            # The challenge itself is loading. Record it, but never raise from
            # inside the handler — the DOM check in the polling loop decides.
            tracker["last_activity"] = time.time()
            tracker["waf_suspect"] = True
        elif "/api/v1/uploads" in res_url:
            tracker["last_activity"] = time.time()
            if "finalize" in res_url and res.status == 200:
                tracker["finalized"] = True
            elif res.status in (403, 503):
                tracker["blocked"] = True
                tracker["error"] = f"Upload API blocked (HTTP {res.status})"
            elif res.status in (429, 500, 502, 503, 504):
                tracker["error"] = f"HTTP {res.status} on {res_url}"

    page.on("response", on_response)

    try:
        try:
            submit_btn.click(timeout=5000)
        except Exception:
            submit_btn.dispatch_event("click")

        tracker["last_activity"] = time.time()

        while True:
            check_stop()

            # A challenge can hijack the tab mid-upload. This has to run BEFORE
            # the success check below: the /@waf/ page is not under /user/upload/,
            # so testing the URL first would report the chapter as uploaded and
            # write it to history even though nothing was submitted.
            kind = detect_challenge(page)
            if kind:
                raise _exception_for(
                    kind, f"{_kind_label(kind)} interrupted the upload."
                )

            if tracker["finalized"] or (
                "/user/upload/" not in page.url and WAF_PATH not in page.url
            ):
                log(
                    f"[✓] Chapter {chapter_num:g} uploaded "
                    f"({tracker['chunks_done']} chunks)."
                )
                return True, None

            if tracker.get("blocked"):
                # 403/503 alone cannot tell a challenge apart from an expired
                # session, so let the DOM settle it before pausing the run.
                if _confirm_block(page):
                    kind = detect_challenge(page) or "cloudflare"
                    raise _exception_for(
                        kind, f"{_kind_label(kind)} triggered on the upload API."
                    )
                log(f"[!] Server error: {tracker['error']}")
                return False, tracker["error"]

            if tracker["error"]:
                log(f"[!] Server error: {tracker['error']}")
                return False, tracker["error"]

            error_el = page.locator(
                ".alert-danger, .error-message, .toast-error, div[role='alert']"
            ).first
            try:
                if error_el.is_visible():
                    err_text = error_el.inner_text().strip()
                    log(f"[!] UI error: {err_text}")
                    return False, err_text
            except Exception:
                pass

            if time.time() - tracker["last_activity"] > idle_timeout:
                kind = detect_challenge(page)
                if kind:
                    raise _exception_for(
                        kind, f"{_kind_label(kind)} stalled the upload."
                    )
                err = f"Upload stalled: no chunk progress for {idle_timeout}s."
                log(f"[!] {err}")
                return False, err

            page.wait_for_timeout(1000)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


@dataclass
class RunSummary:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: list = field(default_factory=list)


@dataclass
class ChapterContext:
    """Everything _process_chapter needs besides the chapter itself.

    Sequential mode builds ONE of these for the whole run. Parallel mode builds
    one per worker, with ``allow_restart=False`` and the hooks stripped: a
    browser restart would kill every other worker's upload mid-flight, and the
    hooks return the session's own page, which a worker must never adopt.
    """

    config: dict
    params: UploadParams
    log: Callable = print
    on_status: Callable | None = None
    on_chunk: Callable | None = None
    control: "Control | None" = None
    max_retries: int = 3
    idle_timeout: int = 90
    waf_max_wait: int = 600
    waf_poll: float = 2.0
    max_pauses: int = 5
    focus: bool = True
    on_cloudflare: Callable | None = None
    on_waf: Callable | None = None
    on_challenge_cleared: Callable | None = None
    cf_resolved_hook: Callable | None = None
    waf_resolved_hook: Callable | None = None
    # Parallel mode only: never restart the shared browser, and use the
    # supplied sliced WAF wait (it also watches the live waf_pass cookie).
    allow_restart: bool = True
    waf_wait_fn: Callable | None = None
    recorder: "HistoryRecorder | None" = None
    # Deliberately on the context, not in the retry loop: the challenge path
    # re-enters that loop, which would reset per-chapter/per-run state and let
    # a hot loop spin forever.
    pause_counts: dict = field(default_factory=dict)
    dumped: bool = False


def build_chapter_context(config: dict, params: UploadParams, **overrides) -> ChapterContext:
    """ChapterContext from config + params, with per-run defaults applied."""
    waf_cfg = config.get("waf") or {}
    kwargs = dict(
        config=config,
        params=params,
        waf_max_wait=int(waf_cfg.get("max_wait_seconds", 600)),
        waf_poll=float(waf_cfg.get("poll_interval_seconds", 2)),
        max_pauses=int(waf_cfg.get("max_pauses_per_chapter", 5)),
        focus=bool(config.get("focus_on_challenge", True)),
        allow_restart=True,
        recorder=HistoryRecorder(params.url),
    )
    kwargs.update(overrides)
    return ChapterContext(**kwargs)


def pending_chapters(params: UploadParams, recorder: HistoryRecorder) -> list:
    """Folder scan + range/selection filters, minus already-uploaded chapters."""
    items = scan_folder(params.folder)
    if params.start is not None:
        items = [i for i in items if i[0] >= params.start]
    if params.end is not None:
        items = [i for i in items if i[0] <= params.end]

    if params.selected is not None:
        wanted = {str(s) for s in params.selected}
        wanted |= {float(s) for s in params.selected if _is_number(s)}
        items = [i for i in items if str(i[0]) in wanted or i[0] in wanted]

    return [i for i in items if not recorder.already_done(i[0])]


def _process_chapter(page, ch_num, file_path, ctx: ChapterContext):
    """Upload one chapter, with retries and verification-gate handling.

    Returns ``(status, error, page)``:

    * ``"done"``    — uploaded and recorded in history.
    * ``"failed"``  — recorded as failed; the caller may continue with others.
    * ``"pending"`` — not completed and NOT recorded: the user stopped the run,
      or a gate outlasted its wait window. The caller stops (sequential) or
      hands the chapter to the sequential fallback (parallel).

    The returned page may differ from the input one — the Cloudflare recovery
    path can restart the browser (sequential mode only).
    """
    params = ctx.params
    log = ctx.log
    control = ctx.control or Control()
    recorder = ctx.recorder or HistoryRecorder(params.url)
    title = params.title_pattern.format(ch=ch_num) if params.title_pattern else ""

    while True:
        success = False
        last_err = None
        challenge_hit = False
        challenge_kind = None

        if ctx.on_status:
            ctx.on_status(ch_num, "uploading", "")

        for attempt in range(1, ctx.max_retries + 1):
            if attempt > 1:
                backoff = attempt * 5
                log(
                    f"[*] Retry {attempt}/{ctx.max_retries} for ch. {ch_num:g} ({backoff}s)"
                )
                for _ in range(backoff):
                    if control.stopped:
                        break
                    time.sleep(1)
            if control.stopped:
                break

            try:
                success, last_err = upload_single_chapter(
                    page=page,
                    url=params.url,
                    chapter_num=ch_num,
                    title=title,
                    file_path=file_path,
                    group_name=params.group,
                    mark_official=params.official,
                    idle_timeout=ctx.idle_timeout,
                    log=log,
                    on_chunk=(lambda c, n=ch_num: ctx.on_chunk(n, c)) if ctx.on_chunk else None,
                    control=control,
                )
                if success:
                    break
            except StopRequested:
                log("[!] Stopped by user.")
                if ctx.on_status:
                    ctx.on_status(ch_num, "pending", "")
                return "pending", None, page
            except VerificationRequired as vreq:
                log(f"\n[!] {_kind_label(vreq.kind)}: {vreq}")
                challenge_hit = True
                challenge_kind = vreq.kind
                last_err = str(vreq)
                break
            except Exception as ex:
                last_err = str(ex)
                kind = detect_challenge(page)
                if kind:
                    log(f"[!] {_kind_label(kind)} detected.")
                    challenge_hit = True
                    challenge_kind = kind
                    break
                log(f"[!] Error on ch. {ch_num:g}: {ex}")

        if control.stopped:
            if ctx.on_status:
                ctx.on_status(ch_num, "pending", "")
            log("[!] Stopped by user.")
            return "pending", None, page

        if challenge_hit:
            key = str(ch_num)
            ctx.pause_counts[key] = ctx.pause_counts.get(key, 0) + 1

            if ctx.pause_counts[key] > ctx.max_pauses:
                log(
                    f"[!] Chapter {ch_num:g} hit a verification screen "
                    f"{ctx.pause_counts[key]} times — skipping it."
                )
                last_err = "Repeated verification screens"
                challenge_hit = False
            elif challenge_kind == "waf":
                # comix's own captcha: the user drags the circle in the Chrome
                # window we already have open, so the context must stay alive —
                # relaunching it would throw the puzzle away. A leftover
                # confirmation click (this run or an earlier pause) must not
                # end the wait before the puzzle is solved, and a click that
                # races ahead of this pause must not be discarded — so the
                # event is cleared before the pause is announced.
                control.waf_event.clear()
                if ctx.on_status:
                    ctx.on_status(ch_num, "waf", "solve the security check")
                if ctx.on_waf:
                    ctx.on_waf(ch_num)
                # One dump per run is plenty to identify the challenge, and it
                # keeps a recurring gate from filling the disk.
                evidence = None
                if not ctx.dumped:
                    evidence = save_challenge_evidence(page, "waf", chapter=ch_num)
                    ctx.dumped = True
                log(
                    "\n"
                    + "=" * 60
                    + "\n  [!] SECURITY CHECK — comix wants you to verify you're human"
                    + "\n"
                    + "=" * 60
                    + f"\nPaused on chapter {ch_num:g}. Nothing was skipped or marked failed."
                    + "\nIn the Chrome window, drag the circle until the picture lines up,"
                    + "\nthen press Verify. Uploading resumes on its own."
                    + (f"\nEvidence saved to {evidence}" if evidence else "")
                )
                if ctx.waf_wait_fn is not None:
                    # Parallel workers slice this wait and also watch the live
                    # waf_pass cookie — another tab may have solved the puzzle.
                    cleared = ctx.waf_wait_fn(page, control, log)
                else:
                    cleared = wait_for_challenge_clear(
                        page,
                        control=control,
                        kind="waf",
                        log=log,
                        max_wait=ctx.waf_max_wait,
                        poll=ctx.waf_poll,
                        focus=ctx.focus,
                    )
                if control.stopped:
                    if ctx.on_status:
                        ctx.on_status(ch_num, "pending", "")
                    log("[!] Stopped by user.")
                    return "pending", None, page
                if not cleared:
                    log(
                        "[!] The security check was not cleared in time. "
                        "Stopping here so nothing is skipped or marked failed."
                    )
                    if ctx.on_status:
                        ctx.on_status(ch_num, "pending", "")
                    return "pending", None, page
                if ctx.waf_resolved_hook is not None:
                    try:
                        page = ctx.waf_resolved_hook() or page
                    except Exception as ex:
                        log(f"[!] Could not re-sync the browser session: {ex}")
                        if ctx.on_status:
                            ctx.on_status(ch_num, "pending", "")
                        return "pending", None, page
                log(f"[✓] Security check cleared. Resuming chapter {ch_num:g}.")
                if ctx.on_challenge_cleared is not None:
                    try:
                        ctx.on_challenge_cleared("waf", ch_num)
                    except Exception as ex:
                        log(f"[!] Cleared callback failed: {ex}")
                continue  # retry the same chapter
            else:
                # Cloudflare: comix's check is non-interactive — a normal visit
                # mints the clearance — so waiting it out beats waiting for a
                # click. The resume buttons stay as manual overrides. As with
                # the security check, the event is cleared before the pause is
                # announced so a racing confirmation click survives.
                control.cf_event.clear()
                if ctx.on_status:
                    ctx.on_status(ch_num, "cloudflare", "waiting for clearance")
                if ctx.on_cloudflare:
                    ctx.on_cloudflare(ch_num)
                cleared = wait_for_challenge_clear(
                    page,
                    control=control,
                    kind="cloudflare",
                    log=log,
                    max_wait=ctx.waf_max_wait,
                    poll=ctx.waf_poll,
                    focus=ctx.focus,
                )
                if control.stopped:
                    if ctx.on_status:
                        ctx.on_status(ch_num, "pending", "")
                    log("[!] Stopped by user.")
                    return "pending", None, page
                if not cleared:
                    if not ctx.allow_restart:
                        # Parallel mode: restarting the shared browser would
                        # kill every other worker's upload mid-flight, so the
                        # chapter is handed back instead.
                        log(
                            "[!] Cloudflare did not clear in time. Leaving this "
                            "chapter for a sequential retry."
                        )
                        if ctx.on_status:
                            ctx.on_status(ch_num, "pending", "")
                        return "pending", None, page
                    if ctx.cf_resolved_hook is not None:
                        # One relaunch usually unsticks a check that will not
                        # die: a fresh launch re-runs the check from scratch,
                        # and the retry re-opens the upload page to trigger it.
                        # A second full window with no clearance means
                        # something is genuinely wrong — stop instead of
                        # spinning forever.
                        log(
                            "[!] Cloudflare did not clear in time. Restarting the "
                            "browser once and waiting again..."
                        )
                        try:
                            restarted = ctx.cf_resolved_hook(True)
                        except Exception as ex:
                            log(f"[!] Could not restart the browser: {ex}")
                            if ctx.on_status:
                                ctx.on_status(ch_num, "pending", "")
                            return "pending", None, page
                        control.restart_browser = False
                        if restarted is None:
                            log("[!] Browser restart produced no page — stopping.")
                            if ctx.on_status:
                                ctx.on_status(ch_num, "pending", "")
                            return "pending", None, page
                        page = restarted
                        # Re-open the upload page so the fresh session runs the
                        # check again; waiting on about:blank proves nothing.
                        try:
                            page.goto(
                                params.url, wait_until="domcontentloaded", timeout=60000
                            )
                        except Exception as ex:
                            log(f"[!] Could not re-open the upload page: {ex}")
                            if ctx.on_status:
                                ctx.on_status(ch_num, "pending", "")
                            return "pending", None, page
                        cleared = wait_for_challenge_clear(
                            page,
                            control=control,
                            kind="cloudflare",
                            log=log,
                            max_wait=ctx.waf_max_wait,
                            poll=ctx.waf_poll,
                            focus=ctx.focus,
                        )
                        if control.stopped:
                            if ctx.on_status:
                                ctx.on_status(ch_num, "pending", "")
                            log("[!] Stopped by user.")
                            return "pending", None, page
                        if not cleared:
                            log(
                                "[!] Cloudflare still did not clear after the "
                                "restart. Stopping here so nothing is skipped or "
                                "marked failed."
                            )
                            if ctx.on_status:
                                ctx.on_status(ch_num, "pending", "")
                            return "pending", None, page
                if ctx.cf_resolved_hook is not None:
                    try:
                        new_page = ctx.cf_resolved_hook(bool(control.restart_browser))
                    except Exception as ex:
                        log(f"[!] Could not refresh the browser session: {ex}")
                        if ctx.on_status:
                            ctx.on_status(ch_num, "pending", "")
                        return "pending", None, page
                    finally:
                        control.restart_browser = False
                    if new_page is not None:
                        page = new_page
                log(f"[✓] Cloudflare cleared. Resuming chapter {ch_num:g}.")
                if ctx.on_challenge_cleared is not None:
                    try:
                        ctx.on_challenge_cleared("cloudflare", ch_num)
                    except Exception as ex:
                        log(f"[!] Cleared callback failed: {ex}")
                continue  # retry the same chapter

        if not challenge_hit:
            break

    if success:
        recorder.record_success(ch_num)
        if ctx.on_status:
            ctx.on_status(ch_num, "done", "")
        return "done", None, page

    recorder.record_failure(ch_num, last_err)
    log(f"[X] Chapter {ch_num:g} failed: {last_err}")
    if ctx.on_status:
        ctx.on_status(ch_num, "failed", last_err or "")
    return "failed", last_err, page


def run_upload_batch(
    page,
    config: dict,
    params: UploadParams,
    log: Callable = print,
    on_status=None,  # (chapter_num, status, detail)
    on_chunk=None,  # (chapter_num, chunks)
    control: Control | None = None,
    on_cloudflare=None,  # (chapter_num) -> called when CF pause begins
    cf_resolved_hook=None,  # (restart_requested) -> page  (lets the caller relaunch)
    on_waf=None,  # (chapter_num) -> called when the WAF pause begins
    waf_resolved_hook=None,  # () -> page  (re-sync only; never relaunch)
    on_challenge_cleared=None,  # (kind, chapter_num) -> a gate cleared itself
    max_retries: int = 3,
    idle_timeout: int = 90,
) -> RunSummary:
    """Upload every pending chapter, one at a time. Blocks until done / stopped.

    This is the sequential engine — and the fallback used whenever parallel
    uploads are unavailable. The per-chapter work lives in _process_chapter,
    which parallel.py drives on several tabs at once.
    """

    control = control or Control()
    # "keep_window_in_background" is the default; flipping it to false restores
    # the old behaviour of raising Chrome at the start of every chapter.
    control.raise_window = not bool(config.get("keep_window_in_background", True))
    summary = RunSummary()

    ctx = build_chapter_context(
        config,
        params,
        log=log,
        on_status=on_status,
        on_chunk=on_chunk,
        control=control,
        max_retries=max_retries,
        idle_timeout=idle_timeout,
        on_cloudflare=on_cloudflare,
        cf_resolved_hook=cf_resolved_hook,
        on_waf=on_waf,
        waf_resolved_hook=waf_resolved_hook,
        on_challenge_cleared=on_challenge_cleared,
    )

    pending = pending_chapters(params, ctx.recorder)
    log(f"[i] {len(pending)} chapter(s) queued.")

    idx = 0
    while idx < len(pending):
        if control.stopped:
            log("[!] Stopped by user.")
            break

        if control.pause_event.is_set():
            log("[‖] Paused.")
            control.pause_event.wait(0.5)
            continue

        ch_num, file_path = pending[idx]
        status, err, page = _process_chapter(page, ch_num, file_path, ctx)

        if status == "done":
            summary.processed += 1
            summary.succeeded += 1
            if idx < len(pending) - 1:
                log(f"[*] Waiting {params.delay}s before next chapter...")
                for _ in range(int(params.delay)):
                    if control.stopped:
                        break
                    time.sleep(1)
        elif status == "failed":
            summary.processed += 1
            summary.failed += 1
            summary.failures.append((ch_num, Path(file_path).name, err))
        else:
            # "pending": nothing was recorded — stop, exactly like before.
            break

        idx += 1

    return summary


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
