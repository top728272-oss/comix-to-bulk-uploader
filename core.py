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

try:
    import urbproxy
except ImportError:
    urbproxy = None  # local-only VPN client; everything runs direct without it

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
PROFILE_DIR = (BASE_DIR / "browser_profile").resolve()
LIBRARY_PATH = BASE_DIR / "series_library.json"

ARCHIVE_EXTS = {".zip", ".cbz", ".cbr", ".rar", ".7z"}
SITE_ROOT = "https://comix.to"

# Chapters uploading at once, each in its own tab. 1 = fully sequential.
DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 8

# Cap on an HTTP 429 Retry-After. Kept under the parallel stall watchdog so
# backing off can't be mistaken for a wedged tab.
MAX_RATE_LIMIT_BACKOFF_SECONDS = 60.0

# Per-chapter "no chunk progress" budget, in seconds (config "timeout_seconds").
# Was hardcoded to 90 s, too short when several large chapters share one uplink.
DEFAULT_IDLE_TIMEOUT = 180

# Where challenge evidence dumps land. A module global so tests can monkeypatch it.
CHALLENGE_DEBUG_DIR = BASE_DIR / ".challenge_debug"

# Cookie names that can carry a challenge clearance worth persisting.
CLEARANCE_COOKIE_HINTS = ("clearance", "challenge")

# Cloudflare error-page markers (502/521/522/...). Test these before the
# challenge detector: those pages render the same div#cf-wrapper a challenge
# does.
#
# "ray id" is deliberately NOT here. Every Cloudflare page footer carries one,
# challenges included, so matching it made is_cloudflare_error_page() report a
# live challenge as an origin outage - which then suppressed is_cloudflare_active()
# and left a challenged tab looking like a page that simply had no search results.
# Extract the Ray ID with _RAY_ID_RE instead, when it is actually needed.
CF_ERROR_MARKERS = (
    ".cf-error-details",
    "#cf-error-details",
    "error 5",
    "connection timed out",
    "web server is down",
    "webserver is down",
    "origin is unreachable",
    "hosting server",
)

# Cloudflare's WAF "access denied" page (Error 1020). A hard IP/path decision
# taken before any cookie is read: neither a solvable challenge nor an outage.
CF_BLOCK_MARKERS = (
    "you have been blocked",
    "you are unable to access",
    "error 1020",
    "why have i been blocked",
    "blocked by cloudflare",
    "this website is using a security service",
)

# "Cloudflare Ray ID: <hex>" - the handle Cloudflare support needs.
_RAY_ID_RE = re.compile(r"ray id\W{0,4}([0-9a-f]{8,})", re.I)

# Cloudflare challenge-only markers (ported from Kotatsu-Redo's detector).
# Safe to match anywhere: these never appear on a normal page.
CF_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "cf-chl-opt",
    "_cf_chl_opt",
    "challenge-platform",
)
# A live interstitial's title. Checked ahead of every error-page marker: a
# challenge and a 5xx look alike in the body text, but the title tells them
# apart unambiguously.
CF_CHALLENGE_TITLE_RE = re.compile(r"^\s*(just a moment|attention required!)\b", re.I)
# Generic Turnstile markers. These CAN appear on a legitimate page (comix's own
# login/upload widget), so they only count when no upload form is rendered.
CF_TURNSTILE_MARKERS = (
    "cf-turnstile",
    "challenges.cloudflare.com",
    "turnstile-wrapper",
)

# The "Verifying you are human" / stuck state: never clears by refreshing, so
# past the grace window the user must paste fresh cookies.
CF_VERIFYING_MARKERS = (
    "verifying you are human",
    "verify you are human",
    "check for human",
    "checking your browser",
)
CF_VERIFYING_GRACE_SECONDS = 45.0

# Low-level network failure strings (Playwright wraps them as "net::ERR_...").
# A page that failed this way is a dead origin, not a challenge.
SERVER_DOWN_MARKERS = (
    "net::err_connection_refused",
    "net::err_connection_timed_out",
    "net::err_connection_closed",
    "net::err_connection_reset",
    "net::err_name_not_resolved",
    "net::err_empty_response",
    "connection refused",
    "connection timed out",
)

# The upload form's file input. Used both as a Playwright locator and as a
# plain CSS selector for the CDP file attach (see attach_upload_file).
FILE_INPUT_SELECTOR = "input.upage-drop__input, input[type='file']"

# Installed before the file attach: records the page's own `change` event, the
# only proof it took the file (the drop zone is often swapped out). Re-bound
# every call so a replaced document can't leave a stale listener behind.
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

# Returns "accepted" | "empty" | "gone". Run via a locator handle, which calls
# ``(element, arg)``, so the selector is inlined rather than passed in.
FILE_PROBE_STATE_JS = """
() => {
  if (window.__comixFileProbe === 1) return 'accepted';
  const input = document.querySelector(%s);
  if (!input) return 'gone';
  if (input.files && input.files.length > 0) return 'accepted';
  return 'empty';
}
""" % json.dumps(FILE_INPUT_SELECTOR)

# How long to wait for the page to acknowledge the file.
FILE_PROBE_POLL_TRIES = 10
FILE_PROBE_POLL_SECONDS = 0.2


class VerificationRequired(Exception):
    """Raised when the site demands a human check before it will continue.

    ``kind`` is always "cloudflare"; refreshing clears it, and once refreshing
    stops working the user pastes fresh cookies.
    """

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message)
        self.kind = kind


class CloudflareBlockException(VerificationRequired):
    """Raised when a Cloudflare interstitial is detected."""

    def __init__(self, message: str = "") -> None:
        super().__init__("cloudflare", message)


class StopRequested(Exception):
    """Raised when the user aborts a running job."""


class ServerDownError(Exception):
    """Raised when the comix origin itself is unreachable (5xx / connection).

    Deliberately distinct from a Cloudflare challenge: a dead origin fails
    every chapter, so the batch is stopped instead of being retried as if a
    human could clear it.
    """


class AccessBlockedError(VerificationRequired):
    """Cloudflare's WAF blocked this IP on the upload path (Error 1020).

    No cookie clears it (IP/path decision), so the batch stops and the user
    changes egress IP or sets ``config.proxy``. ``ray_id`` carries the Ray ID.
    """

    def __init__(self, message: str = "", ray_id: str | None = None) -> None:
        super().__init__("access_blocked", message)
        self.ray_id = ray_id


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


def challenge_config(config: dict) -> dict:
    """The "challenge" config block, with the legacy "waf" key still honoured.

    The block used to be called "waf" (comix's own captcha) and also fed the
    Cloudflare wait/poll timings. WAF is gone, so it is "challenge" now; old
    config files keep working because "challenge" simply wins when both exist.
    """
    merged = dict(config.get("waf") or {})
    merged.update(config.get("challenge") or {})
    return merged


# Defaults for the automatic Cloudflare solver (see solver.py).
CLOUDFLARE_DEFAULTS = {
    "auto_solve": True,
    "preflight_solve": False,
    "solve_max_wait_seconds": 90,
    "solve_window_mode": "offscreen",
    "solve_focus_after_seconds": 15,
}


def cloudflare_config(config: dict) -> dict:
    """The "cloudflare" config block merged over the defaults.

    Tolerates a missing/partial block so older config files keep working.
    """
    merged = dict(CLOUDFLARE_DEFAULTS)
    block = config.get("cloudflare") or {}
    if isinstance(block, dict):
        merged.update(block)
    return merged


