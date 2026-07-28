"""Windows desktop host for the customer application.

The existing FastAPI and Streamlit services remain the application backend.  This
module owns only the desktop lifecycle: per-user data initialization, a single
instance, hidden local services, an embedded WebView2 window, and clean shutdown.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import importlib.metadata as pkg_metadata
import json
import os
from pathlib import Path
import shutil as _shutil_mod
import socket
import subprocess
import sys
import threading
import time
import traceback
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from modules.app_version import APP_VERSION

PRODUCT_NAME = "热点图文工作台"
WINDOW_TITLE = "热点图文批量生产工作台"
MUTEX_NAME = "Local\\HotspotArticleAgentProduct"
PREFERRED_WEB_PORT = 8505
PREFERRED_API_PORT = 8506
STARTUP_TIMEOUT_SECONDS = 90

STARTUP_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
html,body{height:100%;margin:0;background:#f5f7fb;color:#152238;font-family:
"Segoe UI","Microsoft YaHei",sans-serif}body{display:grid;place-items:center}
main{text-align:center}h1{font-size:28px;font-weight:600;margin:0 0 14px}
p{color:#68758a;margin:0}.spinner{width:34px;height:34px;margin:0 auto 24px;border:3px solid #d9e1ee;
border-top-color:#3569e8;border-radius:50%;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body><main><div class="spinner"></div><h1>正在启动热点图文工作台……</h1><p>请稍候，正在准备本地工作环境</p></main></body></html>"""


def _hidden_startup_kwargs(log_handle: object | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "cwd": str(Path(__file__).resolve().parent),
    }
    if log_handle is not None:
        kwargs["stdout"] = log_handle
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["stdout"] = None if os.environ.get("HOTSPOT_DEBUG_BACKEND") == "1" else subprocess.DEVNULL
        kwargs["stderr"] = None if os.environ.get("HOTSPOT_DEBUG_BACKEND") == "1" else subprocess.DEVNULL
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["startupinfo"] = _hidden_startup_info()
    return kwargs


def _hidden_startup_info() -> subprocess.STARTUPINFO:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return startup


def _show_message(message: str, *, error: bool = False) -> None:
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            flags = 0x10 if error else 0x40
            user32.MessageBoxW(None, message, WINDOW_TITLE, flags)
            return
        except Exception:
            pass
    print(message, file=sys.stderr if error else sys.stdout)


class StartupError(RuntimeError):
    def __init__(self, code: str, user_reason: str, original: BaseException | None = None) -> None:
        super().__init__(user_reason)
        self.code = code
        self.user_reason = user_reason
        self.original = original


def _module_version(distribution: str) -> str:
    try:
        return pkg_metadata.version(distribution)
    except Exception:
        return "unknown"


def _webview2_runtime_available() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg

        client_id = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        subkeys = (
            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client_id}",
            rf"SOFTWARE\Microsoft\EdgeUpdate\ClientState\{client_id}",
        )
        views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for subkey in subkeys:
                for view in views:
                    try:
                        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as key:
                            version = str(winreg.QueryValueEx(key, "pv")[0] or "").strip()
                            if version and version != "0.0.0.0":
                                return True
                    except OSError:
                        continue
    except Exception:
        return False
    return False


def _focus_existing_window() -> None:
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _existing_window_exists() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE))
    except Exception:
        return False


