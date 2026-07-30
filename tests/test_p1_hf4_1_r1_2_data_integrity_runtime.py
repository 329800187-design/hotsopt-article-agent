from __future__ import annotations

import pytest

from modules.database import SQLiteStore
from modules.models import HotTopic
from scripts.package_rc1 import _validate_required_runtime_entries


def test_hot_topic_from_dict_requires_id() -> None:
    with pytest.raises(ValueError, match="TOPIC_SNAPSHOT_MISSING_ID"):
        HotTopic.from_dict({"title": "缺少 id 的旧话题快照"})


def test_create_batch_rejects_topic_snapshot_without_id(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "integrity.sqlite")
    store.init_schema()

    with pytest.raises(ValueError, match="TOPIC_SNAPSHOT_MISSING_ID"):
        store.create_batch(
            "坏数据批次",
            "multi_topic",
            [{"title": "缺少 id 的旧话题快照", "source": "test"}],
            {"article_type": "热点资讯"},
        )


def test_runtime_package_requires_core_dlls() -> None:
    with pytest.raises(RuntimeError, match="RUNTIME_PACKAGE_INCOMPLETE"):
        _validate_required_runtime_entries({})
