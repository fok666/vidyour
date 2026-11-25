"""SQLite schema and connection-bound repository."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from app.models import Composition, DetectedPart, Job, MediaAsset, Project

ConnectionFactory = Callable[[], Any]
"""Zero-argument callable returning a DB-API connection (e.g. ``lambda: sqlite3.connect(path)``)."""


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL,
        bpm REAL NOT NULL DEFAULT 120.0,
        beat_offset_sec REAL NOT NULL DEFAULT 0.0,
        resolution_w INTEGER NOT NULL DEFAULT 1280,
        resolution_h INTEGER NOT NULL DEFAULT 720,
        fps INTEGER NOT NULL DEFAULT 30,
        ai_context TEXT NOT NULL DEFAULT '',
        result_target TEXT NOT NULL DEFAULT 'seamless_flow',
        suggested_sequence_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS media_assets (
        id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        filename TEXT NOT NULL,
        local_path TEXT NOT NULL,
        still_path TEXT,
        thumbnail_path TEXT,
        duration_sec REAL NOT NULL,
        width INTEGER,
        height INTEGER,
        fps REAL,
        has_audio INTEGER NOT NULL DEFAULT 0,
        bpm REAL,
        beat_grid_json TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS detected_parts (
        id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
        part_type TEXT NOT NULL,
        start_sec REAL NOT NULL,
        end_sec REAL NOT NULL,
        beat_length REAL NOT NULL,
        label TEXT NOT NULL,
        score REAL NOT NULL,
        ai_reason TEXT NOT NULL DEFAULT '',
        suggested_effect TEXT NOT NULL DEFAULT 'none',
        clip_path TEXT,
        thumbnail_path TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS compositions (
        project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        snap_subdivision TEXT NOT NULL DEFAULT 'beat',
        tracks_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
]

PROJECT_COLUMNS = [
    "id", "name", "bpm", "beat_offset_sec", "resolution_w", "resolution_h", "fps",
    "ai_context", "result_target", "suggested_sequence_json", "created_at", "updated_at",
]
ASSET_COLUMNS = [
    "id", "project_id", "kind", "source", "filename", "local_path", "still_path",
    "thumbnail_path", "duration_sec", "width", "height", "fps", "has_audio", "bpm",
    "beat_grid_json", "metadata_json", "created_at",
]
PART_COLUMNS = [
    "id", "project_id", "asset_id", "part_type", "start_sec", "end_sec", "beat_length",
    "label", "score", "ai_reason", "suggested_effect", "clip_path", "thumbnail_path",
    "created_at",
]
JOB_COLUMNS = [
    "id", "project_id", "kind", "status", "progress", "message", "result_json", "error",
    "created_at", "updated_at",
]

PROJECT_UPDATABLE = {
    "name", "bpm", "beat_offset_sec", "resolution_w", "resolution_h", "fps",
    "ai_context", "result_target", "suggested_sequence",
}
JOB_UPDATABLE = {"status", "progress", "message", "result", "error"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_sql(table: str, columns: list[str]) -> str:
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )


def _row_to_project(row: Any) -> Project:
    return Project(
        id=row["id"], name=row["name"], bpm=row["bpm"],
        beat_offset_sec=row["beat_offset_sec"], resolution_w=row["resolution_w"],
        resolution_h=row["resolution_h"], fps=row["fps"], ai_context=row["ai_context"],
        result_target=row["result_target"],
        suggested_sequence=json.loads(row["suggested_sequence_json"] or "[]"),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _row_to_asset(row: Any) -> MediaAsset:
    beat_grid = row["beat_grid_json"]
    return MediaAsset(
        id=row["id"], project_id=row["project_id"], kind=row["kind"], source=row["source"],
        filename=row["filename"], local_path=row["local_path"], still_path=row["still_path"],
        thumbnail_path=row["thumbnail_path"], duration_sec=row["duration_sec"],
        width=row["width"], height=row["height"], fps=row["fps"],
        has_audio=bool(row["has_audio"]), bpm=row["bpm"],
        beat_grid=json.loads(beat_grid) if beat_grid is not None else None,
        metadata=json.loads(row["metadata_json"] or "{}"), created_at=row["created_at"],
    )


def _row_to_part(row: Any) -> DetectedPart:
    return DetectedPart(**{c: row[c] for c in PART_COLUMNS})


def _row_to_job(row: Any) -> Job:
    return Job(
        id=row["id"], project_id=row["project_id"], kind=row["kind"], status=row["status"],
        progress=row["progress"], message=row["message"],
        result=json.loads(row["result_json"] or "{}"), error=row["error"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class Database:
    """Repository for projects, media assets, detected parts, compositions, and jobs.

    All collaborators are injected: ``connection_factory`` is called with no
    arguments to obtain a sqlite3-compatible connection; ``clock`` returns an
    ISO-8601 UTC timestamp string; ``id_generator`` returns a new id string.
    """

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        clock: Callable[[], str] | None = None,
        id_generator: Callable[[], str] | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.clock = clock or _utc_now
        self.id_generator = id_generator or (lambda: str(uuid.uuid4()))

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        """Open a connection with row access by name and FK enforcement; always close it."""
        conn = self.connection_factory()
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._connection() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def _fetchone(self, sql: str, params: tuple) -> Any:
        with self._connection() as conn:
            return conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        with self._connection() as conn:
            return list(conn.execute(sql, params).fetchall())

    def _update(
        self, table: str, row_id: str, fields: dict[str, Any],
        updatable: set[str], json_field: str, label: str,
    ) -> bool:
        """Apply a partial UPDATE stamping ``updated_at``; ``json_field`` is stored as ``<name>_json``.

        Returns whether a row matched ``row_id``.
        """
        unknown = set(fields) - updatable
        if unknown:
            raise ValueError(f"Unknown {label} fields: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == json_field:
                values[f"{key}_json"] = json.dumps(value)
            else:
                values[key] = value
        values["updated_at"] = self.clock()
        assignments = ", ".join(f"{column} = ?" for column in values)
        return self._write(
            f"UPDATE {table} SET {assignments} WHERE id = ?", (*values.values(), row_id)
        ) != 0

    def init_db(self) -> None:
        with self._connection() as conn:
            for statement in SCHEMA:
                conn.execute(statement)
            conn.commit()

    # projects
    def create_project(self, name: str) -> Project | None:
        now = self.clock()
        project = Project(id=self.id_generator(), name=name, created_at=now, updated_at=now)
        self._write(
            _insert_sql("projects", PROJECT_COLUMNS),
            (
                project.id, project.name, project.bpm, project.beat_offset_sec,
                project.resolution_w, project.resolution_h, project.fps, project.ai_context,
                project.result_target, json.dumps(project.suggested_sequence),
                project.created_at, project.updated_at,
            ),
        )
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = self._fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _row_to_project(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        rows = self._fetchall("SELECT * FROM projects ORDER BY created_at, id")
        return [_row_to_project(r) for r in rows]

    def update_project(self, project_id: str, **fields: Any) -> Project | None:
        if not self._update(
            "projects", project_id, fields, PROJECT_UPDATABLE, "suggested_sequence", "project"
        ):
            return None
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        return self._write("DELETE FROM projects WHERE id = ?", (project_id,)) > 0

    # compositions
    def save_composition(self, composition: Composition) -> Composition | None:
        tracks = {
            "video_clips": [c.model_dump() for c in composition.video_clips],
            "audio_clips": [c.model_dump() for c in composition.audio_clips],
        }
        self._write(
            "INSERT INTO compositions (project_id, snap_subdivision, tracks_json, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
            "snap_subdivision = excluded.snap_subdivision, "
            "tracks_json = excluded.tracks_json, updated_at = excluded.updated_at",
            (
                composition.project_id, composition.snap_subdivision, json.dumps(tracks),
                composition.updated_at,
            ),
        )
        return composition

    def get_composition(self, project_id: str) -> Composition | None:
        row = self._fetchone("SELECT * FROM compositions WHERE project_id = ?", (project_id,))
        if row is None:
            return None
        tracks = json.loads(row["tracks_json"])
        return Composition(
            project_id=row["project_id"], snap_subdivision=row["snap_subdivision"],
            video_clips=tracks.get("video_clips", []),
            audio_clips=tracks.get("audio_clips", []), updated_at=row["updated_at"],
        )

    # media assets
    def create_media_asset(self, asset: MediaAsset) -> MediaAsset | None:
        self._write(
            _insert_sql("media_assets", ASSET_COLUMNS),
            (
                asset.id, asset.project_id, asset.kind, asset.source, asset.filename,
                asset.local_path, asset.still_path, asset.thumbnail_path, asset.duration_sec,
                asset.width, asset.height, asset.fps, 1 if asset.has_audio else 0, asset.bpm,
                json.dumps(asset.beat_grid) if asset.beat_grid is not None else None,
                json.dumps(asset.metadata), asset.created_at,
            ),
        )
        return asset

    def get_media_asset(self, asset_id: str) -> MediaAsset | None:
        row = self._fetchone("SELECT * FROM media_assets WHERE id = ?", (asset_id,))
        return _row_to_asset(row) if row is not None else None

    def list_media_assets(self, project_id: str) -> list[MediaAsset]:
        rows = self._fetchall(
            "SELECT * FROM media_assets WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        )
        return [_row_to_asset(r) for r in rows]

    def delete_media_asset(self, asset_id: str) -> bool:
        return self._write("DELETE FROM media_assets WHERE id = ?", (asset_id,)) > 0

    # detected parts
    def create_detected_part(self, part: DetectedPart) -> DetectedPart | None:
        self._write(
            _insert_sql("detected_parts", PART_COLUMNS),
            tuple(getattr(part, c) for c in PART_COLUMNS),
        )
        return part

    def list_detected_parts(self, project_id: str) -> list[DetectedPart]:
        rows = self._fetchall(
            "SELECT * FROM detected_parts WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        )
        return [_row_to_part(r) for r in rows]

    def delete_detected_part(self, part_id: str) -> bool:
        return self._write("DELETE FROM detected_parts WHERE id = ?", (part_id,)) > 0

    # jobs
    def create_job(self, job: Job) -> Job | None:
        self._write(
            _insert_sql("jobs", JOB_COLUMNS),
            (
                job.id, job.project_id, job.kind, job.status, job.progress, job.message,
                json.dumps(job.result), job.error, job.created_at, job.updated_at,
            ),
        )
        return job

    def get_job(self, job_id: str) -> Job | None:
        row = self._fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return _row_to_job(row) if row is not None else None

    def list_jobs(self, project_id: str) -> list[Job]:
        rows = self._fetchall(
            "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at, id", (project_id,)
        )
        return [_row_to_job(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> Job | None:
        if not self._update("jobs", job_id, fields, JOB_UPDATABLE, "result", "job"):
            return None
        return self.get_job(job_id)
