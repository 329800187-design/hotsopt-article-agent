from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_clipboard_api_declares_pointer_sized_signatures():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    for required in [
        "GetClipboardData.argtypes = [wintypes.UINT]",
        "GetClipboardData.restype = wintypes.HANDLE",
        "GlobalLock.argtypes = [wintypes.HGLOBAL]",
        "GlobalLock.restype = wintypes.LPVOID",
        "GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]",
        "GlobalAlloc.restype = wintypes.HGLOBAL",
        "SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]",
        "SetClipboardData.restype = wintypes.HANDLE",
    ]:
        assert required in source


def test_webview2_runtime_is_checked_before_window_creation():
    source = (ROOT / "desktop_host.py").read_text(encoding="utf-8")
    assert "def _webview2_runtime_available" in source
    assert "WEBVIEW2-001" in source
    assert "if not _webview2_runtime_available()" in source


def test_rebuild_source_contains_packaging_and_webview2_payload():
    phase1 = (ROOT / "scripts" / "package_phase1.py").read_text(encoding="utf-8")
    builder = (ROOT / "scripts" / "build_rc1_3_1.py").read_text(encoding="utf-8")
    assert '"packaging"' in phase1
    assert "MicrosoftEdgeWebView2Setup.exe" in builder
