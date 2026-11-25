"""In-process asynchronous job manager interface."""

import contextlib
import inspect
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.db import Database
from app.models import Job

STDERR_TAIL_LINES = 20


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _error_message(exc: BaseException) -> str:
    """CalledProcessError keeps only the last 20 stderr lines; others use str(exc)."""
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if stderr:
            return "\n".join(stderr.splitlines()[-STDERR_TAIL_LINES:])
    return str(exc)


def _accepts_job_id(worker: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(worker).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "job_id" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


class JobManager:
    def __init__(
        self,
        max_workers: int = 2,
        data_dir: Path = Path(),
        executor: ThreadPoolExecutor | None = None,
        repository: Database | None = None,
        file_cleanup: Callable[[Path], Any] | None = None,
        clock: Callable[[], str] | None = None,
        id_generator: Callable[[], str] | None = None,
    ) -> None:
        self.executor = executor or ThreadPoolExecutor(max_workers=max_workers)
        self.repository = repository
        self.data_dir = data_dir
        self.file_cleanup = file_cleanup or _remove_tree
        self.clock = clock or _utc_now
        self.id_generator = id_generator or (lambda: str(uuid.uuid4()))
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def tmp_dir(self, job_id: str) -> Path:
        return Path(self.data_dir) / "tmp" / job_id

    def _update(self, job_id: str, **fields: Any) -> None:
        if self.repository is not None:
            self.repository.update_job(job_id, **fields)

    def _run(self, job_id: str, worker: Callable[..., Any]) -> None:
        def progress_cb(progress: int, message: str = "") -> None:
            self._update(job_id, progress=int(progress), message=message)

        try:
            self._update(job_id, status="running")
            extra = {"job_id": job_id} if _accepts_job_id(worker) else {}
            result = worker(progress_cb, **extra)
            self._update(
                job_id, status="completed", progress=100, result=result or {}, error=None
            )
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
            self._update(job_id, status="failed", error=_error_message(exc))
        finally:
            # Cleanup must never crash the worker thread.
            with contextlib.suppress(Exception):
                self.file_cleanup(self.tmp_dir(job_id))

    def submit(self, project_id: str, kind: str, worker: Callable[..., Any]) -> Job | None:
        job_id = self.id_generator()
        now = self.clock()
        job = Job(
            id=job_id, project_id=project_id, kind=kind, status="queued", progress=0,
            message="", result={}, error=None, created_at=now, updated_at=now,
        )
        if self.repository is not None:
            job = self.repository.create_job(job) or job
        with self._lock:
            self._futures[job_id] = self.executor.submit(self._run, job_id, worker)
        return job

    def get(self, job_id: str) -> Job | None:
        if self.repository is None:
            return None
        return self.repository.get_job(job_id)

    def wait(self, job_id: str, timeout: float | None = None) -> Job | None:
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.get(job_id)

    def list_for_project(self, project_id: str) -> list[Job]:
        if self.repository is None:
            return []
        return self.repository.list_jobs(project_id)

    def shutdown(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait)
