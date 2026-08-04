from __future__ import annotations

import logging
import threading
import json
import hashlib
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from generation.executor import GenerationExecutor
from generation.recovery import recover_interrupted_tasks
from generation.single_task import prepare_generation_state
from modules.config_store import load_settings
from modules.database import SQLiteStore, get_store
from modules.generation_store import load_generation_task
from modules.generation_store import generation_task_dir, save_generation_task
from modules.security import redact_sensitive_text, sanitize_sensitive_data
from providers.text_provider import ProviderError
from generation.similarity import compare_batch_report
from generation.inline_images import run_inline_images
from modules.models import HotTopic
from research.service import ResearchService


_logger = logging.getLogger(__name__)


class BatchExecutor:
    """Coordinates independent single-task runs without duplicating generation logic."""

    def __init__(self, store: SQLiteStore | None = None, max_workers: int = 3) -> None:
        self.store = store or get_store()
        self.max_workers = 3
        self.single_executor = GenerationExecutor(max_workers=3)
        self._active: dict[str, Future] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._quality_running: set[str] = set()
        self._batch_start_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="batch-start")
        self._batch_starts: dict[str, Future] = {}
        self._lock = threading.RLock()

    def _settings(self) -> dict[str, Any]:
        return load_settings()

    def _forget(self, task_id: str, future: Future, batch_id: str) -> None:
        with self._lock:
            if self._active.get(task_id) is future:
                self._active.pop(task_id, None)
        try:
            self.store.refresh_batch(batch_id)
            self._maybe_check_similarity(batch_id)
        except Exception:
            pass

    def _set_quality(self, task_id: str, status: str, score: float | None, rewrite_count: int | None = None, evidence: dict[str, Any] | None = None) -> None:
        state = load_generation_task(task_id)
        if state:
            current_version = int(state.get("state_version") or 0)
            state["similarity_status"] = status
            state["similarity_score"] = score
            if rewrite_count is not None:
                state["rewrite_count"] = rewrite_count
            if evidence is not None:
                state["similarity_evidence"] = sanitize_sensitive_data(evidence)
            state["state_version"] = current_version + 1
            save_generation_task(state, expected_version=current_version)
        self.store.update_task_quality(task_id, status, score, rewrite_count)

    @staticmethod
    def _file_sha(path: Any) -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, AttributeError):
            return None

    def _prepare_rewrite(
        self,
        batch_id: str,
        item: dict[str, Any],
        score: float,
        violations: list[str],
        conflict_article: dict[str, Any] | None = None,
        similarity_evidence: dict[str, Any] | None = None,
    ) -> None:
        task = item["task"]
        task_id = str(task["task_id"])
        state = load_generation_task(task_id)
        if not state:
            return
        current_rewrite_count = int(state.get("rewrite_count") or task.get("rewrite_count") or 0)
        if current_rewrite_count >= 1:
            self._set_quality(task_id, "review_required", score, current_rewrite_count, similarity_evidence)
            self.store.update_batch_quality(batch_id, "review_required")
            self.store.refresh_batch(batch_id)
            return
        rewrite_count = current_rewrite_count + 1
        root = generation_task_dir(task_id)
        previous_result = {
            "article": state.get("article"),
            "cover": state.get("cover"),
            "inline_images": state.get("inline_images") or [],
            "inline_image_summary": state.get("inline_image_summary") or {},
            "article_sha": self._file_sha(root / "article.json"),
            "prompt_sha": self._file_sha(root / "prompts" / "article_prompt.txt"),
            "cover_prompt_sha": self._file_sha(root / "prompts" / "cover_prompt.txt"),
        }
        conflict_article = sanitize_sensitive_data(conflict_article or {})
        angle_plan = state.get("angle_plan") or {}
        options = sanitize_sensitive_data(state.get("generation_options") or task.get("generation_options") or {})
        options["rewrite_context"] = {
            "reason": "同批次文章内容差异不足",
            "reason_code": "batch_similarity_rewrite",
            "violations": violations,
            "similarity_score": score,
            "rewrite_count": rewrite_count,
            "conflict_article": {
                "title": str(conflict_article.get("title") or "")[:200],
                "opening": str(conflict_article.get("content_markdown") or conflict_article.get("intro") or "")[:200],
                "headings": [str(section.get("heading") or "") for section in conflict_article.get("sections") or [] if isinstance(section, dict)],
            },
            "angle_id": state.get("angle_id"),
            "angle_name": state.get("angle_name"),
            "opening_strategy": angle_plan.get("opening_strategy") or "从当前角度提出不同的问题意识",
            "avoid_expressions": ["值得关注的是", "需要指出的是", "从这个角度来看"],
            "instruction": "保留事实基础和来源，必须更换标题、开头、结构组织和核心论述，不要复用其他文章表达。",
        }
        current_version = int(state.get("state_version") or 0)
        state.update({
            "status": "queued", "stage": "queued", "progress": 0, "completed_at": None,
            "failed_step": None, "error_code": "", "safe_error_message": "",
            "article": None, "cover": None, "previous_result": previous_result, "generation_options": options,
            "rewrite_requested": True, "rewrite_count": rewrite_count,
            "similarity_status": "rewrite_required", "similarity_score": score,
            "state_version": current_version + 1,
        })
        save_generation_task(state, expected_version=current_version)
        self.store.update_batch_quality(batch_id, "rewriting")
        self.store.update_task_generation_options(task_id, options)
        self.store.update_task_quality(task_id, "rewrite_required", score, rewrite_count)
        self.store.update_task_status(task_id, "queued")
        self._submit_item(batch_id, task_id, "retry-article")

    def _maybe_check_similarity(self, batch_id: str) -> None:
        with self._lock:
            if batch_id in self._quality_running:
                return
            batch = self.store.get_batch(batch_id)
            if not batch or batch.get("mode") != "single_topic_multi_angle":
                return
            items = batch.get("items") or []
            # Skip quality check for single-article batches
            if len([item for item in items if item.get("task", {}).get("status") == "completed"]) <= 1:
                self.store.update_batch_quality(batch_id, "not_applicable")
                self.store.refresh_batch(batch_id)
                return
            if not items or any(item["task"].get("status") != "completed" for item in items):
                return
            self._quality_running.add(batch_id)
            self.store.update_batch_quality(batch_id, "checking")
        try:
            articles: list[dict[str, Any]] = []
            article_items: list[dict[str, Any]] = []
            for item in items:
                state = load_generation_task(item["task"]["task_id"]) or {}
                if state.get("article"):
                    articles.append(state["article"])
                    article_items.append(item)
            report = compare_batch_report(articles)
            for index, pair in enumerate(report["pairs"]):
                pair["left_angle_id"] = article_items[pair["left_index"]]["task"].get("angle_id")
                pair["right_angle_id"] = article_items[pair["right_index"]]["task"].get("angle_id")
            for item in article_items:
                self._set_quality(item["task"]["task_id"], "passed", None, 0, report)
            violating_pairs = report["violating_pairs"]
            if violating_pairs:
                offending_scores: dict[int, float] = {}
                conflict_map: dict[int, dict[str, Any]] = {}
                for pair in violating_pairs:
                    pair_score = max(float(pair.get(name) or 0) for name in pair.get("violations", []) or ["overall_similarity"])
                    for index, conflict_index in ((pair["left_index"], pair["right_index"]), (pair["right_index"], pair["left_index"])):
                        offending_scores[index] = max(offending_scores.get(index, 0.0), pair_score)
                        existing = conflict_map.get(index)
                        if not existing or pair_score >= float(existing.get("score") or 0.0):
                            conflict_map[index] = {"score": pair_score, "pair": pair, "conflict_index": conflict_index}
                for index, detail in conflict_map.items():
                    task = article_items[index]["task"]
                    state = load_generation_task(task["task_id"]) or {}
                    rewrite_count = int(state.get("rewrite_count") or task.get("rewrite_count") or 0)
                    self._set_quality(task["task_id"], "review_required", offending_scores.get(index), rewrite_count, report)
                self.store.update_batch_quality(batch_id, "review_required")
                self.store.refresh_batch(batch_id)
            else:
                self.store.update_batch_quality(batch_id, "passed")
                self.store.refresh_batch(batch_id)
        except Exception as exc:
            self.store.update_batch_quality(batch_id, "failed", f"\u6279\u6b21\u76f8\u4f3c\u5ea6\u68c0\u67e5\u5931\u8d25\uff1a{redact_sensitive_text(str(exc))}")
            self.store.refresh_batch(batch_id)
            raise
        finally:
            with self._lock:
                self._quality_running.discard(batch_id)

    def is_task_active(self, task_id: str) -> bool:
        with self._lock:
            future = self._active.get(task_id)
            return bool(future and not future.done()) or self.single_executor.is_running(task_id)

    def _submit_item(self, batch_id: str, task_id: str, retry_step: str | None = None) -> Future:
        _logger.info("_submit_item: 开始提交 batch_id=%s task_id=%s retry_step=%s", batch_id, task_id, retry_step)
        try:
            with self._lock:
                if self.is_task_active(task_id):
                    raise RuntimeError("TASK_ALREADY_RUNNING")
                task = self.store.get_task(task_id)
                if not task:
                    raise ProviderError("TASK_NOT_FOUND", "task not found")
                settings = self._settings()
                text_profile = dict(settings.get("text_profile") or {})
                image_profile = dict(settings.get("image_profile") or {})
                state = prepare_generation_state(task, text_profile, image_profile, store=self.store)
                if state.get("status") == "completed":
                    raise ProviderError("TASK_ALREADY_COMPLETED", "completed task cannot run again")
                if state.get("status") == "cancelled":
                    raise ProviderError("TASK_CANCELLED", "cancelled task cannot run again")
                batch_config = self.store.get_batch(batch_id) or {}
                capacity = 3
                semaphore = self._semaphores.setdefault(batch_id, threading.BoundedSemaphore(capacity))
                def run_with_capacity() -> dict[str, Any]:
                    with semaphore:
                        return self.single_executor.execute_with_retry(task, text_profile, image_profile, settings, self.store, retry_step)
                future = self.single_executor.submit(task_id, run_with_capacity)
                self._active[task_id] = future
                future.add_done_callback(lambda completed: self._forget(task_id, completed, batch_id))
            _logger.info("_submit_item: 提交成功 batch_id=%s task_id=%s future_done=%s",
                         batch_id, task_id, future.done())
            return future
        except Exception:
            _logger.exception("_submit_item: 提交失败 batch_id=%s task_id=%s", batch_id, task_id)
            raise

    def _ensure_shared_research(self, batch: dict[str, Any]) -> dict[str, Any]:
        if batch.get("mode") != "single_topic_multi_angle":
            return batch
        options = dict(batch.get("generation_options") or {})
        shared_bundle = options.get("shared_research_bundle") if isinstance(options.get("shared_research_bundle"), dict) else None
        if shared_bundle and int(shared_bundle.get("accepted_source_count") or 0) > 0:
            return batch
        items = batch.get("items") or []
        first_topic = ((items[0] or {}).get("task") or {}).get("selected_topics", [{}])[0] if items else {}
        if not first_topic:
            return batch
        topic = HotTopic.from_dict(sanitize_sensitive_data(first_topic))
        shared_bundle = ResearchService().collect(topic)
        options["shared_research_bundle"] = sanitize_sensitive_data(shared_bundle)
        self.store.update_batch_generation_options(str(batch.get("batch_id") or ""), options)
        for item in items:
            task = item.get("task") or {}
            task_options = dict(task.get("generation_options") or {})
            task_options["shared_research_bundle"] = sanitize_sensitive_data(shared_bundle)
            self.store.update_task_generation_options(str(task.get("task_id") or ""), task_options)
        return self.store.get_batch(str(batch.get("batch_id") or "")) or batch

    def _submit_inline_item(self, batch_id: str, task_id: str) -> Future:
        _logger.info("_submit_inline_item: 开始提交 batch_id=%s task_id=%s", batch_id, task_id)
        try:
            with self._lock:
                if self.is_task_active(task_id):
                    raise RuntimeError("TASK_ALREADY_RUNNING")
                settings = self._settings()
                future = self.single_executor.submit(
                    task_id,
                    lambda: run_inline_images(
                        task_id,
                        settings.get("image_profile", {}),
                        settings=settings,
                        store=self.store,
                    ),
                )
                self._active[task_id] = future
                future.add_done_callback(lambda completed: self._forget(task_id, completed, batch_id))
            _logger.info("_submit_inline_item: 提交成功 batch_id=%s task_id=%s future_done=%s",
                         batch_id, task_id, future.done())
            return future
        except Exception:
            _logger.exception("_submit_inline_item: 提交失败 batch_id=%s task_id=%s", batch_id, task_id)
            raise

    def _start_batch_worker(self, batch_id: str) -> dict[str, Any]:
        """Run research and child submission outside the HTTP request thread."""
        with self._lock:
            batch = self.store.get_batch(batch_id)
            if not batch:
                raise ProviderError("BATCH_NOT_FOUND", "batch not found")
            if batch.get("status") in {"completed", "cancelled"}:
                return batch
        try:
            batch = self._ensure_shared_research(batch)
        except Exception as exc:
            _logger.exception("start_batch: shared research failed batch_id=%s", batch_id)
            # A worker-level failure must become durable item failures; otherwise
            # the batch remains queued forever after the HTTP request has returned.
            for item in batch.get("items", []):
                task = item.get("task") or {}
                task_id = str(task.get("task_id") or "")
                if not task_id or task.get("status") in {"completed", "cancelled"}:
                    continue
                try:
                    state = load_generation_task(task_id) or {
                        "task_id": task_id,
                        "task_name": task.get("task_name") or "",
                        "mode": task.get("mode") or "",
                        "selected_topics": task.get("selected_topics") or [],
                        "generation_options": task.get("generation_options") or {},
                    }
                    current_version = int(state.get("state_version") or 0)
                    state.update({
                        "status": "failed",
                        "stage": "batch_submit",
                        "progress": 0,
                        "failed_step": "batch_submit",
                        "error_code": "BATCH_RESEARCH_FAILED",
                        "safe_error_message": "批次准备失败，请单独重试任务。",
                        "retryable": True,
                        "state_version": current_version + 1,
                    })
                    save_generation_task(state, expected_version=current_version if current_version else None, allow_terminal_recovery=True)
                    self.store.update_task_status(task_id, "failed")
                except Exception:
                    _logger.exception("start_batch: mark shared-research failure failed task_id=%s", task_id)
            return self.store.refresh_batch(batch_id) or batch
        with self._lock:
            batch = self.store.get_batch(batch_id) or batch
            for item in batch.get("items", []):
                task = item["task"]
                task_id = str(task["task_id"])
                if self.is_task_active(task_id) or task.get("status") in {"completed", "cancelled", "failed", "partial_success"}:
                    continue
                try:
                    self._submit_item(batch_id, task_id)
                except RuntimeError as exc:
                    # TASK_ALREADY_RUNNING 是竞态，跳过继续即可
                    if str(exc) != "TASK_ALREADY_RUNNING":
                        _logger.exception("start_batch: 非预期 RuntimeError task_id=%s", task_id)
                except Exception as exc:
                    _logger.exception("start_batch: 提交 item 失败 batch_id=%s task_id=%s", batch_id, task_id)
                    # 把该 task 标记为失败，然后继续处理下一个 item
                    try:
                        corrupt_task_data = isinstance(exc, ValueError) and "TOPIC_SNAPSHOT_MISSING_ID" in str(exc)
                        state = load_generation_task(task_id) or {
                            "task_id": task_id,
                            "task_name": task.get("task_name") or "",
                            "mode": task.get("mode") or "",
                            "selected_topics": task.get("selected_topics") or [],
                            "generation_options": task.get("generation_options") or {},
                        }
                        current_version = int(state.get("state_version") or 0)
                        state.update({
                            "status": "failed",
                            "stage": "failed",
                            "progress": 0,
                            "failed_step": "batch_submit",
                            "error_code": "TASK-DATA-CORRUPT" if corrupt_task_data else "TASK_SUBMIT_FAILED",
                            "safe_error_message": "这条任务的数据已损坏，请删除后重新选择该话题生成。" if corrupt_task_data else "任务提交失败，请查看 api.log 或手动重试。",
                            "retryable": not corrupt_task_data,
                            "state_version": current_version + 1,
                        })
                        save_generation_task(state, expected_version=current_version if current_version else None,
                                             allow_terminal_recovery=True)
                        self.store.update_task_status(task_id, "failed")
                    except Exception:
                        _logger.exception("start_batch: 标记失败 task 也失败了 task_id=%s", task_id)
                # 无论成功失败都继续下一个 item
            return self.store.refresh_batch(batch_id) or batch

    def _forget_batch_start(self, batch_id: str, future: Future) -> None:
        with self._lock:
            if self._batch_starts.get(batch_id) is future:
                self._batch_starts.pop(batch_id, None)

    def start_batch_async(self, batch_id: str, *, refresh: bool = True) -> dict[str, Any]:
        """Accept a batch immediately; research and task submission happen in the background."""
        with self._lock:
            batch = self.store.get_batch(batch_id)
            if not batch:
                raise ProviderError("BATCH_NOT_FOUND", "batch not found")
            if batch.get("status") in {"completed", "cancelled"}:
                return batch
            current = self._batch_starts.get(batch_id)
            if not current or current.done():
                future = self._batch_start_executor.submit(self._start_batch_worker, batch_id)
                self._batch_starts[batch_id] = future
                future.add_done_callback(lambda completed: self._forget_batch_start(batch_id, completed))
            # The interactive create endpoint only needs the persisted batch id.
            # Refreshing here can contend with the worker's first research write.
            return (self.store.refresh_batch(batch_id) or batch) if refresh else batch

    def start_batch(self, batch_id: str) -> dict[str, Any]:
        """Start synchronously for direct library callers and legacy integrations."""
        return self._start_batch_worker(batch_id)

    def cancel_task(self, batch_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            item = self.store.get_batch_item(batch_id, task_id)
            if not item:
                raise ProviderError("BATCH_ITEM_NOT_FOUND", "task does not belong to batch")
            result = self.single_executor.cancel(task_id, self.store)
            return self.store.refresh_batch(batch_id) or result

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self.store.get_batch(batch_id)
            if not batch:
                raise ProviderError("BATCH_NOT_FOUND", "batch not found")
            if batch.get("status") == "completed":
                return batch
            for item in batch.get("items", []):
                task = item["task"]
                if task.get("status") in {"queued", "running"}:
                    try:
                        self.single_executor.cancel(task["task_id"], self.store)
                    except Exception:
                        continue
            return self.store.refresh_batch(batch_id) or batch

    @staticmethod
    def _retry_step(task: dict[str, Any]) -> str:
        state = load_generation_task(str(task["task_id"])) or {}
        if state.get("failed_step") == "generating_inline_images":
            return "retry-inline"
        if state.get("fallback_notice"):
            return "retry-article"
        if state.get("failed_step") == "generating_cover" and state.get("article"):
            return "retry-cover"
        return "retry-article"

    def retry_task(self, batch_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            item = self.store.get_batch_item(batch_id, task_id)
            if not item:
                raise ProviderError("BATCH_ITEM_NOT_FOUND", "task does not belong to batch")
            task = item["task"]
            if task.get("status") == "cancelled":
                raise ProviderError("TASK_CANCELLED", "cancelled task cannot run again")
            if self.is_task_active(task_id):
                raise ProviderError("TASK_ALREADY_RUNNING", "task is already running")
            if task.get("status") == "completed":
                state = load_generation_task(task_id)
                if not state:
                    raise ProviderError("TASK_ALREADY_COMPLETED", "completed task result is not available for regeneration")
                current_version = int(state.get("state_version") or 0)
                state.update({
                    "status": "queued", "stage": "queued", "progress": 0, "completed_at": None,
                    "failed_step": None, "error_code": "", "safe_error_message": "",
                    "article": None, "cover": None, "rewrite_requested": True,
                    "fallback_notice": "", "similarity_status": "rewrite_required",
                    "previous_result": {
                        "article": state.get("article"),
                        "cover": state.get("cover"),
                        "inline_images": state.get("inline_images") or [],
                        "inline_image_summary": state.get("inline_image_summary") or {},
                        "article_sha": (state.get("quality_evidence") or {}).get("article_sha_after"),
                        "prompt_sha": (state.get("quality_evidence") or {}).get("prompt_sha_after"),
                        "cover_prompt_sha": (state.get("quality_evidence") or {}).get("cover_prompt_sha"),
                    },
                    "state_version": current_version + 1,
                })
                save_generation_task(state, expected_version=current_version)
                self.store.update_task_status(task_id, "queued")
            step = self._retry_step(task)
            if step == "retry-inline":
                future = self._submit_inline_item(batch_id, task_id)
                return {"batch_id": batch_id, "task_id": task_id, "retry_step": step, "status": "queued", "future_active": not future.done()}
            future = self._submit_item(batch_id, task_id, step)
            return {"batch_id": batch_id, "task_id": task_id, "retry_step": step, "status": "queued", "future_active": not future.done()}

    def retry_failed(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        if not batch:
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        submitted: list[str] = []
        errors: list[dict[str, str]] = []
        for item in batch.get("items", []):
            task = item["task"]
            if task.get("status") not in {"failed", "partial_success"}:
                continue
            try:
                self.retry_task(batch_id, task["task_id"])
                submitted.append(task["task_id"])
            except Exception as exc:
                errors.append({"task_id": task["task_id"], "error": redact_sensitive_text(str(exc))})
        refreshed = self.store.refresh_batch(batch_id) or batch
        return sanitize_sensitive_data({"batch": refreshed, "submitted": submitted, "errors": errors})

    def retry_quality_check(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        if not batch:
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        if batch.get("mode") != "single_topic_multi_angle":
            raise ProviderError("QUALITY_NOT_APPLICABLE", "quality check is only for multi-angle batches")
        if len(batch.get("items") or []) <= 1:
            self.store.update_batch_quality(batch_id, "not_applicable")
            return self.store.refresh_batch(batch_id) or batch
        self.store.update_batch_quality(batch_id, "pending")
        self._maybe_check_similarity(batch_id)
        return self.store.refresh_batch(batch_id) or batch

    def recover_batches(self) -> dict[str, list[dict[str, Any]]]:
        report: dict[str, list[dict[str, Any]]] = {"recovered_batches": [], "skipped_batches": [], "recovery_failed": []}
        recover_interrupted_tasks(store=self.store, executor=self.single_executor)
        for batch in self.store.list_batches():
            batch_id = str(batch["batch_id"])
            try:
                refreshed = self.store.refresh_batch(batch_id) or batch
                quality_status = str(refreshed.get("quality_status") or "")
                all_completed = bool(refreshed.get("items")) and all((item.get("task") or {}).get("status") == "completed" for item in refreshed.get("items") or [])
                if refreshed.get("mode") == "single_topic_multi_angle" and all_completed and quality_status in {"pending", "checking", "rewriting"}:
                    items = refreshed.get("items") or []
                    if len(items) <= 1:
                        self.store.update_batch_quality(batch_id, "not_applicable")
                        self.store.refresh_batch(batch_id)
                        report["skipped_batches"].append({"batch_id": batch_id, "status": "single_article_skip"})
                        continue
                    self.store.update_batch_quality(batch_id, "pending")
                    self._maybe_check_similarity(batch_id)
                    report["recovered_batches"].append({"batch_id": batch_id, "status": "quality_check"})
                    continue
                if refreshed.get("mode") == "single_topic_multi_angle" and all_completed and quality_status == "failed":
                    report["skipped_batches"].append({"batch_id": batch_id, "status": "quality_failed"})
                    continue
                if refreshed.get("status") in {"completed", "cancelled"}:
                    report["skipped_batches"].append({"batch_id": batch_id, "status": refreshed.get("status")})
                    continue
                if refreshed.get("status") in {"queued", "running"}:
                    self.start_batch(batch_id)
                    report["recovered_batches"].append({"batch_id": batch_id, "status": "running"})
                else:
                    report["skipped_batches"].append({"batch_id": batch_id, "status": refreshed.get("status")})
            except Exception as exc:
                report["recovery_failed"].append({"batch_id": batch_id, "error": redact_sensitive_text(str(exc))})
        return report


_DEFAULT_BATCH_EXECUTOR = BatchExecutor()


def get_batch_executor() -> BatchExecutor:
    return _DEFAULT_BATCH_EXECUTOR

