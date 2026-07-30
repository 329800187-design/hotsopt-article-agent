from __future__ import annotations

from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from providers.contracts import ArticleGenerationRequest
from providers.text_provider import OpenAITextProvider, ProviderError, _build_request_url


def test_text_request_url_deduplicates_overlapping_endpoint():
    url, details = _build_request_url("https://example.com/v1/chat/completions", "/chat/completions")
    assert url == "https://example.com/v1/chat/completions"
    assert details["normalization"] == "deduplicated_overlap"
    assert details["normalized_endpoint"] == "/chat/completions"


def test_text_provider_502_is_classified_with_diagnostics(monkeypatch):
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(
        502,
        request=request,
        json={"error": {"message": "No available channel for model test"}},
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return response

    import providers.text_provider as text_provider

    monkeypatch.setattr(text_provider, "create_http_client", lambda *_args, **_kwargs: FakeClient())
    provider = OpenAITextProvider(
        {
            "api_key": "key-123",
            "base_url": "https://example.com/v1",
            "endpoint": "/chat/completions",
            "model": "bad-model",
            "timeout_seconds": 30,
        }
    )
    try:
        provider.generate_article(ArticleGenerationRequest("测试"))
    except ProviderError as exc:
        assert exc.code == "MODEL_NOT_FOUND"
        assert exc.details["http_status"] == 502
        assert exc.details["error_type"] == "model_invalid"
        assert exc.details["final_url"] == "https://example.com/v1/chat/completions"
        assert exc.details["model"] == "bad-model"
    else:
        raise AssertionError("expected ProviderError")


def test_generation_failure_502_disables_retry_and_exposes_actions(monkeypatch):
    from generation import single_task

    state = {"task_id": "task-1", "model_info": {"text": {"timeout_seconds": 180}}}
    monkeypatch.setattr(single_task, "_persist", lambda current, _store: current)
    result = single_task._failure(
        state,
        store=None,
        step="generating_article",
        error=ProviderError("PROVIDER_INTERNAL_ERROR", "provider returned HTTP 502", details={"http_status": 502}),
        status="failed",
    )
    assert result["retryable"] is False
    assert result["next_actions"] == ["test_text_model", "retry_article", "open_model_settings"]
    assert result["safe_error_message"]
    assert "test_text_model" in result["next_actions"]


def test_settings_page_uses_scoped_keys_and_shared_key_sync_only():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert 'key="rc132_text_save"' in source
    assert 'key="rc132_text_discover"' in source
    assert 'key="rc132_image_discover"' in source
    assert 'key=f"rc1_clear_topic_basket_{scope}"' in source
    assert 'image_values.update({"api_key": text_key or "***", "has_api_key": bool(text_key or image.get("has_api_key"))})' in source
    assert 'text_values.update({"api_key": image_key or "***", "has_api_key": bool(image_key or text.get("has_api_key"))})' in source
    assert 'image_values.update({"base_url": text_base' not in source
    assert 'text_values.update({"base_url": image_base' not in source


def test_failed_task_panel_surfaces_diagnostics_and_recovery_actions():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert 'def _render_failed_task_panel' in source
    assert '查看失败诊断' in source
    assert '仅重试文章' in source
    assert '去测试文本模型' in source
    assert '返回模型设置' in source
    assert '"/tasks/{task_id}/retry-article"' in source
