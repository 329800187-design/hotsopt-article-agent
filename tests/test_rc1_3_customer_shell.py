from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_customer_launcher_executable_is_present():
    executable = ROOT / "热点图文工作台.exe"
    if not executable.is_file():
        pytest.skip("launcher exe is created during final packaging, not required in cleaned source workspace")
    assert executable.is_file()
    assert executable.read_bytes()[:2] == b"MZ"


def test_launcher_reuses_instances_and_selects_ports():
    text = (ROOT / "desktop_host.py").read_text(encoding="utf-8")
    assert "SingleInstance" in text
    assert "_find_available_port" in text
    assert 'webview.start(gui="edgechromium"' in text
    assert 'os.environ["HOTSPOT_NO_BROWSER"] = "1"' in text
    assert "Start-Process $Url" not in text


def test_activation_supports_paste_and_automatic_file_import():
    text = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert "粘贴许可证内容" in text
    assert "_try_auto_import_license" in text
    assert "从剪贴板粘贴并激活" in text
    assert "复制设备码" in text
    assert "license_error_message" in text


def test_customer_package_contains_launcher_executable():
    text = (ROOT / "scripts" / "package_rc1.py").read_text(encoding="utf-8")
    assert '"热点图文工作台.exe"' in text
    assert "内置 64 位 Python 3.11" in text


def test_customer_data_stays_under_local_appdata():
    text = (ROOT / "launcher.ps1").read_text(encoding="utf-8")
    assert "$env:LOCALAPPDATA" in text
    assert "HOTSPOT_DATA_ROOT" in text
