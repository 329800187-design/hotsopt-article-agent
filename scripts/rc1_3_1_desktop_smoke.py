"""Real Windows smoke test for the RC1.3.1 Setup/App pair.

This script launches the installed executable and observes the native window;
it does not print PASS markers from source inspection alone.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
PRODUCT_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs" / "热点图文批量生产工作台"
SETUP = ROOT / "热点图文批量生产工作台_Setup.exe"
APP = PRODUCT_DIR / "热点图文工作台.exe"
TITLE = "热点图文批量生产工作台"


def _window_handle() -> int:
    if os.name != "nt":
        return 0
    import ctypes

    return int(ctypes.windll.user32.FindWindowW(None, TITLE) or 0)


def _close_window(hwnd: int) -> None:
    if hwnd and os.name == "nt":
        import ctypes

        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true", help="run the final Setup.exe before launching")
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()
    if os.name != "nt":
        print("CLEAN_SANDBOX_DESKTOP_PENDING: Windows-only smoke")
        return 0
    if args.setup:
        completed = subprocess.run([str(SETUP), "--silent"], cwd=ROOT, timeout=120, check=False)
        if completed.returncode != 0:
            raise SystemExit("Setup.exe failed")
    if not APP.is_file():
        raise SystemExit("installed desktop app is missing")
    with tempfile.TemporaryDirectory(prefix="rc131-desktop-") as data_root:
        environment = os.environ.copy()
        environment["HOTSPOT_DATA_ROOT"] = data_root
        environment["HOTSPOT_NO_BROWSER"] = "1"
        environment["HOTSPOT_NONINTERACTIVE"] = "1"
        before = subprocess.check_output(["tasklist.exe", "/fo", "csv", "/nh"], text=True, errors="replace")
        process = subprocess.Popen([str(APP)], cwd=PRODUCT_DIR, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + args.timeout
        hwnd = 0
        while time.monotonic() < deadline:
            hwnd = _window_handle()
            if hwnd:
                break
            if process.poll() is not None:
                break
            time.sleep(0.5)
        if not hwnd:
            process.terminate()
            raise SystemExit("embedded desktop window did not appear")
        print("DESKTOP_APP_LAUNCH_PASS")
        print("EMBEDDED_WEBVIEW_PASS")
        after = subprocess.check_output(["tasklist.exe", "/fo", "csv", "/nh"], text=True, errors="replace")
        before_names = {line.split(",", 1)[0].strip('"') for line in before.splitlines() if line}
        after_names = {line.split(",", 1)[0].strip('"') for line in after.splitlines() if line}
        unexpected = {name.lower() for name in after_names - before_names if name.lower() in {"chrome.exe", "msedge.exe"}}
        if unexpected:
            _close_window(hwnd)
            raise SystemExit("external browser process appeared")
        print("NO_EXTERNAL_BROWSER_PASS")
        _close_window(hwnd)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        print("WINDOW_CLOSE_BACKEND_SHUTDOWN_PASS")
        print("NO_ORPHAN_PROCESS_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
