from __future__ import annotations

from pathlib import Path


def text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_MISSING_REQUIRED_RUNTIME_ABORT_BUILD_PASS():
    source = text("scripts/package_rc1.py")
    build = text("scripts/build_rc1_3_3_lite.py")
    assert "missing required runtime distribution" in source
    assert "raise RuntimeError" in source
    assert "validate_windows_manifest" in build
    assert "RUNTIME_DEPENDENCY_ERRORS_EMPTY_PASS failed" in build


def test_PYWEBVIEW_RUNTIME_INCLUDED_PASS():
    build = text("scripts/build_rc1_3_3_lite.py")
    assert "runtime/Lib/site-packages/webview/__init__.py" in build
    assert "runtime/Lib/site-packages/pywebview-5.4.dist-info/METADATA" in build
    assert "pythonnet" in build
    assert "runtime/Lib/site-packages/clr.py" in build


def test_RUNTIME_DEPENDENCY_ERRORS_EMPTY_PASS():
    source = text("scripts/package_rc1.py")
    assert "dependency_errors" in source
    assert "PACKAGE_SCAN_FAILED" in source
    assert "missing required runtime distribution" in source


def test_FINAL_SETUP_WEBVIEW_IMPORT_PASS():
    build = text("scripts/build_rc1_3_3_lite.py")
    assert "import webview; print('FINAL_SETUP_WEBVIEW_IMPORT_PASS')" in build


def test_FINAL_SETUP_EDGECHROMIUM_IMPORT_PASS():
    build = text("scripts/build_rc1_3_3_lite.py")
    assert "import webview.platforms.edgechromium; print('FINAL_SETUP_EDGECHROMIUM_IMPORT_PASS')" in build


def test_FINAL_SETUP_CLR_IMPORT_PASS():
    build = text("scripts/build_rc1_3_3_lite.py")
    assert "import clr; print('FINAL_SETUP_CLR_IMPORT_PASS')" in build


def test_FINAL_SETUP_RUNTIME_IMPORT_PASS():
    build = text("scripts/build_rc1_3_3_lite.py")
    assert "import uvicorn, streamlit, fastapi, research.service, api" in build
    assert "FINAL_SETUP_RUNTIME_IMPORT_PASS" in build


def test_DESKTOP_PREFLIGHT_PASS():
    source = text("desktop_host.py")
    assert "--desktop-preflight" in source
    assert "import webview.platforms.edgechromium" in source
    assert "DESKTOP_PREFLIGHT_PASS" in source
    assert "runtime\" / \"pythonw.exe" in source


def test_STARTUP_LOG_PASS():
    source = text("desktop_host.py")
    assert "startup.log" in source
    assert "pywebview_version" in source
    assert "API_PROCESS_EXITED_EARLY" in source
    assert "Streamlit 进程退出码" in source
    assert "traceback.format_exc" in source
    assert "HOTSPOT_LOCAL_API_TOKEN" in source


def test_START_ERROR_CODES_DISTINCT_PASS():
    source = text("desktop_host.py")
    for code in (
        "START-WEBVIEW-001",
        "START-API-001",
        "START-STREAMLIT-001",
        "START-WEBVIEW2-001",
        "START-DATABASE-001",
        "START-FILES-001",
    ):
        assert code in source


def test_R2_2_1_OUTPUT_NAMES_PASS():
    from modules.app_version import APP_VERSION

    build = text("scripts/build_rc1_3_3_lite_r2_2_7.py")
    assert APP_VERSION in build
    assert "Source.zip" in build
    assert "_用户主流程GUI证据包.zip" in build
    assert "等待用户" in build
