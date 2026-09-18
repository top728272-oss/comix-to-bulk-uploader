"""Comix Uploader — desktop GUI.

Search the site, pick a series once, and upload chapters without retyping
URLs, folders, group names or title patterns. Cloudflare and comix's own
security check are solved in the real Chrome window the app drives — the app
watches the tabs and resumes on its own once a gate clears.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from core import (
    WAF_CLEARANCE_MARGIN_SECONDS,
    ConfigStore,
    UploadParams,
    get_failed_file,
    get_history_file,
    load_failed,
    load_history,
    load_library,
    remember_folder,
    reset_history,
    save_library,
    scan_folder,
    series_id_from_url,
    upload_url_for,
    upsert_series,
    waf_clearance_seconds_left,
)
from session import BrowserSession

STATUS_COLORS = {
    "pending": "#1f2933",
    "queued": "#1f2933",
    "uploaded": "#7b8794",
    "uploading": "#0b6bcb",
    "done": "#1a7f37",
    "failed": "#c0392b",
    "cloudflare": "#b45309",
    "waf": "#b45309",
}


def _attach(parent, child, weight: int = 1) -> None:
    """Attach a child to a PanedWindow, or pack it into a plain frame."""
    if isinstance(parent, ttk.PanedWindow):
        parent.add(child, weight=weight)
    else:
        child.pack(fill="both", expand=True)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Comix Uploader")
        self.configure(bg="#f4f6f8")

        # Never open taller/wider than the screen — otherwise the run controls
        # end up below the bottom edge and look like they don't exist.
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = max(900, min(1180, screen_w - 80))
        height = max(620, min(1000, screen_h - 90))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(760, 520)

        self.config_store = ConfigStore()
        self.library = load_library()
        self.chapters: dict[str, dict] = {}
        self.current_hid = ""
        self.current_name = ""
        self.run_active = False
        self.total_queued = 0
        self.search_results: list[dict] = []
        self.saved_series: list[dict] = sorted(
            self.library.get("series", []),
            key=lambda s: s.get("last_used", 0),
            reverse=True,
        )
        self.series_panel_open = True

        self._build_style()
        self._build_ui()

        self.session = BrowserSession(self.config_store, emit=self.emit)
        self.session.start()

        self._load_saved_series()
        self._apply_defaults()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#f4f6f8")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("TLabel", background="#f4f6f8", foreground="#1f2933")
        style.configure("Card.TLabel", background="#ffffff", foreground="#1f2933")
        style.configure("Header.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Sub.TLabel", foreground="#66727f")
        style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=5)
        style.configure("Accent.TButton", padding=6)
        style.configure(
            "Go.TButton",
            padding=6,
            font=("Segoe UI", 10, "bold"),
            background="#1a7f37",
            foreground="#ffffff",
        )
        style.map("Go.TButton", background=[("disabled", "#b9c4cf")])
        style.configure("TEntry", padding=4)
        style.configure("Treeview", rowheight=24, fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=10)
        container.pack(fill="both", expand=True)

        self._build_menubar()
        self._build_header(container)
        # Run controls sit near the top so they can never be pushed off-screen.
        self._build_controls(container)
        self._build_settings(container)
        # Search panel is optional — it can be folded away to free up space.
        self._build_series_panel(container)

        # Chapters + log share the remaining height and stay resizable.
        pane = ttk.PanedWindow(container, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(0, 6))
        self._build_chapters(pane)
        self._build_log(pane)

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self)
        run_menu = tk.Menu(menubar, tearoff=0)
        run_menu.add_command(label="Start upload", command=self.start_upload)
        run_menu.add_command(label="Pause / Resume", command=self.toggle_pause)
        run_menu.add_command(label="Stop", command=self.stop_upload)
        run_menu.add_separator()
        run_menu.add_command(label="Scan folder", command=self.scan_chapters)
        run_menu.add_command(label="Refresh Cloudflare", command=self.refresh_clearance)
        menubar.add_cascade(label="Run", menu=run_menu)
        self.configure(menu=menubar)
        self.bind_all("<Control-Return>", lambda _e: self.start_upload())

    def _build_header(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 10))

        ttk.Label(bar, text="Comix Uploader", style="Header.TLabel").pack(side="left")
        ttk.Label(
            bar,
            text="  ·  search once, upload without the copy-paste",
            style="Sub.TLabel",
        ).pack(side="left", pady=(4, 0))

        right = ttk.Frame(bar)
        right.pack(side="right")

        self.browser_status = ttk.Label(
            right, text="Browser: closed", style="Sub.TLabel"
        )
        self.browser_status.pack(side="left", padx=(0, 8))

        ttk.Button(
            right, text="Open browser", command=self.open_browser, width=14
        ).pack(side="left", padx=2)

    def _build_series_panel(self, parent) -> None:
        row = ttk.Frame(parent)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        self.series_row_frame = row
        # Only shown when the user asks for it (or on a fresh, empty library).
        self.series_panel_open = not self.saved_series
        if self.series_panel_open:
            row.pack(fill="x", pady=(0, 8), before=self.settings_card)
        self._sync_panel_btn()

        # --- search
        card = ttk.Frame(row, style="Card.TFrame", padding=10)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(card, text="Search comix.to", style="Section.TLabel").pack(anchor="w")

        line = ttk.Frame(card, style="Card.TFrame")
        line.pack(fill="x", pady=(6, 6))
        self.search_var = tk.StringVar()
        entry = ttk.Entry(line, textvariable=self.search_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.bind("<Return>", lambda _e: self.do_search())
        ttk.Button(line, text="Search", command=self.do_search, width=10).pack(
            side="left"
        )

        self.search_status = ttk.Label(card, text="", style="Sub.TLabel")
        self.search_status.pack(anchor="w")

        self.results = tk.Listbox(card, height=3, activestyle="dotbox")
        self.results.pack(fill="both", expand=True, pady=(4, 6))
        self.results.bind("<Double-Button-1>", lambda _e: self.use_selected_result())
        ttk.Button(
            card, text="Use selected result", command=self.use_selected_result
        ).pack(anchor="w")

        # --- saved series
        card2 = ttk.Frame(row, style="Card.TFrame", padding=10)
        card2.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(card2, text="Saved series", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            card2,
            text="Double-click to load  ·  no more pasting URLs",
            style="Sub.TLabel",
        ).pack(anchor="w")

        self.saved_list = tk.Listbox(card2, height=3, activestyle="dotbox")
        self.saved_list.pack(fill="both", expand=True, pady=(6, 6))
        self.saved_list.bind("<Double-Button-1>", lambda _e: self.use_saved_series())

        btns = ttk.Frame(card2, style="Card.TFrame")
        btns.pack(fill="x")
        ttk.Button(btns, text="Load", command=self.use_saved_series).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(btns, text="Remove", command=self.remove_saved_series).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="Add by ID / URL", command=self.add_by_id).pack(
            side="left", padx=4
        )

    def _build_settings(self, parent) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        self.target_var = tk.StringVar()

        pick = ttk.Frame(card, style="Card.TFrame")
        pick.grid(row=0, column=0, columnspan=6, sticky="ew", pady=(0, 4))
        ttk.Label(pick, text="Series", style="Card.TLabel").pack(side="left")
        self.saved_combo = ttk.Combobox(pick, width=46, state="readonly", values=[])
        self.saved_combo.pack(side="left", padx=(6, 6))
        self.saved_combo.bind("<<ComboboxSelected>>", self._on_saved_combo)
        ttk.Button(pick, text="Load", command=self.use_saved_series, width=7).pack(
            side="left", padx=(0, 6)
        )
        self.panel_btn = ttk.Button(
            pick, text="Search panel ▾", command=self.toggle_series_panel, width=15
        )
        self.panel_btn.pack(side="left")

        self.target_label = ttk.Label(
            card,
            text="No series selected — pick one above or search the site",
            style="Card.TLabel",
            font=("Segoe UI", 10, "bold"),
        )
        self.target_label.grid(row=1, column=0, columnspan=6, sticky="w", pady=(0, 6))

        ttk.Label(card, text="Upload URL", style="Card.TLabel").grid(
            row=2, column=0, sticky="w"
        )
        self.url_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.url_var).grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=(6, 6)
        )
        ttk.Button(
            card, text="Use this URL", command=self.use_typed_url, width=13
        ).grid(row=2, column=4, sticky="w")
        ttk.Button(
            card, text="Open page", command=self.open_upload_page, width=11
        ).grid(row=2, column=5, sticky="w", padx=(6, 0))

        ttk.Label(card, text="Chapters folder", style="Card.TLabel").grid(
            row=3, column=0, sticky="w", pady=(8, 0)
        )
        self.folder_var = tk.StringVar()
        self.folder_box = ttk.Combobox(card, textvariable=self.folder_var)
        self.folder_box.grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=(6, 6), pady=(8, 0)
        )
        self.folder_box.bind("<<ComboboxSelected>>", lambda _e: self.scan_chapters())
        ttk.Button(card, text="Browse", command=self.browse_folder, width=13).grid(
            row=3, column=4, sticky="w", pady=(8, 0)
        )
        ttk.Button(card, text="Scan", command=self.scan_chapters, width=11).grid(
            row=3, column=5, sticky="w", padx=(6, 0), pady=(8, 0)
        )

        # One compact row for the per-upload options.
        opts = ttk.Frame(card, style="Card.TFrame")
        opts.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        self.settings_card = card

        ttk.Label(opts, text="Title", style="Card.TLabel").pack(side="left")
        self.title_var = tk.StringVar(value="Chapter {ch}")
        ttk.Entry(opts, textvariable=self.title_var, width=16).pack(
            side="left", padx=(5, 12)
        )

        ttk.Label(opts, text="Group", style="Card.TLabel").pack(side="left")
        self.group_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.group_var, width=14).pack(
            side="left", padx=(5, 12)
        )

        self.official_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Official", variable=self.official_var).pack(
            side="left", padx=(0, 12)
        )

        ttk.Label(opts, text="Delay", style="Card.TLabel").pack(side="left")
        self.delay_var = tk.IntVar(value=self.config_store.delay)
        ttk.Spinbox(opts, from_=0, to=600, textvariable=self.delay_var, width=5).pack(
            side="left", padx=(5, 12)
        )

        ttk.Label(opts, text="From ch.", style="Card.TLabel").pack(side="left")
        self.start_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.start_var, width=6).pack(
            side="left", padx=(5, 10)
        )

        ttk.Label(opts, text="To ch.", style="Card.TLabel").pack(side="left")
        self.end_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.end_var, width=6).pack(
            side="left", padx=(5, 0)
        )

        # Window behaviour: Chrome stays out of the way while it works, and
        # only comes forward when a verification needs a human.
        win = ttk.Frame(card, style="Card.TFrame")
        win.grid(row=5, column=0, columnspan=6, sticky="ew", pady=(8, 0))

        self.background_var = tk.BooleanVar(
            value=bool(self.config_store.get("keep_window_in_background", True))
        )
        ttk.Checkbutton(
            win,
            text="Keep Chrome in the background (never steal focus)",
            variable=self.background_var,
            command=self._save_window_prefs,
        ).pack(side="left", padx=(0, 14))

        self.focus_challenge_var = tk.BooleanVar(
            value=bool(self.config_store.get("focus_on_challenge", True))
        )
        ttk.Checkbutton(
            win,
            text="Focus Chrome when a verification is needed",
            variable=self.focus_challenge_var,
            command=self._save_window_prefs,
        ).pack(side="left")

    def _build_chapters(self, parent) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        _attach(parent, card, weight=3)

        head = ttk.Frame(card, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="Chapters", style="Section.TLabel").pack(side="left")
        self.counts_label = ttk.Label(head, text="", style="Sub.TLabel")
        self.counts_label.pack(side="left", padx=(10, 0))
        ttk.Label(
            head,
            text="   click a row to include/exclude it, then Start upload above",
            style="Sub.TLabel",
        ).pack(side="left", padx=(4, 0))
        ttk.Button(head, text="Reset history", command=self.reset_series_history).pack(
            side="right", padx=2
        )
        ttk.Button(head, text="Select all", command=self.select_all).pack(
            side="right", padx=2
        )
        ttk.Button(head, text="Select pending", command=self.select_pending).pack(
            side="right", padx=2
        )
        ttk.Button(head, text="Clear selection", command=self.clear_selection).pack(
            side="right", padx=2
        )

        cols = ("sel", "chapter", "file", "status")
        self.tree = ttk.Treeview(
            card, columns=cols, show="headings", height=6, selectmode="none"
        )
        self.tree.heading("sel", text="✓")
        self.tree.heading("chapter", text="Chapter")
        self.tree.heading("file", text="File")
        self.tree.heading("status", text="Status")
        self.tree.column("sel", width=34, anchor="center", stretch=False)
        self.tree.column("chapter", width=90, anchor="center", stretch=False)
        self.tree.column("file", width=520, anchor="w")
        self.tree.column("status", width=230, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click)

        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=color)

    def _build_controls(self, parent) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=8)
        card.pack(fill="x", pady=(0, 8))

        self.start_btn = ttk.Button(
            card,
            text="▶  Start upload",
            command=self.start_upload,
            style="Go.TButton",
            width=18,
        )
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(
            card, text="Pause", command=self.toggle_pause, width=9, state="disabled"
        )
        self.pause_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(
            card, text="Stop", command=self.stop_upload, width=7, state="disabled"
        )
        self.stop_btn.pack(side="left")

        self.progress = ttk.Progressbar(card, mode="determinate", length=200)
        self.progress.pack(side="left", padx=14)
        self.run_label = ttk.Label(card, text="Idle", style="Sub.TLabel")
        self.run_label.pack(side="left")

        ttk.Label(
            card,
            text="   (Ctrl+Enter)",
            style="Sub.TLabel",
        ).pack(side="left")

        ttk.Button(
            card, text="Refresh Cloudflare", command=self.refresh_clearance, width=18
        ).pack(side="right")

        # Anchor the Cloudflare banner here so it always appears under the toolbar.
        self.cf_anchor = ttk.Frame(parent, height=0)
        self.cf_anchor.pack(fill="x")

        # Cloudflare banner (hidden until needed, shown right under the toolbar)
        self.cf_frame = ttk.Frame(parent, style="Card.TFrame", padding=8)
        self.cf_label = ttk.Label(
            self.cf_frame,
            text="",
            foreground="#b45309",
            font=("Segoe UI", 10, "bold"),
        )
        self.cf_label.pack(side="left")
        ttk.Button(self.cf_frame, text="Resume now", command=self.resume_after_cf).pack(
            side="left", padx=8
        )
        ttk.Button(
            self.cf_frame,
            text="Restart browser & resume",
            command=self.restart_and_resume,
        ).pack(side="left", padx=4)
        ttk.Button(self.cf_frame, text="Stop run", command=self.stop_upload).pack(
            side="left", padx=4
        )

        # Security-check banner: comix's own rotate-the-circle captcha. Same
        # shape as the Cloudflare one, but there is no restart option because
        # the clearance lives in the browser we already have open.
        self.waf_frame = ttk.Frame(parent, style="Card.TFrame", padding=8)
        self.waf_label = ttk.Label(
            self.waf_frame,
            text="",
            foreground="#b45309",
            font=("Segoe UI", 10, "bold"),
        )
        self.waf_label.pack(side="left")
        ttk.Button(
            self.waf_frame,
            text="I solved it — resume now",
            command=self.resume_after_waf,
        ).pack(side="left", padx=8)
        ttk.Button(self.waf_frame, text="Stop run", command=self.stop_upload).pack(
            side="left", padx=4
        )

    def _build_log(self, parent) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        _attach(parent, card, weight=1)

        head = ttk.Frame(card, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Log", style="Section.TLabel").pack(side="left")
        ttk.Button(head, text="Clear", command=self.clear_log, width=8).pack(
            side="right"
        )

        self.log = tk.Text(
            card,
            height=6,
            wrap="word",
            bg="#ffffff",
            fg="#1f2933",
            relief="solid",
            borderwidth=1,
            font=("Consolas", 9),
        )
        scroll = ttk.Scrollbar(card, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

    # ------------------------------------------------------------- helpers

    def emit(self, event: str, payload=None) -> None:
        """Thread-safe hand-off from the worker thread to the Tk main loop."""
        try:
            self.after(0, lambda: self._dispatch(event, payload))
        except Exception:
            pass  # window already closed

    def _dispatch(self, event: str, payload) -> None:
        handler = getattr(self, f"_on_{event}", None)
        if handler:
            try:
                handler(payload)
            except Exception as ex:  # keep the UI alive
                self.append_log(f"[!] UI error on {event}: {ex}")

    def append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", str(message).rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _apply_defaults(self) -> None:
        defaults = self.library.get("defaults", {})
        self.group_var.set(defaults.get("group", self.config_store.group))
        self.title_var.set(defaults.get("title_pattern", "Chapter {ch}"))
        self.official_var.set(bool(defaults.get("official", False)))
        self.delay_var.set(int(defaults.get("delay", self.config_store.delay)))

        folders = self.library.get("recent_folders", [])
        self.folder_box["values"] = folders
        if folders:
            self.folder_var.set(folders[0])

        if self.saved_list.size():
            self.saved_list.selection_set(0)

    def _save_defaults(self) -> None:
        self.library.setdefault("defaults", {}).update(
            {
                "group": self.group_var.get(),
                "title_pattern": self.title_var.get(),
                "official": self.official_var.get(),
                "delay": self.delay_var.get(),
            }
        )
        save_library(self.library)

    def _save_window_prefs(self) -> None:
        self.config_store.set(
            "keep_window_in_background", bool(self.background_var.get())
        )
        self.config_store.set("focus_on_challenge", bool(self.focus_challenge_var.get()))

    def _load_saved_series(self) -> None:
        self.saved_list.delete(0, "end")
        self.saved_series = sorted(
            self.library.get("series", []),
            key=lambda s: s.get("last_used", 0),
            reverse=True,
        )
        labels = []
        for entry in self.saved_series:
            label = f"{entry.get('name') or entry['hid']}  ({entry['hid']})"
            labels.append(label)
            self.saved_list.insert("end", label)
        self.saved_combo["values"] = labels

    # --------------------------------------------------------- series flow

    def do_search(self) -> None:
        query = self.search_var.get().strip()
        if not query:
            return
        self._warn_if_waf_expiring()
        self.search_status.configure(text="Searching…")
        self.results.delete(0, "end")
        self.search_results = []
        self.session.submit(("search", query))

    def _warn_if_waf_expiring(self, mention_missing: bool = False) -> None:
        """Log-only heads-up before an action that may hit the security check."""
        left = waf_clearance_seconds_left(self.config_store.data)
        if left is None:
            if mention_missing:
                self.append_log(
                    "[i] No saved security clearance — if comix's check "
                    "appears, solve it once; it holds for about an hour."
                )
            return
        if left > WAF_CLEARANCE_MARGIN_SECONDS:
            return
        if left <= 0:
            state = "has expired"
        else:
            state = f"expires in ~{int(left // 60)} min"
        self.append_log(
            f"[i] Security clearance {state} — the security check will likely "
            "appear. Solving it once holds for about an hour."
        )

    def use_selected_result(self) -> None:
        if not self.search_results:
            return
        idx = self.results.curselection()
        if not idx:
            return
        self.select_series(self.search_results[idx[0]])

    def use_saved_series(self) -> None:
        idx = self.saved_list.curselection()
        if idx:
            entry = self.saved_series[idx[0]]
        else:
            i = self.saved_combo.current()
            if i is None or i < 0 or i >= len(self.saved_series):
                return
            entry = self.saved_series[i]
        settings = {k: v for k, v in entry.items() if k not in ("hid", "name")}
        self.select_series(
            {"hid": entry["hid"], "name": entry.get("name", "")},
            restore=settings,
        )

    def _on_saved_combo(self, _event=None) -> None:
        i = self.saved_combo.current()
        self.saved_list.selection_clear(0, "end")
        if i is not None and i >= 0:
            self.saved_list.selection_set(i)
        self.use_saved_series()

    def _sync_panel_btn(self) -> None:
        if hasattr(self, "panel_btn"):
            self.panel_btn.configure(
                text="Search panel ▾" if self.series_panel_open else "Search panel ▸"
            )

    def toggle_series_panel(self) -> None:
        if self.series_panel_open:
            self.series_row_frame.pack_forget()
            self.series_panel_open = False
            self.panel_btn.configure(text="Search panel ▸")
        else:
            self.series_row_frame.pack(fill="x", pady=(0, 8), before=self.settings_card)
            self.series_panel_open = True
            self.panel_btn.configure(text="Search panel ▾")

    def remove_saved_series(self) -> None:
        idx = self.saved_list.curselection()
        if not idx:
            return
        entry = self.saved_series[idx[0]]
        self.library["series"] = [
            s for s in self.library["series"] if s["hid"] != entry["hid"]
        ]
        save_library(self.library)
        self._load_saved_series()
        self.append_log(f"[i] Removed {entry['hid']} from saved series.")

    def add_by_id(self) -> None:
        value = simpledialog.askstring(
            "Add series",
            "Paste a series ID, title URL or upload URL:",
            parent=self,
        )
        if not value:
            return
        self.use_typed_url(value.strip())

    def use_typed_url(self, value: str = "") -> None:
        raw = (value or self.url_var.get()).strip()
        if not raw:
            return
        hid = series_id_from_url(raw)
        if not hid:
            messagebox.showwarning("Invalid", "Could not read a series ID from that.")
            return
        self.select_series({"hid": hid, "name": ""})

    def select_series(self, result: dict, restore: dict | None = None) -> None:
        hid = result["hid"]
        name = result.get("name") or ""
        self.current_hid = hid
        self.current_name = name
        self.url_var.set(upload_url_for(hid))
        self.target_label.configure(text=f"{name or '(resolving name…)'}  ·  {hid}")

        if not name and self.session.browser_open:
            self.session.submit(("resolve", hid))

        entry = upsert_series(
            self.library,
            hid,
            name,
            **{
                k: v
                for k, v in (restore or {}).items()
                if k in ("folder", "group", "title_pattern", "official", "delay")
            },
        )
        self._load_saved_series()

        if restore:
            if restore.get("folder"):
                self.folder_var.set(restore["folder"])
            if restore.get("group") is not None and restore.get("group") != "":
                self.group_var.set(restore["group"])
            if restore.get("title_pattern"):
                self.title_var.set(restore["title_pattern"])
            if "official" in restore:
                self.official_var.set(bool(restore["official"]))
            if restore.get("delay"):
                self.delay_var.set(int(restore["delay"]))
        elif entry.get("folder"):
            self.folder_var.set(entry["folder"])

        try:
            label = f"{name or hid}  ({hid})"
            values = list(self.saved_combo["values"])
            if label not in values:
                self.saved_combo["values"] = values + [label]
            self.saved_combo.set(label)
        except Exception:
            pass

        self.append_log(f"[i] Target set: {upload_url_for(hid)}")
        # Fold the search panel away so the chapter list gets the space.
        if self.series_panel_open:
            self.toggle_series_panel()
        self.scan_chapters()

    # ------------------------------------------------------------- chapters

    def browse_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select chapters folder")
        if folder:
            self.folder_var.set(folder)
            self.scan_chapters()

    def scan_chapters(self) -> None:
        folder = self.folder_var.get().strip().strip('"')
        self.chapters = {}

        for row in self.tree.get_children():
            self.tree.delete(row)

        if not folder or not Path(folder).is_dir():
            self.counts_label.configure(text="No folder selected")
            return

        remember_folder(self.library, folder)
        self.folder_box["values"] = self.library.get("recent_folders", [])
        self._persist_series_settings(folder=folder)

        items = scan_folder(folder)
        if not items:
            self.counts_label.configure(text="No chapter archives found")
            return

        history = (
            load_history(get_history_file(self.url_var.get()))
            if self.current_hid
            else set()
        )
        failed = (
            load_failed(get_failed_file(self.url_var.get())) if self.current_hid else {}
        )

        for ch_num, path in items:
            key = str(ch_num)
            if key in history:
                status, detail = "uploaded", "already uploaded"
            elif key in failed:
                status, detail = "failed", str(failed[key])[:60]
            else:
                status, detail = "pending", ""
            self.chapters[key] = {
                "ch": ch_num,
                "path": path,
                "status": status,
                "detail": detail,
                "selected": status != "uploaded",
            }

        self._refresh_tree()

    def _refresh_tree(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)

        pending = 0
        for key, data in sorted(self.chapters.items(), key=lambda kv: float(kv[0])):
            mark = "☑" if data["selected"] else "☐"
            detail = data["detail"]
            status_text = data["status"] + (f" — {detail}" if detail else "")
            if data["status"] == "pending":
                pending += 1
            self.tree.insert(
                "",
                "end",
                iid=key,
                values=(mark, f"{data['ch']:g}", data["path"].name, status_text),
                tags=(data["status"],),
            )

        total = len(self.chapters)
        done = sum(1 for d in self.chapters.values() if d["status"] == "done")
        failed = sum(1 for d in self.chapters.values() if d["status"] == "failed")
        self.counts_label.configure(
            text=f"{total} found  ·  {pending} pending  ·  {done} done this run  ·  {failed} failed"
        )

    def _on_tree_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row or self.run_active:
            return
        data = self.chapters.get(row)
        if not data:
            return
        # Clicking anywhere on the row toggles it — no need to hit the tiny box.
        data["selected"] = not data["selected"]
        self._refresh_tree()

    def select_pending(self) -> None:
        for data in self.chapters.values():
            data["selected"] = data["status"] not in ("uploaded", "done")
        self._refresh_tree()

    def select_all(self) -> None:
        for data in self.chapters.values():
            data["selected"] = True
        self._refresh_tree()

    def clear_selection(self) -> None:
        for data in self.chapters.values():
            data["selected"] = False
        self._refresh_tree()

    def reset_series_history(self) -> None:
        if not self.current_hid:
            return
        if not messagebox.askyesno(
            "Reset history",
            f"Forget uploaded/failed records for {self.current_hid}?",
        ):
            return
        reset_history(upload_url_for(self.current_hid))
        self.append_log(f"[i] History reset for {self.current_hid}.")
        self.scan_chapters()

    def _persist_series_settings(self, **extra) -> None:
        if not self.current_hid:
            return
        upsert_series(
            self.library,
            self.current_hid,
            self.current_name,
            group=self.group_var.get(),
            title_pattern=self.title_var.get(),
            official=self.official_var.get(),
            delay=self.delay_var.get(),
            **extra,
        )
        self._save_defaults()

    # ---------------------------------------------------------------- run

    def open_browser(self) -> None:
        self.session.submit(("open",))

    def open_upload_page(self) -> None:
        if not self.current_hid:
            return
        self.session.submit(("open",))
        self.session.submit(("goto", upload_url_for(self.current_hid)))

    def refresh_clearance(self) -> None:
        self.append_log(
            "[i] Opening Chrome — pass Cloudflare / log in, then come back."
        )
        self.session.submit(("restart",))
        self.session.submit(("save_cookies",))

    def start_upload(self) -> None:
        if self.run_active:
            return
        url = self.url_var.get().strip()
        folder = self.folder_var.get().strip().strip('"')
        if not url or not self.current_hid:
            messagebox.showwarning("Missing series", "Pick or search a series first.")
            return
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("Missing folder", "Choose a valid chapters folder.")
            return

        selected = [k for k, d in self.chapters.items() if d["selected"]]
        if not selected:
            messagebox.showwarning("Nothing selected", "No chapters are selected.")
            return

        def to_float(value: str):
            value = value.strip()
            return float(value) if value else None

        params = UploadParams(
            url=url,
            folder=folder,
            title_pattern=self.title_var.get(),
            group=self.group_var.get(),
            official=self.official_var.get(),
            delay=self.delay_var.get(),
            start=to_float(self.start_var.get()),
            end=to_float(self.end_var.get()),
            selected=selected,
        )

        self._persist_series_settings(folder=folder)
        self._warn_if_waf_expiring(mention_missing=True)
        self.total_queued = len(selected)
        self.progress["value"] = 0
        self.progress["maximum"] = max(1, len(selected))
        self.run_active = True
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        self.run_label.configure(text="Starting…")
        self.append_log(f"[▶] Starting: {len(selected)} chapter(s) → {url}")
        self.session.submit(("start", params))

    def toggle_pause(self) -> None:
        if not self.run_active:
            return
        if self.pause_btn["text"] == "Pause":
            self.session.submit(("pause", True))
            self.pause_btn.configure(text="Resume")
        else:
            self.session.submit(("pause", False))
            self.pause_btn.configure(text="Pause")

    def stop_upload(self) -> None:
        if not self.run_active:
            return
        self.append_log("[■] Stopping after the current chapter…")
        self.session.submit(("stop",))

    def resume_after_cf(self) -> None:
        self._hide_cf_banner()
        self.session.submit(("resume_cf",))

    def restart_and_resume(self) -> None:
        self._hide_cf_banner()
        self.session.submit(("restart_cf",))

    def _show_cf_banner(self, chapter: str) -> None:
        self.cf_label.configure(
            text=(
                f"Cloudflare is checking the browser (chapter {chapter}) — it "
                "usually clears by itself; uploading resumes automatically."
            )
        )
        self.cf_frame.pack(fill="x", pady=(0, 8), before=self.cf_anchor)

    def _hide_cf_banner(self) -> None:
        if self.cf_frame.winfo_manager():
            self.cf_frame.pack_forget()

    def resume_after_waf(self) -> None:
        self._hide_waf_banner()
        self.session.submit(("resume_waf",))

    def _show_waf_banner(self, chapter: str) -> None:
        self.waf_label.configure(
            text=(
                f"Security check on chapter {chapter} — drag the circle in Chrome "
                "until it lines up, then press Verify."
            )
        )
        self.waf_frame.pack(fill="x", pady=(0, 8), before=self.cf_anchor)

    def _hide_waf_banner(self) -> None:
        if self.waf_frame.winfo_manager():
            self.waf_frame.pack_forget()

    # ------------------------------------------------------ worker events

    def _on_log(self, message) -> None:
        self.append_log(message)

    def _on_browser(self, state) -> None:
        text = {
            "starting": "Browser: opening…",
            "ready": "Browser: ready",
            "closed": "Browser: closed",
        }.get(state, f"Browser: {state}")
        self.browser_status.configure(text=text)
        if state == "ready":
            self.append_log("[i] Chrome session ready.")

    def _on_search_results(self, results) -> None:
        self.search_results = results
        self.results.delete(0, "end")
        for r in results:
            meta = r.get("meta") or ""
            self.results.insert(
                "end", f"{r['name']}  —  {r['hid']}" + (f"  ({meta})" if meta else "")
            )
        self.search_status.configure(text=f"{len(results)} result(s)")
        if not results:
            self.append_log("[i] No results.")

    def _on_series_name(self, payload) -> None:
        hid, name = payload
        if hid != self.current_hid:
            return
        self.current_name = name
        self.target_label.configure(text=f"{name}  ·  {hid}")
        upsert_series(self.library, hid, name)
        self._load_saved_series()

    def _on_job_blocked(self, payload) -> None:
        """A search or name lookup was stopped by a verification screen.

        The worker already left the browser sitting on the challenge; all this
        does is tell the user how to proceed once they've solved it.
        """
        context, kind = payload
        label = "Cloudflare challenge" if kind == "cloudflare" else "security check"
        if context == "search":
            self.search_status.configure(
                text=f"Blocked by the {label} — solve it in Chrome, then search again"
            )
            self.append_log(
                f"[i] Solve the {label} in the Chrome window and retry the "
                "search later."
            )
        elif context == "resolve":
            if self.current_hid:
                self.target_label.configure(
                    text=(
                        f"{self.current_hid}  ·  name unavailable until the "
                        f"{label} is solved"
                    )
                )
            self.append_log(
                f"[i] Solve the {label} in the Chrome window, then re-pick the "
                "series (or paste its URL again) to load its name."
            )

    def _on_chapter_status(self, payload) -> None:
        key, status, detail = payload
        data = self.chapters.get(key)
        if data is None:
            return
        data["status"] = status
        data["detail"] = detail or ""
        if status in ("done", "uploading"):
            data["selected"] = False
        if status == "done":
            self.progress.step(1)
        self._refresh_tree()

    def _on_chunk(self, payload) -> None:
        key, count = payload
        self.run_label.configure(text=f"Chapter {key} — {count} chunks")
        self.update_idletasks()

    def _on_cloudflare(self, chapter) -> None:
        self.run_label.configure(text="Waiting for Cloudflare…")
        self._show_cf_banner(chapter)
        self.append_log(
            "[!] Cloudflare challenge — it usually clears by itself and "
            "uploading resumes automatically. You only need to act if it "
            "doesn't (use the banner buttons)."
        )

    def _on_waf(self, chapter) -> None:
        self.run_label.configure(text="Waiting for the security check…")
        self._show_waf_banner(chapter)
        self.append_log(
            "[!] comix security check — in the Chrome window, drag the circle "
            "until the picture lines up and press Verify. "
            "Uploading resumes automatically."
        )

    def _on_challenge_cleared(self, payload) -> None:
        kind, chapter = payload
        label = "Cloudflare" if kind == "cloudflare" else "Security check"
        self._hide_cf_banner()
        self._hide_waf_banner()
        self.run_label.configure(text="Resumed — uploading…")
        self.append_log(
            f"[✓] {label} cleared on chapter {chapter} — resuming automatically."
        )

    def _on_run_state(self, state) -> None:
        if state == "running":
            self.run_label.configure(text="Uploading…")
        elif state == "paused":
            self.run_label.configure(text="Paused")
        elif state == "stopping":
            self.run_label.configure(text="Stopping…")
        elif state == "idle":
            self.run_active = False
            self.run_label.configure(text="Idle")
            self.start_btn.configure(state="normal")
            self.pause_btn.configure(state="disabled", text="Pause")
            self.stop_btn.configure(state="disabled")
            self._hide_cf_banner()
            self._hide_waf_banner()

    def _on_summary(self, summary) -> None:
        self.progress["value"] = self.progress["maximum"]
        self.append_log(
            "\n=== Run finished ===\n"
            f"Processed: {summary.processed}   "
            f"Uploaded: {summary.succeeded}   Failed: {summary.failed}"
        )
        for ch, filename, err in summary.failures:
            self.append_log(f"  - Chapter {ch:g} ({filename}): {err}")
        self.run_label.configure(
            text=f"Done — {summary.succeeded} uploaded, {summary.failed} failed"
        )

    # ------------------------------------------------------------- teardown

    def on_close(self) -> None:
        self._save_defaults()
        try:
            self.session.shutdown()
            self.session.join(timeout=5)
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
