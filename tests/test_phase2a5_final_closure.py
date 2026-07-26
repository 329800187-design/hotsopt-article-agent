from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from modules.config_store import load_settings
from modules.database import SQLiteStore
from modules.models import HotTopic
from modules.security import sanitize_json
from providers.image_provider import normalize_image_size
from scripts import phase2a_live_smoke
from scripts.security_scan import scan_tree


def test_capability_probes_validate_content(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, value):
            self.value = value

        def json(self):
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    class Client:
        def __init__(self, responses):
            self.responses = iter(responses)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return next(self.responses)

    profile = {"base_url": "https://example.invalid/v1", "endpoint": "/chat/completions", "model": "test", "api_key": "test-key", "auth_type": "bearer"}
    monkeypatch.setattr(phase2a_live_smoke, "create_http_client", lambda settings: Client([Response({"output": []}), Response(ValueError("not json"))]))
    result = phase2a_live_smoke.probe_text_capabilities(profile, {})
    assert result["responses"]["success"] is False
    assert result["responses"]["error_code"] == "INVALID_RESPONSE"
    assert result["json_schema"]["success"] is False

    monkeypatch.setattr(phase2a_live_smoke, "create_http_client", lambda settings: Client([Response({"output_text": "OK"}), Response({"choices": [{"message": {"content": '{\"ok\": true}'}}]})]))
    result = phase2a_live_smoke.probe_text_capabilities(profile, {})
    assert result["responses"]["success"] is True
    assert result["json_schema"]["json_schema_valid"] is True


def test_security_scans_runtime_files_and_data_parent(tmp_path):
    root = tmp_path / "data" / "project"
    (root / "data").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "outputs").mkdir()
    (root / "data" / "task.json").write_text("api_key=RUNTIME_SECRET_VALUE", encoding="utf-8")
    (root / "logs" / "app.log").write_text("Authorization=Bearer LOG_SECRET_VALUE", encoding="utf-8")
    (root / "outputs" / "result.json").write_text("token=OUTPUT_SECRET_VALUE", encoding="utf-8")
    db = root / "data" / "app.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE secrets (value TEXT)")
        connection.execute("INSERT INTO secrets VALUES ()", ("password=SQLITE_SECRET_VALUE",))
        connection.commit()
    result = scan_tree(root, [])
    assert result["status"] == "SECURITY_SCAN_FAILED"
    assert {item["path"] for item in result["forbidden_hits"]} >= {"data/task.json", "logs/app.log", "outputs/result.json", "data/app.sqlite"}


def test_dashscope_preset_and_size_conversion():
    example = json.loads(Path("config/settings.example.json").read_text(encoding="utf-8"))
    preset = example["dashscope_native_example"]
    assert preset["endpoint"] == "/api/v1/services/aigc/multimodal-generation/generation"
    assert preset["model"] == "qwen-image-2.0-pro"
    assert preset["api_format"] == "dashscope_native"
    assert preset["auth_type"] == "bearer"
    assert normalize_image_size("1024x1024", "dashscope_native") == "1024*1024"
    assert normalize_image_size("1536*1024", "openai_compatible") == "1536x1024"


def test_retry_cover_updates_public_model_info_without_text_retry(tmp_path, monkeypatch):
    from generation import single_task
    import modules.generation_store as generation_store
    from providers.contracts import ImageGenerationRequest

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    topic = HotTopic(id="phase2a5-topic", title="测试热点", summary="摘要", source="test", source_name="测试源", source_url="https://example.com/topic")
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic])
    task = store.create_task("2A.5 retry", "multi_topic", [topic.to_dict()], 1)
    calls = {"text": 0}

    def article(*args, **kwargs):
        calls["text"] += 1
        return {"title": "真实标题", "intro": "摘要", "summary": "摘要", "sections": [{"heading": "一", "body": "正文", "image_brief": "场景"}] * 3, "content_markdown": "# 真实标题\n正文", "tags": [], "demo_mode": False}

    class Image:
        last_response_type = "base64"

        def __init__(self, profile, network_settings=None):
            self.profile = profile

        def generate(self, prompt, output_path):
            if self.profile["model"].startswith("bad-"):
                raise RuntimeError("image failure")
            from PIL import Image as PillowImage
            output_path.parent.mkdir(parents=True, exist_ok=True)
            PillowImage.new("RGB", (128, 128), (1, 2, 3)).save(output_path, format="PNG")
            return output_path

    monkeypatch.setattr(single_task, "generate_article", article)
    monkeypatch.setattr(single_task, "OpenAIImageProvider", Image)
    text = {"model": "mimo-v2.5", "api_key": "test", "base_url": "https://example.invalid", "auth_type": "bearer"}
    bad = {"model": "bad-image", "api_key": "test", "base_url": "https://example.invalid", "auth_type": "bearer"}
    good = {"model": "qwen-image-2.0-pro", "api_format": "dashscope_native", "api_key": "test", "base_url": "https://example.invalid", "auth_type": "bearer"}
    failed = single_task.run_single_task(task, text, bad, settings={"network": {}}, store=store)
    assert failed["status"] == "partial_success"
    before = calls["text"]
    completed = single_task.run_single_task(task, text, good, settings={"network": {}}, store=store, retry_step="retry-cover")
    assert completed["status"] == "completed"
    assert calls["text"] == before == 1
    assert completed["model_info"]["image"]["model"] == "qwen-image-2.0-pro"
    assert completed["model_info"]["image"]["api_format"] == "dashscope_native"
    assert [item["status"] for item in completed["attempt_history"]] == ["failed", "completed"]


def test_status_has_authoritative_test_result():
    status = Path("STATUS.md").read_text(encoding="utf-8")
    assert "当前测试结果" in status
