from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api
import generation.inline_images as inline_images
import generation.selected_images as selected_images
import generation.single_task as single_task
import modules.generation_store as generation_store
from generation.inline_images import normalize_task_images_for_plan, reserve_image_generation_call, sync_inline_image_files
from generation.single_task import prepare_generation_state, run_single_task
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic
from providers.contracts import ModelTestResult
from providers.text_provider import ProviderError


def _make_store_and_task(tmp_path: Path, options: dict | None = None) -> tuple[SQLiteStore, dict]:
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = HotTopic(
        id="hf2-2-topic",
        title="HF2.2 测试热点",
        summary="公开资料摘要",
        source="test",
        source_name="测试源",
        source_url="https://example.com/topic",
    )
    store.save_topics([topic])
    task = store.create_task("HF2.2 任务", "multi_topic", [topic.to_dict()], 1, generation_options=options or {})
    return store, task


def _profiles(text_model: str = "verified-text-model") -> tuple[dict, dict]:
    return (
        {
            "name": "text",
            "api_key": "text-key",
            "base_url": "https://example.com/v1",
            "endpoint": "/chat/completions",
            "model": text_model,
            "auth_type": "bearer",
        },
        {
            "name": "image",
            "api_key": "image-key",
            "base_url": "https://example.com/v1",
            "endpoint": "/images/generations",
            "model": "fake-image-model",
            "auth_type": "bearer",
            "size": "1536x1024",
        },
    )


def _article() -> dict:
    return {
        "title": "HF2.2 正文标题",
        "intro": "这是一段导语。",
        "summary": "这是一段摘要。",
        "content_markdown": "# HF2.2 正文标题\n\n这是一段导语。\n\n## 一\n第一段。\n\n## 二\n第二段。",
        "sections": [
            {"heading": "一", "body": "第一段。", "image_brief": "现场画面"},
            {"heading": "二", "body": "第二段。", "image_brief": "分析画面"},
            {"heading": "三", "body": "第三段。", "image_brief": "观察画面"},
        ],
        "images": [],
        "source_list": ["[1] 测试机构：《测试标题》，2026-07-25\n原文链接：https://example.com/topic"],
    }


def _write_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1536, 1024), color).save(path, format="PNG")


class FakeSelectedImageProvider:
    calls = 0

    def __init__(self, profile, network_settings=None):
        self.profile = profile
        self.network_settings = network_settings or {}
        self.last_response_type = "base64"

    def generate(self, prompt: str, output_path: Path) -> Path:
        type(self).calls += 1
        palette = {
            1: (40, 120, 220),
            2: (220, 120, 40),
            3: (80, 180, 80),
        }
        _write_png(output_path, palette.get(type(self).calls, (120, 120, 120)))
        return output_path


def _seed_completed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inline_total: int = 0,
    include_cover: bool = False,
    text_model: str = "old-text-model",
) -> tuple[SQLiteStore, dict, dict]:
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store, task = _make_store_and_task(tmp_path)
    text_profile, image_profile = _profiles(text_model=text_model)
    state = prepare_generation_state(task, text_profile, image_profile, store=store)
    root = generation_task_dir(task["task_id"])
    article = _article()
    cover = None
    article_images: list[dict] = []
    if include_cover:
        _write_png(root / "images" / "cover.png", (180, 40, 40))
        cover = {"status": "completed", "path": "images/cover.png", "prompt": "封面", "metadata": {"width": 1536, "height": 1024}}
        article["cover"] = cover
        article_images.append({"role": "cover", "path": "images/cover.png", "status": "completed"})
    inline_assets: list[dict] = []
    for index in range(1, inline_total + 1):
        relative_path = f"images/section-{index}.png"
        _write_png(root / relative_path, (40 * index, 60 * index, 80))
        asset = {
            "role": "inline",
            "image_id": f"section-{index}",
            "order": index,
            "status": "completed",
            "path": relative_path,
            "file_path": relative_path,
            "section_title": f"小节 {index}",
            "paragraph_ref": f"section-{index}",
            "insert_after_paragraph": index,
            "prompt": f"正文图片 {index}",
        }
        inline_assets.append(asset)
        article_images.append(asset)
    article["images"] = article_images
    state.update(
        {
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "completed_at": state.get("completed_at") or "2026-07-25T10:00:00+08:00",
            "article": article,
            "cover": cover,
            "inline_images": inline_assets,
            "quality_gate": {"status": "passed", "passed": True},
            "research_bundle": {"sources": [{"url": "https://example.com/topic"}], "usable_fact_count": 1},
        }
    )
    sync_inline_image_files(state)
    save_generation_task(state, expected_version=int(state.get("state_version") or 0), allow_terminal_recovery=True)
    store.update_task_status(task["task_id"], "completed")
    return store, task, state


