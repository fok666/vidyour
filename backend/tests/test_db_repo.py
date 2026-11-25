"""Unit tests for the SQLite schema and repository (fake sqlite3 connection)."""

import json
import re
import sqlite3

import pytest

from app.db import Database
from app.models import (
    Composition,
    DetectedPart,
    Job,
    MediaAsset,
    Project,
    TimelineAudioClip,
    TimelineVisualClip,
)

# ---------------------------------------------------------------------------
# Fake sqlite3 connection / cursor (owned by this module)
# ---------------------------------------------------------------------------

SCHEMA_COLUMNS = {
    "projects": [
        "id", "name", "bpm", "beat_offset_sec", "resolution_w", "resolution_h", "fps",
        "ai_context", "result_target", "suggested_sequence_json", "created_at", "updated_at",
    ],
    "media_assets": [
        "id", "project_id", "kind", "source", "filename", "local_path", "still_path",
        "thumbnail_path", "duration_sec", "width", "height", "fps", "has_audio", "bpm",
        "beat_grid_json", "metadata_json", "created_at",
    ],
    "detected_parts": [
        "id", "project_id", "asset_id", "part_type", "start_sec", "end_sec", "beat_length",
        "label", "score", "ai_reason", "suggested_effect", "clip_path", "thumbnail_path",
        "created_at",
    ],
    "compositions": ["project_id", "snap_subdivision", "tracks_json", "updated_at"],
    "jobs": [
        "id", "project_id", "kind", "status", "progress", "message", "result_json", "error",
        "created_at", "updated_at",
    ],
}


def norm(sql):
    return " ".join(sql.split()).lower()


class FakeRow(tuple):
    """Behaves like sqlite3.Row: index access, key access and keys()."""

    def __new__(cls, columns, values):
        row = super().__new__(cls, values)
        row._columns = list(columns)
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._columns.index(key))
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._columns)


def _table_of(sql):
    s = norm(sql)
    for pattern in (r"\binto\s+(\w+)", r"^update\s+(\w+)", r"\bfrom\s+(\w+)"):
        m = re.search(pattern, s)
        if m:
            return m.group(1)
    return None


def _where_columns(sql):
    s = norm(sql)
    if " where " not in s:
        return []
    where = s.split(" where ", 1)[1]
    return re.findall(r"(\w+)\s*=\s*[?:]", where)


def _set_columns(sql):
    s = norm(sql)
    m = re.search(r"\bset\s+(.*?)\s+where\b", s)
    if not m:
        return []
    return re.findall(r"(\w+)\s*=\s*[?:]", m.group(1))


def written_values(sql, params):
    """Map column -> value written by an INSERT or UPDATE statement."""
    s = norm(sql)
    if isinstance(params, dict):
        return dict(params)
    params = list(params or ())
    if s.startswith("update"):
        cols = _set_columns(sql)
        return dict(zip(cols, params))
    table = _table_of(sql)
    m = re.search(r"into\s+\w+\s*\(([^)]*)\)\s*values", s)
    if m:
        cols = [c.strip() for c in m.group(1).split(",")]
    else:
        cols = SCHEMA_COLUMNS.get(table, [])
    return dict(zip(cols, params))


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.rowcount = -1
        self.description = None
        self.lastrowid = None

    def execute(self, sql, params=()):
        self.conn.statements.append((sql, params))
        s = norm(sql)
        table = _table_of(sql)
        self._rows = []
        self.description = None
        if s.startswith("select"):
            cols = SCHEMA_COLUMNS.get(table, [])
            rows = self.conn._matching(table, sql, params)
            self._rows = [FakeRow(cols, [r.get(c) for c in cols]) for r in rows]
            self.description = [(c, None, None, None, None, None, None) for c in cols]
            self.rowcount = -1
        elif s.startswith("delete"):
            rows = self.conn._matching(table, sql, params)
            self.conn.tables[table] = [r for r in self.conn.tables.get(table, []) if r not in rows]
            self.rowcount = len(rows)
        elif s.startswith("update"):
            rows = self.conn._matching(table, sql, params, skip=len(_set_columns(sql)))
            values = written_values(sql, params)
            for r in rows:
                r.update({k: v for k, v in values.items() if k in SCHEMA_COLUMNS.get(table, [])})
            self.rowcount = len(rows)
        elif s.startswith("insert") or s.startswith("replace"):
            values = written_values(sql, params)
            cols = SCHEMA_COLUMNS.get(table, [])
            new = {c: values.get(c) for c in cols}
            key = cols[0] if cols else None
            existing = self.conn.tables.setdefault(table, [])
            existing[:] = [r for r in existing if key is None or r.get(key) != new.get(key)]
            existing.append(new)
            self.rowcount = 1
        else:
            self.rowcount = -1
        return self

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)
        return self

    def executescript(self, script):
        self.conn.scripts.append(script)
        self.conn.statements.append((script, ()))
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        pass