def proxy_config(config: dict) -> dict | None:
    """The "proxy" config block as Playwright's ``proxy`` dict, or None.

    Blank fields are dropped; a missing/blank block means connect directly. The
    SAME proxy must be given to the solver (cf_clearance is bound to the IP that
    earned it). Chromium only sends credentials for HTTP(S) proxies - an
    authenticated SOCKS5 upstream needs a local unauthenticated bridge.
    """
    block = config.get("proxy") or {}
    if not isinstance(block, dict):
        return None
    server = str(block.get("server") or "").strip()
    if not server:
        return None
    proxy = {"server": server}
    for key in ("username", "password", "bypass"):
        value = block.get(key)
        if value:
            proxy[key] = str(value)
    return proxy


def effective_proxy(config: dict, refresh_vpn: bool = True) -> dict | None:
    """The proxy a new browser (or a solver run) should use.

    The local, gitignored urbproxy.py wins while its VPN is active; otherwise
    the config.json "proxy" block applies. The solver passes refresh_vpn=False
    so it shares the token the running browser already holds (cf_clearance is
    bound to the egress IP that earned it).
    """
    if urbproxy is not None and urbproxy.is_active():
        try:
            return urbproxy.playwright_proxy(refresh=refresh_vpn)
        except Exception:
            pass
    return proxy_config(config)


def vpn_gateway_active() -> bool:
    """True while traffic is routed through the UrbanVPN gateway."""
    if urbproxy is None:
        return False
    try:
        return bool(urbproxy.is_active())
    except Exception:
        return False


def effective_concurrency(config: dict) -> int:
    """Worker count this network can actually sustain.

    Parallel workers are driven over ``connect_over_cdp``, which takes no proxy
    argument - so those connections carry no gateway credentials. Chrome hits
    the gateway's 407 and the auth handshake stalls, leaving every worker tab
    parked on about:blank while the launch tab works fine. Measured: direct
    connection, 2 worker tabs navigate in under a second each; behind the
    gateway they never resolve.

    It is NOT a gateway tunnel limit - four concurrent CONNECT tunnels on one
    token all succeed at the socket level - and a second token would not help,
    because the missing piece is credentials in the worker's connection rather
    than the credential itself.

    One page is one connection, so the gateway means sequential uploads. A
    plain ``config.proxy`` is not affected and keeps its configured count.
    """
    try:
        n = validate_concurrency(config.get("concurrency", DEFAULT_CONCURRENCY))
    except ConfigError:
        n = 1
    if n > 1 and vpn_gateway_active():
        return 1
    return n


def rotate_vpn_egress(log: Callable = print) -> bool:
    """Move the VPN to a different egress after this IP was banned.

    False when there is no VPN, no alternative gateway, or the rotation failed.
    The caller must relaunch the browser afterwards: the proxy is a launch
    flag, so a running Chrome keeps the old egress no matter what we mint here.
    """
    if urbproxy is None or not urbproxy.is_active():
        return False
    try:
        blocked = {urbproxy.current_ip()} - {""}
        return bool(urbproxy.rotate_egress(avoid=blocked, log=log))
    except Exception as ex:
        log(f"[!] Could not rotate the VPN egress: {ex}")
        return False


def set_config_cookies(config: dict, values: dict) -> bool:
    """Update (or append) named cookies in ``config`` and save it.

    ``values`` maps cookie name -> value; empty/None values are skipped so a
    blank GUI field cannot wipe an existing cookie. Returns True when
    something changed.
    """
    changed = False
    by_name = {c.get("name"): c for c in config.setdefault("cookies", [])}
    for name, value in values.items():
        value = (value or "").strip()
        if not value:
            continue
        if name in by_name:
            if by_name[name].get("value") != value:
                by_name[name]["value"] = value
                changed = True
        else:
            config["cookies"].append({"name": name, "value": value, "url": SITE_ROOT})
            changed = True
    if changed:
        save_config(config)
    return changed


def apply_cookies_to_context(context, cookies: list) -> bool:
    """Inject cookies into a live browser context (best-effort, never raises)."""
    if context is None or not cookies:
        return False
    try:
        context.add_cookies(cookies)
        return True
    except Exception:
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


# --------------------------------------------------------------------------
# Chapter title mapping (rename.py's chapter_mapping.txt)
# --------------------------------------------------------------------------

_MAP_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*->\s*(.+?)\s*$")

# A leading folder counter with an optional chapter token ("002 1话"). Arc
# labels like "番外-" or "Adventure Arc 03." are kept on purpose.
_MAP_NUM_PREFIX = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*"
    r"(?:"
    r"(?:ch(?:apter)?\.?|第)?\s*\d+(?:\.\d+)?\s*话?"
    r"[\s.\-–—:：、]*"
    r")?",
    re.IGNORECASE,
)

_MAP_LEADING_SEP = re.compile(r"^[\s.\-–—:：、]+")


def parse_chapter_mapping(text: str) -> dict:
    """Parse rename.py's chapter_mapping.txt into ``{chapter_key: raw_title}``.

    Only ``new -> original`` lines are kept. Keys are normalized as ``f"{n:g}"``
    so they match scan_folder() numbers ("2" and "002" both key as "2", and
    decimals survive). First mapping wins on duplicates.
    """
    mapping = {}
    for line in text.splitlines():
        match = _MAP_LINE.match(line)
        if not match:
            continue
        key = f"{float(match.group(1)):g}"
        if key not in mapping:
            mapping[key] = match.group(2)
    return mapping


def clean_mapped_title(raw: str, strip_numbers: bool = True) -> str:
    """Turn a mapping's right-hand side into the title to upload.

    With ``strip_numbers`` on, the leading folder counter and chapter token are
    removed ("002 1话.倒霉体质" -> "倒霉体质"); arc prefixes ("番外-",
    "Adventure Arc 03.") are preserved.
    """
    title = raw.strip()
    if strip_numbers:
        title = _MAP_NUM_PREFIX.sub("", title, count=1)
        title = _MAP_LEADING_SEP.sub("", title)
    return title.strip()


def resolve_chapter_title(params: UploadParams, ch_num) -> str:
    """Pick the title for one chapter: mapping hit, else pattern, else "".

    Mapping keys are looked up with the same ``f"{n:g}"`` normalization the
    parser uses, so int and float chapter numbers hit the same entry.
    """
    title_map = getattr(params, "title_map", None)
    if title_map:
        raw = title_map.get(f"{float(ch_num):g}")
        if raw is not None:
            return clean_mapped_title(raw, getattr(params, "strip_map_numbers", True))
    pattern = getattr(params, "title_pattern", "") or ""
    return pattern.format(ch=ch_num) if pattern else ""


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
    moved rather than abandoned - losing them would re-upload everything.
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
    touch it from several - hence the lock around every mutate-and-save pair.
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
            # A stale retry must never contradict a recorded success.
            if str(ch_num) in self.history:
                return
            self.failed_history[str(ch_num)] = err or "Unknown error"
            save_failed(self.failed_file, self.failed_history)


# --------------------------------------------------------------------------
# Browser helpers
# --------------------------------------------------------------------------


