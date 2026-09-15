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
    get_failed_file,
    get_history_file,
    launch_context,
    load_failed,
    load_history,
    reset_history,
    run_upload_batch,
    scan_folder,
    sync_cookies_from_context,
)


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
        print(
            "\nPress Enter in this terminal after Cloudflare is passed / login is complete..."
        )
        input()
        sync_cookies_from_context(context, config.data)
        config.reload()
        print("[✓] Fresh cookies captured and saved to config.json.")
        context.close()


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
        print("  [6] Refresh Cloudflare clearance / login")
        print("  [7] Exit")

        choice = input("\nSelect an action [1-7]: ").strip()

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
            return "refresh"
        elif choice == "7":
            return "exit"


def main() -> None:
    try:
        config = ConfigStore()
    except FileNotFoundError as ex:
        print(ex)
        sys.exit(1)

    params = collect_params(config)

    while True:
        action = summary_menu(params)
        if action == "exit":
            print("Exiting.")
            return
        if action == "refresh":
            refresh_clearance_interactive(config)
            continue
        break

    control = Control()

    with sync_playwright() as p:
        context, page = launch_context(p, config.data)

        def on_cloudflare(ch_num):
            print("\n" + "=" * 60)
            print("  [!] CLOUDFLARE CLEARANCE EXPIRED / CHALLENGE DETECTED")
            print("=" * 60)
            print(f"Paused on chapter {ch_num}. Nothing was skipped or marked failed.")
            print("Pass Cloudflare in the Chrome window, then press Enter here.")
            input()

        def on_waf(ch_num):
            # No input() here: the engine polls the tab itself and carries on
            # as soon as the challenge page navigates away.
            print("\n" + "=" * 60)
            print("  [!] SECURITY CHECK — comix wants you to verify you're human")
            print("=" * 60)
            print(f"Paused on chapter {ch_num}. Nothing was skipped or marked failed.")
            print("In the Chrome window, drag the circle until the picture lines")
            print("up, then press Verify. Uploading resumes on its own.")

        try:
            summary = run_upload_batch(
                page=page,
                config=config.data,
                params=params,
                control=control,
                on_cloudflare=on_cloudflare,
                cf_resolved_hook=lambda restart: page,
                on_waf=on_waf,
                waf_resolved_hook=lambda: page,
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
    for ch, filename, err in summary.failures:
        print(f"  - Chapter {ch:g} ({filename}): {err}")
    print(f"\nFailed details saved to {get_failed_file(params.url)}")
    print("\nProcess finished.")


if __name__ == "__main__":
    main()
