from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from modules.models import HotTopic, utc_now
from modules.app_paths import PROJECT_ROOT, database_path
from modules.security import redact_sensitive_text, sanitize_sensitive_data
from modules.hot_score import normalize_hot_score
from hot_sources.classifier import CATEGORIES, normalize_category
from generation.angle_planner import plan_angles


ROOT = PROJECT_ROOT
DB_PATH = database_path()
LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class SQLiteStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _write(self, action: Callable[[sqlite3.Connection], T]) -> T:
        last_error: Exception | None = None
        for attempt in range(3):
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = action(connection)
                connection.commit()
                return result
            except sqlite3.OperationalError as exc:
                connection.rollback()
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                LOGGER.warning("SQLite busy, retry %s/3", attempt + 1)
                time.sleep(0.1 * (attempt + 1))
            finally:
                connection.close()
        raise RuntimeError(f"SQLite 写入重试 3 次仍失败：{last_error}") from last_error

    def init_schema(self) -> None:
        with self.connect() as connection:
            # Set the journal mode once during initialization. Repeating this on
            # every short-lived read connection serializes the whole local UI.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hot_topics (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    hot_value TEXT,
                    hot_score REAL,
                    rank INTEGER,
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    captured_at TEXT NOT NULL,
                    provider_status TEXT NOT NULL,
                    is_cached INTEGER NOT NULL DEFAULT 0,
                    raw_data TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hot_topic_observations (
                    observation_id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    hot_value TEXT,
                    hot_score REAL,
                    rank INTEGER,
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    captured_at TEXT NOT NULL,
                    provider_status TEXT NOT NULL,
                    is_cached INTEGER NOT NULL DEFAULT 0,
                    raw_data TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(topic_id) REFERENCES hot_topics(id)
                );
                CREATE INDEX IF NOT EXISTS idx_topic_captured_at ON hot_topics(captured_at);
                CREATE INDEX IF NOT EXISTS idx_observation_topic ON hot_topic_observations(topic_id, captured_at);
                CREATE TABLE IF NOT EXISTS provider_status (
                    provider_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    last_success_at TEXT,
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    selected_topics TEXT NOT NULL,
                    article_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    generation_options TEXT NOT NULL DEFAULT '{}',
                    angle_id TEXT,
                    angle_name TEXT,
                    angle_plan TEXT NOT NULL DEFAULT '{}',
                    angle_position INTEGER,
                    similarity_status TEXT NOT NULL DEFAULT 'not_checked',
                    similarity_score REAL,
                    rewrite_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topic_basket (
                    basket_id INTEGER PRIMARY KEY CHECK (basket_id = 1),
                    topics TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_batches (
                    batch_id TEXT PRIMARY KEY,
                    batch_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_count INTEGER NOT NULL,
                    concurrency INTEGER NOT NULL DEFAULT 2,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    running_count INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    cancelled_count INTEGER NOT NULL DEFAULT 0,
                    partial_success_count INTEGER NOT NULL DEFAULT 0,
                    generation_options TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    quality_status TEXT NOT NULL DEFAULT 'not_applicable',
                    quality_started_at TEXT,
                    quality_completed_at TEXT,
                    quality_error TEXT NOT NULL DEFAULT '',
                    final_ready INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS generation_batch_items (
                    batch_item_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    topic_id TEXT NOT NULL,
                    topic_snapshot TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    angle_id TEXT,
                    angle_name TEXT,
                    angle_plan TEXT NOT NULL DEFAULT '{}',
                    angle_position INTEGER,
                    similarity_status TEXT NOT NULL DEFAULT 'not_checked',
                    similarity_score REAL,
                    rewrite_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(batch_id) REFERENCES generation_batches(batch_id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES generation_tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_generation_batch_items_batch ON generation_batch_items(batch_id, position);
                """
            )
            self._ensure_column(connection, "hot_topics", "hot_score", "REAL")
            self._ensure_column(connection, "hot_topic_observations", "hot_score", "REAL")
            self._ensure_column(connection, "generation_tasks", "generation_options", "TEXT NOT NULL DEFAULT '{}'")
            for column, definition in {
                "angle_id": "TEXT",
                "angle_name": "TEXT",
                "angle_plan": "TEXT NOT NULL DEFAULT '{}'",
                "angle_position": "INTEGER",
                "similarity_status": "TEXT NOT NULL DEFAULT 'not_checked'",
                "similarity_score": "REAL",
                "rewrite_count": "INTEGER NOT NULL DEFAULT 0",
                "article_revision": "INTEGER NOT NULL DEFAULT 0",
                "article_edit_status": "TEXT NOT NULL DEFAULT 'saved'",
                "article_content_sha": "TEXT NOT NULL DEFAULT ''",
            }.items():
                self._ensure_column(connection, "generation_tasks", column, definition)
            for column, definition in {
                "angle_id": "TEXT",
                "angle_name": "TEXT",
                "angle_plan": "TEXT NOT NULL DEFAULT '{}'",
                "angle_position": "INTEGER",
                "similarity_status": "TEXT NOT NULL DEFAULT 'not_checked'",
                "similarity_score": "REAL",
                "rewrite_count": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                self._ensure_column(connection, "generation_batch_items", column, definition)
            self._ensure_column(connection, "generation_batches", "concurrency", "INTEGER NOT NULL DEFAULT 2")
            self._ensure_column(connection, "generation_batches", "quality_status", "TEXT NOT NULL DEFAULT 'not_applicable'")
            self._ensure_column(connection, "generation_batches", "quality_started_at", "TEXT")
            self._ensure_column(connection, "generation_batches", "quality_completed_at", "TEXT")
            self._ensure_column(connection, "generation_batches", "quality_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "generation_batches", "final_ready", "INTEGER NOT NULL DEFAULT 0")
            connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', '1')")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def save_topics(self, topics: list[HotTopic], record_observation: bool = True) -> None:
        now = utc_now()
        def write(connection: sqlite3.Connection) -> None:
            for topic in topics:
                topic.category = normalize_category(topic.category, topic.title, topic.summary)
                topic.hot_score = normalize_hot_score(topic.hot_value)
                safe_topic = sanitize_sensitive_data(topic.to_dict())
                safe_raw_data = json.dumps(safe_topic.get("raw_data", {}), ensure_ascii=False)
                connection.execute(
                    """
                    INSERT INTO hot_topics(id,title,hot_value,hot_score,rank,category,summary,source,source_name,source_url,captured_at,provider_status,is_cached,raw_data,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,hot_value=excluded.hot_value,hot_score=excluded.hot_score,rank=excluded.rank,category=excluded.category,summary=excluded.summary,source=excluded.source,source_name=excluded.source_name,source_url=excluded.source_url,captured_at=excluded.captured_at,provider_status=excluded.provider_status,is_cached=excluded.is_cached,raw_data=excluded.raw_data,updated_at=excluded.updated_at
                    """,
                    (safe_topic["id"], safe_topic["title"], safe_topic.get("hot_value"), safe_topic.get("hot_score"), safe_topic.get("rank"), safe_topic["category"], safe_topic["summary"], safe_topic["source"], safe_topic["source_name"], safe_topic["source_url"], safe_topic["captured_at"], safe_topic["provider_status"], int(safe_topic["is_cached"]), safe_raw_data, safe_topic.get("created_at") or now, safe_topic.get("updated_at") or now),
                )
                if record_observation:
                    connection.execute(
                        "INSERT INTO hot_topic_observations(observation_id,topic_id,title,hot_value,hot_score,rank,category,summary,source,source_name,source_url,captured_at,provider_status,is_cached,raw_data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, safe_topic["id"], safe_topic["title"], safe_topic.get("hot_value"), safe_topic.get("hot_score"), safe_topic.get("rank"), safe_topic["category"], safe_topic["summary"], safe_topic["source"], safe_topic["source_name"], safe_topic["source_url"], safe_topic["captured_at"], safe_topic["provider_status"], int(safe_topic["is_cached"]), safe_raw_data),
                    )
        self._write(write)

    def list_topics(self, keyword: str = "", category: str = "全部", source: str = "全部", sort: str = "captured_at_desc", captured_after: str | None = None, limit: int = 100) -> list[HotTopic]:
        clauses = ["1=1"]
        params: list[Any] = []
        if keyword.strip():
            clauses.append("(title LIKE ? OR summary LIKE ?)")
            term = f"%{keyword.strip()}%"
            params.extend([term, term])
        if category and category != "全部":
            clauses.append("category = ?")
            params.append(category)
        if source and source != "全部":
            clauses.append("source_name = ?")
            params.append(source)
        if captured_after:
            clauses.append("captured_at >= ?")
            params.append(captured_after)
        order_by = {"hot_desc": "hot_score DESC, rank ASC", "rank_asc": "rank ASC", "captured_at_desc": "captured_at DESC"}.get(sort, "captured_at DESC")
        params.append(max(1, min(limit, 500)))
        query = f"SELECT * FROM hot_topics WHERE {' AND '.join(clauses)} ORDER BY {order_by} LIMIT ?"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [HotTopic.from_dict(dict(row) | {"raw_data": json.loads(row["raw_data"] or "{}")}) for row in rows]

    def update_topic_category(self, topic_id: str, category: str) -> HotTopic | None:
        if category not in CATEGORIES:
            raise ValueError("不支持的热点分类")
        self._write(lambda connection: connection.execute("UPDATE hot_topics SET category=?, updated_at=? WHERE id=?", (category, utc_now(), topic_id)))
        topics = self.list_topics(limit=500)
        return next((topic for topic in topics if topic.id == topic_id), None)

    def save_provider_status(self, provider_name: str, display_name: str, status: str, last_success_at: str | None = None, last_error: str | None = None) -> None:
        safe_provider_name = redact_sensitive_text(provider_name)
        safe_display_name = redact_sensitive_text(display_name)
        safe_status = redact_sensitive_text(status)
        safe_last_success_at = redact_sensitive_text(last_success_at) if last_success_at is not None else None
        safe_last_error = redact_sensitive_text(last_error) if last_error is not None else None
        self._write(lambda connection: connection.execute(
                "INSERT INTO provider_status(provider_name,display_name,last_success_at,last_error,status,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(provider_name) DO UPDATE SET display_name=excluded.display_name,last_success_at=COALESCE(excluded.last_success_at,provider_status.last_success_at),last_error=excluded.last_error,status=excluded.status,updated_at=excluded.updated_at",
                (safe_provider_name, safe_display_name, safe_last_success_at, safe_last_error, safe_status, utc_now()),
            ))

    def list_provider_status(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM provider_status ORDER BY provider_name").fetchall()
        return [sanitize_sensitive_data(dict(row)) for row in rows]

    def create_task(self, task_name: str, mode: str, selected_topics: list[dict[str, Any]], article_count: int, status: str = "queued", generation_options: dict[str, Any] | None = None) -> dict[str, Any]:
        selected_topics = sanitize_sensitive_data(selected_topics)
        generation_options = sanitize_sensitive_data(generation_options or {})
        safe_task_name = redact_sensitive_text(task_name.strip())
        if mode not in {"multi_topic", "single_topic_multi_angle"}:
            raise ValueError("不支持的任务模式")
        if not 1 <= len(selected_topics) <= 5:
            raise ValueError("一次任务必须选择 1～5 个话题")
        if not 1 <= article_count <= 5:
            raise ValueError("文章数量必须在 1～5 之间")
        if mode == "single_topic_multi_angle" and len(selected_topics) != 1:
            raise ValueError("单热点五角度模式只能选择 1 个话题")
        for topic in selected_topics:
            if not isinstance(topic, dict) or not topic.get("id"):
                raise ValueError("TOPIC_SNAPSHOT_MISSING_ID: topic snapshot missing id")
        now = utc_now()
        task = {"task_id": uuid.uuid4().hex[:12], "task_name": safe_task_name or "未命名热点任务", "mode": mode, "selected_topics": selected_topics, "article_count": article_count, "status": redact_sensitive_text(status), "source_name": redact_sensitive_text("、".join(sorted({str(item.get('source_name') or item.get('source') or '未知来源') for item in selected_topics}))), "generation_options": generation_options, "created_at": now, "updated_at": now}
        self._write(lambda connection: connection.execute("INSERT INTO generation_tasks(task_id,task_name,mode,selected_topics,article_count,status,source_name,generation_options,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (task["task_id"], task["task_name"], mode, json.dumps(selected_topics, ensure_ascii=False), article_count, task["status"], task["source_name"], json.dumps(generation_options, ensure_ascii=False), now, now)))
        return task

    def create_batch(self, batch_name: str, mode: str, selected_topics: list[dict[str, Any]], generation_options: dict[str, Any] | None = None, concurrency: int = 2, angles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        safe_topics = sanitize_sensitive_data(selected_topics)
        safe_options = sanitize_sensitive_data(generation_options or {})
        safe_angles = sanitize_sensitive_data(angles or [])
        safe_name = redact_sensitive_text(batch_name.strip()) or "未命名批次"
        if mode not in {"multi_topic", "single_topic_multi_angle"}:
            raise ValueError("批次模式不受支持")
        if not 1 <= int(concurrency) <= 5:
            raise ValueError("批次并发数必须在 1 到 3 之间")
        if mode == "multi_topic":
            if not 1 <= len(safe_topics) <= 5:
                raise ValueError("批次必须包含 1 到 5 个话题")
            topic_keys = [str(item.get("id") or item.get("title") or "") for item in safe_topics]
            if any(not key for key in topic_keys) or len(set(topic_keys)) != len(topic_keys):
                raise ValueError("同一批次不允许重复话题")
            jobs = [(topic, None) for topic in safe_topics]
        else:
            if len(safe_topics) != 1:
                raise ValueError("单热点多角度模式只能选择 1 个话题")
            requested_count = len(safe_angles) or int(safe_options.get("article_count") or 0)
            if not 1 <= requested_count <= 5:
                raise ValueError("文章数量必须在 1 到 5 之间")
            if not safe_angles:
                safe_angles = sanitize_sensitive_data(plan_angles(requested_count))
            if len(safe_angles) != requested_count:
                raise ValueError("角度数量必须与文章数量一致")
            angle_ids = [str(item.get("angle_id") or item.get("id") or "") for item in safe_angles]
            if any(not angle_id for angle_id in angle_ids) or len(set(angle_ids)) != len(angle_ids):
                raise ValueError("角度不能重复")
            jobs = [(safe_topics[0], angle) for angle in safe_angles]
        now = utc_now()
        batch_id = uuid.uuid4().hex[:12]
        created_tasks: list[dict[str, Any]] = []

        def write(connection: sqlite3.Connection) -> None:
            quality_status = "pending" if mode == "single_topic_multi_angle" and len(jobs) >= 2 else "not_applicable"
            connection.execute(
                "INSERT INTO generation_batches(batch_id,batch_name,mode,status,total_count,concurrency,queued_count,generation_options,created_at,updated_at,state_version,quality_status,final_ready) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, safe_name, mode, "queued", len(jobs), int(concurrency), len(jobs), json.dumps(safe_options, ensure_ascii=False), now, now, 0, quality_status, 0),
            )
            for position, (topic, angle) in enumerate(jobs, start=1):
                task_id = uuid.uuid4().hex[:12]
                topic_snapshot = sanitize_sensitive_data(topic)
                if not isinstance(topic_snapshot, dict) or not topic_snapshot.get("id"):
                    raise ValueError("TOPIC_SNAPSHOT_MISSING_ID: topic snapshot missing id")
                title = redact_sensitive_text(str(topic_snapshot.get("title") or f"话题 {position}"))
                angle_id = redact_sensitive_text(str((angle or {}).get("angle_id") or "")) or None
                angle_name = redact_sensitive_text(str((angle or {}).get("angle_name") or (angle or {}).get("name") or "")) or None
                angle_plan = sanitize_sensitive_data(angle or {})
                child_options = dict(safe_options)
                if angle:
                    child_options["angle_plan"] = angle_plan
                task_name = redact_sensitive_text(" - ".join(value for value in (safe_name, title, angle_name) if value))[:100]
                source_name = redact_sensitive_text(str(topic_snapshot.get("source_name") or topic_snapshot.get("source") or "未知来源"))
                task = {
                    "task_id": task_id,
                    "task_name": task_name,
                    "mode": mode,
                    "selected_topics": [topic_snapshot],
                    "article_count": 1,
                    "status": "queued",
                    "source_name": source_name,
                    "generation_options": child_options,
                    "angle_id": angle_id,
                    "angle_name": angle_name,
                    "angle_plan": angle_plan,
                    "angle_position": position if angle else None,
                    "similarity_status": "not_checked",
                    "similarity_score": None,
                    "rewrite_count": 0,
                    "created_at": now,
                    "updated_at": now,
                }
                connection.execute(
                    "INSERT INTO generation_tasks(task_id,task_name,mode,selected_topics,article_count,status,source_name,generation_options,angle_id,angle_name,angle_plan,angle_position,similarity_status,similarity_score,rewrite_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, task_name, mode, json.dumps([topic_snapshot], ensure_ascii=False), 1, "queued", source_name, json.dumps(child_options, ensure_ascii=False), angle_id, angle_name, json.dumps(angle_plan, ensure_ascii=False), position if angle else None, "not_checked", None, 0, now, now),
                )
                connection.execute(
                    "INSERT INTO generation_batch_items(batch_item_id,batch_id,task_id,topic_id,topic_snapshot,position,status,angle_id,angle_name,angle_plan,angle_position,similarity_status,similarity_score,rewrite_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], batch_id, task_id, redact_sensitive_text(str(topic_snapshot.get("id") or "")), json.dumps(topic_snapshot, ensure_ascii=False), position, "queued", angle_id, angle_name, json.dumps(angle_plan, ensure_ascii=False), position if angle else None, "not_checked", None, 0, now, now),
                )
                created_tasks.append(task)

        self._write(write)
        return self.get_batch(batch_id) or {"batch_id": batch_id, "batch_name": safe_name, "mode": mode, "status": "queued", "tasks": created_tasks}

    @staticmethod
    def _summarize_batch_status(statuses: list[str]) -> str:
        if not statuses:
            return "failed"
        status_set = set(statuses)
        active = {"queued", "running", "retry_waiting"}
        exportable = {"completed", "completed_with_warning", "warning", "partial_success", "review_required"}
        if status_set & active:
            return "running"
        if status_set and status_set.issubset(exportable):
            return "completed"
        if status_set == {"cancelled"}:
            return "cancelled"
        if status_set == {"failed"}:
            return "failed"
        return "partial_success"

    def update_batch_quality(self, batch_id: str, quality_status: str, error: str | None = None) -> None:
        safe_status = redact_sensitive_text(quality_status)
        safe_error = redact_sensitive_text(error or "")
        now = utc_now()

        def write(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT quality_started_at, state_version FROM generation_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if not row:
                return
            started_at = row[0] or (now if safe_status in {"checking", "rewriting", "passed", "review_required", "failed"} else None)
            completed_at = now if safe_status in {"passed", "review_required", "failed"} else None
            connection.execute(
                "UPDATE generation_batches SET quality_status=?,quality_started_at=?,quality_completed_at=?,quality_error=?,final_ready=0,completed_at=NULL,updated_at=?,state_version=? WHERE batch_id=?",
                (safe_status, started_at, completed_at, safe_error, now, int(row[1] or 0) + 1, batch_id),
            )

        self._write(write)

    def refresh_batch(self, batch_id: str) -> dict[str, Any] | None:
        def write(connection: sqlite3.Connection) -> None:
            rows = connection.execute(
                "SELECT i.batch_item_id, t.status FROM generation_batch_items i JOIN generation_tasks t ON t.task_id=i.task_id WHERE i.batch_id=? ORDER BY i.position",
                (batch_id,),
            ).fetchall()
            if not rows:
                return
            statuses = [redact_sensitive_text(str(row[1] or "queued")) for row in rows]
            counts = {key: statuses.count(key) for key in ("queued", "running", "completed", "failed", "cancelled", "partial_success")}
            exportable_statuses = {"completed", "completed_with_warning", "warning", "partial_success", "review_required"}
            status = self._summarize_batch_status(statuses)
            current = connection.execute("SELECT status,started_at,completed_at,state_version,mode,quality_status,total_count FROM generation_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if not current:
                return
            is_multi_angle = current[4] == "single_topic_multi_angle"
            requires_quality_check = is_multi_angle and int(current[6] or 0) >= 2
            quality_status = str(current[5] or ("pending" if is_multi_angle else "not_applicable"))
            if requires_quality_check and quality_status == "not_applicable":
                quality_status = "pending"
            final_ready = 0
            if status == "completed":
                if requires_quality_check and quality_status in {"pending", "checking", "rewriting"}:
                    status = "running"
                    completed_at = None
                elif quality_status == "failed":
                    status = "failed"
                    completed_at = None
                else:
                    final_ready = int(any(item in exportable_statuses for item in statuses) and all(item in exportable_statuses or item == "cancelled" for item in statuses))
                    if requires_quality_check and quality_status not in {"passed", "review_required"}:
                        final_ready = 0
            started_at = current[1] or (utc_now() if status in {"running", "completed", "failed", "cancelled", "partial_success"} else None)
            completed_at = current[2] or (utc_now() if status in {"completed", "failed", "cancelled", "partial_success"} else None)
            if status == "running":
                completed_at = None
                final_ready = 0
            elif status != "completed":
                final_ready = 0
            connection.execute(
                "UPDATE generation_batch_items SET status=(SELECT status FROM generation_tasks WHERE task_id=generation_batch_items.task_id),updated_at=? WHERE batch_id=?",
                (utc_now(), batch_id),
            )
            connection.execute(
                "UPDATE generation_batches SET status=?,queued_count=?,running_count=?,completed_count=?,failed_count=?,cancelled_count=?,partial_success_count=?,started_at=?,completed_at=?,final_ready=?,updated_at=?,state_version=? WHERE batch_id=?",
                (status, counts["queued"], counts["running"], counts["completed"], counts["failed"], counts["cancelled"], counts["partial_success"], started_at, completed_at, int(final_ready), utc_now(), int(current[3] or 0) + 1, batch_id),
            )
        self._write(write)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM generation_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            return None
        batch = sanitize_sensitive_data(dict(row))
        batch["generation_options"] = json.loads(batch.get("generation_options") or "{}")
        batch["items"] = self.list_batch_items(batch_id)
        batch["tasks"] = [item["task"] for item in batch["items"]]
        return batch

    def list_batches(self, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT batch_id FROM generation_batches ORDER BY created_at DESC"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([max(1, min(int(limit), 100)), max(0, int(offset))])
        with self.connect() as connection:
            ids = [str(row[0]) for row in connection.execute(sql, params).fetchall()]
        values: list[dict[str, Any]] = []
        for batch_id in ids:
            batch = self.get_batch(batch_id)
            if batch:
                values.append(batch)
        return values

    def list_batch_summaries(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """Return the bounded content-list snapshot without N+1 detail queries."""
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        with self.connect() as connection:
            batch_rows = connection.execute(
                "SELECT * FROM generation_batches ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (bounded_limit, bounded_offset),
            ).fetchall()
            if not batch_rows:
                return []
            batch_ids = [str(row["batch_id"]) for row in batch_rows]
            placeholders = ",".join("?" for _ in batch_ids)
            item_rows = connection.execute(
                f"SELECT i.batch_id,i.batch_item_id,i.position,i.topic_snapshot,"
                f"t.task_id,t.task_name,t.mode,t.selected_topics,t.article_count,t.status "
                f"FROM generation_batch_items i JOIN generation_tasks t ON t.task_id=i.task_id "
                f"WHERE i.batch_id IN ({placeholders}) ORDER BY i.batch_id,i.position",
                batch_ids,
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {batch_id: [] for batch_id in batch_ids}
        for row in item_rows:
            selected_topics = self._json_loads(row["selected_topics"], [])
            grouped[str(row["batch_id"])].append({
                "batch_item_id": row["batch_item_id"],
                "position": row["position"],
                "topic_snapshot": self._json_loads(row["topic_snapshot"], {}),
                "task": {
                    "task_id": row["task_id"],
                    "task_name": row["task_name"],
                    "mode": row["mode"],
                    "selected_topics": selected_topics,
                    "article_count": row["article_count"],
                    "status": row["status"],
                },
            })
        values: list[dict[str, Any]] = []
        for row in batch_rows:
            batch = sanitize_sensitive_data(dict(row))
            batch["generation_options"] = self._json_loads(batch.get("generation_options"), {})
            batch["items"] = grouped.get(str(batch["batch_id"]), [])
            batch["tasks"] = [item["task"] for item in batch["items"]]
            values.append(batch)
        return values

    def list_batch_items(self, batch_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT i.*,t.task_name,t.mode,t.selected_topics,t.article_count,t.status AS task_status,t.source_name,t.generation_options AS task_generation_options,t.angle_id AS task_angle_id,t.angle_name AS task_angle_name,t.angle_plan AS task_angle_plan,t.angle_position AS task_angle_position,t.similarity_status AS task_similarity_status,t.similarity_score AS task_similarity_score,t.rewrite_count AS task_rewrite_count,t.created_at AS task_created_at,t.updated_at AS task_updated_at FROM generation_batch_items i JOIN generation_tasks t ON t.task_id=i.task_id WHERE i.batch_id=? ORDER BY i.position",
                (batch_id,),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["topic_snapshot"] = self._json_loads(item.get("topic_snapshot"), {})
            task = {
                "task_id": item.pop("task_id"), "task_name": item.pop("task_name"), "mode": item.pop("mode"),
                "selected_topics": self._json_loads(item.pop("selected_topics", "[]"), []), "article_count": item.pop("article_count"),
                "status": item.pop("task_status"), "source_name": item.pop("source_name"),
                "generation_options": self._json_loads(item.pop("task_generation_options", "{}"), {}),
                "angle_id": item.pop("task_angle_id"), "angle_name": item.pop("task_angle_name"),
                "angle_plan": self._json_loads(item.pop("task_angle_plan", "{}"), {}), "angle_position": item.pop("task_angle_position"),
                "similarity_status": item.pop("task_similarity_status"), "similarity_score": item.pop("task_similarity_score"),
                "rewrite_count": item.pop("task_rewrite_count"),
                "created_at": item.pop("task_created_at"), "updated_at": item.pop("task_updated_at"),
            }
            values.append({"batch_item_id": item["batch_item_id"], "batch_id": item["batch_id"], "topic_id": item["topic_id"], "topic_snapshot": sanitize_sensitive_data(item["topic_snapshot"]), "position": item["position"], "status": item["status"], "angle_id": item["angle_id"], "angle_name": item["angle_name"], "angle_plan": self._json_loads(item.get("angle_plan"), {}), "angle_position": item["angle_position"], "similarity_status": item["similarity_status"], "similarity_score": item["similarity_score"], "rewrite_count": item["rewrite_count"], "created_at": item["created_at"], "updated_at": item["updated_at"], "task": sanitize_sensitive_data(task)})
        return values

    def update_task_quality(self, task_id: str, similarity_status: str, similarity_score: float | None, rewrite_count: int | None = None) -> None:
        safe_status = redact_sensitive_text(similarity_status)
        def write(connection: sqlite3.Connection) -> None:
            if rewrite_count is None:
                connection.execute("UPDATE generation_tasks SET similarity_status=?,similarity_score=?,updated_at=? WHERE task_id=?", (safe_status, similarity_score, utc_now(), task_id))
            else:
                connection.execute("UPDATE generation_tasks SET similarity_status=?,similarity_score=?,rewrite_count=?,updated_at=? WHERE task_id=?", (safe_status, similarity_score, int(rewrite_count), utc_now(), task_id))
        self._write(write)

    def update_task_generation_options(self, task_id: str, generation_options: dict[str, Any]) -> None:
        safe_options = sanitize_sensitive_data(generation_options)
        self._write(lambda connection: connection.execute("UPDATE generation_tasks SET generation_options=?,updated_at=? WHERE task_id=?", (json.dumps(safe_options, ensure_ascii=False), utc_now(), task_id)))

    def update_batch_generation_options(self, batch_id: str, generation_options: dict[str, Any]) -> None:
        safe_options = sanitize_sensitive_data(generation_options)
        self._write(lambda connection: connection.execute("UPDATE generation_batches SET generation_options=?,updated_at=? WHERE batch_id=?", (json.dumps(safe_options, ensure_ascii=False), utc_now(), batch_id)))

    def update_task_edit_metadata(self, task_id: str, revision: int, edit_status: str, content_sha: str) -> None:
        self._write(lambda connection: connection.execute(
            "UPDATE generation_tasks SET article_revision=?,article_edit_status=?,article_content_sha=?,updated_at=? WHERE task_id=?",
            (int(revision), redact_sensitive_text(edit_status), redact_sensitive_text(content_sha), utc_now(), task_id),
        ))

    def get_batch_item(self, batch_id: str, task_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_batch_items(batch_id) if item["task"]["task_id"] == task_id), None)

    def get_task_batch_ids(self, task_id: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT batch_id FROM generation_batch_items WHERE task_id=?", (task_id,)).fetchall()
        return [str(row[0]) for row in rows]

    def get_basket(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT topics FROM topic_basket WHERE basket_id=1").fetchone()
        return json.loads(row["topics"] or "[]") if row else []

    def set_basket(self, topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(topics) > 5:
            raise ValueError("选题篮最多只能有 5 个话题")
        topics = sanitize_sensitive_data(topics)
        now = utc_now()
        self._write(lambda connection: connection.execute("INSERT INTO topic_basket(basket_id,topics,updated_at) VALUES(1,?,?) ON CONFLICT(basket_id) DO UPDATE SET topics=excluded.topics,updated_at=excluded.updated_at", (json.dumps(topics, ensure_ascii=False), now)))
        return topics

    @staticmethod
    def _json_loads(value: Any, default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, json.JSONDecodeError):
            return default

    def list_tasks(self, limit: int | None = None, offset: int = 0, unbatched: bool = False) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if unbatched:
            clauses.append("task_id NOT IN (SELECT task_id FROM generation_batch_items)")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM generation_tasks {where} ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([max(1, min(int(limit), 100)), max(0, int(offset))])
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._task_from_row(row) for row in rows]
    def delete_task(self, task_id: str) -> bool:
        def write(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute("DELETE FROM generation_tasks WHERE task_id=?", (task_id,))
            return cursor.rowcount > 0
        return bool(self._write(write))

    def delete_batch(self, batch_id: str) -> list[str]:
        def write(connection: sqlite3.Connection) -> list[str]:
            rows = connection.execute("SELECT task_id FROM generation_batch_items WHERE batch_id=?", (batch_id,)).fetchall()
            task_ids = [str(row[0]) for row in rows]
            connection.execute("DELETE FROM generation_batches WHERE batch_id=?", (batch_id,))
            for task_id in task_ids:
                connection.execute("DELETE FROM generation_tasks WHERE task_id=?", (task_id,))
            return task_ids
        return self._write(write)

    def delete_failed_tasks(self) -> list[str]:
        def write(connection: sqlite3.Connection) -> list[str]:
            rows = connection.execute("SELECT task_id FROM generation_tasks WHERE status IN ('failed','partial_success')").fetchall()
            task_ids = [str(row[0]) for row in rows]
            for task_id in task_ids:
                connection.execute("DELETE FROM generation_tasks WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM generation_batches WHERE batch_id NOT IN (SELECT DISTINCT batch_id FROM generation_batch_items)")
            return task_ids
        return self._write(write)

    def update_task_status(self, task_id: str, status: str) -> None:
        self._write(lambda connection: connection.execute("UPDATE generation_tasks SET status=?, updated_at=? WHERE task_id=?", (redact_sensitive_text(status), utc_now(), task_id)))

    def force_task_status(self, task_id: str, status: str) -> None:
        """Recovery-only status write that bypasses injected orchestration hooks."""
        self._write(lambda connection: connection.execute("UPDATE generation_tasks SET status=?, updated_at=? WHERE task_id=?", (redact_sensitive_text(status), utc_now(), task_id)))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM generation_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["selected_topics"] = SQLiteStore._json_loads(task.pop("selected_topics", "[]"), [])
        task["generation_options"] = SQLiteStore._json_loads(task.get("generation_options"), {})
        task["angle_plan"] = SQLiteStore._json_loads(task.get("angle_plan"), {})
        task = sanitize_sensitive_data(task)
        return task


def get_store() -> SQLiteStore:
    return SQLiteStore()


def init_db() -> Path:
    SQLiteStore().init_schema()
    return DB_PATH