# Hides the webdriver flag the site would otherwise detect.
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
    upload workers can attach to this same browser from their own threads -
    they then share the profile, the login and the Cloudflare clearance cookie.
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
        # None means "connect directly". effective_proxy returns the active
        # UrbanVPN gateway when toggled on, else the config proxy. The solver
        # must be given the same egress: cf_clearance is bound to the IP that
        # earned it.
        proxy=effective_proxy(config),
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
    """Auto-close popups on ``context``.

    Only a tab with an opener (``window.open`` / ``target=_blank``) is a popup;
    tabs from ``context.new_page()`` have none. That distinction keeps parallel
    workers alive: every CDP connection sees every tab, so closing opener-less
    tabs would kill the other workers' fresh tabs. Trade-off: ``rel=noopener``
    popups are left open (rare, harmless).
    """

    def handle_popup(new_page):
        if new_page == page:
            return
        try:
            # No opener => opened programmatically (a parallel worker's tab,
            # or our own), not by the page. Never close those.
            if new_page.opener() is None:
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
    are unavailable" - callers fall back to sequential and never crash.
    """
    path = PROFILE_DIR / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return int(path.read_text(encoding="utf-8").splitlines()[0].strip())
        except Exception:
            time.sleep(0.25)
    return None


# Every DOM read that feeds a decision must carry a deadline: page.title(),
# page.content() and page.evaluate() take no timeout, so a wedged tab blocks the
# calling thread (and, in parallel mode, the batch) forever. Route the detectors
# through locator actions, which do carry a deadline.
SAFE_READ_TIMEOUT_MS = 1500


def _safe_title(page, timeout_ms: int = SAFE_READ_TIMEOUT_MS) -> str:
    """The tab's ``<title>``, bounded. Empty string when unreadable/wedged."""
    try:
        return (
            page.locator("title").first.text_content(timeout=timeout_ms) or ""
        ).strip()
    except Exception:
        return ""


def _safe_html(page, timeout_ms: int = SAFE_READ_TIMEOUT_MS, lower: bool = True) -> str:
    """The tab's document HTML, bounded. Empty string when unreadable/wedged.

    Reads ``<html>`` rather than ``<body>`` because the Cloudflare markers live
    in ``<head>``/``<script>`` as often as in the body.
    """
    try:
        html = page.locator("html").first.inner_html(timeout=timeout_ms) or ""
    except Exception:
        return ""
    return html.lower() if lower else html


def _safe_visible(page, selector: str, timeout_ms: int = SAFE_READ_TIMEOUT_MS) -> bool:
    """Bounded element-visibility check. False when absent OR unreadable."""
    try:
        return bool(page.locator(selector).first.is_visible(timeout=timeout_ms))
    except Exception:
        return False


def _bounded_eval(
    page, expression: str, arg=None, timeout_ms: int = SAFE_READ_TIMEOUT_MS
):
    """``page.evaluate`` with a deadline.

    ``page.evaluate`` has no timeout and blocks forever on a wedged renderer; a
    locator's ``evaluate`` does, so this routes through a handle on ``<html>``.
    The expression is invoked as ``expression(element, arg)``.
    """
    return page.locator("html").first.evaluate(expression, arg, timeout=timeout_ms)


def _page_text(page, timeout_ms: int = 1000) -> str:
    """Best-effort lowercased body text; empty string when unreadable."""
    try:
        return (page.locator("body").inner_text(timeout=timeout_ms) or "").lower()
    except Exception:
        return ""


def page_is_responsive(page, timeout_ms: int = 2000) -> bool:
    """False when the tab's renderer does not answer a bounded read.

    The tab-liveness probe. ``about:blank`` counts as responsive (only a
    *failed* read is dead); ``get_attribute`` avoids serialising the whole
    document on a check that runs once per tab per poll.
    """
    if page is None:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False
    try:
        page.locator("html").first.get_attribute("lang", timeout=timeout_ms)
        return True
    except Exception:
        return False


def is_cloudflare_access_block(page) -> bool:
    """True on Cloudflare's WAF "access denied" page (Error 1020).

    A hard IP/path decision taken before any cookie is read: no ``cf_clearance``
    can clear it, and it is NOT an origin outage. Callers change the egress IP
    (or pace the run) rather than wait or re-solve.
    """
    text = _safe_title(page).lower() + " " + _page_text(page)
    return any(marker in text for marker in CF_BLOCK_MARKERS)


def cloudflare_ray_id(page) -> str | None:
    """The "Cloudflare Ray ID: <hex>" token on the current page, if any."""
    haystack = _safe_title(page) + " " + _page_text(page)
    match = _RAY_ID_RE.search(haystack)
    return match.group(1) if match else None


def is_cloudflare_error_page(page) -> bool:
    """True when the tab is a Cloudflare *error* page (origin down), not a check.

    Tested before the challenge detector because Cloudflare's 5xx pages render
    the same ``div#cf-wrapper`` a challenge does. The WAF block page is
    excluded, and so is a live challenge: the two must never be confused, or a
    challenge reads as an outage and nothing tries to solve it.
    """
    if _safe_visible(page, ".cf-error-details, #cf-error-details"):
        if not is_cloudflare_access_block(page):
            return True
    text = _safe_title(page).lower() + " " + _page_text(page)
    if is_cloudflare_access_block(page):
        return False
    if CF_CHALLENGE_TITLE_RE.match(_safe_title(page).lower()):
        # "Just a moment..." wins over any error wording a challenge may carry.
        return False
    return any(marker in text for marker in CF_ERROR_MARKERS)


def _looks_like_upload_form(page) -> bool:
    """True when the real comix upload form is rendered on this tab.

    Keeps the generic Turnstile markers from flagging comix's own widget.
    """
    return _safe_visible(page, ".upage-field, input.upage-drop__input")


def is_cloudflare_active(page) -> bool:
    """True while a real Cloudflare interstitial is showing.

    Cloudflare's error pages and the WAF block page share its markers but are
    not solvable challenges, so both are excluded first.
    """
    if is_cloudflare_access_block(page):
        return False
    if is_cloudflare_error_page(page):
        return False
    title = _safe_title(page).lower()
    if CF_CHALLENGE_TITLE_RE.match(title):
        return True
    if "attention required! | cloudflare" in title:
        return True
    if _safe_visible(page, "#challenge-stage, #challenge-running, #turnstile-wrapper"):
        return True
    # Challenge-only markers, scanned across the HTML too (they live in <head>).
    text = _page_text(page)
    if any(marker in text for marker in CF_CHALLENGE_MARKERS):
        return True
    # Generic Turnstile markers: only when this is not a working upload form.
    if _looks_like_upload_form(page):
        return False
    haystack = text + " " + _safe_html(page)
    return any(marker in haystack for marker in CF_TURNSTILE_MARKERS)


def is_cloudflare_verifying(page) -> bool:
    """True while a Cloudflare page sits on the "Verifying you are human" step.

    This state no longer clears on a refresh, so the wait loop gives up early
    and asks for fresh cookies.
    """
    text = _safe_title(page).lower() + " " + _page_text(page)
    return any(marker in text for marker in CF_VERIFYING_MARKERS)


def detect_challenge(page) -> str | None:
    """Return "cloudflare" or None for the tab's current state."""
    return "cloudflare" if is_cloudflare_active(page) else None


def _kind_label(kind: str) -> str:
    if kind == "access_blocked":
        return "Cloudflare access block"
    return "Cloudflare challenge"


def _looks_like_server_down(page, exc_text: str) -> bool:
    """True when a failure means the origin is down rather than a challenge.

    Combines the exception text (``net::ERR_...``) with the page's error state.
    """
    text = (exc_text or "").lower()
    if any(marker in text for marker in SERVER_DOWN_MARKERS):
        return True
    try:
        return is_cloudflare_error_page(page)
    except Exception:
        return False


