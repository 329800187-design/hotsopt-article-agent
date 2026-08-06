from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation.image_budget import calculate_image_budget, image_plan_for, normalize_image_plan, recommended_word_count
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore
from modules.models import HotTopic

def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _topic(title: str, source: str, rank: int) -> HotTopic:
    return HotTopic(
        id=f"{source}-{rank}",
        title=title,
        hot_value=str(rank * 100),
        rank=rank,
        category="综合热点",
        summary="测试热点",
        source=source,
        source_name=source,
        source_url=f"https://example.com/{source}/{rank}",
        captured_at="2026-07-25T12:00:00+00:00",
        raw_data={},
    )


class _Provider:
    def __init__(self, provider_name: str, display_name: str, titles: list[str]) -> None:
        self.provider_name = provider_name
        self.display_name = display_name
        self.last_success_at = "2026-07-25T12:00:00+00:00"
        self.last_error = None
        self._titles = titles

    def fetch_trends(self) -> list[HotTopic]:
        return [_topic(title, self.display_name, index) for index, title in enumerate(self._titles, start=1)]


def test_image_budget_unique_rule_pass():
    assert normalize_image_plan("rich") == "standard"
    assert calculate_image_budget(1, "none") == 0
    assert calculate_image_budget(1, "economy") == 1
    assert calculate_image_budget(1, "standard") == 2
    assert calculate_image_budget(2, "economy") == 2
    assert calculate_image_budget(2, "standard") == 4
    assert calculate_image_budget(5, "economy") == 5
    assert calculate_image_budget(5, "standard") == 10
    assert image_plan_for(1600, "standard")["inline_count"] == 1


def test_word_count_floor_and_choices_pass():
    assert recommended_word_count(800) == 1200
    assert recommended_word_count(1200) == 1200
    assert recommended_word_count(1800) == 1600
    ui = read("ui/rc1_app.py")
    api = read("api.py")
    assert 'st.selectbox("目标字数", [1200, 1500, 1600], index=0)' in ui
    assert 'word_count: Literal[1200, 1500, 1600] = 1200' in api
    assert '[20, 50, 100, "全部"]' in ui


def test_single_task_defers_images_until_manual_confirmation_pass():
    source = read("generation/single_task.py")
    assert 'auto_image_requested = bool(options.get("image_generation_requested"))' in source
    assert 'execution_image_mode = requested_image_mode if auto_image_requested else "none"' in source
    assert 'state["pending_image_confirmation"] = bool(requested_image_plan.get("max_calls")) and not auto_image_requested' in source


def test_manual_image_generation_limits_inline_to_one_pass():
    api = read("api.py")
    selected_images = read("generation/selected_images.py")
    assert "inline_count: int = Field(default=0, ge=0, le=4)" in api
    assert 'inline_count = max(0, min(4, int(inline_count)))' in selected_images


def test_hotspot_refresh_merges_multiple_sources_and_dedupes_pass(tmp_path):
    provider_a = _Provider("toutiao_official", "今日头条官方热榜", [f"A{i}" for i in range(1, 121)])
    provider_b = _Provider("newsnow_toutiao", "NewsNow 今日头条备用源", [f"A{i}" for i in range(60, 161)])
    store = SQLiteStore(tmp_path / "hotspot.db")
    service = HotTrendService(settings={"network": {}, "hot_cache_ttl_seconds": 21600}, store=store, providers=[provider_a, provider_b])
    result = service.refresh()
    assert result["status"] == "online"
    assert result["hotlist_evidence"]["provider_count"] == 2
    assert len(result["topics"]) == 160
    assert len({topic.title for topic in result["topics"]}) == 160
