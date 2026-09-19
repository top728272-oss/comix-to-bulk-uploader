# Comix Uploader

Batch chapter uploader for comix.to — now with a desktop GUI so you stop
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

After that you rarely need to touch `config.json` again — the app refreshes
`cf_clearance` and `session` automatically from the browser profile.

---

## 3. GUI (recommended)

Double-click **`Comix Uploader.bat`**, or run:

```bash
.venv\Scripts\python.exe gui.py
```

### What it does for you

| Feature                     | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Site search**             | Type a title, hit Search — results come straight from comix.to with chapter counts and ratings. Double-click one and the upload URL is filled in.                                                                                                                                                                                                                                                                                                   |
| **Saved series**            | Every series you use is remembered (name + ID). Next time: double-click, done. No pasting.                                                                                                                                                                                                                                                                                                                                                          |
| **Add by ID / URL**         | Paste a series ID, `/title/...` or `/user/upload/...` URL once — it gets resolved and saved.                                                                                                                                                                                                                                                                                                                                                        |
| **Recent folders**          | Folder picker remembers the last 15 folders you used.                                                                                                                                                                                                                                                                                                                                                                                               |
| **Per-series settings**     | Group, title pattern, official flag and delay are saved per series and restored when you pick it again.                                                                                                                                                                                                                                                                                                                                             |
| **Chapter table**           | Shows pending / already uploaded / failed per chapter, with checkboxes so you can upload just a few.                                                                                                                                                                                                                                                                                                                                                |
| **Pause / Stop**            | Stop lands after the current chapter; nothing is lost.                                                                                                                                                                                                                                                                                                                                                                                              |
| **Cloudflare hand-off**     | When a challenge hits, the app pauses, shows a banner, and **polls the tabs itself** — the check usually clears on its own and uploading resumes automatically. If it sits still the app reloads it, and if a full wait window (default 10 min) passes it restarts the browser once and waits one more window. **Resume now** / **Restart browser & resume** stay as manual overrides. The chapter is retried — never skipped, never marked failed. |
| **Security check hand-off** | comix's own rotate-the-circle captcha (`/@waf/`). The app pauses, shows a banner, and **polls the tabs itself** — drag the circle until the picture lines up and press Verify; uploading resumes automatically with no button press needed.                                                                                                                                                                                                         |
| **Refresh Cloudflare**      | One button restarts the browser session and re-saves cookies to `config.json`.                                                                                                                                                                                                                                                                                                                                                                      |
| **Stays out of your way**   | Chrome no longer steals focus. The uploader works in the background and leaves the window wherever you put it (minimized or not). The only time it comes forward is when a Cloudflare check or security check actually needs you to look at it — and then it restores and focuses that window so the puzzle is right there. Both behaviours are toggleable in the options row.                                                                 |

### Typical flow

1. Search the series → double-click the result.
2. Pick the chapters folder (or accept the remembered one).
3. Tick the chapters you want (pending ones are pre-ticked).
4. **Start upload.**
5. If Cloudflare pops up → nothing to do in most cases; it clears itself and
   the upload resumes. If it doesn't, use the banner's **Resume now** /
   **Restart browser & resume**.
6. If the security check pops up → drag the circle in Chrome → press **Verify**
   → it resumes by itself.

Verification is still on you — the app just stops making you redo everything else.

---

## 4. Verification screens

comix puts two different gates in front of you, and the app tells them apart:

|            | Cloudflare                                                                                                      | Security check (WAF)                                              |
| ---------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Looks like | "Just a moment…" / "Attention required"                                                                         | Dark card, _Verify you're human_, a circle you drag to rotate     |
| Where      | `comix.to`                                                                                                      | `comix.to/@waf/challenge`                                         |
| Fixed by   | Refreshing `cf_clearance`                                                                                       | Solving the puzzle in the open browser                            |
| Recovery   | Automatic — polls every tab, reloads the check if it stalls, restarts the browser once after a full wait window | Automatic — the app polls until the challenge page navigates away |
| Browser    | Restarted once if the check will not clear                                                                      | Never relaunched (that would discard the puzzle)                  |

