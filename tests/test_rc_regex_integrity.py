from __future__ import annotations

from generation.content_quality import claim_supported_by_fact
from modules.security import is_sensitive_key, redact_sensitive_text, sanitize_sensitive_data
from research.service import _fact_conflicts, _fact_location, _fact_number, _fact_time, _is_background_fact, _number_signature


def test_RC_REGEX_NUMBER_SIGNATURE_FULL_TEXT_PASS():
    assert _number_signature("投入30亿元") == {"30亿元"}
    assert "300亿元" not in _number_signature("投入30亿元")
    assert _number_signature("投入300万元") == {"300万元"}
    assert "300亿元" not in _number_signature("投入300万元")


def test_RC_REGEX_CLAIM_DIRECTION_AND_OPPOSITE_PASS():
    assert not claim_supported_by_fact("投入300亿元", "投入30亿元用于相关工作。")
    assert not claim_supported_by_fact("2025年7月30日发布公告", "2026年7月30日发布公告。")
    assert _fact_conflicts("公司称营收增长", "公司称营收下降")
    assert _fact_conflicts("预算增加300万元", "预算减少300万元")


def test_RC_REGEX_FACT_EXTRACTION_PASS():
    text = "2026年7月30日，北京市某公司投入300万元，占比12%。"
    assert _fact_time(text) == "2026年7月30日"
    assert _fact_location(text)
    assert _fact_number(text) == "300万元"
    assert "12%" in _number_signature(text)
    assert _is_background_fact("近年来相关规定持续完善")
    assert not _is_background_fact("公司今天发布公告")


def test_RC_REGEX_SECURITY_SANITIZE_PASS():
    for key in (
        "cookie",
        "set-cookie",
        "authorization",
        "api_key",
        "api-key",
        "access_token",
        "access-token",
        "password",
        "proxy_password",
        "secret",
        "client_secret",
    ):
        assert is_sensitive_key(key), key
    assert not is_sensitive_key("article_title")
    payload = {
        "outer": {"client_secret": "secret-value", "normal": "keep"},
        "items": [{"access_token": "tok"}, ("password", {"plain": "ok"})],
    }
    sanitized = sanitize_sensitive_data(payload)
    assert "client_secret" not in sanitized["outer"]
    assert sanitized["outer"]["normal"] == "keep"
    assert "access_token" not in sanitized["items"][0]
    assert redact_sensitive_text("https://user:pass@example.com Bearer abc.def") == "https://***:***@example.com Bearer [REDACTED]"
