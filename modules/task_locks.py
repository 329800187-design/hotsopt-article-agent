from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Iterator


_registry_lock = threading.Lock()
_task_locks: dict[str, threading.RLock] = {}


def get_task_lock(task_id: str) -> threading.RLock:
    with _registry_lock:
        return _task_locks.setdefault(str(task_id), threading.RLock())


@contextmanager
def task_lock(task_id: str) -> Iterator[None]:
    lock = get_task_lock(task_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
