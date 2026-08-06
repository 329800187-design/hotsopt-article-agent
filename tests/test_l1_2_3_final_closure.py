from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_recovery_failure_cannot_show_success_message():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    block = source.split('if st.button("重新检查系统时间"):', 1)[1].split('if st.button("重新检查许可证"):', 1)[0]
    assert "recovery_result = recover_clock_rollback()" in block
    assert 'if recovery_result.get("recovered"):' in block
    assert block.index('st.success("系统时间已恢复检查，授权已恢复。")') > block.index('if recovery_result.get("recovered"):')
    assert "RECOVERY_FAILED_NO_FALSE_SUCCESS_PASS" not in block


def test_expired_recovery_has_user_facing_message():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert 'recovery_result.get("code") == "LICENSE_EXPIRED"' in source
    assert "当前许可证已经过期，请联系软件提供方续期" in source
    assert "系统时间仍未校准到可信范围，请校准后再试" in source


def test_technical_audit_describes_json_time_scope_truthfully():
    source = (ROOT / "TECH_AUDIT.md").read_text(encoding="utf-8")
    assert "最后可信时间 `license_last_seen_utc` 使用 Windows DPAPI 保护" in source
    assert "状态 JSON 会保存回退状态、可信参考时间和恢复流程时间" in source
    assert "不提供防本地文件篡改、防反编译或专业级 DRM/反破解能力" in source
    assert "许可证状态 JSON 不保存明文时间" not in source