# --------------------------------------------------------------------------
# Window focus
# --------------------------------------------------------------------------
# Routine automation never raises the Chrome window; the one exception is a
# verification gate, which needs a human. These helpers are that exception.


def focus_challenge_window(
    page, config: dict | None = None, log: Callable = print
) -> bool:
    """Restore + foreground the Chrome window holding a verification gate.

    Returns False when disabled, off Windows, or when the window can't be
    found - never raises, so a focus failure cannot break a run.
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


def _raise_if_access_blocked(page, path: str = "the upload path") -> None:
    """Raise AccessBlockedError when Cloudflare's WAF block page is showing.

    Separate from ``require_no_challenge`` because a block is not a gate: there
    is nothing to wait out and no cookie to refresh. The message has to say so,
    or the user is sent to solve a captcha that does not exist.

    ``path`` names what was blocked, so the message does not accuse the upload
    path of something the user asked a search to do.
    """
    if not is_cloudflare_access_block(page):
        return
    ray = cloudflare_ray_id(page)
    raise AccessBlockedError(
        f"Cloudflare blocked this IP on {path}"
        + (f" (Ray ID {ray})" if ray else "")
        + ". No cookie can clear a WAF block - switch network (VPN) or set "
        "config.proxy, then retry.",
        ray_id=ray,
    )


def _confirm_block(page, grace: float = 3.0) -> bool:
    """Wait briefly for a blocked API call to surface a challenge in the DOM.

    A bare HTTP 403/503 is ambiguous - it can be an expired session rather than
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
    folder = CHALLENGE_DEBUG_DIR / f"{stamp}_{kind}"
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
    html = _safe_html(page, lower=False)
    try:
        meta["url"] = page.url
        meta["title"] = _safe_title(page)
        meta["frames"] = [f.url for f in page.frames]
        meta["ray_id"] = cloudflare_ray_id(page)
    except Exception:
        pass
    if tracker:
        try:
            meta["tracker"] = dict(tracker)
        except Exception:
            pass

    for name, write in (
        ("page.html", lambda p: p.write_text(html, encoding="utf-8")),
        (
            "meta.json",
            lambda p: p.write_text(json.dumps(meta, indent=2), encoding="utf-8"),
        ),
    ):
        try:
            write(folder / name)
        except Exception:
            pass
    # Only when the bounded read actually succeeded: screenshot() has no
    # timeout either, so on a wedged tab it would block the dump.
    if html:
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
    """True when a tab shows a real page - not about:blank, not an error dump.

    Kept loose: the gate detectors decide what is a challenge; this only stops a
    blank/5xx tab from being read as "the challenge is gone".
    """
    try:
        if (page.url or "").strip() in ("", "about:blank"):
            return False
        title = _safe_title(page).lower()
        body = _page_text(page)
        errors = (
            "bad gateway",
            "gateway time-out",
            "gateway timeout",
            "service unavailable",
            "internal server error",
        )
        if any(marker in title or marker in body for marker in errors):
            return False
        return bool(body)
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


def _tab_gate_state(page) -> dict:
    """One bounded snapshot of a tab's gate state.

    ``readable`` is False when the tab did not answer a bounded read, so one
    wedged tab cannot stall every worker's challenge wait.
    """
    if not page_is_responsive(page):
        return {
            "readable": False,
            "challenged": False,
            "verifying": False,
            "ready": False,
        }
    challenged = is_cloudflare_active(page)
    return {
        "readable": True,
        "challenged": challenged,
        "verifying": challenged and is_cloudflare_verifying(page),
        "ready": (not challenged) and _page_ready_after_challenge(page),
    }


def wait_for_challenge_clear(
    page,
    control: Control | None = None,
    log: Callable = print,
    max_wait: int = 600,
    poll: float = 2.0,
    settle: int = 2,
    cf_reload_after: float = 30.0,
    cf_reload_every: float = 15.0,
    focus: bool = True,
    stuck_grace: float = CF_VERIFYING_GRACE_SECONDS,
    on_heartbeat: Callable | None = None,
) -> bool:
    """Block until the Cloudflare gate is gone from every tab, or the window expires.

    Polls every site tab (a clearance nobody watches never ends the wait) and
    requires ``settle`` consecutive clean reads, where a clean read means every
    tab shows real content (not blank or 5xx).

    A check stuck on "Verifying you are human" past ``stuck_grace`` will not
    clear on its own, so it gives up early and raises ``control.cf_stuck_event``
    for the caller to request fresh cookies; otherwise a stalled check is
    reloaded (the fresh visit is what mints a clearance).

    ``on_heartbeat`` (optional) is called once per poll so a parallel worker can
    prove its tab is alive-but-waiting rather than wedged.
    """
    manual = control.cf_event if control is not None else None

    deadline = time.time() + max_wait
    clean = 0
    challenge_since: float | None = None
    verifying_since: float | None = None
    last_reload = 0.0
    warned_unreadable = False

    while time.time() < deadline:
        if control is not None:
            if control.stopped or control.aborted:
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
            # One bounded probe per tab per poll. Every read carries a deadline,
            # so a wedged renderer surfaces as "unreadable" instead of blocking
            # this loop (and, in parallel mode, every worker in it).
            states = {p: _tab_gate_state(p) for p in pages}
            unreadable = [p for p, st in states.items() if not st["readable"]]
            if unreadable and not warned_unreadable:
                warned_unreadable = True
                log(
                    f"[i] {len(unreadable)} tab(s) stopped answering during the "
                    "challenge wait - ignoring them so the wait can still finish."
                )
            challenged = [p for p, st in states.items() if st["challenged"]]
            if challenged:
                clean = 0
                now = time.time()
                if challenge_since is None:
                    challenge_since = now
                    # First sighting of the gate: surface the window.
                    if focus:
                        _focus_for_challenge(page, challenged, log)

                if any(states[p]["verifying"] for p in challenged):
                    if verifying_since is None:
                        verifying_since = now
                    elif now - verifying_since > stuck_grace:
                        # Refreshing no longer clears it - hand it to the user.
                        log(
                            "[!] Cloudflare is stuck on 'Verifying you are "
                            "human'. Refreshing no longer clears it - paste a "
                            "fresh cf_clearance cookie and try again."
                        )
                        if control is not None:
                            control.cf_stuck_event.set()
                        return False
                else:
                    verifying_since = None

                if (
                    now - challenge_since > cf_reload_after
                    and now - last_reload > cf_reload_every
                ):
                    # A spinning check never finishes; reload the stuck tab
                    # (prefer the tracked page so the retry lands on a live tab).
                    target = next((p for p in challenged if p == page), challenged[0])
                    try:
                        log("[i] The Cloudflare check seems stuck - reloading it.")
                        target.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    last_reload = time.time()
                    challenge_since = time.time()
            elif pages and all(st["ready"] for st in states.values() if st["readable"]):
                challenge_since = None
                verifying_since = None
                clean += 1
                if clean >= settle:
                    return True
            else:
                challenge_since = None
                verifying_since = None
                clean = 0

        if on_heartbeat is not None:
            try:
                on_heartbeat()
            except Exception:
                pass

        slept = 0.0
        while slept < poll:
            if control is not None and (
                control.stopped
                or control.aborted
                or (manual is not None and manual.is_set())
            ):
                break
            # Bounded reads so the next guard clears on redirects rather than the old URL.
            _pump_browser(page, 0.25)
            slept += 0.25

    return False


