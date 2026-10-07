# Comix Uploader

Batch chapter uploader for comix.to - now with a desktop GUI so you stop
re-pasting the same URL, folder, group name and title pattern every run.

---

## 1. Installation

```bash
# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate  # On Windows

# Install dependencies
pip install -r requirements.txt
```

Google Chrome must be installed (the uploader drives your real Chrome profile
so Cloudflare sees a normal browser).

`requirements.txt` also installs **patchright**, a drop-in Playwright fork used
only by the automatic Cloudflare solver. It drives your system Google Chrome
(`channel="chrome"`), so no extra browser download is normally needed - if it
ever complains, run `patchright install chrome` once.

---

## 2. Configuration Setup

Copy `config.example.json` and rename it to `config.json`:

```bash
copy config.example.json config.json
```

### How to Fill `config.json`

Open `https://comix.to` in your browser, log in, and press `F12`:

1. **Get `user_agent`**
   - Console tab → type `navigator.userAgent` → paste the output.

2. **Get Cookies (`remember_web_*`, `session`, `cf_clearance`)**
   - Application tab → Cookies → `https://comix.to`
   - Copy `remember_web_<HASH>` (full name + value), `session`, `cf_clearance`.

After that you rarely need to touch `config.json` again - the app refreshes
`cf_clearance` and `session` automatically, and **solves Cloudflare for you**
(see §4).

---

## 3. GUI (recommended)

Double-click **`Comix Uploader.bat`**, or run:

```bash
.venv\Scripts\python.exe gui.py
```

### What it does for you

| Feature                   | Notes                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Site search**           | Type a title, hit Search - results come straight from comix.to with chapter counts and ratings. Double-click one and the upload URL is filled in.                                                                                                                                                                                                                                                               |
| **Saved series**          | Every series you use is remembered (name + ID). Next time: double-click, done. No pasting.                                                                                                                                                                                                                                                                                                                      |
| **Add by ID / URL**       | Paste a series ID, `/title/...` or `/user/upload/...` URL once - it gets resolved and saved.                                                                                                                                                                                                                                                                                                                    |
| **Recent folders**        | Folder picker remembers the last 15 folders you used.                                                                                                                                                                                                                                                                                                                                                           |
| **Per-series settings**   | Group, title pattern, official flag and delay are saved per series and restored when you pick it again.                                                                                                                                                                                                                                                                                                         |
| **Chapter title mapping** | **Import map…** loads `chapter_mapping.txt` and titles each chapter from its original folder name (e.g. `2 -> 002 1话.倒霉体质` uploads as `倒霉体质`). The title pattern is disabled while a map is active; "Strip numbering" removes the leading counters, and the chapter table previews the exact title per row. The map is saved per series. Chapters missing from the map fall back to the title pattern. |

| **Chapter table** | Shows pending / already uploaded / failed per chapter, with checkboxes so you can upload just a few. |
| **Pause / Stop** | Stop lands after the current chapter; nothing is lost. |
| **Cloudflare hand-off** | When a challenge hits, the app pauses, shows a banner, and **polls the tabs itself** - the check usually clears on its own and uploading resumes automatically. If it sits still the app reloads it, and if a full wait window (default 10 min) passes it restarts the browser once and waits one more window. **Resume now** / **Restart browser & resume** stay as manual overrides. The chapter is retried - never skipped, never marked failed. |
| **Automatic Cloudflare solve** | When the check will not clear on its own, the app mints a fresh `cf_clearance` with **patchright** (a real, non-automated Chrome) and retries the chapter - no manual step. This is the fix for the "Verifying you are human" hang, which a Playwright-driven Chrome can never clear by itself. |
| **Server-down stop** | If comix itself is down (502/503/521/522, connection refused), the app no longer mistakes it for a Cloudflare check. After a chapter's retries are exhausted the whole batch stops - nothing is marked failed, so you can just run it again once the site is back. |
| **Cookie update (fallback)** | If the automatic solver fails, paste a fresh `cf_clearance` (and optionally `session`) into the cookie row and press **Update cookies** - no hand-editing `config.json`. |
| **Refresh Cloudflare** | One button runs the automatic solver; if it fails, it falls back to restarting the browser session and re-saving cookies to `config.json`. |
| **Stays out of your way** | Chrome no longer steals focus. The uploader works in the background and leaves the window wherever you put it (minimized or not). The only time it comes forward is when a Cloudflare check actually needs you to look at it - and then it restores and focuses that window. Both behaviours are toggleable in the options row. |