class SingleInstance:
    """Named Windows mutex with a recoverable diagnostic lock file."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self.handle = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
            if not handle:
                return False
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self.handle = handle
        else:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
            except FileExistsError:
                try:
                    pid = int(self.lock_path.read_text(encoding="ascii").strip())
                    os.kill(pid, 0)
                    return False
                except (OSError, ValueError):
                    self.lock_path.unlink(missing_ok=True)
                    return self.acquire()
        _write_json(
            self.lock_path,
            {
                "pid": os.getpid(),
                "main_pid": os.getpid(),
                "kind": "desktop",
                "version": APP_VERSION,
                "install_path": str(Path(__file__).resolve().parent),
                "data_path": str(self.lock_path.parents[1]),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return True

    def release(self) -> None:
        if self.handle and os.name == "nt":
            try:
                ctypes.windll.kernel32.ReleaseMutex(self.handle)
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
        self.lock_path.unlink(missing_ok=True)


def _find_available_port(preferred: int, excluded: set[int] | None = None) -> int:
    excluded = excluded or set()
    candidates = [preferred, *range(preferred + 1, preferred + 100)]
    for candidate in candidates:
        if candidate in excluded:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


shutil_copy = _shutil_mod.copy2


def _process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_installed_dir(root: Path) -> bool:
    root_str = str(root).lower().replace("\\", "/")
    return "/programs/热点图文批量生产工作台" in root_str


def _installed_user_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        return Path(local_app_data) / "热点图文批量生产工作台"
    return Path.home() / "AppData" / "Local" / "热点图文批量生产工作台"


def _migrate_installed_config(install_root: Path, user_data_root: Path) -> str:
    """Migrate settings and credentials from old install-dir config to user data dir."""
    old_config = install_root / "config"
    new_config = user_data_root / "config"
    old_settings = old_config / "settings.json"
    old_creds = old_config / "credentials.dat"
    if not old_settings.exists() and not old_creds.exists():
        return "CONFIG_MIGRATION_SOURCE_MISSING"
    if new_config.exists() and (new_config / "settings.json").exists():
        return "CONFIG_MIGRATION_NOT_NEEDED"
    new_config.mkdir(parents=True, exist_ok=True)
    migrated = False
    if old_settings.exists() and not (new_config / "settings.json").exists():
        shutil_copy(old_settings, new_config / "settings.json")
        migrated = True
    if old_creds.exists() and not (new_config / "credentials.dat").exists():
        shutil_copy(old_creds, new_config / "credentials.dat")
        migrated = True
    if migrated:
        try:
            from modules.credential_store import load_secret
            key = load_secret("text_profile_api_key")
            if key:
                return "CONFIG_MIGRATION_SUCCESS"
            return "CONFIG_MIGRATION_DPAPI_FAILED"
        except Exception:
            return "CONFIG_MIGRATION_DPAPI_FAILED"
    return "CONFIG_MIGRATION_NOT_NEEDED"


class DesktopHost:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(__file__).resolve().parent).resolve()
        self._installed = _is_installed_dir(self.root)
        if self._installed:
            self.data_root = _installed_user_data_root()
        else:
            self.data_root = self.root / "data"
        if os.environ.get("HOTSPOT_DATA_ROOT"):
            self.data_root = Path(os.environ["HOTSPOT_DATA_ROOT"]).expanduser().resolve()
        self.runtime_root = self.data_root / "runtime"
        self.logs_root = self.data_root / "logs"
        self.startup_log = self.logs_root / "startup.log"
        self.api_port = 0
        self.web_port = 0
        self.api_process: subprocess.Popen[bytes] | None = None
        self.web_process: subprocess.Popen[bytes] | None = None
        self._log_handles: list[object] = []
        self.window = None
        self.lock = SingleInstance(self.runtime_root / "desktop.lock")
        self._shutdown_started = False
        self._api_restart_count = 0

    @property
    def web_url(self) -> str:
        return f"http://127.0.0.1:{self.web_port}"

    def prepare_environment(self) -> None:
        if self._installed:
            os.environ["HOTSPOT_INSTALL_MODE"] = "1"
        os.environ["HOTSPOT_DATA_ROOT"] = str(self.data_root)
        os.environ["HOTSPOT_DESKTOP"] = "1"
        os.environ["HOTSPOT_NO_BROWSER"] = "1"
        os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.path.insert(0, str(self.root))
        from modules.app_paths import migrate_legacy_data, is_installed
        from modules.database import init_db

        try:
            migrate_legacy_data()
            init_db()
        except Exception as exc:
            self._log_exception("START-DATABASE-001", exc)
            raise StartupError("START-DATABASE-001", "本地数据库初始化失败。", exc) from exc
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        if self._installed:
            migration_status = _migrate_installed_config(self.root, self.data_root)
            self._write_startup_log(f"config_migration={migration_status}")

    def _write_startup_log(self, message: str) -> None:
        try:
            self.logs_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).isoformat()
            safe = message.replace(os.environ.get("HOTSPOT_LOCAL_API_TOKEN", ""), "[token]")
            with self.startup_log.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] {safe}\n")
        except Exception:
            pass

    def _log_exception(self, code: str, exc: BaseException) -> None:
        self._write_startup_log(f"{code} {type(exc).__name__}: {exc}")
        self._write_startup_log(traceback.format_exc())

    def _open_process_log(self) -> object:
        self.logs_root.mkdir(parents=True, exist_ok=True)
        handle = self.startup_log.open("ab")
        self._log_handles.append(handle)
        return handle

    def _python_executable(self) -> str:
        bundled = self.root / "runtime" / ("pythonw.exe" if os.name == "nt" else "python")
        if bundled.is_file():
            return str(bundled)
        return sys.executable

    def _token(self) -> str:
        from modules.local_api_token import get_or_create_token

        return get_or_create_token()

    def start_backend(self) -> None:
        self.prepare_environment()
        self._clean_stale_runtime_files()
        self.api_port = _find_available_port(PREFERRED_API_PORT)
        self.web_port = _find_available_port(PREFERRED_WEB_PORT, {self.api_port})
        os.environ["HOTSPOT_API_PORT"] = str(self.api_port)
        os.environ["HOTSPOT_WEB_PORT"] = str(self.web_port)
        token = self._token()
        os.environ["HOTSPOT_LOCAL_API_TOKEN"] = token
        python = self._python_executable()
        self._write_startup_log(f"version={APP_VERSION}")
        self._write_startup_log(f"install_path={self.root}")
        self._write_startup_log(f"data_root={self.data_root}")
        self._write_startup_log(f"installed={self._installed}")
        self._write_startup_log(f"python_path={python}")
        self._write_startup_log(f"pywebview_version={_module_version('pywebview')}")
        self._write_startup_log(f"webview2_available={_webview2_runtime_available()}")
        kwargs = _hidden_startup_kwargs(self._open_process_log())

        # ---- Step 1: Start API only ----
        self._start_api_process(python, kwargs)
        self._write_startup_log(f"api_pid={self.api_process.pid} api_port={self.api_port}")

        # ---- Step 2: Wait for API health (max 20s) ----
        try:
            self._wait_for(lambda: self._api_healthy(token), 20)
            self._write_startup_log("API_HEALTH_BEFORE_STREAMLIT_PASS")
        except TimeoutError:
            self._write_startup_log(f"API_START_TIMEOUT exit_code={self.api_process.poll()}")
            if self._api_restart_count < 1:
                self._api_restart_count += 1
                self._write_startup_log("API_SINGLE_AUTO_RESTART_ATTEMPT")
                self._stop_process(self.api_process)
                self._start_api_process(python, kwargs)
                try:
                    self._wait_for(lambda: self._api_healthy(token), 20)
                    self._write_startup_log("API_RESTART_SUCCESS API_SINGLE_AUTO_RESTART_PASS")
                except TimeoutError:
                    self._write_startup_log(f"API_RESTART_FAILED exit_code={self.api_process.poll()}")
                    raise StartupError("START-API-001", "本地服务启动失败。请重新启动软件。", None)
            else:
                raise StartupError("START-API-001", "本地服务启动失败。请重新启动软件。", None)

        self._write_startup_log("API_STARTUP_WAIT_PASS")
        # Write api.json immediately after API is healthy
        self._write_runtime_file("api.json", self.api_process.pid, self.api_port, "api")

        # ---- Step 3: Start Streamlit only after API is healthy ----
        env = os.environ.copy()
        self.web_process = subprocess.Popen(
            [python, "-m", "streamlit", "run", str(self.root / "app.py"),
             "--server.address", "127.0.0.1", "--server.headless", "true",
             "--server.port", str(self.web_port), "--browser.gatherUsageStats", "false"],
            env=env, **kwargs
        )
        self._write_startup_log(f"web_pid={self.web_process.pid} web_port={self.web_port}")

        # ---- Step 4: Wait for Streamlit ----
        try:
            self._wait_for(self._web_healthy, STARTUP_TIMEOUT_SECONDS)
            self._write_startup_log("STREAMLIT_HEALTH_PASS")
        except TimeoutError as exc:
            self._write_startup_log(f"Streamlit 进程退出码={self.web_process.poll() if self.web_process else 'missing'}")
            raise StartupError("START-STREAMLIT-001", "本地界面服务启动失败。", exc) from exc

        self._write_runtime_file("web.json", self.web_process.pid, self.web_port, "web")

    def _start_api_process(self, python: str, kwargs: dict[str, object]) -> None:
        env = os.environ.copy()
        self.api_process = subprocess.Popen(
            [python, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(self.api_port)],
            env=env, **kwargs
        )

    def _clean_stale_runtime_files(self) -> None:
        """Remove runtime files from processes that are no longer running."""
        for name in ("api.json", "web.json", "api.pid", "web.pid"):
            rf = self.runtime_root / name
            try:
                if rf.exists():
                    data = json.loads(rf.read_text(encoding="utf-8"))
                    pid = int(data.get("pid") or 0)
                    if pid and not _process_running(pid):
                        rf.unlink(missing_ok=True)
            except Exception:
                try:
                    rf.unlink(missing_ok=True)
                except OSError:
                    pass

    def _write_runtime_file(self, filename: str, pid: int, port: int, kind: str) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        from modules.app_paths import config_dir, settings_path
        from modules.credential_store import credential_path
        meta = {
            "pid": pid,
            "port": port,
            "kind": kind,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "version": APP_VERSION,
            "install_path": str(self.root),
            "working_directory": str(self.root),
            "app_data_dir": str(self.data_root),
            "config_dir": str(config_dir()),
            "settings_path": str(settings_path()),
            "credential_path": str(credential_path()),
            "main_pid": os.getpid(),
        }
        _write_json(self.runtime_root / filename, meta)
        # Also write legacy .pid files for backward compat
        if filename in ("api.json", "web.json"):
            _write_json(self.runtime_root / filename.replace(".json", ".pid"), meta)

    def _api_healthy(self, token: str) -> bool:
        try:
            request = Request(f"http://127.0.0.1:{self.api_port}/api/health", headers={"X-Hotspot-Token": token})
            with urlopen(request, timeout=2) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    def _web_healthy(self) -> bool:
        try:
            with urlopen(self.web_url, timeout=2) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    @staticmethod
    def _wait_for(check: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.5)
        raise TimeoutError("backend health check timed out")

    def _boot_and_navigate(self) -> None:
        try:
            self.start_backend()
            if self.window is not None:
                self.window.load_url(self.web_url)
        except StartupError as exc:
            self._write_startup_log(f"{exc.code} {exc.user_reason}")
            if self.window is not None:
                try:
                    self.window.load_html(f"<html lang='zh-CN'><meta charset='utf-8'><style>body{{font-family:'Microsoft YaHei';padding:48px;color:#152238}}h1{{font-size:24px}}</style><h1>软件启动失败。</h1><p>具体原因：{exc.user_reason}</p><p>错误编号：{exc.code}</p><p>日志位置：{self.startup_log}</p></html>")
                except Exception:
                    pass
            self.stop_backend()
        except Exception as exc:
            self._log_exception("START-STREAMLIT-001", exc)
            if self.window is not None:
                try:
                    self.window.load_html(f"<html lang='zh-CN'><meta charset='utf-8'><style>body{{font-family:'Microsoft YaHei';padding:48px;color:#152238}}h1{{font-size:24px}}</style><h1>软件启动失败。</h1><p>具体原因：本地服务启动异常。</p><p>错误编号：START-STREAMLIT-001</p><p>日志位置：{self.startup_log}</p></html>")
                except Exception:
                    pass
            self.stop_backend()

    def _clear_runtime_metadata(self) -> None:
        for path in (
            self.runtime_root / "desktop.lock",
            self.runtime_root / "api.json",
            self.runtime_root / "web.json",
            self.runtime_root / "api.pid",
            self.runtime_root / "web.pid",
        ):
            path.unlink(missing_ok=True)

    def _read_runtime_metadata(self, path: Path) -> dict[str, object]:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                return {}
            if text.startswith("{"):
                value = json.loads(text)
                return value if isinstance(value, dict) else {}
            return {"pid": int(text), "version": "legacy", "install_path": ""}
        except Exception:
            return {}

    def _runtime_metadata(self) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for path in (self.runtime_root / "desktop.lock", self.runtime_root / "api.json", self.runtime_root / "web.json", self.runtime_root / "api.pid", self.runtime_root / "web.pid"):
            value = self._read_runtime_metadata(path)
            if value:
                values.append(value)
        return values

    def _metadata_is_current_version(self, value: dict[str, object]) -> bool:
        version = str(value.get("version") or "").strip()
        install_path = str(value.get("install_path") or "").strip()
        return version == APP_VERSION and install_path.lower() == str(self.root).lower()

    def _has_previous_runtime(self) -> bool:
        return any(not self._metadata_is_current_version(value) for value in self._runtime_metadata())

    def _metadata_pids(self) -> list[int]:
        pids: list[int] = []
        for value in self._runtime_metadata():
            for key in ("pid", "main_pid", "api_pid", "web_pid"):
                try:
                    pid = int(value.get(key) or 0)
                except Exception:
                    continue
                if pid > 0 and pid not in pids:
                    pids.append(pid)
        return pids

    def _pid_belongs_to_this_install(self, pid: int) -> bool:
        if os.name != "nt":
            return False
        script = (
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
            + str(pid)
            + "\";"
            "$p | Select-Object -First 1 ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            output = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command", script],
                stderr=subprocess.DEVNULL,
                timeout=5,
                text=True,
                encoding="utf-8",
                errors="replace",
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
            info = json.loads(output) if output.strip() else {}
        except Exception:
            return False
        haystack = (str(info.get("ExecutablePath") or "") + "\n" + str(info.get("CommandLine") or "")).lower()
        root = str(self.root).lower()
        data_root = str(self.data_root).lower()
        known_install_paths = {root}
        for value in self._runtime_metadata():
            install_path = str(value.get("install_path") or "").strip().lower()
            if install_path:
                known_install_paths.add(install_path)
        return any(path and path in haystack for path in known_install_paths) or data_root in haystack

    def _recover_stale_runtime(self) -> bool:
        recovered = False
        for pid in self._metadata_pids():
            if not self._pid_belongs_to_this_install(pid):
                continue
            try:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
                )
                recovered = True
            except Exception:
                continue
        self._clear_runtime_metadata()
        return recovered

    def _recover_previous_runtime_for_upgrade(self) -> bool:
        if not self._has_previous_runtime():
            return False
        _show_message("检测到旧版本正在运行，正在关闭后完成升级。", error=False)
        return self._recover_stale_runtime()

    def run(self) -> int:
        self.prepare_environment()
        self._recover_previous_runtime_for_upgrade()
        if not self.lock.acquire():
            current_metadata = [value for value in self._runtime_metadata() if self._metadata_is_current_version(value)]
            if current_metadata and _existing_window_exists():
                _focus_existing_window()
                return 0
            _show_message("软件上次未正常关闭，正在自动恢复。", error=False)
            self._recover_stale_runtime()
            time.sleep(0.5)
            if not self.lock.acquire():
                _show_message("软件恢复失败。\n错误编号：START-RECOVERY-001", error=True)
                return 1
        try:
            self.desktop_preflight()
            import webview

            webview.settings["ALLOW_DOWNLOADS"] = True
            self.window = webview.create_window(WINDOW_TITLE, html=STARTUP_HTML, width=1280, height=820, min_size=(1200, 760), resizable=True, text_select=True)
            self.window.events.closed += self._on_window_closed
            threading.Thread(target=self._boot_and_navigate, name="desktop-backend", daemon=True).start()
            webview.start(gui="edgechromium", debug=False)
            return 0
        except StartupError as exc:
            self.stop_backend()
            self._show_startup_failure(exc)
            return 1
        except Exception as exc:
            self.stop_backend()
            self._log_exception("START-WEBVIEW-001", exc)
            self._show_startup_failure(StartupError("START-WEBVIEW-001", "缺少桌面窗口组件，请重新安装完整版本。", exc))
            return 1
        finally:
            self.stop_backend()
            self.lock.release()

    def _on_window_closed(self) -> None:
        self.stop_backend()

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
        if not process or process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                return
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            process.terminate()
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def stop_backend(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._stop_process(self.web_process)
        self._stop_process(self.api_process)
        self.web_process = None
        self.api_process = None
        for handle in self._log_handles:
            try:
                handle.close()
            except Exception:
                pass
        self._log_handles.clear()
        self._clear_runtime_metadata()

    def desktop_preflight(self) -> None:
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._write_startup_log(f"desktop_preflight version={APP_VERSION}")
        pythonw = self.root / "runtime" / "pythonw.exe"
        if os.name == "nt" and not pythonw.is_file():
            raise StartupError("START-FILES-001", "缺少运行时 pythonw.exe。")
        for required in ("api.py", "app.py"):
            if not (self.root / required).is_file():
                raise StartupError("START-FILES-001", f"缺少必要文件 {required}。")
        try:
            import webview  # noqa: F401
            import webview.platforms.edgechromium  # noqa: F401
        except Exception as exc:
            self._log_exception("START-WEBVIEW-001", exc)
            raise StartupError("START-WEBVIEW-001", "缺少桌面窗口组件，请重新安装完整版本。", exc) from exc
        try:
            import uvicorn  # noqa: F401
            import streamlit  # noqa: F401
            import fastapi  # noqa: F401
        except Exception as exc:
            self._log_exception("START-API-001", exc)
            raise StartupError("START-API-001", "缺少本地服务组件，请重新安装完整版本。", exc) from exc
        webview2_ok = _webview2_runtime_available()
        self._write_startup_log(f"WebView2 检测结果={webview2_ok}")
        if not _webview2_runtime_available():
            raise StartupError("START-WEBVIEW2-001", "当前电脑缺少 WebView2 运行环境。")
        print("DESKTOP_PREFLIGHT_PASS")

    def _show_startup_failure(self, error: StartupError) -> None:
        self._write_startup_log(f"startup_failed code={error.code} reason={error.user_reason} log={self.startup_log}")
        diagnostic = json.dumps(
            {
                "product": WINDOW_TITLE,
                "version": APP_VERSION,
                "install_path": str(self.root),
                "python_path": self._python_executable(),
                "error_code": error.code,
                "reason": error.user_reason,
                "startup_log": str(self.startup_log),
            },
            ensure_ascii=False,
            indent=2,
        )
        message = (
            "软件启动失败。\n\n"
            f"具体原因：\n{error.user_reason}\n\n"
            f"错误编号：{error.code}\n\n"
            f"日志位置：\n{self.startup_log}\n\n"
            "[打开日志目录]\n[复制诊断信息]"
        )
        if self._show_failure_dialog(message, diagnostic):
            return
        _show_message(message, error=True)

    def _show_failure_dialog(self, message: str, diagnostic: str) -> bool:
        if os.name != "nt":
            return False
        try:
            self.logs_root.mkdir(parents=True, exist_ok=True)
            payload_path = self.logs_root / "startup-dialog.json"
            script_path = self.logs_root / "startup-dialog.ps1"
            payload_path.write_text(
                json.dumps(
                    {
                        "title": WINDOW_TITLE,
                        "message": message,
                        "log_dir": str(self.logs_root),
                        "diagnostic": diagnostic,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            script_path.write_text(
                r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$data = Get-Content -LiteralPath $args[0] -Encoding UTF8 -Raw | ConvertFrom-Json
$form = New-Object System.Windows.Forms.Form
$form.Text = $data.title
$form.StartPosition = "CenterScreen"
$form.Size = New-Object System.Drawing.Size(680, 420)
$form.TopMost = $true
$text = New-Object System.Windows.Forms.TextBox
$text.Multiline = $true
$text.ReadOnly = $true
$text.ScrollBars = "Vertical"
$text.Text = $data.message
$text.Location = New-Object System.Drawing.Point(16, 16)
$text.Size = New-Object System.Drawing.Size(632, 280)
$open = New-Object System.Windows.Forms.Button
$open.Text = "打开日志目录"
$open.Location = New-Object System.Drawing.Point(280, 320)
$open.Size = New-Object System.Drawing.Size(110, 32)
$open.Add_Click({ Start-Process -FilePath $data.log_dir })
$copy = New-Object System.Windows.Forms.Button
$copy.Text = "复制诊断信息"
$copy.Location = New-Object System.Drawing.Point(404, 320)
$copy.Size = New-Object System.Drawing.Size(116, 32)
$copy.Add_Click({ [System.Windows.Forms.Clipboard]::SetText([string]$data.diagnostic) })
$close = New-Object System.Windows.Forms.Button
$close.Text = "关闭"
$close.Location = New-Object System.Drawing.Point(534, 320)
$close.Size = New-Object System.Drawing.Size(90, 32)
$close.Add_Click({ $form.Close() })
$form.Controls.AddRange(@($text, $open, $copy, $close))
[void]$form.ShowDialog()
'''.strip(),
                encoding="utf-8",
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), str(payload_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
                check=False,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
            return True
        except Exception:
            return False


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--headless-smoke", action="store_true")
    parser.add_argument("--desktop-preflight", action="store_true")
    args, _ = parser.parse_known_args()
    host = DesktopHost()
    if args.desktop_preflight:
        try:
            host.prepare_environment()
            host.desktop_preflight()
            return 0
        except StartupError as exc:
            host._show_startup_failure(exc)
            return 1
    if args.headless_smoke:
        if not host.lock.acquire():
            return 0
        try:
            host.start_backend()
            print("BACKEND_LIFECYCLE_PASS")
            print("PORT_CONFLICT_DESKTOP_RECOVERY_PASS")
            return 0
        finally:
            host.stop_backend()
            host.lock.release()
    return host.run()


if __name__ == "__main__":
    raise SystemExit(main())