# --------------------------------------------------------------------------
# Site search (runs through the real browser: the API needs a per-request token)
# --------------------------------------------------------------------------

# One attempt is a navigation plus a wait for the popup, so this is bounded
# to well under a minute even when every attempt has to be solved.
SEARCH_MAX_ATTEMPTS = 3
# The popup intermittently renders nothing, and its empty marker is transient
# while a long query is still being typed. Both are indistinguishable from a
# real miss on a single pass, so an unmarked or empty result is confirmed with a
# second pass before it is believed.
SEARCH_RENDER_RETRIES = 2
# How long the popup may take to produce results. Long queries are typed
# character by character, and the site's request is debounced behind that, so
# this has to outlast the typing itself.
SEARCH_RESULT_TIMEOUT_SECONDS = 20.0
# How long the empty marker must hold still before the pass calls it a miss.
SEARCH_EMPTY_SETTLE_SECONDS = 2.5
# Let the result rows finish painting before reading them.
SEARCH_RESULT_SETTLE_MS = 300
SEARCH_POLL_MS = 200


class _SearchRenderTimeout(RuntimeError):
    """The results popup rendered neither results nor its empty-state marker."""


class _SearchNoMatch(RuntimeError):
    """The popup settled on its empty-state marker - not yet believed."""


def search_site(
    page,
    query: str,
    limit: int = 15,
    log: Callable | None = None,
    *,
    cf_solver_hook: Callable | None = None,
    max_attempts: int = SEARCH_MAX_ATTEMPTS,
):
    """Use the site's own search box and scrape the rendered result list.

    Returns a list of dicts: {hid, name, type, chapters, rating, url}

    ``cf_solver_hook`` is the engine's ``(context, log) -> bool`` Cloudflare
    solver. When a challenge blocks the search it is called and the search is
    retried, so a gated query resolves itself instead of asking for a human.

    An egress IP ban (``AccessBlockedError``) is deliberately NOT solved: no
    cookie clears it, so it propagates for the caller to rotate the VPN or
    report the ban.

    A popup that renders nothing, or that shows its empty marker, is retried
    before it counts. The site's empty marker doubles as a transient state while
    a long query is still being typed, so believing the first one is what made
    longer queries come back as zero results.
    """
    query = (query or "").strip()
    if not query:
        return []
    logger = log if log is not None else (lambda _message: None)
    attempts = max(1, int(max_attempts or 1))
    render_retries = 0

    for attempt in range(1, attempts + 1):
        try:
            return _search_site_once(page, query, limit, logger)
        except AccessBlockedError:
            # Terminal for this IP. Let the caller rotate the egress.
            raise
        except VerificationRequired as vreq:
            if cf_solver_hook is None or attempt >= attempts:
                raise
            logger(
                f"[i] Cloudflare blocked the search - running the solver "
                f"(attempt {attempt}/{attempts})."
            )
            try:
                solved = bool(cf_solver_hook(page.context, logger))
            except Exception as ex:
                logger(f"[!] Cloudflare solver failed: {ex}")
                solved = False
            if not solved:
                raise vreq
        except _SearchNoMatch:
            render_retries += 1
            if attempt >= attempts or render_retries >= SEARCH_RENDER_RETRIES:
                logger(f"Search '{query}': no matches.")
                return []
            logger(
                f"[i] The popup showed no results - confirming "
                f"(attempt {attempt}/{attempts})."
            )
        except _SearchRenderTimeout:
            render_retries += 1
            if attempt >= attempts or render_retries >= SEARCH_RENDER_RETRIES:
                logger(
                    "[i] The search popup never rendered - treating it as no "
                    "results."
                )
                return []
            logger(
                f"[i] The search popup did not render - retrying "
                f"({attempt}/{attempts})."
            )

    return []


def _search_site_once(page, query: str, limit: int, log: Callable):
    """One navigation plus one popup render. Raises rather than returning [] on
    a gate, so ``search_site`` can tell "blocked" apart from "no matches"."""
    page.goto(SITE_ROOT, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)
    # Before the challenge check: a ban is terminal for this IP and must not be
    # reported as a solvable verification screen.
    _raise_if_access_blocked(page, "the search path")
    require_no_challenge(
        page, "A verification screen blocked the site while searching."
    )

    toggle = page.locator("button.search-toggle").first
    try:
        if toggle.is_visible(timeout=SAFE_READ_TIMEOUT_MS):
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

    return _await_search_results(page, query, limit, log)


def _await_search_results(page, query: str, limit: int, log: Callable):
    """Wait for the popup to settle, then scrape it.

    ``.search-pop__empty`` is NOT a completion signal on its own. While a long
    query is still being typed the site renders it for the prefixes typed so far
    and swaps in the real results a moment later - measured at roughly 800 ms on
    a 42-character query, and wider on a slow egress. So an empty popup only
    raises once it has held still for ``SEARCH_EMPTY_SETTLE_SECONDS``, and even
    then ``_SearchNoMatch`` asks the caller to confirm before believing it.

    Raises ``_SearchRenderTimeout`` when the popup never rendered at all, and
    re-checks the Cloudflare gates before that, because the popup's own request
    can be blocked while the document still looks fine.
    """
    deadline = time.time() + SEARCH_RESULT_TIMEOUT_SECONDS
    empty_since: float | None = None

    while time.time() < deadline:
        if _safe_visible(page, ".search-pop__item--result"):
            page.wait_for_timeout(SEARCH_RESULT_SETTLE_MS)
            rows = _scrape_search_rows(page, query, limit, log)
            if rows:
                return rows
        elif _safe_visible(page, ".search-pop__empty"):
            now = time.time()
            if empty_since is None:
                empty_since = now
            elif now - empty_since >= SEARCH_EMPTY_SETTLE_SECONDS:
                # Not believed yet: search_site confirms with a second pass
                # before reporting this as a genuine miss.
                raise _SearchNoMatch(query)
        else:
            empty_since = None
        page.wait_for_timeout(SEARCH_POLL_MS)

    _raise_if_access_blocked(page, "the search path")
    kind = detect_challenge(page)
    if kind:
        raise _exception_for(
            kind, "A verification screen blocked the site while searching."
        )
    raise _SearchRenderTimeout(query)


def _scrape_search_rows(page, query: str, limit: int, log: Callable) -> list:
    """Read the rendered results out of the popup.

    The one deliberate un-bounded read: eval_on_selector_all takes no timeout,
    but it is read-only and the caller only reaches it after a bounded poll
    proved the renderer is answering and that results are present.
    """
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
                    (
                        re.search(r"\b(MANGA|MANHWA|MANHUA|OTHER|NOVEL)\b", meta)
                        or [None, ""]
                    )[1]
                    if re.search(r"\b(MANGA|MANHWA|MANHUA|OTHER|NOVEL)\b", meta)
                    else ""
                ),
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
    title = _safe_title(page)
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
    # Chapter titles from rename.py's chapter_mapping.txt. While set, map
    # hits win over title_pattern; misses still fall back to the pattern.
    title_map: dict | None = None
    strip_map_numbers: bool = True


