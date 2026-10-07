"""Command-line entry point for the Comix uploader.

Same engine as the GUI (`core.py`). Run `python gui.py` if you'd rather use
the desktop app.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from core import (
    ConfigStore,
    Control,
    UploadParams,
    focus_challenge_window,
    get_failed_file,
    get_history_file,
    launch_context,
    load_failed,
    load_history,
    parse_chapter_mapping,
    read_devtools_port,
    reset_history,
    scan_folder,
    set_config_cookies,
    sync_cookies_from_context,
)
from parallel import run_upload_batch_auto
import solver


def prompt_input(prompt_text: str, default_val: str = "") -> str:
    if default_val:
        user_val = input(f"{prompt_text} [{default_val}]: ").strip()
        return user_val if user_val else default_val
    while True:
        user_val = input(f"{prompt_text}: ").strip()
        if user_val:
            return user_val
        print("This field cannot be empty. Please enter a value.")


def refresh_clearance_interactive(config: ConfigStore) -> None:
    print("\n" + "=" * 50)
    print("      REFRESH CLOUDFLARE CLEARANCE / LOGIN")
    print("=" * 50)
    print("Opening real Google Chrome... Please pass Cloudflare or log in if prompted.")

    with sync_playwright() as p:
        context, page = launch_context(p, config.data)
        page.goto("https://comix.to", wait_until="domcontentloaded")
        # Explicit "come solve this" action - make sure the window is in front.
        focus_challenge_window(page, config.data)
        print(
            "\nPress Enter in this terminal after Cloudflare is passed / login is complete..."
        )
        input()
        sync_cookies_from_context(context, config.data)
        config.reload()
        print("[✓] Fresh cookies captured and saved to config.json.")
        context.close()


def update_cookies_interactive(config: ConfigStore) -> None:
    """Paste a fresh cf_clearance (and optionally session) into config.json.

    Cloudflare can no longer be cleared by refreshing and can get stuck on
    "Verifying you are human" - copying the cookie out of a browser that is
    already past the check is the reliable fix.
    """
    print("\n" + "=" * 50)
    print("            PASTE CLOUDFLARE COOKIES")
    print("=" * 50)
    print("Leave a field blank to keep its current value.")
    cf = input("cf_clearance value: ").strip()
    session = input("session value (optional): ").strip()
    changed = set_config_cookies(config.data, {"cf_clearance": cf, "session": session})
    config.reload()
    print("[✓] Cookies updated in config.json." if changed else "[i] Nothing changed.")


def collect_params(config: ConfigStore) -> UploadParams:
    print("\n" + "=" * 50)
    print("         COMIX AUTOMATED UPLOADER")
    print("=" * 50)

    url = prompt_input(
        "Enter target upload URL (e.g. https://comix.to/user/upload/<ID>)"
    )

    while True:
        target_dir = Path(
            prompt_input("Enter path to chapter archives folder").strip('"')
        )
        if target_dir.exists() and target_dir.is_dir():
            break
        print(f"[!] Directory '{target_dir}' does not exist. Please check the path.")

    # Optional: titles from rename.py's chapter_mapping.txt. When one loads,
    # it owns the titles and the pattern prompt is skipped.
    title_map = None
    map_path = (
        input("Path to chapter_mapping.txt (Enter for none): ").strip().strip('"')
    )
    if map_path:
        try:
            title_map = parse_chapter_mapping(
                Path(map_path).read_text(encoding="utf-8-sig")
            )
        except OSError as ex:
            print(f"[!] Could not read mapping file: {ex}")
        if title_map:
            print(f"[✓] Loaded {len(title_map)} chapter titles from mapping.")
        else:
            print("[!] No mapping lines found - falling back to title pattern.")
            title_map = None

    title_pattern = ""
    if not title_map:
        title_pattern = input(
            "Enter chapter title pattern (use {ch} for number, Enter for none): "
        ).strip()

    default_grp = config.group
    group = input(
        f"Enter group name (Enter for '{default_grp}'): "
        if default_grp
        else "Enter group name (Enter for solo): "
    ).strip()
    group = group or default_grp

    start_str = input("Enter start chapter number (Enter for beginning): ").strip()
    end_str = input("Enter end chapter number (Enter for all): ").strip()
    official = input("Mark as official release? (y/N): ").strip().lower() in (
        "y",
        "yes",
    )
    delay_str = input(
        f"Enter delay between uploads in seconds [{config.delay}]: "
    ).strip()

    return UploadParams(
        url=url,
        folder=str(target_dir),
        title_pattern=title_pattern,
        group=group,
        official=official,
        delay=int(delay_str) if delay_str.isdigit() else config.delay,
        start=float(start_str) if start_str else None,
        end=float(end_str) if end_str else None,
        title_map=title_map,
    )


def summary_menu(params: UploadParams) -> str:
    items = scan_folder(params.folder)
    history_file = get_history_file(params.url)
    failed_file = get_failed_file(params.url)
    history = load_history(history_file)
    failed = load_failed(failed_file)

    pending = [i for i in items if str(i[0]) not in history]

    while True:
        print("\n" + "-" * 50)
        print("SUMMARY:")
        print(f"  Target URL       : {params.url}")
        print(f"  Folder           : {params.folder}")
        print(f"  Group            : {params.group or '(None / Solo)'}")
        print(f"  Title Pattern    : {params.title_pattern or '(None)'}")
        if params.title_map:
            print(
                f"  Title Map        : {len(params.title_map)} titles "
                "(overrides pattern)"
            )
        print(f"  Total Found      : {len(items)}")
        print(f"  Already Uploaded : {len(items) - len(pending)}")
        print(f"  Failed in Past   : {len(failed)}")
        print(f"  Pending          : {len(pending)}")
        print("-" * 50)
        print("  [1] Start uploading pending chapters")
        print("  [2] List pending chapters")
        print("  [3] List already uploaded chapters")
        print("  [4] Show failed chapters log")
        print("  [5] Reset history for this series")
        print("  [6] Auto-solve Cloudflare (patchright)")
        print("  [7] Refresh Cloudflare clearance / login (manual)")
        print("  [8] Paste Cloudflare cookies (cf_clearance / session)")
        print("  [9] Exit")

        choice = input("\nSelect an action [1-9]: ").strip()

        if choice == "1":
            if not pending:
                print("\n[!] No pending chapters to upload.")
                continue
            return "start"
        if choice == "2":
            for ch, path in pending:
                print(f"  Chapter {ch:g}: {path.name}")
            input("\nPress Enter to return to menu...")
        elif choice == "3":
            for ch, path in items:
                if str(ch) in history:
                    print(f"  Chapter {ch:g}: {path.name}")
            input("\nPress Enter to return to menu...")
        elif choice == "4":
            if not failed:
                print("  No failed chapters recorded.")
            for ch, err in failed.items():
                print(f"  Chapter {ch}: {err}")
            input("\nPress Enter to return to menu...")
        elif choice == "5":
            if input(f"Reset history for {params.url}? (y/N): ").strip().lower() in (
                "y",
                "yes",
            ):
                reset_history(params.url)
                history_file = get_history_file(params.url)
                history = set()
                pending = list(items)
                print("History and failure logs reset.")
        elif choice == "6":
            return "solve_cf"
        elif choice == "7":
            return "refresh"
        elif choice == "8":
            return "paste_cookies"
        elif choice == "9":
            return "exit"


def main() -> None:
    try:
        config = ConfigStore()
    except (FileNotFoundError, ValueError) as ex:
        # ValueError covers ConfigError (e.g. an out-of-range "concurrency")
        # and malformed JSON - both must stop the run with a clear message.
        print(ex)
        sys.exit(1)

    params = collect_params(config)

    while True:
        action = summary_menu(params)
        if action == "exit":
            print("Exiting.")
            return
        if action == "solve_cf":
            if not solver.solve_into_config(config):
                print("[!] Auto-solve failed - try option [8] to paste cookies.")
            continue
        if action == "refresh":
            refresh_clearance_interactive(config)
            continue
        if action == "paste_cookies":
            update_cookies_interactive(config)
            continue
        break

    control = Control()

    enable_cdp = config.concurrency > 1
    with sync_playwright() as p:
        context, page = launch_context(p, config.data, enable_cdp=enable_cdp)
        endpoint = None
        if enable_cdp:
            port = read_devtools_port()
            if port is None:
                print("[!] Chrome's debug port did not appear - running sequentially.")
            else:
                endpoint = f"http://127.0.0.1:{port}"

        def on_cloudflare(ch_num):
            print("\n" + "=" * 60)
            print("  [!] CLOUDFLARE CHALLENGE DETECTED")
            print("=" * 60)
            print(f"Paused on chapter {ch_num}. Nothing was skipped or marked failed.")
            print("comix's Cloudflare check usually clears by itself - the run")
            print("resumes on its own. Keep this terminal open.")

        def on_cloudflare_stuck(ch_num):
            print("\n" + "=" * 60)
            print("  [!] CLOUDFLARE STUCK ON 'VERIFYING YOU ARE HUMAN'")
            print("=" * 60)
            print(f"Stopped on chapter {ch_num}. Nothing was marked failed.")
            print("The automatic solver could not clear it either - get a fresh")
            print("cf_clearance cookie and use menu option [8] to paste it, then")
            print("run again.")

        def cf_resolved(restart: bool):
            # restart=True: the first wait window expired, so relaunch once.
            nonlocal context, page
            if restart:
                try:
                    sync_cookies_from_context(context, config.data)
                    context.close()
                except Exception:
                    pass
                context, page = launch_context(p, config.data)
                return page
            try:
                sync_cookies_from_context(context, config.data)
            except Exception:
                pass
            return page

        def on_challenge_cleared(kind, ch_num):
            print(f"[✓] Cloudflare cleared on chapter {ch_num:g}. Resuming...")

        try:
            summary = run_upload_batch_auto(
                page=page,
                endpoint=endpoint,
                config=config.data,
                params=params,
                control=control,
                on_cloudflare=on_cloudflare,
                cf_resolved_hook=cf_resolved,
                on_cloudflare_stuck=on_cloudflare_stuck,
                cf_solver_hook=solver.make_cf_solver_hook(config.data, print),
                on_challenge_cleared=on_challenge_cleared,
                max_retries=config.max_retries,
            )
        except KeyboardInterrupt:
            print("\n[!] Interrupted.")
            return
        finally:
            try:
                context.close()
            except Exception:
                pass

    print("\n" + "=" * 50)
    print("               UPLOAD RUN SUMMARY")
    print("=" * 50)
    print(f"Processed : {summary.processed}")
    print(f"Uploaded  : {summary.succeeded}")
    print(f"Failed    : {summary.failed}")
    if getattr(summary, "aborted", False):
        print(
            "[!] Server appears down - the batch was stopped. Nothing was "
            "marked failed; try again once comix is back."
        )
        if summary.abort_reason:
            print(f"    ({summary.abort_reason})")
    for ch, filename, err in summary.failures:
        print(f"  - Chapter {ch:g} ({filename}): {err}")
    print(f"\nFailed details saved to {get_failed_file(params.url)}")
    print("\nProcess finished.")


if __name__ == "__main__":
    main()