Both are detected on page load, before the upload form is touched, and again
every second during an upload. A challenge that appears **mid-upload** pauses the
run instead of losing it: the in-flight attempt is retried from scratch once the
gate clears, and the chapter is never written to history while paused.

If a gate keeps reappearing on the same chapter (default: 5 times), the app gives
up on that one chapter rather than looping forever, and moves on.

### Tunables (`config.json`)

```json
"waf": {
  "max_wait_seconds": 600,      // how long to wait for a gate to clear (both gates)
  "poll_interval_seconds": 2,   // how often the tabs are re-checked
  "max_pauses_per_chapter": 5   // give up on a chapter after this many gates
},
"concurrency": 5,                   // chapters uploading at once (1-8)
"keep_window_in_background": true,  // never raise Chrome during normal work
"focus_on_challenge": true          // restore + focus Chrome when a gate appears
```

`concurrency` is how many chapters upload **at the same time**, each in its own
tab of the same Chrome window. `1` is the old strictly-one-at-a-time behaviour;
the default is `5`. Because every tab belongs to the same browser, they share
your login and the WAF clearance — one captcha solve still covers the whole
batch. Values above **8** (or anything that is not a whole number) are refused
with a clear message when the app starts, so a typo cannot quietly halve or
double your upload rate.

Parallel uploads need a local Chrome debugging endpoint; if it cannot be
opened, the app says so and simply runs the batch sequentially — it never
fails a run over this. Two things worth knowing when running several at once:
the browser restart that Cloudflare recovery normally does is skipped (it would
kill the other uploads mid-flight — the chapter is retried sequentially at the
end instead), and the site has not been tested for parallel uploads to the
*same* series, so if you see server-side errors, drop `concurrency` to `2` or
`1`.

`keep_window_in_background` is on by default: the app no longer calls
`bring_to_front()` on any routine path, so Chrome keeps whatever state you left
it in for the whole run. Set it to `false` only if you want to watch the
upload happen, in which case Chrome is raised again at the start of each
chapter.

`focus_on_challenge` is the deliberate exception. The gates are unsolvable
without a human looking at them, so when one is detected the app restores the
window (un-minimizing it if needed) and brings it to the foreground. Once the
gate clears the app leaves the window alone — it does not force it back down.
Windows only; on other platforms this is a no-op.

`max_wait_seconds` applies to both gates. Cloudflare effectively gets two
windows: when the first expires, the browser is restarted once and the app
waits one more window before stopping the run (the pending chapter is kept,
nothing is marked failed). The security check never gets a browser restart —
that would throw away the puzzle mid-solve.

### When something unexpected shows up

If a gate is hit that the app does not recognise, it dumps evidence to
`.waf_debug/<timestamp>_<kind>/` — a screenshot, the page HTML, and a `meta.json`
with the URL, title and frame list. That folder is enough to add a selector for a
new challenge variant without reproducing it.

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
- **Challenge evidence dumps** land in `.waf_debug/` (screenshots, page HTML,
  `meta.json`) and are safe to delete.
- The browser profile in `browser_profile/` keeps you logged in between runs.
  Clearance for both gates lives there, so a solved security check usually
  covers the rest of the run.

---

## 7. Tests

```bash
.venv\Scripts\python.exe -m unittest discover tests
```

The suite needs no browser, no network and no Playwright account: `core.py` is
stdlib-only by design and `parallel.py`'s browser plumbing is stubbed, so it
runs in well under a second. It covers the config validation (including the
"concurrency too large" error), the upload engine's control flow — success,
failure, skip-already-uploaded, stop, gate-cleared retry, gate-expiry, the
parallel/sequential dispatch, the file-input attach and its fallbacks — and the
history layout including the migration of pre-`.history/` files.

---

## 8. Project layout

```
core.py      shared engine: config, chapter scanning, history, uploading, site search
parallel.py  parallel upload engine (worker tabs attached to the same Chrome)
session.py   background browser worker (persistent Chrome + job queue)
gui.py       Tkinter desktop app
uploader.py  command-line interface
window_ctl.py Windows-only helper that focuses Chrome for a verification gate
tests/       stdlib unittest suite (no browser needed)
```