@dataclass
class Control:
    stop_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    cf_event: threading.Event = field(default_factory=threading.Event)
    # Set when Cloudflare is stuck on "Verifying you are human" and needs the
    # user to paste fresh cookies; cleared by the caller after it reacts.
    cf_stuck_event: threading.Event = field(default_factory=threading.Event)
    # Set when the batch must stop because the origin is down (or Cloudflare
    # cannot be cleared). Distinct from a user stop.
    abort_event: threading.Event = field(default_factory=threading.Event)
    abort_reason: str = ""
    restart_browser: bool = False
    # Off by default: draining a queue is not a reason to steal the desktop.
    # Only a verification challenge is (see focus_challenge_window).
    raise_window: bool = False

    def reset(self) -> None:
        self.stop_event.clear()
        self.pause_event.clear()
        self.cf_event.clear()
        self.cf_stuck_event.clear()
        self.abort_event.clear()
        self.abort_reason = ""
        self.restart_browser = False

    def request_stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.cf_event.set()

    def request_abort(self, reason: str = "") -> None:
        """Stop the batch because of an infrastructure failure, not a user stop."""
        if reason:
            self.abort_reason = reason
        self.abort_event.set()

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    @property
    def aborted(self) -> bool:
        return self.abort_event.is_set()


def attach_upload_file(page, file_input, file_path, log: Callable = print) -> None:
    """Attach the archive to the upload form's file input.

    Uses plain CDP (``DOM.setFileInputFiles``) rather than
    ``set_input_files``: Playwright's path-based attach goes through a private
    CDP method that is ~150x slower on a parallel worker's ``connect_over_cdp``
    connection (~16 s), randomly exceeding the action timeout. Playwright's call
    stays as a fallback. Success is judged by the page's own ``change`` event
    (comix replaces the drop-zone input as soon as it has the file, so reading
    the input back would look like a failure).
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
            _bounded_eval(page, FILE_PROBE_INSTALL_JS)
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
        # enough: comix replaces its drop-zone input the moment it has the file.
        gone_reads = 0
        state = ""
        for _ in range(FILE_PROBE_POLL_TRIES):
            state = _bounded_eval(page, FILE_PROBE_STATE_JS)
            if state == "accepted":
                return True
            if state == "gone":
                # Require two consecutive reads so a mid-render moment cannot
                # be mistaken for "the form consumed it".
                gone_reads += 1
                if gone_reads >= 2:
                    log(
                        "[i] The upload form replaced its file input - "
                        "treating the file as accepted."
                    )
                    return True
            else:
                gone_reads = 0
            time.sleep(FILE_PROBE_POLL_SECONDS)
        log(f"[!] The upload form did not take the file via CDP ({state}).")
        return False
    except Exception as ex:
        log(f"[i] CDP file attach failed ({ex}) - trying the standard route.")
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
            if picked_el.is_visible(timeout=SAFE_READ_TIMEOUT_MS):
                if (
                    group_name.lower()
                    in picked_el.inner_text(timeout=SAFE_READ_TIMEOUT_MS).lower()
                ):
                    return
                clear_btn = page.locator(".upage-group__clear")
                if clear_btn.is_visible(timeout=SAFE_READ_TIMEOUT_MS):
                    clear_btn.click(force=True)
                    page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            # Playwright drives the dropdown over CDP; it does not need the OS
            # foreground, so the raise is opt-in only.
            if allow_raise:
                page.bring_to_front()
            _bounded_eval(
                page,
                '() => document.querySelectorAll(\'a[href="#"][target="_blank"]\')'
                ".forEach(e => e.remove())",
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
            if (
                group_name.lower()
                in picked_el.inner_text(timeout=SAFE_READ_TIMEOUT_MS).lower()
            ):
                log(f"[✓] Group '{group_name}' selected.")
                return
        except Exception:
            page.wait_for_timeout(1000)

    # Runs through a locator handle, so the expression receives the element
    # first and the group name second.
    try:
        _bounded_eval(
            page,
            """(_el, name) => {
            const btns = Array.from(document.querySelectorAll('button.upage-group__item'));
            const match = btns.find(b => b.textContent.toLowerCase().includes(name));
            if (match) match.click();
        }""",
            group_name.lower(),
        )
    except Exception:
        pass
    page.wait_for_timeout(1000)
    try:
        if page.locator(".upage-group--picked").is_visible(
            timeout=SAFE_READ_TIMEOUT_MS
        ):
            log(f"[✓] Group '{group_name}' selected via DOM fallback.")
            return
    except Exception:
        pass
    log(f"[!] Warning: group '{group_name}' was not confirmed picked.")


def _retry_after_seconds(res) -> float | None:
    """``Retry-After`` off a response, in seconds. None when absent/unparsable.

    Accepts both forms the header allows: a delay in seconds, or an HTTP-date.
    """
    try:
        raw = (res.headers or {}).get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
    except Exception:
        return None


def upload_single_chapter(
    page,
    url: str,
    chapter_num,
    title: str,
    file_path,
    group_name: str,
    mark_official: bool,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    log: Callable = print,
    on_chunk=None,
    on_heartbeat=None,
    control: Control | None = None,
):
    """Upload one chapter. Returns (success, error).

    Raises CloudflareBlockException when a challenge hijacks the upload, and
    ServerDownError when the origin itself is failing (5xx / connection loss).
    ``on_heartbeat`` is called once per poll iteration so the parallel stall
    watchdog can tell a slow-but-alive upload from a wedged tab.
    """

    def check_stop():
        if control is not None and control.stopped:
            raise StopRequested()

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    log(
        f"\n[+] Chapter {chapter_num:g} - {Path(file_path).name} ({file_size_mb:.1f} MB)"
    )

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)
    check_stop()
    require_no_challenge(page)
    # A WAF block renders a real page with no upload form, so catch it here,
    # before the form wait (which would otherwise read as "origin is down").
    _raise_if_access_blocked(page)

    try:
        page.wait_for_selector(".upage-field input", timeout=15000)
    except Exception as ex:
        kind = detect_challenge(page)
        if kind:
            raise _exception_for(
                kind, "A verification screen blocked the upload form."
            ) from ex
        _raise_if_access_blocked(page)
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
        "server_down": False,
        "last_activity": time.time(),
        "chunks_done": 0,
        "retry_after": None,
    }

    def on_response(res):
        res_url = res.url
        if "/upload/chunk" in res_url and res.status == 200:
            tracker["last_activity"] = time.time()
            tracker["chunks_done"] += 1
            if on_chunk:
                on_chunk(tracker["chunks_done"])
        elif "/api/v1/uploads" in res_url:
            tracker["last_activity"] = time.time()
            if "finalize" in res_url and res.status == 200:
                tracker["finalized"] = True
            elif res.status in (403, 503):
                tracker["blocked"] = True
                tracker["error"] = f"Upload API blocked (HTTP {res.status})"
            elif res.status in (500, 502, 503, 504):
                # Origin failing, not the account: flag server-down, not challenge.
                tracker["server_down"] = True
                tracker["error"] = f"HTTP {res.status} on {res_url}"
            elif res.status == 429:
                tracker["error"] = f"HTTP {res.status} on {res_url}"
                # Remember Retry-After so the retry backs off deliberately.
                tracker["retry_after"] = _retry_after_seconds(res)

    page.on("response", on_response)

    try:
        try:
            submit_btn.click(timeout=5000)
        except Exception:
            submit_btn.dispatch_event("click")

        tracker["last_activity"] = time.time()

        while True:
            check_stop()

            # A WAF block page is not under /user/upload/, so test it before the
            # URL success check below or nothing-sent would read as uploaded.
            _raise_if_access_blocked(page)

            # A challenge can hijack the tab mid-upload; also checked before the
            # success test for the same reason.
            kind = detect_challenge(page)
            if kind:
                raise _exception_for(
                    kind, f"{_kind_label(kind)} interrupted the upload."
                )

            if tracker["finalized"] or "/user/upload/" not in page.url:
                log(
                    f"[✓] Chapter {chapter_num:g} uploaded "
                    f"({tracker['chunks_done']} chunks)."
                )
                return True, None

            if tracker.get("blocked"):
                # 403/503 is ambiguous; let the DOM decide before pausing.
                if _confirm_block(page):
                    kind = detect_challenge(page) or "cloudflare"
                    raise _exception_for(
                        kind, f"{_kind_label(kind)} triggered on the upload API."
                    )
                if tracker.get("server_down"):
                    raise ServerDownError(tracker["error"])
                log(f"[!] Server error: {tracker['error']}")
                return False, tracker["error"]

            if tracker["error"]:
                if tracker.get("server_down"):
                    raise ServerDownError(tracker["error"])
                delay = tracker.get("retry_after")
                if delay:
                    # Wait the Retry-After out so the retry does not re-trip it.
                    delay = min(float(delay), MAX_RATE_LIMIT_BACKOFF_SECONDS)
                    log(
                        f"[!] Rate limited (HTTP 429) - waiting {delay:.0f}s "
                        "before retrying."
                    )
                    waited = 0.0
                    while waited < delay:
                        if control is not None and control.stopped:
                            raise StopRequested()
                        page.wait_for_timeout(500)
                        waited += 0.5
                log(f"[!] Server error: {tracker['error']}")
                return False, tracker["error"]

            error_el = page.locator(
                ".alert-danger, .error-message, .toast-error, div[role='alert']"
            ).first
            try:
                if error_el.is_visible(timeout=SAFE_READ_TIMEOUT_MS):
                    err_text = error_el.inner_text(timeout=SAFE_READ_TIMEOUT_MS).strip()
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

            # Still polling, so the tab is alive - feed the stall watchdog.
            if on_heartbeat is not None:
                try:
                    on_heartbeat()
                except Exception:
                    pass

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
    # Set when the batch stopped early (origin down / Cloudflare stuck).
    aborted: bool = False
    abort_reason: str = ""


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
    # Parallel mode only: called by the challenge wait once per poll so the
    # stall watchdog can tell "alive but waiting" apart from "wedged".
    on_heartbeat: Callable | None = None
    control: Control | None = None
    max_retries: int = 3
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    challenge_max_wait: int = 600
    challenge_poll: float = 2.0
    max_pauses: int = 5
    focus: bool = True
    on_cloudflare: Callable | None = None
    on_cloudflare_stuck: Callable | None = None
    on_challenge_cleared: Callable | None = None
    cf_resolved_hook: Callable | None = None
    # Cloudflare solver hook: ``hook(context, log) -> bool``, called with the
    # page's own (thread-affine) context. A worker thread must not write config.
    cf_solver_hook: Callable | None = None
    # Parallel mode only: never restart the shared browser.
    allow_restart: bool = True
    recorder: HistoryRecorder | None = None
    # Kept on the context, not the retry loop, so a re-entered loop cannot
    # reset the counters and spin forever.
    pause_counts: dict = field(default_factory=dict)
    dumped: bool = False


def build_chapter_context(
    config: dict, params: UploadParams, **overrides
) -> ChapterContext:
    """ChapterContext from config + params, with per-run defaults applied."""
    challenge_cfg = challenge_config(config)
    kwargs = dict(
        config=config,
        params=params,
        challenge_max_wait=int(challenge_cfg.get("max_wait_seconds", 600)),
        challenge_poll=float(challenge_cfg.get("poll_interval_seconds", 2)),
        max_pauses=int(challenge_cfg.get("max_pauses_per_chapter", 5)),
        focus=bool(config.get("focus_on_challenge", True)),
        allow_restart=True,
        recorder=HistoryRecorder(params.url),
        idle_timeout=int(config.get("timeout_seconds", DEFAULT_IDLE_TIMEOUT)),
    )
    kwargs.update(overrides)
    # A caller that does not care passes None; keep the configured value.
    if kwargs.get("idle_timeout") is None:
        kwargs["idle_timeout"] = int(
            config.get("timeout_seconds", DEFAULT_IDLE_TIMEOUT)
        )
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

    * ``"done"``    - uploaded and recorded.
    * ``"failed"``  - recorded as failed; the caller may continue.
    * ``"pending"`` - not completed and not recorded (user stop, or a gate
      outlasted its window).
    * ``"aborted"`` - origin down or Cloudflare stuck; batch stops, nothing
      recorded, so the chapter is retried next run.

    The returned page may differ - Cloudflare recovery can restart the browser
    (sequential mode only).
    """
    params = ctx.params
    log = ctx.log
    control = ctx.control or Control()
    recorder = ctx.recorder or HistoryRecorder(params.url)
    title = resolve_chapter_title(params, ch_num)

    while True:
        success = False
        last_err = None
        challenge_hit = False
        server_down = False
        access_blocked = False

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
                    on_chunk=(
                        (lambda c, n=ch_num: ctx.on_chunk(n, c))
                        if ctx.on_chunk
                        else None
                    ),
                    on_heartbeat=ctx.on_heartbeat,
                    control=control,
                )
                if success:
                    break
            except StopRequested:
                log("[!] Stopped by user.")
                if ctx.on_status:
                    ctx.on_status(ch_num, "pending", "")
                return "pending", None, page
            except ServerDownError as ex:
                # Origin failing: let the retry loop run it out.
                server_down = True
                last_err = str(ex)
                log(f"[!] Server error on ch. {ch_num:g}: {ex}")
            except AccessBlockedError as ex:
                # A WAF block is per-IP: abort with a reason and record nothing
                # (this is not a VerificationRequired gate).
                log(f"\n[!] {_kind_label(ex.kind)}: {ex}")
                if ctx.on_status:
                    ctx.on_status(ch_num, "aborted", str(ex))
                control.request_abort(str(ex))
                return "aborted", str(ex), page
            except VerificationRequired as vreq:
                log(f"\n[!] {_kind_label(vreq.kind)}: {vreq}")
                challenge_hit = True
                last_err = str(vreq)
                break
            except Exception as ex:
                last_err = str(ex)
                kind = detect_challenge(page)
                if kind:
                    log(f"[!] {_kind_label(kind)} detected.")
                    challenge_hit = True
                    break
                if is_cloudflare_access_block(page):
                    # A failure on a blocked page is the block, not the origin.
                    access_blocked = True
                    log(f"[!] {_kind_label('access_blocked')} on ch. {ch_num:g}: {ex}")
                elif _looks_like_server_down(page, str(ex)):
                    server_down = True
                    log(f"[!] Server error on ch. {ch_num:g}: {ex}")
                else:
                    log(f"[!] Error on ch. {ch_num:g}: {ex}")

        if control.stopped:
            if ctx.on_status:
                ctx.on_status(ch_num, "pending", "")
            log("[!] Stopped by user.")
            return "pending", None, page

        if control.aborted:
            return "aborted", control.abort_reason, page

        if access_blocked and not success:
            ray = cloudflare_ray_id(page)
            reason = (
                "Cloudflare blocked this IP on the upload path"
                + (f" (Ray ID {ray})" if ray else "")
                + ". No cookie can clear a WAF block - switch network (VPN) or "
                "set config.proxy, then retry."
            )
            log(
                "[!] Cloudflare blocked this IP on the upload path - stopping "
                "the batch; nothing was recorded."
            )
            if ctx.on_status:
                ctx.on_status(ch_num, "aborted", reason)
            control.request_abort(reason)
            return "aborted", reason, page

        if server_down and not success and not challenge_hit:
            reason = last_err or "comix server appears down"
            log(
                "[!] Server appears down - stopping the batch so nothing else "
                "is attempted."
            )
            if ctx.on_status:
                ctx.on_status(ch_num, "aborted", reason)
            control.request_abort(reason)
            return "aborted", reason, page

        if challenge_hit:
            key = str(ch_num)
            ctx.pause_counts[key] = ctx.pause_counts.get(key, 0) + 1

            if ctx.pause_counts[key] > ctx.max_pauses:
                log(
                    f"[!] Chapter {ch_num:g} hit a verification screen "
                    f"{ctx.pause_counts[key]} times - skipping it."
                )
                last_err = "Repeated verification screens"
                challenge_hit = False
            else:
                # The check is non-interactive (a normal visit mints the
                # clearance), so waiting it out beats waiting for a click. The
                # event is cleared first so a racing confirmation click survives.
                control.cf_event.clear()
                if ctx.on_status:
                    ctx.on_status(ch_num, "cloudflare", "waiting for clearance")
                if ctx.on_cloudflare:
                    ctx.on_cloudflare(ch_num)
                # One dump per run is enough to identify an unfamiliar challenge.
                if not ctx.dumped:
                    evidence = save_challenge_evidence(
                        page, "cloudflare", chapter=ch_num
                    )
                    ctx.dumped = True
                    if evidence:
                        log(f"[i] Challenge evidence saved to {evidence}")
                cleared = wait_for_challenge_clear(
                    page,
                    control=control,
                    log=log,
                    max_wait=ctx.challenge_max_wait,
                    poll=ctx.challenge_poll,
                    focus=ctx.focus,
                    on_heartbeat=ctx.on_heartbeat,
                )
                if control.stopped:
                    if ctx.on_status:
                        ctx.on_status(ch_num, "pending", "")
                    log("[!] Stopped by user.")
                    return "pending", None, page

                stuck = control.cf_stuck_event.is_set()
                if (stuck or not cleared) and ctx.cf_solver_hook is not None:
                    # The check is not clearing by itself. Mint a fresh
                    # cf_clearance with the patchright solver and retry.
                    if stuck:
                        control.cf_stuck_event.clear()
                    log(
                        "[i] Cloudflare is not clearing on its own - running the solver…"
                    )
                    if ctx.on_status:
                        ctx.on_status(ch_num, "cloudflare", "solving…")
                    solved = False
                    try:
                        solved = bool(ctx.cf_solver_hook(page.context, log))
                    except Exception as ex:
                        log(f"[!] Cloudflare solver failed: {ex}")
                    if control.stopped:
                        if ctx.on_status:
                            ctx.on_status(ch_num, "pending", "")
                        return "pending", None, page
                    if solved:
                        log(
                            f"[✓] Solver obtained a fresh clearance. "
                            f"Resuming chapter {ch_num:g}."
                        )
                        if ctx.on_challenge_cleared is not None:
                            try:
                                ctx.on_challenge_cleared("cloudflare", ch_num)
                            except Exception as ex:
                                log(f"[!] Cleared callback failed: {ex}")
                        continue
                    if stuck:
                        # Restore the flag so the existing abort path below fires.
                        control.cf_stuck_event.set()

                if control.cf_stuck_event.is_set():
                    # Refreshing no longer clears it - ask for fresh cookies.
                    control.cf_stuck_event.clear()
                    reason = (
                        "Cloudflare stuck on 'Verifying you are human' - "
                        "paste a fresh cf_clearance cookie"
                    )
                    if ctx.on_cloudflare_stuck:
                        try:
                            ctx.on_cloudflare_stuck(ch_num)
                        except Exception as ex:
                            log(f"[!] Stuck callback failed: {ex}")
                    if ctx.on_status:
                        ctx.on_status(ch_num, "aborted", reason)
                    control.request_abort(reason)
                    return "aborted", reason, page
                if not cleared:
                    if not ctx.allow_restart or ctx.cf_resolved_hook is None:
                        # Parallel mode (a restart would kill other workers) or no
                        # relaunch hook: hand the chapter back for a sequential retry.
                        log(
                            "[!] Cloudflare did not clear in time. Leaving this "
                            "chapter for a sequential retry."
                        )
                        if ctx.on_status:
                            ctx.on_status(ch_num, "pending", "")
                        return "pending", None, page
                    if ctx.cf_resolved_hook is not None:
                        # A relaunch usually unsticks the check; a second full
                        # window with no clearance means something is wrong.
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
                            log("[!] Browser restart produced no page - stopping.")
                            if ctx.on_status:
                                ctx.on_status(ch_num, "pending", "")
                            return "pending", None, page
                        page = restarted
                        # Re-open the upload page so the fresh session runs the check.
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
                            log=log,
                            max_wait=ctx.challenge_max_wait,
                            poll=ctx.challenge_poll,
                            focus=ctx.focus,
                            on_heartbeat=ctx.on_heartbeat,
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

    if control.aborted:
        # Another worker (or an earlier chapter) decided the batch is over.
        return "aborted", control.abort_reason, page

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
    on_cloudflare_stuck=None,  # (chapter_num) -> CF stuck on "Verifying"
    cf_solver_hook=None,  # (context, log) -> bool  (auto-solve Cloudflare)
    on_challenge_cleared=None,  # (kind, chapter_num) -> a gate cleared itself
    max_retries: int = 3,
    idle_timeout: int | None = None,
) -> RunSummary:
    """Upload every pending chapter, one at a time. Blocks until done / stopped.

    This is the sequential engine - and the fallback used whenever parallel
    uploads are unavailable. The per-chapter work lives in _process_chapter,
    which parallel.py drives on several tabs at once.

    ``idle_timeout`` defaults to the config's ``timeout_seconds``; pass a value
    only to override it.
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
        on_cloudflare_stuck=on_cloudflare_stuck,
        cf_solver_hook=cf_solver_hook,
        on_challenge_cleared=on_challenge_cleared,
    )

    pending = pending_chapters(params, ctx.recorder)
    log(f"[i] {len(pending)} chapter(s) queued.")

    idx = 0
    while idx < len(pending):
        if control.stopped:
            log("[!] Stopped by user.")
            break
        if control.aborted:
            break

        if control.pause_event.is_set():
            log("[‖] Paused.")
            control.pause_event.wait(0.5)
            continue

        ch_num, file_path = pending[idx]

        # Re-check before uploading: pending is a one-time snapshot, so a worker
        # may have finished this chapter since (guard the duplicate).
        if ctx.recorder.already_done(ch_num):
            log(f"[i] Chapter {ch_num:g} was already uploaded - skipping.")
            idx += 1
            continue

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
        elif status == "aborted":
            # Origin down (or CF stuck): stop the whole batch. Nothing is
            # recorded, so the chapter is retried on the next run.
            summary.aborted = True
            summary.abort_reason = err or summary.abort_reason
            break
        else:
            # "pending": nothing was recorded - stop, exactly like before.
            break

        idx += 1

    return summary


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
