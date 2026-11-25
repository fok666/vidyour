"""Unit tests for the in-process JobManager (fake job repository, mocked cleanup)."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.jobs import JobManager
from app.models import Job

FIXED_NOW = "2026-11-22T12:00:00+00:00"
DATA_DIR = Path("/fake/data")
JOB_TMP_DIR = DATA_DIR / "tmp" / "job-1"


# ---------------------------------------------------------------------------
# In-memory fake job repository (owned by this module)
# ---------------------------------------------------------------------------


class FakeJobRepository:
    """Implements the job subset of app.db.Database in memory."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.history: list[dict[str, Any]] = []

    def create_job(self, job: Job) -> Job:
        self.jobs[job.id] = job
        self.history.append(job.model_dump())
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_jobs(self, project_id: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.project_id == project_id]

    def update_job(self, job_id: str, **fields: Any) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        updated = job.model_copy(update=fields)
        self.jobs[job_id] = updated
        self.history.append(updated.model_dump())
        return updated


@pytest.fixture
def repo() -> FakeJobRepository:
    return FakeJobRepository()


@pytest.fixture
def cleanup() -> MagicMock:
    return MagicMock(name="file_cleanup")


@pytest.fixture
def manager(repo: FakeJobRepository, cleanup: MagicMock):
    mgr = JobManager(
        max_workers=1,
        data_dir=DATA_DIR,
        executor=ThreadPoolExecutor(max_workers=1),
        repository=repo,  # type: ignore[arg-type]
        file_cleanup=cleanup,
        clock=lambda: FIXED_NOW,
        id_generator=lambda: "job-1",
    )
    yield mgr
    mgr.executor.shutdown(wait=True)  # type: ignore[union-attr]


def _run(manager: JobManager, worker) -> None:
    manager.submit("proj-1", "slice", worker)
    manager.wait("job-1", timeout=5)
    manager.executor.shutdown(wait=True)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Behavior 1: progress + successful completion
# ---------------------------------------------------------------------------


def test_successful_job_records_progress_and_completed_result(manager, repo):
    # GIVEN a JobManager with fake repo, stubbed id 'job-1' and fixed clock
    def worker(progress_cb):
        progress_cb(50, "Slicing")
        return {"parts_count": 3}

    # WHEN the worker is submitted and awaited
    _run(manager, worker)

    # THEN intermediate progress and final completion are recorded
    intermediate = [
        (h["progress"], h["message"]) for h in repo.history if h["id"] == "job-1"
    ]
    assert (50, "Slicing") in intermediate
    final = repo.jobs.get("job-1")
    assert final is not None, "job-1 was never recorded in the repository"
    assert final.status == "completed"
    assert final.progress == 100
    assert final.result == {"parts_count": 3}
    assert final.error is None
    assert final.project_id == "proj-1"
    assert final.kind == "slice"


# ---------------------------------------------------------------------------
# Behavior 2: CalledProcessError stderr truncated to last 20 lines + cleanup
# ---------------------------------------------------------------------------


def test_called_process_error_keeps_last_20_stderr_lines_and_cleans_tmp(
    manager, repo, cleanup
):
    # GIVEN a worker raising CalledProcessError with 30 distinct stderr lines
    stderr = "\n".join(f"line{i}" for i in range(1, 31))

    def worker(progress_cb):
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=stderr)

    # WHEN it is submitted and awaited
    _run(manager, worker)

    # THEN the job failed with only lines 11..30 and tmp cleanup ran
    final = repo.jobs.get("job-1")
    assert final is not None, "job-1 was never recorded in the repository"
    assert final.status == "failed"
    assert final.error is not None
    error_lines = {line.strip() for line in final.error.splitlines()}
    for i in range(11, 31):
        assert f"line{i}" in error_lines
    for i in range(1, 11):
        assert f"line{i}" not in error_lines
    cleanup.assert_any_call(JOB_TMP_DIR)


def test_called_process_error_decodes_bytes_before_taking_stderr_tail(manager, repo):
    # GIVEN byte stderr with more than 20 lines and one invalid UTF-8 byte
    stderr_lines = [f"line{i}".encode() for i in range(1, 22)] + [b"line22-\xff"]
    stderr = b"\n".join(stderr_lines)

    def worker(progress_cb):
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=stderr)

    # WHEN the worker is submitted and awaited
    _run(manager, worker)

    # THEN decoding uses replacement and truncation retains exactly the last 20 lines
    final = repo.jobs.get("job-1")
    assert final is not None, "job-1 was never recorded in the repository"
    expected = "\n".join([*(f"line{i}" for i in range(3, 22)), "line22-\ufffd"])
    assert final.error == expected


def test_called_process_error_with_empty_stderr_uses_exception_string(manager, repo):
    # GIVEN a CalledProcessError with empty byte stderr
    error = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"")

    def worker(progress_cb):
        raise error

    # WHEN the worker is submitted and awaited
    _run(manager, worker)

    # THEN the exception's exact fallback message is recorded
    final = repo.jobs.get("job-1")
    assert final is not None, "job-1 was never recorded in the repository"
    assert final.error == "Command '['ffmpeg']' returned non-zero exit status 1."


# ---------------------------------------------------------------------------
# Behavior 3: generic exception message recorded + cleanup
# ---------------------------------------------------------------------------


def test_generic_exception_message_recorded_and_tmp_cleaned(manager, repo, cleanup):
    # GIVEN a worker raising a RuntimeError
    def worker(progress_cb):
        raise RuntimeError("Gemini rate limit exceeded (429)")

    # WHEN it is submitted and awaited
    _run(manager, worker)

    # THEN the job failed with the exact message and tmp cleanup still ran
    final = repo.jobs.get("job-1")
    assert final is not None, "job-1 was never recorded in the repository"
    assert final.status == "failed"
    assert final.error == "Gemini rate limit exceeded (429)"
    cleanup.assert_any_call(JOB_TMP_DIR)


def test_uninspectable_worker_runs_without_job_id(manager, repo):
    # GIVEN a callable whose signature cannot be inspected
    class UninspectableWorker:
        @property
        def __signature__(self):
            raise ValueError("signature unavailable")

        def __call__(self, progress_cb):
            return {"ran_without_job_id": True}

    # WHEN the worker is submitted and awaited
    _run(manager, UninspectableWorker())

    # THEN signature inspection failure falls back to calling it without job_id
    final = repo.jobs.get("job-1")
    assert final is not None, "job-1 was never recorded in the repository"
    assert final.status == "completed"
    assert final.result == {"ran_without_job_id": True}


def test_repositoryless_manager_returns_empty_results_and_waits_safely():
    # GIVEN a manager with no repository
    cleanup = MagicMock(name="file_cleanup")
    manager = JobManager(
        data_dir=DATA_DIR,
        executor=ThreadPoolExecutor(max_workers=1),
        repository=None,
        file_cleanup=cleanup,
        clock=lambda: FIXED_NOW,
        id_generator=lambda: "job-1",
    )
    ran: list[bool] = []

    try:
        # THEN repository-backed lookups return their empty results
        assert manager.get("job-1") is None
        assert manager.list_for_project("proj-1") == []
        assert manager.wait("unknown-job") is None

        # WHEN a job is submitted and awaited, it runs without repository writes
        submitted = manager.submit(
            "proj-1", "slice", lambda progress_cb: ran.append(True)
        )
        assert submitted is not None
        assert submitted.status == "queued"
        assert manager.wait("job-1", timeout=5) is None
        assert ran == [True]
    finally:
        manager.executor.shutdown(wait=True)  # type: ignore[union-attr]
