import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CONFIG_PATH = Path("config.json")
PROFILE_DIR = Path("browser_profile").resolve()


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Configuration file {CONFIG_PATH} not found.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def sync_cookies_from_context(context, config):
    current_cookies = context.cookies()
    cookie_map = {c["name"]: c["value"] for c in current_cookies}

    updated = False
    for cfg_cookie in config.get("cookies", []):
        c_name = cfg_cookie["name"]
        if c_name in cookie_map and cfg_cookie.get("value") != cookie_map[c_name]:
            cfg_cookie["value"] = cookie_map[c_name]
            updated = True

    known_names = {c["name"] for c in config.get("cookies", [])}
    for c in current_cookies:
        if (
            c["name"] in ("cf_clearance", "session")
            or c["name"].startswith("remember_web_")
        ) and c["name"] not in known_names:
            config["cookies"].append(
                {"name": c["name"], "value": c["value"], "url": "https://comix.to"}
            )
            updated = True

    if updated:
        save_config(config)


def ensure_cloudflare_passed(page, context, config):
    first_notice = True
    while True:
        title = page.title()
        is_cf = (
            "Just a moment" in title
            or page.locator(
                "#challenge-running, #challenge-stage, .cf-turnstile"
            ).is_visible()
        )
        if not is_cf:
            if not first_notice:
                print("\n[✓] Cloudflare verification cleared! Resuming automation...")
                sync_cookies_from_context(context, config)
            return True

        if first_notice:
            print("\n[!] Cloudflare verification detected in browser.")
            print("[*] Waiting for verification (browser will stay open)...")
            first_notice = False

        page.wait_for_timeout(3000)


def parse_chapter_number(filename: str):
    base = Path(filename).stem
    match = re.search(r"(\d+(\.\d+)?)", base)
    if not match:
        raise ValueError(f"Could not parse chapter number from filename: {filename}")
    num_str = match.group(1)
    if "." in num_str:
        return float(num_str) if not num_str.endswith(".0") else int(float(num_str))
    return int(num_str)


def get_history_file(url: str):
    manga_id = url.rstrip("/").split("/")[-1]
    return Path(f".upload_history_{manga_id}.json")


def get_failed_file(url: str):
    manga_id = url.rstrip("/").split("/")[-1]
    return Path(f".upload_failed_{manga_id}.json")


def load_history(history_file: Path):
    if history_file.exists():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_history(history_file: Path, history: set):
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(
            sorted(
                history,
                key=lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0,
            ),
            f,
            indent=2,
        )