### Typical flow

1. Search the series → double-click the result.
2. Pick the chapters folder (or accept the remembered one).
3. Tick the chapters you want (pending ones are pre-ticked).
4. **Start upload.**
5. If Cloudflare pops up → nothing to do in most cases; it clears itself and
   the upload resumes. If it doesn't, the app **solves it automatically** with
   patchright and retries.
6. Only if the automatic solver also fails: paste a fresh `cf_clearance` in the
   cookie row → **Update cookies**, or use the banner's manual overrides.
7. If the server is down → the batch stops by itself; run it again once comix
   is back.

### Typical flow - no manual verification needed

The whole point of the automatic solver is that steps 5–6 above normally never
require you to touch anything.

---

## 4. Verification screens

comix puts a Cloudflare check in front of you, and the app tells it apart from
a dead server:

|            | Cloudflare check                                                             | Server down                                                                   |
| ---------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Looks like | "Just a moment…" / "Attention required" / "Verifying you are human"          | Error 502/521/522, "Connection timed out"                                     |
| Where      | `comix.to`                                                                   | Cloudflare's error page for a dead origin                                     |
| Fixed by   | The automatic patchright solver (or a pasted cookie as fallback)             | Waiting for comix to come back                                                |
| Recovery   | Wait → reload → restart once → **automatic solver** → retry; only then abort | After the chapter's retries fail, the batch **stops** - nothing marked failed |
| Browser    | Restarted once if the check will not clear                                   | Never restarted                                                               |

The check is detected on page load, before the upload form is touched, and again
every second during an upload. A challenge that appears **mid-upload** pauses the
run instead of losing it: the in-flight attempt is retried from scratch once the
check clears, and the chapter is never written to history while paused.

If a check keeps reappearing on the same chapter (default: 5 times), the app
gives up on that one chapter rather than looping forever, and moves on.

### How the automatic solver works

A real Chrome driven by Playwright **cannot** clear Cloudflare: Playwright sends
the CDP `Runtime.enable` command on attach, and the challenge page detects that
automation-protocol signal - which is why the browser sits on "Verifying you are
human" forever. So the app shells out to `solver.py`, which drives a **second**
Chrome with **patchright** (a Playwright fork that avoids that signal), waits for
the challenge to clear, and harvests `cf_clearance` (+ `__cf_bm`). Those cookies
are injected into the uploader's own browser and the chapter is retried.

Because `cf_clearance` is bound to the exact **User-Agent and IP**, both browsers
must be the same Google Chrome on the same machine - they are, by default. The
solver window starts off-screen (so it does not disrupt you) and is brought to
the front only if the check needs a visible window.

If the solver fails, the app falls back to the manual path: paste a fresh
`cf_clearance` into the GUI's cookie row and press **Update cookies**, or use CLI
menu option **[8] Paste Cloudflare cookies**. CLI option **[7]** still opens a
real Chrome window for a normal refresh-and-login flow, and **[6]** runs the
solver directly.

### Blocked IPs (Cloudflare Error 1020) - not a challenge

Sometimes comix answers with Cloudflare's **"Sorry, you have been blocked"**
page instead of the upload form. That is a WAF **access-denied** decision, not a
check, and the two behave nothing alike:

- It is decided on the **IP and path** _before_ any cookie is read, so
  `cf_clearance` is irrelevant and the solver cannot help.
- It does not clear on its own, so waiting it out is pointless.
- It can affect only some paths - typically `/user/upload/<hid>` - while
  browsing still works, which is exactly what makes it look like a site bug.

The app recognises it, reports the **Ray ID**, and stops the batch with
_nothing recorded_, so the chapters are retried as soon as the egress IP
changes. Fix it by switching network (a VPN is enough) or by setting `proxy`
below.

Scraped public proxy lists are **not** a solution. They are almost entirely
datacenter IPs, which Cloudflare rates _worse_ than a home connection, most are
dead or already listed, and a mid-run IP change invalidates the IP-bound
clearance and strands half-uploaded chapters. Use your VPN's own local proxy
(most VPNs expose `socks5://127.0.0.1:1080`) or a paid residential proxy with a
**sticky** session - one stable IP per run is required.

### Diagnosing a block

`cf_diag.py` reports exactly what Cloudflare does to the current IP - HTTP
status, `cf-ray` / `cf-mitigated` headers, the page title, the Ray ID, which
detector matched, the egress IP, and the real User-Agent plus client hints:

