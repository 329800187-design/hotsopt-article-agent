from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from export.docx_exporter import export_article
from generation.image_prompt_generator import plan_inline_image_assets
from generation.workflow import confirm_images, confirm_final_draft, prepare_fusion
from modules import license_service
from modules.config_store import DEFAULT_SETTINGS
from modules.license_schema import _parse_time as parse_license_time


def _png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color).save(path)


def test_license_times_are_normalized_to_utc_and_not_before_has_only_five_minute_skew() -> None:
    parsed = parse_license_time("2026-08-06T08:00:00+08:00", "not_before")
    assert parsed.tzinfo == timezone.utc
    assert parsed.hour == 0
    assert license_service.NOT_BEFORE_CLOCK_SKEW == timedelta(minutes=5)


def test_license_expiry_does_not_receive_not_before_grace() -> None:
    assert license_service.NOT_BEFORE_CLOCK_SKEW != timedelta(hours=24)


def test_license_candidates_include_user_data_license_and_skip_symlink_escape(tmp_path: Path, monkeypatch) -> None:
    import modules.app_paths as app_paths
    from ui import rc1_app

    install = tmp_path / "install"
    user = tmp_path / "user"
    outside = tmp_path / "outside.license"
    install.mkdir()
    (user / "license").mkdir(parents=True)
    (user / "license" / "active.license").write_text("{}", encoding="utf-8")
    outside.write_text("{}", encoding="utf-8")
    (install / "escaped.license").symlink_to(outside)
    monkeypatch.setattr(app_paths, "data_root", lambda: user)
    monkeypatch.setattr(app_paths, "license_root", lambda: user / "license")
    candidates = rc1_app._license_candidates(install)
    assert user / "license" / "active.license" in candidates
    assert outside.resolve() not in candidates


def test_three_persisted_images_are_all_written_to_docx_even_when_sections_are_short(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    _png(task_root / "images" / "cover.png", (220, 50, 50))
    _png(task_root / "images" / "section-1.png", (50, 220, 50))
    _png(task_root / "images" / "section-2.png", (50, 50, 220))
    article = {
        "title": "三图导出",
        "lead": "导语内容",
        "body_char_count": 20,
        "sections": [{"heading": "第一段", "body": "正文内容足够导出。"}],
        "images": [
            {"role": "cover", "status": "completed", "path": "images/cover.png"},
            {"role": "inline", "status": "completed", "image_id": "section-1", "paragraph_ref": "section-1", "path": "images/section-1.png"},
            {"role": "inline", "status": "completed", "image_id": "section-2", "paragraph_ref": "section-2", "path": "images/section-2.png"},
        ],
    }
    output = export_article(article, tmp_path / "three.docx", task_root)
    with zipfile.ZipFile(output) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert len(media) == 3


def test_planned_images_have_stable_image_and_slot_ids() -> None:
    plans = plan_inline_image_assets({"title": "测试", "sections": [{"heading": "一"}]}, "editorial", exact_count=3)
    assert [item["image_id"] for item in plans] == ["section-1", "section-2", "section-3"]
    assert [item["slot_id"] for item in plans] == ["section-1", "section-2", "section-3"]


def test_first_run_defaults_are_not_materialized_in_user_data(tmp_path, monkeypatch) -> None:
    import modules.config_store as config_store

    settings_path = tmp_path / "config" / "settings.json"
    monkeypatch.setattr(config_store, "CONFIG_DIR", settings_path.parent)
    monkeypatch.setattr(config_store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(config_store, "ensure_user_data_dirs", lambda: settings_path.parent.mkdir(parents=True, exist_ok=True))
    loaded = config_store.load_settings()
    assert loaded["first_run_configuration_required"] is True
    assert not settings_path.exists()
    assert DEFAULT_SETTINGS["text_profile"]["model"] == "gpt-4o-mini"


def test_corrupt_settings_are_backed_up_instead_of_silently_reset(tmp_path, monkeypatch) -> None:
    import modules.config_store as config_store

    settings_path = tmp_path / "config" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config_store, "CONFIG_DIR", settings_path.parent)
    monkeypatch.setattr(config_store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(config_store, "ensure_user_data_dirs", lambda: None)
    loaded = config_store.load_settings()
    assert loaded["configuration_recovery_required"] is True
    assert not settings_path.exists()
    assert list(settings_path.parent.glob("settings.json.corrupt-*"))