class FakeConnection:
    def __init__(self, tables=None):
        self.statements = []
        self.scripts = []
        self.commits = 0
        self.row_factory = None
        self.closed = False
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def _matching(self, table, sql, params, skip=0):
        rows = self.tables.get(table, [])
        if isinstance(params, dict):
            cols = _where_columns(sql)
            values = [params.get(c) for c in cols]
        else:
            cols = _where_columns(sql)
            values = list(params or ())[skip:skip + len(cols)]
        return [r for r in rows if all(r.get(c) == v for c, v in zip(cols, values))]

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, params=()):
        return FakeCursor(self).execute(sql, params)

    def executemany(self, sql, seq):
        return FakeCursor(self).executemany(sql, seq)

    def executescript(self, script):
        return FakeCursor(self).executescript(script)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        return False

    # helpers for assertions
    def find(self, prefix, table):
        out = []
        for sql, params in self.statements:
            s = norm(sql)
            if s.startswith(prefix) and _table_of(sql) == table:
                out.append((sql, params))
        return out


CLOCK = "2026-01-01T00:00:00Z"
CLOCK2 = "2026-01-02T00:00:00Z"


def make_db(conn, clock=CLOCK, new_id="proj-1"):
    return Database(lambda: conn, clock=lambda: clock, id_generator=lambda: new_id)


def project_row(**over):
    row = {
        "id": "proj-1", "name": "Road Trip", "bpm": 120.0, "beat_offset_sec": 0.0,
        "resolution_w": 1280, "resolution_h": 720, "fps": 30, "ai_context": "",
        "result_target": "seamless_flow", "suggested_sequence_json": "[]",
        "created_at": CLOCK, "updated_at": CLOCK,
    }
    row.update(over)
    return row


def asset_row(**over):
    row = {
        "id": "asset-1", "project_id": "proj-1", "kind": "video", "source": "local",
        "filename": "clip.mp4", "local_path": "assets/clip.mp4", "still_path": None,
        "thumbnail_path": None, "duration_sec": 5.0, "width": 1920, "height": 1080,
        "fps": 30.0, "has_audio": 0, "bpm": None, "beat_grid_json": None,
        "metadata_json": "{}", "created_at": CLOCK,
    }
    row.update(over)
    return row


def part_row(**over):
    row = {
        "id": "part-1", "project_id": "proj-1", "asset_id": "asset-1",
        "part_type": "visual_clip", "start_sec": 1.0, "end_sec": 3.0, "beat_length": 4.0,
        "label": "jump", "score": 0.9, "ai_reason": "", "suggested_effect": "none",
        "clip_path": None, "thumbnail_path": None, "created_at": CLOCK,
    }
    row.update(over)
    return row


def job_row(**over):
    row = {
        "id": "job-1", "project_id": "proj-1", "kind": "analyze", "status": "queued",
        "progress": 0, "message": "", "result_json": "{}", "error": None,
        "created_at": CLOCK, "updated_at": CLOCK,
    }
    row.update(over)
    return row


def visual_clip(**over):
    data = {
        "id": "v1", "asset_id": "asset-1", "part_id": None, "kind": "video",
        "start_sec": 0.0, "duration_sec": 2.0, "trim_start_sec": 0.0, "speed": 1.0,
        "transition_in": "none", "transition_duration_sec": 0.5, "ken_burns": "none",
        "color_preset": "original", "volume": 0.0, "muted": False,
        "fade_in_sec": 0.5, "fade_out_sec": 0.0,
    }
    data.update(over)
    return data


def audio_clip(**over):
    data = {
        "id": "a1", "asset_id": "asset-2", "part_id": None, "track_index": 0,
        "start_sec": 0.0, "duration_sec": 8.0, "trim_start_sec": 0.0, "volume": 1.0,
        "muted": False, "fade_in_sec": 0.0, "fade_out_sec": 1.0,
    }
    data.update(over)
    return data


