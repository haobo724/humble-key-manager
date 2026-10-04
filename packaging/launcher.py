"""Windows portable release entry point."""
import multiprocessing
import os
import sys
from pathlib import Path


def launch():
    multiprocessing.freeze_support()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    from humble_bundle_keys.web import main

    if "--check-runtime" in sys.argv:
        from playwright.sync_api import sync_playwright

        from humble_bundle_keys.web import HTML_PATH

        assert HTML_PATH.is_file()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.set_content("<title>Runtime OK</title>")
            assert page.title() == "Runtime OK"
            browser.close()
        print("Bundled browser and UI: OK", flush=True)
        return
    if "--data-dir" not in sys.argv:
        directory = Path(os.environ["LOCALAPPDATA"]) / "HumbleKeyManager" / "data"
        sys.argv.extend(["--data-dir", str(directory)])
    try:
        main()
    except OSError as exc:
        print(f"启动失败：{exc}\n请检查是否已有程序占用端口 8765。", flush=True)
        if sys.stdin.isatty():
            input("按 Enter 退出。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    launch()
