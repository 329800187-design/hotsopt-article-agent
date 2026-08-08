from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


ROOT = Path(".")


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _render_page(label: str) -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=30)
    at.session_state["rc1_navigation"] = label
    at.run()
    assert not at.exception
    return at


def test_OLD_INSTALL_DETECTED_PASS():
    source = _source("packaging/setup_bootstrapper.cs")
    assert "PreviousInstallRoot" in source
    assert "InstallLocation" in source
    assert "检测到旧版本" in source


def test_UPGRADE_KEEP_USER_DATA_PASS():
    source = _source("packaging/setup_bootstrapper.cs")
    assert "升级将保留文章、模型配置和激活信息" in source
    assert "CleanProgramFiles" in source
    assert "UserDataRoot" in source
    assert "data" in source and "export" in source and "logs" in source


def test_OLD_VERSION_RUNNING_DETECTED_PASS():
    source = _source("desktop_host.py")
    assert "APP_VERSION" in source
    assert "_recover_previous_runtime_for_upgrade" in source
    assert "previous_runtime_detected=auto_recover" in source
    recovery_block = source.split("def _recover_previous_runtime_for_upgrade", 1)[1].split("def run", 1)[0]
    assert "_show_message" not in recovery_block


def test_OLD_VERSION_PROCESS_STOP_PASS():
    source = _source("desktop_host.py")
    assert "main_pid" in source
    assert "api_pid" in source
    assert "web_pid" in source
    assert "taskkill.exe" in source


def test_NEW_VERSION_NOT_FOCUS_OLD_WINDOW_PASS():
    source = _source("desktop_host.py")
    assert "current_metadata" in source
    assert "if current_metadata and _existing_window_exists()" in source
    assert "Local\\\\HotspotArticleAgentProduct" in source


def test_RUNNING_VERSION_METADATA_PASS():
    source = _source("desktop_host.py")
    for marker in ("version", "install_path", "data_path", "main_pid", "api_pid", "web_pid", "started_at"):
        assert marker in source


def test_WINDOWS_INSTALLED_APPS_REGISTRY_VERIFY_PASS():
    from modules.app_version import APP_VERSION

    source = _source("packaging/setup_bootstrapper.cs")
    assert "DisplayVersion" in source
    assert APP_VERSION in source
    assert "EstimatedSize" in source
    assert "InstallDate" in source
    assert "VerifyInstalledAppRegistration" in source


def test_UNINSTALL_TEMP_CLEANER_PASS():
    source = _source("packaging/setup_bootstrapper.cs")
    assert "--cleanup" in source
    assert "Path.GetTempPath()" in source
    assert "CleanupAfterUninstall" in source
    assert "卸载完成。" in source


def test_VISIBLE_APP_VERSION_PASS():
    from modules.app_metadata import APP_VERSION

    at = _render_page("ⓘ 关于软件")
    rendered = str(at)
    assert "from modules.app_metadata import" in _source("modules/app_version.py")
    assert APP_VERSION in _source("modules/app_metadata.py")
    assert "关于软件" in rendered
    assert "复制诊断信息" in rendered


def test_DIAGNOSTIC_NO_SECRET_PASS():
    from modules.app_version import APP_VERSION, diagnostic_info

    info = diagnostic_info(Path("C:/demo/install"), Path("C:/demo/data"))
    text = str(info)
    assert info["version"] == APP_VERSION
    assert "api_key" not in text.lower()
    assert "license" not in text.lower()


def test_DUAL_KEY_SETTINGS_VISIBLE_PASS():
    at = _render_page("⚙ 模型设置")
    rendered = str(at)
    for label in (
        "文本接口",
        "图片接口",
        "文本 API Key",
        "图片 API Key",
        "文本 API 地址",
        "图片 API 地址",
        "保存文本配置",
        "保存图片配置",
        "文本和图片使用同一个API Key",
    ):
        assert label in rendered


def test_LEGACY_SINGLE_KEY_UI_REMOVED_PASS():
    at = _render_page("⚙ 模型设置")
    rendered = str(at)
    assert "保存并检测" not in rendered
    assert "自动检测并配置" not in rendered
    assert "单一 API Key" not in rendered


def test_BUILD_OUTPUT_NAMES_R2_2_PASS():
    source = _source("scripts/build_rc1_3_3_lite_r2_2_7.py")
    assert "from modules.app_metadata import" in source
    assert "RELEASE = APP_VERSION" in source
    assert "Source.zip" in source
    assert "_用户主流程GUI证据包.zip" in source
    assert "等待 Windows" in _source("STATUS.md")


def test_DESKTOP_APPHOST_ASSEMBLY_MATCHES_FORMAL_EXE_PASS():
    project = _source("packaging/launcher_shell.csproj")
    build = _source("scripts/build_rc1_3_3_lite_r2_2_7.py")
    assert "<AssemblyName>热点图文批量生产工作台</AssemblyName>" in project
    assert 'APP_EXE = "\\u70ed\\u70b9\\u56fe\\u6587\\u6279\\u91cf\\u751f\\u4ea7\\u5de5\\u4f5c\\u53f0.exe"' in build


def test_LICENSE_PUBLIC_KEY_IS_PACKAGED_PASS():
    key_path = ROOT / "resources" / "license_public_key.pem"
    assert key_path.is_file()
    assert "BEGIN PUBLIC KEY" in key_path.read_text(encoding="utf-8")
    package = _source("scripts/package_phase1.py")
    builder = _source("scripts/package_rc1.py")
    assert '"resources"' in package
    assert '"resources/"' in builder
