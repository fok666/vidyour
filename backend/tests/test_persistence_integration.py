"""Integration: real Settings, SQLite Database, JobManager and LocalStorageProvider on disk."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.jobs import JobManager
from app.models import (
    Composition,
    DetectedPart,
    Job,
    MediaAsset,
    TimelineAudioClip,
    TimelineVisualClip,
)
from app.providers.storage.local import LocalStorageProvider

NOW = "2026-11-24T00:00:00Z"
CHILD_TABLES = ("media_assets", "detected_parts", "compositions", "jobs")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db_path = tmp_path / "vidyour.db"
    database = Database(lambda: sqlite3.connect(db_path, check_same_thread=False))
    database.init_db()
    return database


def _count(tmp_path: Path, table: str) -> int:
    conn = sqlite3.connect(tmp_path / "vidyour.db")
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_ensure_data_dirs_creates_subdirectories_on_disk(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, _env_file=None)

    settings.ensure_data_dirs()

    for name in ("assets", "clips", "thumbs", "renders", "tmp"):
        assert (data_dir / name).is_dir(), name


def test_delete_project_cascades_all_child_rows(tmp_path: Path, db: Database) -> None:
    project = db.create_project("Holiday")
    assert project is not None
    asset = MediaAsset(
        id="asset-1", project_id=project.id, kind="video", source="upload",
        filename="a.mp4", local_path=str(tmp_path / "a.mp4"), still_path=None,
        thumbnail_path=None, duration_sec=10.0, width=1280, height=720, fps=30.0,
        has_audio=True, bpm=None, beat_grid=None, metadata={}, created_at=NOW,
    )
    db.create_media_asset(asset)
    db.create_detected_part(DetectedPart(
        id="part-1", project_id=project.id, asset_id=asset.id, part_type="scene",
        start_sec=0.0, end_sec=2.0, beat_length=4.0, label="intro", score=0.9,
        ai_reason="", suggested_effect="none", clip_path=None, thumbnail_path=None,
        created_at=NOW,
    ))
    db.save_composition(Composition(
        project_id=project.id,
        video_clips=[TimelineVisualClip(
            id="v1", asset_id=asset.id, part_id="part-1", kind="video", start_sec=0.0,
            duration_sec=2.0, trim_start_sec=0.0, speed=1.0, transition_in="none",
            ken_burns="none", color_preset="original", volume=1.0, muted=False,
            fade_in_sec=0.0, fade_out_sec=0.0,
        )],
        audio_clips=[TimelineAudioClip(
            id="a1", asset_id=asset.id, part_id=None, track_index=0, start_sec=0.0,
            duration_sec=2.0, trim_start_sec=0.0, volume=1.0, muted=False,
            fade_in_sec=0.0, fade_out_sec=0.0,
        )],
        updated_at=NOW,
    ))
    db.create_job(Job(
        id="job-1", project_id=project.id, kind="render", status="queued", progress=0,
        message="", result={}, error=None, created_at=NOW, updated_at=NOW,
    ))
    for table in CHILD_TABLES:
        assert _count(tmp_path, table) == 1, table

    assert db.delete_project(project.id) is True

    assert _count(tmp_path, "projects") == 0
    for table in CHILD_TABLES:
        assert _count(tmp_path, table) == 0, table


def test_failed_job_truncates_stderr_and_cleans_tmp(tmp_path: Path, db: Database) -> None:
    Settings(data_dir=tmp_path, _env_file=None).ensure_data_dirs()
    project = db.create_project("Render")
    assert project is not None
    manager = JobManager(max_workers=1, data_dir=tmp_path, repository=db)
    stderr = "\n".join(f"line {i}" for i in range(1, 31))
    created: list[Path] = []

    def worker(progress_cb, job_id):
        proxy = tmp_path / "tmp" / job_id / "proxy.mp4"
        proxy.parent.mkdir(parents=True, exist_ok=True)
        proxy.write_bytes(b"partial")
        created.append(proxy)
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=stderr)

    try:
        job = manager.submit(project.id, "render", worker)
        assert job is not None
        final = manager.wait(job.id, timeout=10)
    finally:
        manager.shutdown()

    assert created and created[0] == tmp_path / "tmp" / job.id / "proxy.mp4"
    assert not created[0].exists()
    assert not (tmp_path / "tmp" / job.id).exists()
    conn = sqlite3.connect(tmp_path / "vidyour.db")
    try:
        status, error = conn.execute(
            "SELECT status, error FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()
    finally:
        conn.close()
    assert status == "failed"
    assert error == "\n".join(f"line {i}" for i in range(11, 31))
    assert final is not None and final.status == "failed" and final.error == error


def test_local_storage_persists_render_on_disk(tmp_path: Path) -> None:
    Settings(data_dir=tmp_path, _env_file=None).ensure_data_dirs()
    source = tmp_path / "tmp" / "out.mp4"
    payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
    source.write_bytes(payload)

    stored = LocalStorageProvider(data_dir=tmp_path).store_render(
        source, project_id="proj-1", job_id="job-123", project_name="Demo",
        duration_sec=12.5,
    )

    destination = tmp_path / "renders" / "job-123.mp4"
    assert destination.exists()
    assert destination.read_bytes() == payload
    assert stored is not None
    assert stored.storage_backend == "local"
    assert stored.size_bytes == len(payload) == destination.stat().st_size
    assert stored.download_url == "/api/renders/job-123/download"
