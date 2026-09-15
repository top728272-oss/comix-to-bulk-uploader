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

| Feature                     | Notes                                                                                                                                                                                                                                      |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Site search**             | Type a title, hit Search — results come straight from comix.to with chapter counts and ratings. Double-click one and the upload URL is filled in.                                                                                          |
| **Saved series**            | Every series you use is remembered (name + ID). Next time: double-click, done. No pasting.                                                                                                                                                 |
| **Add by ID / URL**         | Paste a series ID, `/title/...` or `/user/upload/...` URL once — it gets resolved and saved.                                                                                                                                               |
| **Recent folders**          | Folder picker remembers the last 15 folders you used.                                                                                                                                                                                      |
| **Per-series settings**     | Group, title pattern, official flag and delay are saved per series and restored when you pick it again.                                                                                                                                    |
| **Chapter table**           | Shows pending / already uploaded / failed per chapter, with checkboxes so you can upload just a few.                                                                                                                                       |
| **Pause / Stop**            | Stop lands after the current chapter; nothing is lost.                                                                                                                                                                                     |
| **Cloudflare hand-off**     | When a challenge hits, the app pauses, shows a banner, and waits. Solve it in the Chrome window, press **"I passed it — resume"** (or **"Restart browser & resume"**). The chapter is retried — never skipped, never marked failed.        |
| **Security check hand-off** | comix's own rotate-the-circle captcha (`/@waf/`). The app pauses, shows a banner, and **polls the tab itself** — drag the circle until the picture lines up and press Verify; uploading resumes automatically with no button press needed. |
| **Refresh Cloudflare**      | One button restarts the browser session and re-saves cookies to `config.json`.                                                                                                                                                             |

### Typical flow

1. Search the series → double-click the result.
2. Pick the chapters folder (or accept the remembered one).
3. Tick the chapters you want (pending ones are pre-ticked).
4. **Start upload.**
5. If Cloudflare pops up → solve it in Chrome → **Resume**.
6. If the security check pops up → drag the circle in Chrome → press **Verify**
   → it resumes by itself.

Verification is still on you — the app just stops making you redo everything else.

---

## 4. Verification screens

comix puts two different gates in front of you, and the app tells them apart:

|            | Cloudflare                                                | Security check (WAF)                                              |
| ---------- | --------------------------------------------------------- | ----------------------------------------------------------------- |
| Looks like | "Just a moment…" / "Attention required"                   | Dark card, _Verify you're human_, a circle you drag to rotate     |
| Where      | `comix.to`                                                | `comix.to/@waf/challenge`                                         |
| Fixed by   | Refreshing `cf_clearance`                                 | Solving the puzzle in the open browser                            |
| Recovery   | **I passed it — resume**, or **Restart browser & resume** | Automatic — the app polls until the challenge page navigates away |
| Browser    | May be relaunched                                         | Never relaunched (that would discard the puzzle)                  |

Both are detected on page load, before the upload form is touched, and again
every second during an upload. A challenge that appears **mid-upload** pauses the
run instead of losing it: the in-flight attempt is retried from scratch once the
gate clears, and the chapter is never written to history while paused.

If a gate keeps reappearing on the same chapter (default: 5 times), the app gives
up on that one chapter rather than looping forever, and moves on.

### Tunables (`config.json`)

```json
"waf": {
  "max_wait_seconds": 600,      // how long to wait for you to solve it
  "poll_interval_seconds": 2,   // how often the tab is re-checked
  "max_pauses_per_chapter": 5   // give up on a chapter after this many gates
}
```

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
- **Upload history** lives in `.upload_history_<series-id>.json`,
  failures in `.upload_failed_<series-id>.json`. "Reset history" clears both.
- **Saved series + recent folders** live in `series_library.json`.
- **Challenge evidence dumps** land in `.waf_debug/` (screenshots, page HTML,
  `meta.json`) and are safe to delete.
- The browser profile in `browser_profile/` keeps you logged in between runs.
  Clearance for both gates lives there, so a solved security check usually
  covers the rest of the run.

---

## 7. Project layout

```
core.py      shared engine: config, chapter scanning, history, uploading, site search
session.py   background browser worker (persistent Chrome + job queue)
gui.py       Tkinter desktop app
uploader.py  command-line interface
```
