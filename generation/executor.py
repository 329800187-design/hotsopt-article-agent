from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from generation.single_task import cancel_single_task, is_cancel_requested, run_single_task
from modules.database import SQLiteStore
from modules.generation_store import load_generation_task, save_generation_task
from modules.models import utc_now
from modules.task_locks import get_task_lock
from providers.errors import is_retryable_error


class GenerationExecutor:
    def __init__(self, max_workers: int = 3) -> None:
        worker_count = max(1, min(3, int(max_workers or 3)))
        self.pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="generation")
        self._futures: dict[str, Future] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _task_lock(self, task_id: str) -> threading.Lock:
        return get_task_lock(task_id)

    def task_lock(self, task_id: str):
        return get_task_lock(task_id)

    def is_running(self, task_id: str) -> bool:
        with self._registry_lock:
            future = self._futures.get(task_id)
            return bool(future and not future.done())

    def submit(self, task_id: str, function: Callable[[], dict[str, Any]]) -> Future:
        with self.task_lock(task_id):
            with self._registry_lock:
                current = self._futures.get(task_id)
                if current and not current.done():
                    raise RuntimeError("TASK_ALREADY_RUNNING")
                future = self.pool.submit(self._run_locked, task_id, function)
                self._futures[task_id] = future
                future.add_done_callback(lambda completed: self._forget(task_id, completed))
                return future

    def submit_inline_images(
        self,
        task_id: str,
        image_profile: dict[str, Any],
        settings: dict[str, Any],
        store: SQLiteStore,
        target_ids: list[str] | None = None,
        regenerate_all: bool = False,
    ) -> Future:
        """Submit an inline-image-only operation using the task's existing future slot."""
        from generation.inline_images import run_inline_images

        with self.task_lock(task_id):
            with self._registry_lock:
                current = self._futures.get(task_id)
                if current and not current.done():
                    raise RuntimeError("TASK_ALREADY_RUNNING")
                future = self.pool.submit(
                    run_inline_images,
                    task_id,
                    image_profile,
                    settings,
                    store,
                    target_ids,
                    regenerate_all,
                    False,
                )
                self._futures[task_id] = future
                future.add_done_callback(lambda completed: self._forget(task_id, completed))
                return future

    def _run_locked(self, task_id: str, function: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        return function()

    def _forget(self, task_id: str, future: Future) -> None:
        with self._registry_lock:
            if self._futures.get(task_id) is future:
                self._futures.pop(task_id, None)

    def cancel(self, task_id: str, store: SQLiteStore) -> dict[str, Any]:
        with self.task_lock(task_id):
            result = cancel_single_task(task_id, store)
        with self._registry_lock:
            future = self._futures.get(task_id)
            if future and not future.running():
                future.cancel()
        return result

    def execute_with_retry(self, task: dict[str, Any], text_profile: dict[str, Any], image_profile: dict[str, Any], settings: dict[str, Any], store: SQLiteStore, retry_step: str | None = None) -> dict[str, Any]:
        max_auto_retries = max(0, min(2, int(settings.get("max_auto_retries", 0))))
        delays = [1, 3]
        current_step = retry_step
        retries = 0
        while True:
            result = run_single_task(task, text_profile, image_profile, settings=settings, store=store, retry_step=current_step)
            status = result.get("status")
            if status in {"completed", "cancelled"}:
                return result
            code = str(result.get("error_code") or "")
            if is_cancel_requested(task["task_id"]):
                from generation.single_task import finalize_cancelled_task
                return finalize_cancelled_task(task["task_id"], store)
            if not is_retryable_error(code) or retries >= max_auto_retries:
                return result
            if code == "TIMEOUT" and result.get("failed_step") == "generating_article":
                return result
            if result.get("failed_step") == "generating_inline_images":
                return result
            retries += 1
            current_step = "retry-cover" if result.get("failed_step") == "generating_cover" else "retry-article"
            retry_after = result.get("retry_after_seconds")
            delay = int(retry_after) if retry_after is not None else delays[min(retries - 1, len(delays) - 1)]
            delay = max(1, min(300, delay))
            self._mark_retry_waiting(result, store, delay, retries)
            if not self._sleep_or_cancel(task["task_id"], delay):
                from generation.single_task import finalize_cancelled_task
                return finalize_cancelled_task(task["task_id"], store)

    def _sleep_or_cancel(self, task_id: str, delay: int) -> bool:
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if is_cancel_requested(task_id):
                return False
            time.sleep(0.2)
        return not is_cancel_requested(task_id)

    def _mark_retry_waiting(self, result: dict[str, Any], store: SQLiteStore, delay: int, retries: int) -> None:
        state = load_generation_task(result["task_id"])
        if not state:
            return
        now = datetime.now(timezone.utc)
        state["status"] = "running"
        state["next_retry_at"] = (now + timedelta(seconds=delay)).isoformat()
        state["retry_count"] = retries
        state["updated_at"] = utc_now()
        current_version = int(state.get("state_version") or 0)
        state["state_version"] = current_version + 1
        save_generation_task(state, expected_version=current_version)
        store.update_task_status(state["task_id"], state["status"])


_DEFAULT_EXECUTOR = GenerationExecutor()


def get_executor() -> GenerationExecutor:
    return _DEFAULT_EXECUTOR