```bash
python cf_diag.py --url https://comix.to/user/upload/<hid>
```

Run it once with the VPN off and once with it on, then compare. That is what
distinguishes a hard IP block (needs a different egress) from a rate-limit rule
(needs slower pacing). It is read-only and nothing in the uploader depends on it.

### Tunables (`config.json`)

```json
"challenge": {
  "max_wait_seconds": 600,      // how long to wait for the check to clear
  "poll_interval_seconds": 2,   // how often the tabs are re-checked
  "max_pauses_per_chapter": 5   // give up on a chapter after this many checks
},
"cloudflare": {
  "auto_solve": true,               // mint a clearance with patchright when stuck
  "preflight_solve": false,         // solve once before a batch starts
  "solve_max_wait_seconds": 90,     // how long the solver waits for the check
  "solve_window_mode": "offscreen", // offscreen | focus | headless
  "solve_focus_after_seconds": 15   // offscreen -> bring the window forward
},
"concurrency": 5,                    // chapters uploading at once (1-8)
"timeout_seconds": 180,              // per-chapter "no chunk progress" budget
"worker_stall_timeout_seconds": 180, // abandon a tab that stops making progress
"keep_window_in_background": true,   // never raise Chrome during normal work
"focus_on_challenge": true,          // restore + focus Chrome when a check appears
"proxy": {
  "server": "",                      // "" = connect directly; e.g. socks5://127.0.0.1:1080
  "username": "",
  "password": "",
  "bypass": ""
}
```

`concurrency` is how many chapters upload **at the same time**, each in its own
tab of the same Chrome window. `1` is the old strictly-one-at-a-time behaviour;
the default is `5`. Because every tab belongs to the same browser, they share
your login and the Cloudflare clearance - one clearance covers the whole
batch. Values above **8** (or anything that is not a whole number) are refused
with a clear message when the app starts, so a typo cannot quietly halve or
double your upload rate.

Parallel uploads need a local Chrome debugging endpoint; if it cannot be
opened, the app says so and simply runs the batch sequentially - it never
fails a run over this. Two things worth knowing when running several at once:
the browser restart that Cloudflare recovery normally does is skipped (it would
kill the other uploads mid-flight - the chapter is retried sequentially at the
end instead), and the site has not been tested for parallel uploads to the
_same_ series, so if you see server-side errors, drop `concurrency` to `2` or
`1`.

`timeout_seconds` is how long one chapter may go without a single chunk
completing before it is treated as stalled and retried. It used to be **dead
data** - nothing read it, so the value was always the hardcoded 90 s, which is
too short when several 10-20 MB chapters upload at once over one uplink: the
link saturates, no chunk finishes in time, and a perfectly good upload gets
reported as _"Upload stalled: no chunk progress for 90s"_. It now works, and the
default is 180.

`worker_stall_timeout_seconds` is the safety net for a tab that stops
responding. Playwright's `page.title()` / `page.content()` / `page.evaluate()`
carry **no timeout at all**, so a wedged renderer used to block its worker - and
therefore the whole batch - forever, with the chapter stuck on "uploading" and
no error. Every check now reads the page through a call that _does_ have a
deadline, and a worker that reports no progress for this long is abandoned by
name (`Tab N stopped making progress on chapter X`) and its chapter handed to
the sequential retry. Leave it unset and it is derived from `timeout_seconds`
(`timeout_seconds + 60`, floor 180), so the watchdog can never fire on a chapter
that is still inside its own stall window. A worker that is genuinely waiting
keeps reporting heartbeats - from each chunk, each status change, and once per
poll of the challenge wait and the upload loop - so only a truly wedged tab
trips it.

A worker tab that stops answering is replaced (it is probed twice first, so a
merely busy renderer is not mistaken for a dead one), but only a few times per
worker: the old tab **cannot be closed** - `Page.close()` takes no timeout, so
closing a wedged tab is itself the call that would hang - and an uncapped
replacement loop leaks a tab per chapter.

Two guards stop one run from uploading the same chapter twice. The sequential
tail re-checks the history before every chapter (the queue snapshot is taken
once, so a parallel worker can finish a chapter after it), and a worker the
watchdog abandoned is **told to stop** rather than being allowed to silently
resume - a resumed worker would keep pulling chapters from the queue and upload
them at the same time as the tail, which is how a chapter could end up uploaded
twice and recorded as both done _and_ failed.