def select_params(conn, table):
    selects = conn.find("select", table)
    return [tuple(p) if not isinstance(p, dict) else p for _, p in selects]


# ---------------------------------------------------------------------------
# Behavior 1: schema DDL
# ---------------------------------------------------------------------------

EXPECTED_TABLE_INFO = {
    # (name, type, notnull, dflt_value, pk)
    "projects": [
        ("id", "TEXT", 1, None, 1),
        ("name", "TEXT", 1, None, 0),
        ("bpm", "REAL", 1, "120.0", 0),
        ("beat_offset_sec", "REAL", 1, "0.0", 0),
        ("resolution_w", "INTEGER", 1, "1280", 0),
        ("resolution_h", "INTEGER", 1, "720", 0),
        ("fps", "INTEGER", 1, "30", 0),
        ("ai_context", "TEXT", 1, "''", 0),
        ("result_target", "TEXT", 1, "'seamless_flow'", 0),
        ("suggested_sequence_json", "TEXT", 1, "'[]'", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
    "media_assets": [
        ("id", "TEXT", 1, None, 1),
        ("project_id", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("source", "TEXT", 1, None, 0),
        ("filename", "TEXT", 1, None, 0),
        ("local_path", "TEXT", 1, None, 0),
        ("still_path", "TEXT", 0, None, 0),
        ("thumbnail_path", "TEXT", 0, None, 0),
        ("duration_sec", "REAL", 1, None, 0),
        ("width", "INTEGER", 0, None, 0),
        ("height", "INTEGER", 0, None, 0),
        ("fps", "REAL", 0, None, 0),
        ("has_audio", "INTEGER", 1, "0", 0),
        ("bpm", "REAL", 0, None, 0),
        ("beat_grid_json", "TEXT", 0, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
        ("created_at", "TEXT", 1, None, 0),
    ],
    "detected_parts": [
        ("id", "TEXT", 1, None, 1),
        ("project_id", "TEXT", 1, None, 0),
        ("asset_id", "TEXT", 1, None, 0),
        ("part_type", "TEXT", 1, None, 0),
        ("start_sec", "REAL", 1, None, 0),
        ("end_sec", "REAL", 1, None, 0),
        ("beat_length", "REAL", 1, None, 0),
        ("label", "TEXT", 1, None, 0),
        ("score", "REAL", 1, None, 0),
        ("ai_reason", "TEXT", 1, "''", 0),
        ("suggested_effect", "TEXT", 1, "'none'", 0),
        ("clip_path", "TEXT", 0, None, 0),
        ("thumbnail_path", "TEXT", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
    ],
    "compositions": [
        ("project_id", "TEXT", 1, None, 1),
        ("snap_subdivision", "TEXT", 1, "'beat'", 0),
        ("tracks_json", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
    "jobs": [
        ("id", "TEXT", 1, None, 1),
        ("project_id", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("progress", "INTEGER", 1, "0", 0),
        ("message", "TEXT", 1, "''", 0),
        ("result_json", "TEXT", 1, "'{}'", 0),
        ("error", "TEXT", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ],
}

EXPECTED_FKS = {
    # table -> set of (from, ref_table, to, on_delete)
    "projects": set(),
    "media_assets": {("project_id", "projects", "id", "CASCADE")},
    "detected_parts": {
        ("project_id", "projects", "id", "CASCADE"),
        ("asset_id", "media_assets", "id", "CASCADE"),
    },
    "compositions": {("project_id", "projects", "id", "CASCADE")},
    "jobs": {("project_id", "projects", "id", "CASCADE")},
}


def replay(conn):
    real = sqlite3.connect(":memory:")
    for sql, params in conn.statements:
        if sql in conn.scripts:
            real.executescript(sql)
        else:
            real.execute(sql, params or ())
    return real


def test_init_db_enables_foreign_keys_and_creates_schema_with_cascades():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)

    # WHEN
    db.init_db()

    # THEN
    all_sql = " ; ".join(norm(sql) for sql, _ in conn.statements)
    assert "pragma foreign_keys = on" in all_sql.replace("foreign_keys=on", "foreign_keys = on")
    assert all_sql.count("on delete cascade") >= 5

    real = replay(conn)
    for table, expected in EXPECTED_TABLE_INFO.items():
        info = real.execute(f"PRAGMA table_info({table})").fetchall()
        actual = [(r[1], r[2], r[3], r[4], r[5]) for r in info]
        assert actual == expected, table
        fks = real.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        actual_fks = {(r[3], r[2], r[4], r[6]) for r in fks}
        assert actual_fks == EXPECTED_FKS[table], table


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_create_project_inserts_row_and_returns_project_with_defaults():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn, clock=CLOCK, new_id="proj-1")

    # WHEN
    project = db.create_project(name="Road Trip")

    # THEN
    assert isinstance(project, Project)
    assert (
        project.id, project.name, project.bpm, project.resolution_w,
        project.resolution_h, project.fps, project.result_target, project.created_at,
    ) == ("proj-1", "Road Trip", 120.0, 1280, 720, 30, "seamless_flow", CLOCK)
    inserts = conn.find("insert", "projects")
    assert len(inserts) == 1
    values = written_values(*inserts[0])
    assert values["id"] == "proj-1"
    assert values["name"] == "Road Trip"


def test_save_composition_writes_snap_and_tracks_json():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)
    composition = Composition(
        project_id="proj-1",
        snap_subdivision="beat",
        video_clips=[TimelineVisualClip(**visual_clip(fade_in_sec=0.5))],
        audio_clips=[],
        updated_at=CLOCK,
    )

    # WHEN
    db.save_composition(composition)

    # THEN
    writes = conn.find("insert", "compositions") + conn.find("update", "compositions")
    assert writes, "no write to compositions"
    values = written_values(*writes[-1])
    assert values["snap_subdivision"] == "beat"
    tracks = json.loads(values["tracks_json"])
    clips = tracks["video_clips"]
    assert [c["id"] for c in clips] == ["v1"]
    assert clips[0]["fade_in_sec"] == 0.5


def test_get_composition_returns_stored_composition():
    # GIVEN
    tracks = {"video_clips": [visual_clip()], "audio_clips": [audio_clip()]}
    conn = FakeConnection(
        {
            "compositions": [
                {
                    "project_id": "proj-1",
                    "snap_subdivision": "beat",
                    "tracks_json": json.dumps(tracks),
                    "updated_at": CLOCK,
                }
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    composition = db.get_composition("proj-1")

    # THEN
    assert isinstance(composition, Composition)
    assert composition.project_id == "proj-1"
    assert composition.snap_subdivision == "beat"
    assert [c.id for c in composition.video_clips] == ["v1"]
    assert [c.id for c in composition.audio_clips] == ["a1"]


def test_delete_project_deletes_by_id_and_returns_true():
    # GIVEN
    conn = FakeConnection({"projects": [project_row()]})
    db = make_db(conn)

    # WHEN
    result = db.delete_project("proj-1")

    # THEN
    deletes = [(norm(s), tuple(p)) for s, p in conn.find("delete", "projects")]
    assert deletes == [("delete from projects where id = ?", ("proj-1",))]
    assert result is True


def test_delete_project_returns_false_for_nonexistent_project():
    # GIVEN
    db = make_db(FakeConnection())

    # WHEN
    result = db.delete_project("missing")

    # THEN
    assert result is False


def test_get_project_selects_by_id_and_maps_row():
    # GIVEN
    conn = FakeConnection(
        {
            "projects": [
                project_row(
                    bpm=128.0, ai_context="fun", result_target="perfect_loop",
                    suggested_sequence_json='["part-1"]',
                )
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    project = db.get_project("proj-1")

    # THEN
    assert ("proj-1",) in select_params(conn, "projects")
    assert any("id" in _where_columns(s) for s, _ in conn.find("select", "projects"))
    assert isinstance(project, Project)
    assert (
        project.id, project.name, project.bpm, project.ai_context,
        project.result_target, project.suggested_sequence,
    ) == ("proj-1", "Road Trip", 128.0, "fun", "perfect_loop", ["part-1"])


def test_get_project_missing_returns_none():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)

    # WHEN
    project = db.get_project("missing")

    # THEN
    assert project is None


def test_list_projects_returns_all_rows_as_projects():
    # GIVEN
    conn = FakeConnection(
        {
            "projects": [
                project_row(id="proj-1", name="Trip A", created_at="2026-01-01T00:00:00Z"),
                project_row(id="proj-2", name="Trip B", created_at="2026-01-01T00:00:01Z"),
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    projects = db.list_projects()

    # THEN
    assert len(projects) == 2
    assert all(isinstance(p, Project) for p in projects)
    assert sorted(p.id for p in projects) == ["proj-1", "proj-2"]
    assert sorted(p.name for p in projects) == ["Trip A", "Trip B"]


def test_list_projects_returns_empty_list_for_initialized_empty_database():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)
    db.init_db()

    # WHEN
    projects = db.list_projects()

    # THEN
    assert projects == []


def test_update_project_writes_fields_and_returns_updated_project():
    # GIVEN
    conn = FakeConnection({"projects": [project_row(name="Road Trip", bpm=120.0)]})
    db = make_db(conn, clock=CLOCK2)

    # WHEN
    project = db.update_project(
        "proj-1",
        name="Beach Day",
        bpm=128.0,
        beat_offset_sec=0.25,
        resolution_w=1920,
        resolution_h=1080,
        ai_context="upbeat summer",
        result_target="perfect_loop",
        suggested_sequence=["part-1"],
    )

    # THEN
    updates = conn.find("update", "projects")
    assert updates, "no UPDATE on projects"
    written = {}
    for sql, params in updates:
        written.update(written_values(sql, params))
        assert "id" in _where_columns(sql)
        assert "proj-1" in (params.values() if isinstance(params, dict) else params)
    assert written["name"] == "Beach Day"
    assert written["bpm"] == 128.0
    assert written["beat_offset_sec"] == 0.25
    assert written["resolution_w"] == 1920
    assert written["resolution_h"] == 1080
    assert written["ai_context"] == "upbeat summer"
    assert written["result_target"] == "perfect_loop"
    assert json.loads(written["suggested_sequence_json"]) == ["part-1"]
    assert written["updated_at"] == CLOCK2
    assert conn.commits >= 1
    assert isinstance(project, Project)
    assert (
        project.name, project.bpm, project.beat_offset_sec, project.resolution_w,
        project.resolution_h, project.ai_context, project.result_target,
        project.suggested_sequence, project.updated_at,
    ) == (
        "Beach Day", 128.0, 0.25, 1920, 1080, "upbeat summer", "perfect_loop",
        ["part-1"], CLOCK2,
    )


def test_update_project_missing_returns_none():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn, clock=CLOCK2)

    # WHEN
    project = db.update_project("missing", bpm=128.0)

    # THEN
    assert project is None


def test_update_project_rejects_unknown_field():
    db = make_db(FakeConnection(), clock=CLOCK2)

    with pytest.raises(ValueError, match=r"Unknown project fields: \['unknown'\]"):
        db.update_project("proj-1", unknown="value")


# ---------------------------------------------------------------------------
# Media assets
# ---------------------------------------------------------------------------


def test_create_media_asset_serializes_json_and_bool_columns():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)
    asset = MediaAsset(
        id="asset-1", project_id="proj-1", kind="audio", source="audio_catalog",
        filename="song.mp3", local_path="assets/song.mp3", still_path=None,
        thumbnail_path=None, duration_sec=30.0, width=None, height=None, fps=None,
        has_audio=True, bpm=124.0, beat_grid={"beats": [0.0, 0.48]},
        metadata={"license": "CC0"}, created_at=CLOCK,
    )

    # WHEN
    result = db.create_media_asset(asset)

    # THEN
    inserts = conn.find("insert", "media_assets")
    assert len(inserts) == 1
    values = written_values(*inserts[0])
    assert values["id"] == "asset-1"
    assert values["has_audio"] == 1
    assert values["beat_grid_json"] == '{"beats": [0.0, 0.48]}'
    assert values["metadata_json"] == '{"license": "CC0"}'
    assert conn.commits >= 1
    assert result == asset


def test_get_media_asset_maps_row_to_model():
    # GIVEN
    conn = FakeConnection(
        {
            "media_assets": [
                asset_row(
                    id="asset-1", project_id="proj-1", kind="live_photo",
                    filename="IMG_1.MOV", local_path="assets/IMG_1.MOV",
                    still_path="assets/IMG_1.JPG", has_audio=0, beat_grid_json=None,
                    metadata_json='{"live": true}',
                )
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    asset = db.get_media_asset("asset-1")

    # THEN
    assert isinstance(asset, MediaAsset)
    assert (
        asset.id, asset.project_id, asset.kind, asset.local_path, asset.still_path,
        asset.has_audio, asset.beat_grid, asset.metadata,
    ) == (
        "asset-1", "proj-1", "live_photo", "assets/IMG_1.MOV", "assets/IMG_1.JPG",
        False, None, {"live": True},
    )


def test_get_media_asset_missing_returns_none():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)

    # WHEN
    asset = db.get_media_asset("missing")

    # THEN
    assert asset is None


def test_list_media_assets_filters_by_project():
    # GIVEN
    conn = FakeConnection(
        {
            "media_assets": [
                asset_row(id="asset-1", filename="clip.mp4", created_at="2026-01-01T00:00:00Z"),
                asset_row(
                    id="asset-2", kind="photo", filename="pic.png", local_path="assets/pic.png",
                    created_at="2026-01-01T00:00:01Z",
                ),
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    assets = db.list_media_assets("proj-1")

    # THEN
    selects = conn.find("select", "media_assets")
    assert any("project_id" in _where_columns(s) for s, _ in selects)
    assert ("proj-1",) in select_params(conn, "media_assets")
    assert len(assets) == 2
    assert all(isinstance(a, MediaAsset) for a in assets)
    assert [a.filename for a in assets] == ["clip.mp4", "pic.png"]


def test_delete_media_asset_deletes_by_id_commits_and_returns_true():
    # GIVEN
    conn = FakeConnection({"media_assets": [asset_row(id="asset-1")]})
    db = make_db(conn)

    # WHEN
    result = db.delete_media_asset("asset-1")

    # THEN
    deletes = [(norm(s), tuple(p)) for s, p in conn.find("delete", "media_assets")]
    assert deletes == [("delete from media_assets where id = ?", ("asset-1",))]
    assert conn.commits >= 1
    assert result is True


def test_delete_media_asset_missing_returns_false():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)

    # WHEN
    result = db.delete_media_asset("missing")

    # THEN
    assert result is False


# ---------------------------------------------------------------------------
# Detected parts
# ---------------------------------------------------------------------------


def test_create_detected_part_inserts_and_returns_part():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)
    part = DetectedPart(
        id="part-1", project_id="proj-1", asset_id="asset-1", part_type="visual_clip",
        start_sec=1.0, end_sec=3.0, beat_length=4.0, label="jump", score=0.9,
        ai_reason="peak action", suggested_effect="none", clip_path="clips/part-1.mp4",
        thumbnail_path="thumbs/part-1.jpg", created_at=CLOCK,
    )

    # WHEN
    result = db.create_detected_part(part)

    # THEN
    inserts = conn.find("insert", "detected_parts")
    assert len(inserts) == 1
    values = written_values(*inserts[0])
    assert (
        values["id"], values["asset_id"], values["start_sec"], values["end_sec"],
        values["score"],
    ) == ("part-1", "asset-1", 1.0, 3.0, 0.9)
    assert conn.commits >= 1
    assert result == part


def test_list_detected_parts_filters_by_project():
    # GIVEN
    conn = FakeConnection(
        {
            "detected_parts": [
                part_row(id="part-1", part_type="visual_clip", created_at="2026-01-01T00:00:00Z"),
                part_row(id="part-2", part_type="audio_segment", created_at="2026-01-01T00:00:01Z"),
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    parts = db.list_detected_parts("proj-1")

    # THEN
    selects = conn.find("select", "detected_parts")
    assert any("project_id" in _where_columns(s) for s, _ in selects)
    assert ("proj-1",) in select_params(conn, "detected_parts")
    assert len(parts) == 2
    assert all(isinstance(p, DetectedPart) for p in parts)
    assert [p.id for p in parts] == ["part-1", "part-2"]
    assert [p.part_type for p in parts] == ["visual_clip", "audio_segment"]


def test_delete_detected_part_deletes_by_id_commits_and_returns_true():
    # GIVEN
    conn = FakeConnection({"detected_parts": [part_row(id="part-1")]})
    db = make_db(conn)

    # WHEN
    result = db.delete_detected_part("part-1")

    # THEN
    deletes = [(norm(s), tuple(p)) for s, p in conn.find("delete", "detected_parts")]
    assert deletes == [("delete from detected_parts where id = ?", ("part-1",))]
    assert conn.commits >= 1
    assert result is True


def test_delete_detected_part_missing_returns_false():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)

    # WHEN
    result = db.delete_detected_part("missing")

    # THEN
    assert result is False


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_create_job_inserts_and_returns_job():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)
    job = Job(
        id="job-1", project_id="proj-1", kind="analyze", status="queued", progress=0,
        message="", result={}, error=None, created_at=CLOCK, updated_at=CLOCK,
    )

    # WHEN
    result = db.create_job(job)

    # THEN
    inserts = conn.find("insert", "jobs")
    assert len(inserts) == 1
    values = written_values(*inserts[0])
    assert (
        values["id"], values["kind"], values["status"], values["progress"],
        values["result_json"], values["error"],
    ) == ("job-1", "analyze", "queued", 0, "{}", None)
    assert conn.commits >= 1
    assert result == job


def test_get_job_maps_row_to_model():
    # GIVEN
    conn = FakeConnection(
        {
            "jobs": [
                job_row(
                    id="job-a1", status="running", progress=75,
                    message="Extracting thumbnails", result_json='{"parts_count": 3}',
                    error=None,
                )
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    job = db.get_job("job-a1")

    # THEN
    assert isinstance(job, Job)
    assert (
        job.id, job.status, job.progress, job.message, job.result, job.error,
    ) == ("job-a1", "running", 75, "Extracting thumbnails", {"parts_count": 3}, None)


def test_get_job_missing_returns_none():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn)

    # WHEN
    job = db.get_job("missing")

    # THEN
    assert job is None


def test_list_jobs_filters_by_project_and_decodes_result():
    # GIVEN
    conn = FakeConnection(
        {
            "jobs": [
                job_row(id="job-1", kind="analyze", status="completed",
                        created_at="2026-01-01T00:00:00Z"),
                job_row(
                    id="job-2", kind="render", status="completed",
                    result_json='{"download_url": "/api/renders/job-2/download"}',
                    created_at="2026-01-01T00:00:01Z",
                ),
            ]
        }
    )
    db = make_db(conn)

    # WHEN
    jobs = db.list_jobs("proj-1")

    # THEN
    selects = conn.find("select", "jobs")
    assert any("project_id" in _where_columns(s) for s, _ in selects)
    assert ("proj-1",) in select_params(conn, "jobs")
    assert len(jobs) == 2
    assert all(isinstance(j, Job) for j in jobs)
    assert [j.id for j in jobs] == ["job-1", "job-2"]
    assert jobs[1].result == {"download_url": "/api/renders/job-2/download"}


def test_update_job_writes_fields_and_returns_updated_job():
    # GIVEN
    conn = FakeConnection(
        {"jobs": [job_row(id="job-1", status="running", progress=50, message="Slicing")]}
    )
    db = make_db(conn, clock=CLOCK2)

    # WHEN
    job = db.update_job(
        "job-1",
        status="failed",
        progress=100,
        message="Analysis failed",
        result={"parts_count": 3},
        error="boom",
    )

    # THEN
    updates = conn.find("update", "jobs")
    assert updates, "no UPDATE on jobs"
    written = {}
    for sql, params in updates:
        written.update(written_values(sql, params))
        assert "id" in _where_columns(sql)
        assert "job-1" in (params.values() if isinstance(params, dict) else params)
    assert written["status"] == "failed"
    assert written["progress"] == 100
    assert written["message"] == "Analysis failed"
    assert json.loads(written["result_json"]) == {"parts_count": 3}
    assert written["error"] == "boom"
    assert written["updated_at"] == CLOCK2
    assert conn.commits >= 1
    assert isinstance(job, Job)
    assert (
        job.id, job.status, job.progress, job.message, job.result, job.error, job.updated_at,
    ) == ("job-1", "failed", 100, "Analysis failed", {"parts_count": 3}, "boom", CLOCK2)


def test_update_job_missing_returns_none():
    # GIVEN
    conn = FakeConnection()
    db = make_db(conn, clock=CLOCK2)

    # WHEN
    job = db.update_job("missing", status="failed")

    # THEN
    assert job is None


def test_update_job_rejects_unknown_field():
    db = make_db(FakeConnection(), clock=CLOCK2)

    with pytest.raises(ValueError, match=r"Unknown job fields: \['unknown'\]"):
        db.update_job("job-1", unknown="value")
