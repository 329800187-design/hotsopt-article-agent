from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

import modules.generation_store as generation_store
import generation.inline_images as inline_images
from api import app
from generation.image_prompt_generator import plan_inline_image_assets
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from providers.text_provider import ProviderError


def _article(section_count: int = 3) -> dict:
    return {
        "title": "城市生活观察",
        "summary": "一段公开新闻摘要",
        "content_markdown": "正文内容不会被正文图片重试修改。",
        "sections": [
            {"heading": f"小标题 {index}", "body": f"第 {index} 段内容", "image_brief": f"场景 {index}"}
            for index in range(1, section_count + 1)
        ],
        "images": [{"role": "cover", "path": "images/cover.png", "status": "completed"}],
    }


def _write_png(path: Path, color: str = "blue") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (128, 96), color).save(path, format="PNG")


class FakeImageProvider:
    calls = 0
    failures: set[int] = set()
    last_response_type = "base64"

    def __init__(self, profile, network_settings=None):
        self.profile = profile

    def generate(self, prompt: str, output_path: Path) -> Path:
        type(self).calls += 1
        if type(self).calls in type(self).failures:
            raise ProviderError("INVALID_RESPONSE", "fake inline image failure")
        _write_png(output_path, "blue" if "1" in prompt else "green")
        return output_path


@pytest.fixture
def inline_task(tmp_path, monkeypatch):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    task_id = "inline-test-task"
    state = {
        "task_id": task_id,
        "status": "completed",
        "stage": "completed",
        "state_version": 0,
        "generation_options": {"image_style": "动漫新闻插画"},
        "article": _article(),
        "cover": {"status": "completed", "path": "images/cover.png"},
        "inline_images": [],
        "inline_image_summary": {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "pending"},
        "paths": {},
        "cancellation_requested": False,
    }
    root = generation_task_dir(task_id)
    root.mkdir(parents=True)
    _write_png(root / "images" / "cover.png", "red")
    save_generation_task(state)
    FakeImageProvider.calls = 0
    FakeImageProvider.failures = set()
    monkeypatch.setattr(inline_images, "OpenAIImageProvider", FakeImageProvider)
    return task_id, root


def test_planner_without_sections_has_two_stable_assets():
    assets = plan_inline_image_assets({"title": "无小标题", "summary": "摘要"}, "动漫")
    assert [asset["image_id"] for asset in assets] == ["section-1", "section-2"]
    assert all(asset["paragraph_ref"] == asset["image_id"] for asset in assets)
    assert all(asset["insert_after_paragraph"] >= 1 for asset in assets)


def test_planner_caps_at_four_and_prompts_are_distinct():
    assets = plan_inline_image_assets(_article(8), "动漫")
    assert len(assets) == 4
    assert len({asset["prompt"] for asset in assets}) == 4
    assert [asset["order"] for asset in assets] == [1, 2, 3, 4]
    assert all(asset["section_title"] for asset in assets)


def test_three_inline_images_success_and_persist(inline_task):
    task_id, root = inline_task
    result = inline_images.run_inline_images(task_id, {"model": "fake"}, persist_article=True)
    assert result["inline_image_summary"]["completed"] == 3
    assert result["inline_image_summary"]["failed"] == 0
    assert {item["image_id"] for item in result["inline_images"]} == {"section-1", "section-2", "section-3"}
    assert all((root / "images" / f"section-{index}.png").exists() for index in range(1, 4))
    saved_article = load_generation_task(task_id)["article"]
    assert len(saved_article["images"]) == 4
    assert (root / "images" / "assets.json").exists()


def test_middle_failure_does_not_fail_article(inline_task):
    task_id, root = inline_task
    FakeImageProvider.failures = {2}
    result = inline_images.run_inline_images(task_id, {"model": "fake"}, persist_article=True)
    statuses = {item["image_id"]: item["status"] for item in result["inline_images"]}
    assert statuses == {"section-1": "completed", "section-2": "failed", "section-3": "completed"}
    assert result["article"]["title"] == "城市生活观察"
    assert (root / "article.json").exists()
    assert list((root / ".attempts").glob("*")) == []