`proxy` routes **both** the uploader and the Cloudflare solver through a proxy,
which is the only real remedy for a WAF IP block (see above). Leave `server`
empty to connect directly. Two constraints:

- The solver must use the **same** proxy as the uploader - `cf_clearance` is
  bound to the IP that earned it - so the setting is applied to both
  automatically.
- Chromium only sends proxy credentials for **HTTP(S)** proxies. SOCKS5
  authentication is not supported (it fails with
  `net::ERR_PROXY_AUTH_UNSUPPORTED`), so an authenticated SOCKS5 upstream needs
  a local bridge - `gost`, `3proxy` or `privoxy` listening unauthenticated on
  `127.0.0.1` - with `server` pointing at the bridge.

The solver receives the proxy through its environment rather than its command
line, so a password never shows up in the process list.

`keep_window_in_background` is on by default: the app no longer calls
`bring_to_front()` on any routine path, so Chrome keeps whatever state you left
it in for the whole run. Set it to `false` only if you want to watch the
upload happen, in which case Chrome is raised again at the start of each
chapter.

`focus_on_challenge` is the deliberate exception. The check is unsolvable
without a human looking at it, so when one is detected the app restores the
window (un-minimizing it if needed) and brings it to the foreground. Once the
check clears the app leaves the window alone - it does not force it back down.
Windows only; on other platforms this is a no-op.

`max_wait_seconds` applies to the Cloudflare check. It effectively gets two
windows: when the first expires, the browser is restarted once and the app
waits one more window before stopping the run (the pending chapter is kept,
nothing is marked failed).

### When something unexpected shows up

If a challenge is hit that the app does not recognise, it dumps evidence to
`.challenge_debug/<timestamp>_<kind>/` - a screenshot, the page HTML, and a
`meta.json` with the URL, title and frame list. That folder is enough to add a
selector for a new challenge variant without reproducing it.

---

## 5. CLI (still available)

```bash
python uploader.py
```

Same engine, same history/retry behaviour, terminal prompts.

---

## 6. Notes

- **Supported archives:** `.zip`, `.cbz`, `.cbr`, `.rar`, `.7z`
- **Duplicate chapter numbers** are auto-renumbered (e.g. a second `10` becomes
  `10.5`) instead of being skipped.
- **Upload history** lives in `.history/` (gitignored), one file per series:
  `upload_history_<series-id>.json` and `upload_failed_<series-id>.json`.
  "Reset history" clears both for the current series. History files written by
  older versions (`.upload_history_*.json` in the project root) are moved into
  `.history/` automatically the first time the app runs.
- **Saved series + recent folders** live in `series_library.json`.
- **Challenge evidence dumps** land in `.challenge_debug/` (screenshots, page
  HTML, `meta.json`) and are safe to delete.
- The browser profile in `browser_profile/` keeps you logged in between runs.
  The Cloudflare clearance lives there too, so a fresh check usually covers the
  rest of the run.
- The solver keeps its own Chrome profile in `solver_profile/` (gitignored).
  It is only used to mint `cf_clearance`, and it is safe to delete (the next
  solve recreates it).

---

## 7. Tests

```bash
.venv\Scripts\python.exe -m unittest discover tests
```

The suite needs no browser, no network and no Playwright account: `core.py` is
stdlib-only by design, `solver.py`'s browser path is only exercised through its
pure helpers, and `parallel.py`'s browser plumbing is stubbed, so it runs in
well under a second. It covers the config validation (including the
"concurrency too large" error), the upload engine's control flow - success,
failure, skip-already-uploaded, stop, gate-cleared retry, gate-expiry, the
Cloudflare solver hook, the server-down abort - the parallel/sequential
dispatch, the file-input attach and its fallbacks, the solver's cookie/JSON
helpers, and the history layout including the migration of pre-`.history/`
files.

---

## 8. Project layout

```
core.py      shared engine: config, chapter scanning, history, uploading, site search
solver.py    automatic Cloudflare solver (patchright, runs as a subprocess)
parallel.py  parallel upload engine (worker tabs attached to the same Chrome)
session.py   background browser worker (persistent Chrome + job queue)
gui.py       Tkinter desktop app
uploader.py  command-line interface
window_ctl.py Windows-only helper that focuses/moves Chrome for a verification gate
tests/       stdlib unittest suite (no browser needed)
```
