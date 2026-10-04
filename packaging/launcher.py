"""Windows portable release entry point."""
import multiprocessing
import os
import sys
from pathlib import Path


def launch():
    multiprocessing.freeze_support()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    from humble_bundle_keys.web import available_browser_channel, main

    edge_mode = Path(__file__).parent.joinpath("humble_bundle_keys", "edge-mode").is_file()
    # __file__ for the entry point lives at the root of PyInstaller's resource directory.
    if edge_mode and available_browser_channel() != "msedge":
        print("轻量版需要安装 Microsoft Edge，请安装后重新运行。", flush=True)
        if sys.stdin.isatty():
            input("按 Enter 退出。")
        raise SystemExit(1)

    if "--check-runtime" in sys.argv:
        from playwright.sync_api import sync_playwright

        from humble_bundle_keys.web import HTML_PATH

        assert HTML_PATH.is_file()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=available_browser_channel())
            page = browser.new_page()
            page.set_content("<title>Runtime OK</title>")
            assert page.title() == "Runtime OK"
            browser.close()
        print("Browser and UI: OK", flush=True)
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