def test_retry_only_target_preserves_article_cover_and_other_hashes(inline_task):
    task_id, root = inline_task
    FakeImageProvider.failures = {2}
    first = inline_images.run_inline_images(task_id, {"model": "fake"}, persist_article=True)
    article_hash = hashlib.sha256((root / "article.json").read_bytes()).hexdigest()
    cover_hash = hashlib.sha256((root / "images" / "cover.png").read_bytes()).hexdigest()
    section_one_hash = hashlib.sha256((root / "images" / "section-1.png").read_bytes()).hexdigest()
    FakeImageProvider.failures = set()
    second = inline_images.run_inline_images(task_id, {"model": "fake"}, target_ids=["section-2"])
    assert next(item for item in second["inline_images"] if item["image_id"] == "section-2")["status"] == "completed"
    updated_article = __import__("json").loads((root / "article.json").read_text(encoding="utf-8"))
    assert updated_article["content_markdown"] == "正文内容不会被正文图片重试修改。"
    assert next(item for item in updated_article["images"] if item.get("image_id") == "section-2")["status"] == "completed"
    assert hashlib.sha256((root / "article.json").read_bytes()).hexdigest() != article_hash
    assert hashlib.sha256((root / "images" / "cover.png").read_bytes()).hexdigest() == cover_hash
    assert hashlib.sha256((root / "images" / "section-1.png").read_bytes()).hexdigest() == section_one_hash
    assert FakeImageProvider.calls == 4


def test_failed_retry_preserves_previous_formal_image(inline_task):
    task_id, root = inline_task
    inline_images.run_inline_images(task_id, {"model": "fake"}, target_ids=["section-1"])
    previous = hashlib.sha256((root / "images" / "section-1.png").read_bytes()).hexdigest()
    FakeImageProvider.failures = {2}
    result = inline_images.run_inline_images(task_id, {"model": "fake"}, target_ids=["section-1"])
    item = next(item for item in result["inline_images"] if item["image_id"] == "section-1")
    assert item["status"] == "failed"
    assert item["fallback_available"] is True
    assert hashlib.sha256((root / "images" / "section-1.png").read_bytes()).hexdigest() == previous


def test_invalid_image_id_cannot_escape_task_directory(inline_task):
    task_id, _ = inline_task
    with pytest.raises(ProviderError) as error:
        inline_images.run_inline_images(task_id, {"model": "fake"}, target_ids=["../../secret"])
    assert error.value.code == "INLINE_IMAGE_NOT_FOUND"


def test_retry_failed_with_no_failures_is_idempotent(inline_task):
    task_id, _ = inline_task
    inline_images.run_inline_images(task_id, {"model": "fake"})
    calls = FakeImageProvider.calls
    second = inline_images.run_inline_images(task_id, {"model": "fake"}, target_ids=[])
    assert second["inline_image_summary"]["failed"] == 0
    assert FakeImageProvider.calls == calls


def test_api_routes_exist():
    routes = {route.path for route in app.routes}
    assert "/api/tasks/{task_id}/inline-images" in routes
    assert "/api/tasks/{task_id}/inline-images/retry-failed" in routes
    assert "/api/tasks/{task_id}/inline-images/regenerate" in routes
    assert "/api/tasks/{task_id}/inline-images/{image_id}/retry" in routes


def test_normal_page_uses_chinese_inline_actions():
    source = Path(__file__).parents[1] / "ui" / "rc1_app.py"
    text = source.read_text(encoding="utf-8")
    assert "重新生成这张" in text
    assert "重试失败图片" in text
    assert "重新生成全部正文图片" in text
    assert "st.json(result)" not in text[text.index("def _content"):text.index("def _settings_page")]