def test_image_test_endpoint_calls_provider_once_and_only_writes_model_test(monkeypatch, tmp_path):
    class FakeTestProvider:
        calls = 0

        def __init__(self, profile, network_settings=None):
            self.profile = profile

        def test_connection(self, output_path: Path) -> ModelTestResult:
            type(self).calls += 1
            _write_png(output_path, (10, 80, 180))
            return ModelTestResult(
                True,
                "openai-compatible-image",
                str(self.profile.get("model") or ""),
                elapsed_ms=12,
                image_response_type="base64",
                details={"generation_calls": 1, "charged": True},
            )

    settings = {"image_profile": {}, "network": {}}
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    monkeypatch.setattr(api, "OpenAIImageProvider", FakeTestProvider)
    monkeypatch.setattr(api, "load_settings", lambda: settings)
    monkeypatch.setattr(api, "save_settings", lambda updated: settings.update(updated))
    monkeypatch.setattr(api, "model_test_root", lambda: tmp_path / "model_test")
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")

    client = TestClient(api.app)
    response = client.post(
        "/api/models/image/test",
        json={
            "confirm_paid_test": True,
            "profile": {
                "base_url": "https://example.com/v1",
                "endpoint": "/images/generations",
                "model": "fake-image-model",
                "api_key": "image-key",
            },
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert FakeTestProvider.calls == 1
    assert body["details"]["generation_calls"] == 1
    assert (tmp_path / "model_test" / "image-test.png").is_file()
    assert not (tmp_path / "tasks").exists()


def test_economy_mode_generates_exactly_one_cover_image(monkeypatch, tmp_path):
    store, task, _ = _seed_completed_state(tmp_path, monkeypatch)
    FakeSelectedImageProvider.calls = 0
    monkeypatch.setattr(selected_images, "OpenAIImageProvider", FakeSelectedImageProvider)

    result = selected_images.generate_selected_images(
        task["task_id"],
        _profiles()[1],
        {"network": {}},
        store,
        include_cover=True,
        inline_count=0,
    )

    assert FakeSelectedImageProvider.calls == 1
    assert result["image_generation_calls"] == 1
    assert result["inline_image_summary"]["total"] == 0
    assert len(result["article"]["images"]) == 1
    assert result["article"]["images"][0]["role"] == "cover"


def test_standard_mode_generates_one_cover_and_one_inline(monkeypatch, tmp_path):
    store, task, _ = _seed_completed_state(tmp_path, monkeypatch)
    FakeSelectedImageProvider.calls = 0
    monkeypatch.setattr(selected_images, "OpenAIImageProvider", FakeSelectedImageProvider)

    result = selected_images.generate_selected_images(
        task["task_id"],
        _profiles()[1],
        {"network": {}},
        store,
        include_cover=True,
        inline_count=1,
    )

    assert FakeSelectedImageProvider.calls == 2
    assert result["image_generation_calls"] == 2
    assert result["inline_image_summary"]["total"] == 1
    assert len(result["article"]["images"]) == 2
    assert [item["role"] for item in result["article"]["images"]] == ["cover", "inline"]


def test_old_three_inline_images_are_cleared_in_economy_mode(monkeypatch, tmp_path):
    store, task, _ = _seed_completed_state(tmp_path, monkeypatch, inline_total=3, include_cover=True)

    result = normalize_task_images_for_plan(task["task_id"], "economy", store=store)
    root = generation_task_dir(task["task_id"])
    assets = json.loads((root / "images" / "assets.json").read_text(encoding="utf-8"))

    assert len(result["inline_images"]) == 0
    assert result["inline_image_summary"]["total"] == 0
    assert len(result["article"]["images"]) == 1
    assert len(list((root / "images" / "legacy-unused").glob("section-*.png"))) == 3
    assert assets["summary"]["total"] == 0


def test_old_three_inline_images_keep_only_one_in_standard_mode(monkeypatch, tmp_path):
    store, task, _ = _seed_completed_state(tmp_path, monkeypatch, inline_total=3, include_cover=True)

    result = normalize_task_images_for_plan(task["task_id"], "standard", store=store)
    root = generation_task_dir(task["task_id"])

    assert len(result["inline_images"]) == 1
    assert result["inline_image_summary"]["total"] == 1
    assert len(result["article"]["images"]) == 2
    assert len(list((root / "images" / "legacy-unused").glob("section-*.png"))) == 2
    assert (root / "images" / "section-1.png").is_file()


def test_repeated_selected_image_submission_is_idempotent(monkeypatch, tmp_path):
    store, task, _ = _seed_completed_state(tmp_path, monkeypatch)
    FakeSelectedImageProvider.calls = 0
    monkeypatch.setattr(selected_images, "OpenAIImageProvider", FakeSelectedImageProvider)

    first = selected_images.generate_selected_images(
        task["task_id"],
        _profiles()[1],
        {"network": {}},
        store,
        include_cover=True,
        inline_count=1,
    )
    calls_after_first = FakeSelectedImageProvider.calls
    second = selected_images.generate_selected_images(
        task["task_id"],
        _profiles()[1],
        {"network": {}},
        store,
        include_cover=True,
        inline_count=1,
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert calls_after_first == 2
    assert FakeSelectedImageProvider.calls == calls_after_first


def test_budget_guard_blocks_extra_provider_calls():
    state = {"task_id": "budget-test", "approved_image_budget": 1, "image_generation_calls": 1}
    with pytest.raises(ProviderError) as error:
        reserve_image_generation_call(state)
    assert error.value.code == "IMAGE_BUDGET_EXCEEDED"
    assert state["image_generation_calls"] == 1
    assert state["image_usage"]["budget_exceeded"] is True


def test_retry_preparation_refreshes_model_info_to_latest_verified_profile(monkeypatch, tmp_path):
    store, task, state = _seed_completed_state(tmp_path, monkeypatch, text_model="old-model")
    state.update({"status": "failed", "stage": "failed", "failed_step": "generating_article"})
    save_generation_task(state, expected_version=int(state.get("state_version") or 0), allow_terminal_recovery=True)
    store.update_task_status(task["task_id"], "failed")

    refreshed = prepare_generation_state(task, _profiles(text_model="new-verified-model")[0], _profiles()[1], store=store)

    assert refreshed["status"] == "queued"
    assert refreshed["model_info"]["text"]["model"] == "new-verified-model"


def test_model_not_found_failure_keeps_research_bundle_and_zero_image_calls(monkeypatch, tmp_path):
    store, task, state = _seed_completed_state(tmp_path, monkeypatch, text_model="old-model")
    original_bundle = {
        "research_status": "sufficient",
        "sources": [{"url": "https://example.com/topic"}],
        "usable_fact_count": 1,
        "accepted_source_count": 2,
        "source_list": ["kept-source"],
    }
    state.update(
        {
            "status": "failed",
            "stage": "failed",
            "failed_step": "generating_article",
            "article": None,
            "cover": None,
            "inline_images": [],
            "inline_image_summary": {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "completed"},
            "research_bundle": original_bundle,
            "image_generation_calls": 0,
        }
    )
    save_generation_task(state, expected_version=int(state.get("state_version") or 0), allow_terminal_recovery=True)
    store.update_task_status(task["task_id"], "failed")

    class FailIfResearchReloaded:
        def __call__(self, *_args, **_kwargs):
            raise AssertionError("research bundle should be reused from task state")

    class NeverUsedImageProvider:
        calls = 0

        def __init__(self, *args, **kwargs):
            type(self).calls += 1

    def fake_generate_article(*args, **kwargs):
        raise ProviderError(
            "MODEL_NOT_FOUND",
            "provider model is not available",
            details={
                "model": "new-model",
                "final_url": "https://example.com/v1/chat/completions",
                "http_status": 503,
            },
        )

    monkeypatch.setattr(single_task, "load_research_bundle", FailIfResearchReloaded())
    monkeypatch.setattr(single_task, "generate_article", fake_generate_article)
    monkeypatch.setattr(single_task, "OpenAIImageProvider", NeverUsedImageProvider)

    result = run_single_task(
        task,
        _profiles(text_model="new-model")[0],
        _profiles()[1],
        settings={"network": {}},
        store=store,
        retry_step="retry-article",
    )

    saved = load_generation_task(task["task_id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "MODEL_NOT_FOUND"
    assert "当前文本模型不可用" in result["safe_error_message"]
    assert result["next_actions"] == ["test_text_model", "retry_article", "open_model_settings"]
    assert result["model_info"]["text"]["model"] == "new-model"
    assert result["image_generation_calls"] == 0
    assert NeverUsedImageProvider.calls == 0
    assert saved["research_bundle"]["research_status"] == "sufficient"
    assert saved["research_bundle"]["sources"] == original_bundle["sources"]
    assert saved["research_bundle"]["source_list"] == original_bundle["source_list"]


def test_hf2_2_source_guards_are_present():
    inline_source = (ROOT / "generation" / "inline_images.py").read_text(encoding="utf-8")
    selected_source = (ROOT / "generation" / "selected_images.py").read_text(encoding="utf-8")
    single_task_source = (ROOT / "generation" / "single_task.py").read_text(encoding="utf-8")
    ui_source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")

    assert "plan_inline_image_assets(article, style, 2, 4)" not in inline_source
    assert "exact_count=inline_count" in selected_source
    assert "requested_image_plan[\"max_calls\"] = calculate_image_budget(1, requested_image_mode)" in single_task_source
    assert "set_approved_image_budget(state, approved_calls)" in single_task_source
    assert "pending_image_confirmation" in single_task_source
    assert "\u91cd\u8bd5\u5931\u8d25\u56fe\u7247" in ui_source
    assert "\u91cd\u65b0\u751f\u6210\u5168\u90e8\u6b63\u6587\u56fe\u7247" in ui_source
    assert "rc132_image_test_inflight" in ui_source
    assert "TEXT_MODEL_NOT_VERIFIED" in ui_source