def load_failed(failed_file: Path):
    if failed_file.exists():
        try:
            with open(failed_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_failed(failed_file: Path, failed_dict: dict):
    with open(failed_file, "w", encoding="utf-8") as f:
        json.dump(failed_dict, f, indent=2)


def prompt_input(prompt_text: str, default_val: str = ""):
    if default_val:
        user_val = input(f"{prompt_text} [{default_val}]: ").strip()
        return user_val if user_val else default_val
    while True:
        user_val = input(f"{prompt_text}: ").strip()
        if user_val:
            return user_val
        print("This field cannot be empty. Please enter a value.")


def upload_single_chapter(
    page,
    context,
    config,
    url,
    chapter_num,
    title,
    file_path,
    group_name,
    mark_official,
    idle_timeout=90,
):
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    print(
        f"\n[+] Uploading Chapter {chapter_num} ({Path(file_path).name}, {file_size_mb:.1f} MB)..."
    )

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    ensure_cloudflare_passed(page, context, config)
    page.wait_for_timeout(1000)

    page.wait_for_selector(".upage-field input", timeout=30000)
    page.wait_for_timeout(1000)

    # Bait any initial click-jacking/popunder triggers
    try:
        page.locator("body").click(position={"x": 5, "y": 5}, force=True, timeout=2000)
        page.wait_for_timeout(1000)
    except Exception:
        pass
    page.bring_to_front()

    chapter_input = page.locator(
        ".upage-field input[placeholder*='42'], input[placeholder*='42']"
    ).first
    title_input = page.locator(
        ".upage-field input[placeholder*='Walk Home'], input[placeholder*='Walk Home']"
    ).first
    group_input = page.locator(
        ".upage-field input[placeholder*='group'], input[placeholder*='Search a group']"
    ).first
    file_input = page.locator("input.upage-drop__input, input[type='file']").first
    official_checkbox = page.locator("input[type='checkbox']").first
    submit_btn = page.locator(
        "button[type='submit'], button:has-text('Submit upload')"
    ).first

    chapter_input.fill(str(chapter_num))

    if title:
        title_input.fill(title)

    if group_name:
        group_selected = False
        for g_attempt in range(1, 4):
            picked_el = page.locator(".upage-group--picked")
            if picked_el.is_visible():
                if group_name.lower() in picked_el.inner_text().lower():
                    group_selected = True
                    break
                clear_btn = page.locator(".upage-group__clear")
                if clear_btn.is_visible():
                    clear_btn.click(force=True)
                    page.wait_for_timeout(500)

            page.bring_to_front()
            page.evaluate(
                '() => document.querySelectorAll(\'a[href="#"][target="_blank"]\').forEach(e => e.remove())'
            )

            group_input.click(force=True)
            group_input.fill("")
            page.wait_for_timeout(300)
            group_input.press_sequentially(group_name, delay=90)

            try:
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
                    print(f"[✓] Group '{group_name}' selected successfully.")
                    group_selected = True
                    break
            except Exception:
                if g_attempt < 3:
                    print(
                        f"[*] Group search attempt {g_attempt}/3 was interrupted by popup or delay. Retrying..."
                    )
                    page.wait_for_timeout(1000)

        if not group_selected:
            page.evaluate(f"""() => {{
                const btns = Array.from(document.querySelectorAll('button.upage-group__item'));
                const match = btns.find(b => b.textContent.toLowerCase().includes('{group_name.lower()}'));
                if (match) match.click();
            }}""")
            page.wait_for_timeout(1000)
            if page.locator(".upage-group--picked").is_visible():
                print(f"[✓] Group '{group_name}' selected via DOM fallback.")
            else:
                print(f"[!] Warning: Group '{group_name}' was not confirmed picked.")

    if mark_official and not official_checkbox.is_checked():
        official_checkbox.check(force=True)
    elif not mark_official and official_checkbox.is_checked():
        official_checkbox.uncheck(force=True)

    file_input.set_input_files(str(file_path))
    page.wait_for_timeout(1500)

    upload_tracker = {
        "finalized": False,
        "error": None,
        "last_activity": time.time(),
        "chunks_done": 0,
    }

    def on_response(res):
        res_url = res.url
        if "/upload/chunk" in res_url and res.status == 200:
            upload_tracker["last_activity"] = time.time()
            upload_tracker["chunks_done"] += 1
            print(
                f"\r[*] Uploading chunk #{upload_tracker['chunks_done']}...",
                end="",
                flush=True,
            )
        elif "/api/v1/uploads" in res_url:
            upload_tracker["last_activity"] = time.time()
            if "finalize" in res_url and res.status == 200:
                upload_tracker["finalized"] = True
            elif res.status in (429, 500, 502, 503, 504):
                upload_tracker["error"] = f"HTTP {res.status} on {res_url}"

    page.on("response", on_response)

    try:
        submit_btn.click(timeout=5000)
    except Exception:
        submit_btn.dispatch_event("click")

    upload_tracker["last_activity"] = time.time()

    while True:
        if upload_tracker["error"]:
            print(f"\n[!] Server error: {upload_tracker['error']}")
            return False, upload_tracker["error"]

        if "/user/upload/" not in page.url or upload_tracker["finalized"]:
            chunks = upload_tracker["chunks_done"]
            print(
                f"\n[✓] Chapter {chapter_num} uploaded successfully! ({chunks} chunks completed)"
            )
            return True, None

        error_el = page.locator(
            ".alert-danger, .error-message, .toast-error, div[role='alert']"
        ).first
        if error_el.is_visible():
            err_text = error_el.inner_text().strip()
            print(f"\n[!] UI error detected: {err_text}")
            return False, err_text

        if time.time() - upload_tracker["last_activity"] > idle_timeout:
            err_msg = f"Upload stalled: No chunk progress for {idle_timeout}s."
            print(f"\n[!] {err_msg}")
            return False, err_msg

        page.wait_for_timeout(1000)


def refresh_clearance_interactive(config):
    print("\n" + "=" * 50)
    print("      REFRESH CLOUDFLARE CLEARANCE / LOGIN")
    print("=" * 50)
    print("Opening real Google Chrome... Please pass Cloudflare or log in if prompted.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        def handle_popup(new_page):
            if new_page != page:
                try:
                    new_page.close()
                    page.bring_to_front()
                except Exception:
                    pass

        context.on("page", handle_popup)

        context.add_cookies(config.get("cookies", []))
        page.goto("https://comix.to", wait_until="domcontentloaded")
        print(
            "\nPress Enter in this terminal after Cloudflare is passed / login is complete..."
        )
        input()

        sync_cookies_from_context(context, config)
        print("[✓] Fresh cookies captured and saved to config.json.")
        context.close()


def main():
    config = load_config()

    print("\n" + "=" * 50)
    print("         COMIX AUTOMATED UPLOADER")
    print("=" * 50)

    url = prompt_input(
        "Enter target upload URL (e.g. https://comix.to/user/upload/<ID>)"
    )

    while True:
        dir_str = prompt_input("Enter path to chapter archives folder")
        target_dir = Path(dir_str.strip('"').strip("'"))
        if target_dir.exists() and target_dir.is_dir():
            break
        print(f"[!] Directory '{target_dir}' does not exist. Please check the path.")

    title_pattern = input(
        "Enter chapter title pattern (use {ch} for number, e.g. 'Chapter {ch}' or press Enter for none): "
    ).strip()

    default_grp = config.get("default_group", "")
    grp_prompt = (
        f"Enter group name (press Enter for '{default_grp}' or solo): "
        if default_grp
        else "Enter group name (or press Enter for solo): "
    )
    group_input_val = input(grp_prompt).strip()
    group_name = group_input_val if group_input_val else default_grp

    start_str = input(
        "Enter start chapter number (or press Enter for start from beginning): "
    ).strip()
    start_ch = float(start_str) if start_str else None

    end_str = input("Enter end chapter number (or press Enter for all): ").strip()
    end_ch = float(end_str) if end_str else None

    official_ans = input("Mark as official release? (y/N): ").strip().lower()
    mark_official = official_ans in ("y", "yes")

    delay_str = input(
        f"Enter delay between uploads in seconds [{config.get('default_delay_seconds', 6)}]: "
    ).strip()
    delay = (
        int(delay_str)
        if delay_str.isdigit()
        else config.get("default_delay_seconds", 6)
    )

    archive_exts = {".zip", ".cbz", ".cbr", ".rar", ".7z"}
    files = [
        f
        for f in target_dir.iterdir()
        if f.is_file() and f.suffix.lower() in archive_exts
    ]

    chapter_items = []
    for f in files:
        try:
            ch_num = parse_chapter_number(f.name)
            chapter_items.append((ch_num, f))
        except ValueError:
            print(f"[!] Skipping unrecognized file: {f.name}")

    chapter_items.sort(key=lambda x: x[0])

    if start_ch is not None:
        chapter_items = [item for item in chapter_items if item[0] >= start_ch]
    if end_ch is not None:
        chapter_items = [item for item in chapter_items if item[0] <= end_ch]

    history_file = get_history_file(url)
    history = load_history(history_file)

    failed_file = get_failed_file(url)
    failed_history = load_failed(failed_file)

    pending_items = [item for item in chapter_items if str(item[0]) not in history]
    completed_items = [item for item in chapter_items if str(item[0]) in history]

    while True:
        print("\n" + "-" * 50)
        print("SUMMARY:")
        print(f"  Target URL       : {url}")
        print(f"  Folder           : {target_dir}")
        print(f"  Group            : {group_name or '(None / Solo)'}")
        print(f"  Title Pattern    : {title_pattern or '(None)'}")
        print(f"  Total in Range   : {len(chapter_items)}")
        print(f"  Already Uploaded : {len(completed_items)}")
        print(f"  Failed in Past   : {len(failed_history)}")
        print(f"  Pending to Upload: {len(pending_items)}")
        print("-" * 50)
        print("Actions:")
        print("  [1] Start uploading pending chapters")
        print("  [2] View list of pending chapters")
        print("  [3] View list of already uploaded chapters")
        print("  [4] View failed chapters log")
        print("  [5] Reset history for this series")
        print("  [6] Refresh Cloudflare clearance / login in browser")
        print("  [7] Exit")

        choice = input("\nSelect an action [1-7]: ").strip()

        if choice == "1":
            if not pending_items:
                print("\n[!] No pending chapters to upload.")
                continue
            break
        elif choice == "2":
            print(f"\n--- Pending Chapters ({len(pending_items)}) ---")
            for ch_num, f_path in pending_items:
                formatted_title = (
                    title_pattern.format(ch=ch_num) if title_pattern else ""
                )
                title_display = (
                    f" | Title: '{formatted_title}'" if formatted_title else ""
                )
                print(f"  Chapter {ch_num:g}: {f_path.name}{title_display}")
            input("\nPress Enter to return to menu...")
        elif choice == "3":
            print(f"\n--- Already Uploaded Chapters ({len(completed_items)}) ---")
            for ch_num, f_path in completed_items:
                print(f"  Chapter {ch_num:g}: {f_path.name}")
            input("\nPress Enter to return to menu...")
        elif choice == "4":
            print(f"\n--- Failed Chapters Log ({len(failed_history)}) ---")
            if not failed_history:
                print("  No failed chapters recorded.")
            else:
                for ch_k, err_v in failed_history.items():
                    print(f"  Chapter {ch_k}: {err_v}")
            input("\nPress Enter to return to menu...")
        elif choice == "5":
            confirm = (
                input(f"Are you sure you want to reset history for {url}? (y/N): ")
                .strip()
                .lower()
            )
            if confirm in ("y", "yes"):
                if history_file.exists():
                    history_file.unlink()
                if failed_file.exists():
                    failed_file.unlink()
                history = set()
                failed_history = {}
                pending_items = list(chapter_items)
                completed_items = []
                print("History and failure logs have been reset.")
        elif choice == "6":
            refresh_clearance_interactive(config)
            config = load_config()
        elif choice == "7":
            print("Exiting.")
            sys.exit(0)
        else:
            print("Invalid selection. Please choose 1 to 7.")

    max_retries = config.get("max_retries", 3)
    session_success = []
    session_failed = []

    print(f"\nStarting upload of {len(pending_items)} chapters with Google Chrome...")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        def handle_popup(new_page):
            if new_page != page:
                try:
                    new_page.close()
                    page.bring_to_front()
                except Exception:
                    pass

        context.on("page", handle_popup)
        context.add_cookies(config["cookies"])

        for idx, (ch_num, file_path) in enumerate(pending_items, 1):
            title = title_pattern.format(ch=ch_num) if title_pattern else ""
            success = False
            last_err = None

            for attempt in range(1, max_retries + 1):
                if attempt > 1:
                    backoff = attempt * 5
                    print(
                        f"[*] Retry attempt {attempt}/{max_retries} for Chapter {ch_num} (waiting {backoff}s)..."
                    )
                    time.sleep(backoff)

                try:
                    success, last_err = upload_single_chapter(
                        page=page,
                        context=context,
                        config=config,
                        url=url,
                        chapter_num=ch_num,
                        title=title,
                        file_path=file_path,
                        group_name=group_name,
                        mark_official=mark_official,
                        idle_timeout=90,
                    )
                    if success:
                        break
                except Exception as ex:
                    last_err = str(ex)
                    print(f"[!] Exception during upload of Chapter {ch_num}: {ex}")

            if success:
                history.add(str(ch_num))
                save_history(history_file, history)
                session_success.append(ch_num)
                if str(ch_num) in failed_history:
                    del failed_history[str(ch_num)]
                    save_failed(failed_file, failed_history)
                if idx < len(pending_items):
                    print(f"[*] Waiting {delay}s before next chapter...")
                    time.sleep(delay)
            else:
                print(
                    f"[X] Failed to upload Chapter {ch_num} after {max_retries} attempts. Skipping to next chapter..."
                )
                failed_history[str(ch_num)] = last_err or "Unknown error"
                save_failed(failed_file, failed_history)
                session_failed.append((ch_num, file_path.name, last_err))

        context.close()

    print("\n" + "=" * 50)
    print("               UPLOAD RUN SUMMARY")
    print("=" * 50)
    print(f"Total Processed in this run : {len(session_success) + len(session_failed)}")
    print(f"Successfully Uploaded       : {len(session_success)}")
    print(f"Failed / Skipped            : {len(session_failed)}")

    if session_failed:
        print("\nFailed Chapters:")
        for ch_n, fn, err in session_failed:
            print(f"  - Chapter {ch_n:g} ({fn}): {err}")
        print(f"\nFailed chapter details saved to {failed_file}")

    print("\nProcess finished.")


if __name__ == "__main__":
    main()
