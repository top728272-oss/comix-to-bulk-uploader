# Comix Uploader

Automated batch chapter uploader for comix.to.

---

## 1. Installation

```bash
# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate  # On Windows

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

---

## 2. Configuration Setup

Copy `config.example.json` and rename it to `config.json`:

```bash
copy config.example.json config.json
```

---

## 3. How to Fill `config.json`

Open `https://comix.to` in your browser, log in, and press `F12` to open Developer Tools:

### 1. Get `user_agent`
1. Go to the **Console** tab in DevTools.
2. Type `navigator.userAgent` and press Enter.
3. Copy the output string and paste it into `"user_agent"` in `config.json`.

### 2. Get Cookies (`remember_web_*`, `session`, `cf_clearance`)
1. Go to the **Application** tab (or **Storage** in Firefox) in DevTools.
2. Expand **Cookies** in the left sidebar and click `https://comix.to`.
3. Locate and copy the following cookie values into `config.json`:
   - **`remember_web_<HASH>`**: Copy the full cookie name (including the hash) and its value.
   - **`session`**: Copy its value.
   - **`cf_clearance`**: Copy its value.

---

## 4. Usage

Run the uploader interactively:

```bash
python uploader.py
```

Follow the on-screen prompts to specify:
- Target upload URL
- Local folder containing chapter archives (`.zip`, `.cbz`, `.rar`, etc.)
- Chapter title pattern (e.g. `Chapter {ch}`)
- Group name (e.g. `ScanlationGroup` or blank for solo)
- Chapter range (optional)
