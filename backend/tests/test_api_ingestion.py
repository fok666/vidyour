"""Integration tests for the ingestion API mounted with real app collaborators."""

import io
import json
import shutil
import sqlite3
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import app.api.media as media_api
from app.api import AppContext, mount_ingestion_routers
from app.api.gallery_oauth import OAuthTransactionStore
from app.api.media import create_media_router
from app.api.projects import create_projects_router
from app.config import Settings, open_private_file
from app.db import Database
from app.jobs import JobManager
from app.models import AudioCatalogTrack, MediaAsset
from app.providers.audio import (
    BuiltinDemoAudioProvider,
    FreesoundAudioProvider,
    JamendoAudioProvider,
    synthesize_track,
)
from app.providers.gallery.google_photos import GooglePhotosPickerProvider
from app.services.audio_analyzer import AudioAnalyzer


def _wav_bytes(bpm: float = 120.0) -> bytes:
    track = synthesize_track(bpm)
    samples = (track.pcm.clip(-1, 1) * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(track.sample_rate)
        stream.writeframes(samples.tobytes())
    return output.getvalue()


def _image_bytes(fmt: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 20, 10)).save(output, format=fmt)
    return output.getvalue()


def _video_bytes(path: Path, duration: float = 2.0) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for media API integration tests")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s=160x120:r=24:d={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


class MemoryTokenStore:
    def __init__(self, token=None):
        self.token = token

    def load(self):
        return self.token

    def save(self, token):
        self.token = dict(token)

    def clear(self):
        self.token = None


class Rig:
    def __init__(self, tmp_path: Path):
        self.settings = Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            cors_origins="",
            ai_provider="local",
            gemini_api_key="",
            bedrock_model_id="",
            aws_access_key_id="",
            aws_secret_access_key="",
            google_client_id="",
            google_client_secret="",
            google_redirect_uri="",
            jamendo_client_id="",
            freesound_api_key="",
            s3_bucket="",
        )
        self.settings.ensure_data_dirs()
        db_path = tmp_path / "integration.sqlite3"
        self.repository = Database(lambda: sqlite3.connect(db_path))
        self.repository.init_db()
        self.job_manager = JobManager(
            max_workers=1,
            data_dir=self.settings.data_dir,
            repository=self.repository,
        )
        self.analyzer = AudioAnalyzer(data_dir=self.settings.data_dir)
        self.audio_providers = [
            BuiltinDemoAudioProvider(data_dir=self.settings.data_dir)
        ]
        self.context = AppContext(
            settings=self.settings,
            repository=self.repository,
            job_manager=self.job_manager,
            audio_providers=self.audio_providers,
            analyzer=self.analyzer,
        )

    def close(self):
        self.job_manager.shutdown()


@pytest.fixture
def rig(tmp_path):
    instance = Rig(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def client(rig):
    application = FastAPI()
    mount_ingestion_routers(application, rig.context)
    with TestClient(application) as test_client:
        yield test_client


def _project(client, name="Trip"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def test_capabilities_and_project_crud_use_context_and_real_sqlite(client):
    capabilities = client.get("/api/config/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json() == {
        "google_photos": False,
        "gemini": False,
        "bedrock": False,
        "jamendo": False,
        "freesound": False,
        "s3": False,
        "active_ai_provider": "local",
        "storage_backend": "local",
    }

    created = _project(client)
    assert created["name"] == "Trip"
    assert created["bpm"] == 120.0
    assert created["resolution_w"] == 1280
    assert created["resolution_h"] == 720
    assert created["result_target"] == "seamless_flow"

    _project(client, "Trip A")
    _project(client, "Trip B")
    updated = client.patch(
        f"/api/projects/{created['id']}",
        json={"bpm": 128.0, "ai_context": "fun", "result_target": "perfect_loop"},
    )
    assert updated.status_code == 200
    assert updated.json()["bpm"] == 128.0
    saved = client.get(f"/api/projects/{created['id']}").json()
    assert saved["bpm"] == 128.0
    assert saved["result_target"] == "perfect_loop"

    deleted = client.delete(f"/api/projects/{created['id']}")
    assert deleted.status_code in {200, 204}
    assert client.get(f"/api/projects/{created['id']}").status_code == 404
    listed = client.get("/api/projects").json()
    assert len(listed) == 2
    assert {item["name"] for item in listed} == {"Trip A", "Trip B"}


@pytest.mark.parametrize(
    "payload",
    [{}, {"name": ""}, {"name": "  "}, {"name": None}, {"name": 123}],
)
def test_create_project_rejects_missing_or_invalid_names(client, payload):
    response = client.post("/api/projects", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "Project name is required"}


def test_project_endpoints_report_errors_when_repository_is_unavailable():
    application = FastAPI()
    application.include_router(create_projects_router(), prefix="/api")

    with TestClient(application) as unavailable_client:
        create = unavailable_client.post("/api/projects", json={"name": "Trip"})
        assert create.status_code == 503
        assert create.json() == {"detail": "Project repository unavailable"}

        listing = unavailable_client.get("/api/projects")
        assert listing.status_code == 503
        assert listing.json() == {"detail": "Project repository unavailable"}

        missing = unavailable_client.get("/api/projects/missing")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Project not found"}

        update = unavailable_client.patch(
            "/api/projects/missing", json={"bpm": 128.0}
        )
        assert update.status_code == 503
        assert update.json() == {"detail": "Project repository unavailable"}

        delete = unavailable_client.delete("/api/projects/missing")
        assert delete.status_code == 503
        assert delete.json() == {"detail": "Project repository unavailable"}


def test_project_update_and_delete_report_invalid_or_missing_projects(client):
    project = _project(client)
    invalid_update = client.patch(
        f"/api/projects/{project['id']}", json={"unknown_field": "value"}
    )
    assert invalid_update.status_code == 422
    assert invalid_update.json() == {"detail": "Invalid project update"}

    missing_update = client.patch(
        "/api/projects/missing", json={"bpm": 128.0}
    )
    assert missing_update.status_code == 404
    assert missing_update.json() == {"detail": "Project not found"}

    missing_delete = client.delete("/api/projects/missing")
    assert missing_delete.status_code == 404
    assert missing_delete.json() == {"detail": "Project not found"}


def test_upload_batch_pairs_live_photo_and_persists_audio_analysis(rig, client, tmp_path):
    project = _project(client)
    normal_video = _video_bytes(tmp_path / "clip.mp4", 3.0)
    live_motion = _video_bytes(tmp_path / "IMG_1.MOV", 2.0)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[
            ("files", ("clip.mp4", normal_video, "video/mp4")),
            ("files", ("pic.png", _image_bytes(), "image/png")),
            ("files", ("IMG_1.JPG", _image_bytes("JPEG"), "image/jpeg")),
            ("files", ("IMG_1.MOV", live_motion, "video/quicktime")),
            ("files", ("song.wav", _wav_bytes(), "audio/wav")),
        ],
    )
    assert response.status_code == 200, response.text
    assets = response.json()
    assert len(assets) == 4
    live = next(asset for asset in assets if asset["kind"] == "live_photo")
    assert live["local_path"].startswith("assets/")
    assert live["still_path"].startswith("assets/")
    audio = next(asset for asset in assets if asset["kind"] == "audio")
    assert audio["bpm"] == pytest.approx(120.0, abs=1.5)
    assert audio["beat_grid"]["beat_times"]
    saved_project = client.get(f"/api/projects/{project['id']}").json()
    assert saved_project["bpm"] == pytest.approx(120.0, abs=1.5)
    assert saved_project["beat_offset_sec"] == pytest.approx(
        audio["beat_grid"]["beat_offset_sec"]
    )
    assert (rig.settings.data_dir / audio["local_path"]).is_file()


def test_later_audio_analysis_preserves_user_edited_project_bpm_and_offset(
    rig, client, monkeypatch
):
    project = _project(client)
    analyses = iter(
        [
            SimpleNamespace(
                bpm=124.0,
                beat_offset_sec=0.2,
                beat_times=[0.2, 0.684],
                downbeats=[0.2],
                waveform=[],
                sections=[],
            ),
            SimpleNamespace(
                bpm=132.0,
                beat_offset_sec=0.1,
                beat_times=[0.1, 0.555],
                downbeats=[0.1],
                waveform=[],
                sections=[],
            ),
        ]
    )
    monkeypatch.setattr(
        rig.analyzer, "analyze_audio", lambda _path: next(analyses)
    )

    first_upload = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("first.wav", _wav_bytes(), "audio/wav"))],
    )
    assert first_upload.status_code == 200, first_upload.text
    first_audio = first_upload.json()[0]
    assert first_audio["beat_grid"]["bpm"] == pytest.approx(124.0)
    assert first_audio["beat_grid"]["beat_offset_sec"] == pytest.approx(0.2)
    first_project = client.get(f"/api/projects/{project['id']}").json()
    assert first_project["bpm"] == pytest.approx(124.0)
    assert first_project["beat_offset_sec"] == pytest.approx(0.2)

    edited = client.patch(
        f"/api/projects/{project['id']}",
        json={"bpm": 98.5, "beat_offset_sec": 0.73},
    )
    assert edited.status_code == 200
    assert edited.json()["bpm"] == pytest.approx(98.5)
    assert edited.json()["beat_offset_sec"] == pytest.approx(0.73)

    later_upload = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("later.wav", _wav_bytes(132.0), "audio/wav"))],
    )
    assert later_upload.status_code == 200, later_upload.text
    later_audio = later_upload.json()[0]
    assert later_audio["beat_grid"]["bpm"] == pytest.approx(132.0)
    assert later_audio["beat_grid"]["beat_offset_sec"] == pytest.approx(0.1)
    saved_project = client.get(f"/api/projects/{project['id']}").json()
    assert saved_project["bpm"] == pytest.approx(98.5)
    assert saved_project["beat_offset_sec"] == pytest.approx(0.73)


@pytest.mark.parametrize(
    "analysis_result",
    [
        pytest.param(None, id="no-beat-grid"),
        pytest.param(SimpleNamespace(bpm=None), id="no-bpm"),
    ],
)
def test_audio_upload_without_analysis_keeps_existing_project_bpm(
    rig, client, monkeypatch, analysis_result
):
    project = _project(client)
    existing_bpm = 96.5
    update = client.patch(
        f"/api/projects/{project['id']}", json={"bpm": existing_bpm}
    )
    assert update.status_code == 200
    monkeypatch.setattr(
        rig.analyzer, "analyze_audio", lambda _path: analysis_result
    )

    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("song.wav", _wav_bytes(), "audio/wav"))],
    )

    assert response.status_code == 200, response.text
    audio = response.json()[0]
    assert audio["kind"] == "audio"
    assert audio["bpm"] is None
    assert audio["beat_grid"] is None
    saved_project = client.get(f"/api/projects/{project['id']}").json()
    assert saved_project["bpm"] == pytest.approx(existing_bpm)


def test_heic_upload_converts_to_jpeg_and_creates_thumbnail(rig, client):
    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("IMG_99.HEIC", _image_bytes("JPEG"), "image/heic"))],
    )
    assert response.status_code == 200, response.text
    asset = response.json()[0]
    assert asset["kind"] == "photo"
    assert asset["local_path"].startswith("assets/")
    assert asset["local_path"].endswith(".jpg")
    assert asset["thumbnail_path"].startswith("thumbs/")
    assert (rig.settings.data_dir / asset["local_path"]).is_file()
    assert (rig.settings.data_dir / asset["thumbnail_path"]).is_file()
    assert list((rig.settings.data_dir / "tmp").iterdir()) == []


def test_heic_live_photo_upload_converts_and_stores_paired_still(
    rig, client, tmp_path, monkeypatch
):
    project = _project(client)
    still_bytes = _image_bytes("JPEG")
    live_motion = _video_bytes(tmp_path / "IMG_100.MOV", 2.0)
    convert_image = media_api.convert_image_to_jpeg
    converted_sources = []

    def track_conversion(source_path, output_path, *args, **kwargs):
        converted_sources.append(Path(source_path).suffix.lower())
        return convert_image(source_path, output_path, *args, **kwargs)

    monkeypatch.setattr(media_api, "convert_image_to_jpeg", track_conversion)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[
            ("files", ("IMG_100.HEIC", still_bytes, "image/heic")),
            ("files", ("IMG_100.MOV", live_motion, "video/quicktime")),
        ],
    )

    assert response.status_code == 200, response.text
    assets = response.json()
    assert len(assets) == 1
    live = assets[0]
    assert live["kind"] == "live_photo"
    assert live["filename"] == "IMG_100.jpg"
    assert live["local_path"].startswith("assets/")
    assert live["still_path"].startswith("assets/")
    assert live["still_path"].endswith("_still.jpg")
    assert live["thumbnail_path"].startswith("thumbs/")
    assert ".heic" in converted_sources

    stored_still = rig.settings.data_dir / live["still_path"]
    assert stored_still.is_file()
    with Image.open(stored_still) as image:
        assert image.format == "JPEG"
        assert image.size == (40, 30)
    assert (rig.settings.data_dir / live["local_path"]).is_file()
    assert (rig.settings.data_dir / live["thumbnail_path"]).is_file()
    assert list((rig.settings.data_dir / "tmp").iterdir()) == []


def test_upload_rejects_unsupported_extension(client):
    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("virus.exe", b"no executable", "application/octet-stream"))],
    )
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Unsupported media extension: .exe"
    }


def test_media_upload_validates_repository_project_and_empty_batch(rig):
    unavailable_app = FastAPI()
    unavailable_app.include_router(
        create_media_router(app_settings=rig.settings), prefix="/api"
    )
    with TestClient(unavailable_app) as unavailable_client:
        unavailable = unavailable_client.post(
            "/api/projects/project-1/media/upload",
            files=[("files", ("pic.png", _image_bytes(), "image/png"))],
        )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Media repository unavailable"}

    application = FastAPI()
    application.include_router(
        create_media_router(rig.repository, rig.settings), prefix="/api"
    )
    with TestClient(application) as media_client:
        missing_project = media_client.post(
            "/api/projects/missing/media/upload",
            files=[("files", ("pic.png", _image_bytes(), "image/png"))],
        )
        project = rig.repository.create_project("Upload validation")
        empty_batch = media_client.post(
            f"/api/projects/{project.id}/media/upload"
        )

    assert missing_project.status_code == 404
    assert missing_project.json() == {"detail": "Project not found"}
    assert empty_batch.status_code == 400
    assert empty_batch.json() == {"detail": "No media files provided"}
    assert rig.repository.list_media_assets(project.id) == []


def test_media_list_and_delete_report_unavailable_repository(rig):
    application = FastAPI()
    application.include_router(
        create_media_router(app_settings=rig.settings), prefix="/api"
    )

    with TestClient(application) as unavailable_client:
        listing = unavailable_client.get("/api/projects/project-1/media")
        deletion = unavailable_client.delete("/api/media/asset-1")

    assert listing.status_code == 503
    assert listing.json() == {"detail": "Media repository unavailable"}
    assert deletion.status_code == 503
    assert deletion.json() == {"detail": "Media repository unavailable"}


def test_media_list_delete_and_file_serving_report_missing_resources(rig, client):
    missing_project = client.get("/api/projects/missing/media")
    missing_asset = client.delete("/api/media/missing")
    missing_file = client.get("/api/files/assets/not-present.mp4")

    assert missing_project.status_code == 404
    assert missing_project.json() == {"detail": "Project not found"}
    assert missing_asset.status_code == 404
    assert missing_asset.json() == {"detail": "Media not found"}
    assert missing_file.status_code == 404
    assert missing_file.json() == {"detail": "File not found"}


def test_media_upload_collaborator_failure_cleans_staged_and_persisted_files(
    rig, client, monkeypatch
):
    project = _project(client)

    def fail_create(_asset):
        raise OSError("database unavailable")

    monkeypatch.setattr(rig.repository, "create_media_asset", fail_create)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("pic.png", _image_bytes(), "image/png"))],
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Unable to process uploaded media"}
    assert rig.repository.list_media_assets(project["id"]) == []
    assert list((rig.settings.data_dir / "assets").iterdir()) == []
    assert list((rig.settings.data_dir / "thumbs").iterdir()) == []
    assert list((rig.settings.data_dir / "tmp").iterdir()) == []


def test_video_upload_preserves_valid_video_when_thumbnail_generation_fails(
    rig, client, monkeypatch
):
    project = _project(client)
    video_bytes = b"valid video content"
    monkeypatch.setattr(
        media_api,
        "probe_media",
        lambda *_args, **_kwargs: SimpleNamespace(
            duration_sec=2.0,
            width=160,
            height=120,
            fps=24.0,
            has_audio=False,
        ),
    )

    def fail_thumbnail(*_args, **_kwargs):
        raise RuntimeError("preview generation failed")

    monkeypatch.setattr(media_api, "generate_thumbnail", fail_thumbnail)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("clip.mp4", video_bytes, "video/mp4"))],
    )

    assert response.status_code == 200, response.text
    asset = response.json()[0]
    assert asset["kind"] == "video"
    assert asset["thumbnail_path"] is None
    assert (rig.settings.data_dir / asset["local_path"]).read_bytes() == video_bytes
    assert rig.repository.get_media_asset(asset["id"]) is not None


def test_media_store_cleanup_os_errors_do_not_mask_upload_failure(
    rig, client, monkeypatch
):
    project = _project(client)
    original_unlink = Path.unlink
    failed_cleanup_paths = []
    monkeypatch.setattr(
        media_api,
        "probe_media",
        lambda *_args, **_kwargs: SimpleNamespace(
            duration_sec=2.0,
            width=160,
            height=120,
            fps=24.0,
            has_audio=False,
        ),
    )

    def write_thumbnail(_video_path, output_path, *_args, **_kwargs):
        Path(output_path).write_bytes(b"thumbnail")

    monkeypatch.setattr(media_api, "generate_thumbnail", write_thumbnail)

    def fail_create(_asset):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(rig.repository, "create_media_asset", fail_create)

    def fail_internal_unlink(path, *args, **kwargs):
        if path.parent.name in {"assets", "thumbs"} or path.name.endswith(
            "_thumb.jpg"
        ):
            failed_cleanup_paths.append(path)
            raise OSError("filesystem is read-only")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_internal_unlink)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("clip.mp4", b"video", "video/mp4"))],
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Unable to process uploaded media"}
    assert {path.parent.name for path in failed_cleanup_paths} == {
        "assets",
        "thumbs",
        "tmp",
    }
    assert len(failed_cleanup_paths) == 3
    assert all(path.is_file() for path in failed_cleanup_paths)
    assert rig.repository.list_media_assets(project["id"]) == []


def test_media_upload_passes_through_store_http_exception(rig, client, monkeypatch):
    from fastapi import HTTPException

    project = _project(client)

    def reject_store(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="Storage conflict")

    monkeypatch.setattr(media_api, "store_media_file", reject_store)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[("files", ("pic.png", _image_bytes(), "image/png"))],
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Storage conflict"}
    assert rig.repository.list_media_assets(project["id"]) == []
    assert list((rig.settings.data_dir / "tmp").iterdir()) == []


def test_media_upload_rolls_back_prior_assets_after_later_processing_failure(
    rig, client, monkeypatch
):
    project = _project(client)
    store = media_api.store_media_file
    calls = 0

    def fail_second_store(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("processing failed")
        return store(*args, **kwargs)

    monkeypatch.setattr(media_api, "store_media_file", fail_second_store)
    response = client.post(
        f"/api/projects/{project['id']}/media/upload",
        files=[
            ("files", ("first.png", _image_bytes(), "image/png")),
            ("files", ("second.png", _image_bytes(), "image/png")),
        ],
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Unable to process uploaded media"}
    assert calls == 2
    assert rig.repository.list_media_assets(project["id"]) == []
    assert list((rig.settings.data_dir / "assets").iterdir()) == []
    assert list((rig.settings.data_dir / "thumbs").iterdir()) == []
    assert list((rig.settings.data_dir / "tmp").iterdir()) == []


def test_media_delete_keeps_private_paths_and_survives_file_cleanup_errors(
    rig, client, monkeypatch
):
    project = _project(client)
    private = rig.settings.data_dir / "tmp" / "keep-private.txt"
    private.write_text("private")
    asset = MediaAsset(
        id="unsafe-path-asset",
        project_id=project["id"],
        kind="photo",
        source="upload",
        filename="private.txt",
        local_path="../tmp/keep-private.txt",
        still_path=None,
        thumbnail_path=None,
        duration_sec=0.0,
        width=None,
        height=None,
        fps=None,
        has_audio=False,
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at=rig.repository.clock(),
    )
    rig.repository.create_media_asset(asset)
    invalid_path_delete = client.delete(f"/api/media/{asset.id}")
    assert invalid_path_delete.status_code == 204
    assert private.read_text() == "private"

    media_file = rig.settings.data_dir / "assets" / "cannot-delete.png"
    media_file.write_bytes(b"image")
    second_asset = asset.model_copy(
        update={
            "id": "unlink-error-asset",
            "filename": "cannot-delete.png",
            "local_path": "assets/cannot-delete.png",
        }
    )
    rig.repository.create_media_asset(second_asset)

    def fail_unlink(_path, *args, **kwargs):
        raise OSError("filesystem is read-only")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    failed_cleanup = client.delete(f"/api/media/{second_asset.id}")

    assert failed_cleanup.status_code == 204
    assert failed_cleanup.content == b""
    assert rig.repository.get_media_asset(second_asset.id) is None
    assert media_file.read_bytes() == b"image"


def test_media_list_delete_and_file_cleanup_use_real_repository(rig, client):
    project = _project(client)
    data_dir = rig.settings.data_dir
    first_path = data_dir / "assets" / "clip.mp4"
    second_path = data_dir / "assets" / "pic.png"
    first_path.write_bytes(b"clip")
    second_path.write_bytes(b"picture")
    now = rig.repository.clock()
    assets = [
        MediaAsset(
            id="clip-asset",
            project_id=project["id"],
            kind="video",
            source="upload",
            filename="clip.mp4",
            local_path="assets/clip.mp4",
            still_path=None,
            thumbnail_path=None,
            duration_sec=2.0,
            width=160,
            height=120,
            fps=24.0,
            has_audio=False,
            bpm=None,
            beat_grid=None,
            metadata={},
            created_at=now,
        ),
        MediaAsset(
            id="pic-asset",
            project_id=project["id"],
            kind="photo",
            source="upload",
            filename="pic.png",
            local_path="assets/pic.png",
            still_path=None,
            thumbnail_path=None,
            duration_sec=4.0,
            width=40,
            height=30,
            fps=None,
            has_audio=False,
            bpm=None,
            beat_grid=None,
            metadata={},
            created_at=now,
        ),
    ]
    for asset in assets:
        rig.repository.create_media_asset(asset)

    listed = client.get(f"/api/projects/{project['id']}/media")
    assert listed.status_code == 200
    assert {asset["filename"] for asset in listed.json()} == {"clip.mp4", "pic.png"}
    deleted = client.delete("/api/media/clip-asset")
    assert deleted.status_code == 204
    assert client.get(f"/api/projects/{project['id']}/media").json() == [
        assets[1].model_dump()
    ]
    assert not first_path.exists()
    assert second_path.exists()


def test_file_streaming_supports_ranges_and_rejects_traversal_and_private_dirs(
    rig, client
):
    path = rig.settings.data_dir / "assets" / "range.bin"
    payload = bytes(range(256))
    path.write_bytes(payload)

    response = client.get("/api/files/assets/range.bin", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-99/{len(payload)}"
    assert response.content == payload[:100]

    for directory in ("clips", "thumbs"):
        public_path = rig.settings.data_dir / directory / "public.bin"
        public_path.write_bytes(b"public")
        allowed = client.get(f"/api/files/{directory}/public.bin")
        assert allowed.status_code == 200
        assert allowed.content == b"public"

    traversal = client.get("/api/files/..%2F..%2Fetc%2Fpasswd")
    assert traversal.status_code == 400
    assert traversal.json() == {"detail": "Invalid file path"}
    for forbidden in ("database/private.db", "tmp/private", "renders/private.mp4"):
        response = client.get(f"/api/files/{forbidden}")
        assert response.status_code == 404
        assert response.json() == {"detail": "File not found"}


@pytest.mark.parametrize(
    "range_header",
    [
        "items=0-1",
        "bytes=",
        "bytes=-",
        "bytes=0-1,3-4",
        "bytes=256-",
        "bytes=4-2",
        "bytes=not-a-range",
        "bytes=-0",
    ],
)
def test_file_streaming_rejects_malformed_empty_multi_and_unsatisfiable_ranges(
    rig, client, range_header
):
    path = rig.settings.data_dir / "assets" / "range-errors.bin"
    payload = b"012345"
    path.write_bytes(payload)

    response = client.get(
        "/api/files/assets/range-errors.bin",
        headers={"Range": range_header},
    )

    assert response.status_code == 416
    assert response.content == b""
    assert response.headers["content-range"] == f"bytes */{len(payload)}"
    assert response.headers["accept-ranges"] == "bytes"


def test_file_streaming_supports_suffix_and_empty_file_ranges(rig, client):
    path = rig.settings.data_dir / "assets" / "range-boundaries.bin"
    payload = bytes(range(32))
    path.write_bytes(payload)
    suffix = client.get(
        "/api/files/assets/range-boundaries.bin",
        headers={"Range": "bytes=-7"},
    )
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == "bytes 25-31/32"
    assert suffix.headers["content-length"] == "7"
    assert suffix.content == payload[-7:]

    empty_path = rig.settings.data_dir / "assets" / "empty.bin"
    empty_path.write_bytes(b"")
    full_empty = client.get("/api/files/assets/empty.bin")
    assert full_empty.status_code == 200
    assert full_empty.content == b""
    empty_range = client.get(
        "/api/files/assets/empty.bin",
        headers={"Range": "bytes=-1"},
    )
    assert empty_range.status_code == 416
    assert empty_range.content == b""
    assert empty_range.headers["content-range"] == "bytes */0"


def test_file_streaming_reports_truncated_range_read_length(rig, client, monkeypatch):
    path = rig.settings.data_dir / "assets" / "truncated.bin"
    path.write_bytes(b"abcdef")
    original_open = Path.open

    def truncated_open(candidate, *args, **kwargs):
        if candidate == path:
            return io.BytesIO(b"ab")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", truncated_open)
    response = client.get(
        "/api/files/assets/truncated.bin",
        headers={"Range": "bytes=0-5"},
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 0-5/6"
    assert response.headers["content-length"] == "2"
    assert response.content == b"ab"


def test_file_streaming_rejects_symlink_into_private_storage(rig, client):
    private = rig.settings.data_dir / "tmp" / "secret.txt"
    private.write_text("not public")
    link = rig.settings.data_dir / "assets" / "shortcut.txt"
    link.symlink_to(private)

    response = client.get("/api/files/assets/shortcut.txt")
    assert response.status_code == 400


def test_file_streaming_rejects_symlinked_servable_directory(rig, client):
    private_dir = rig.settings.data_dir / "tmp" / "private-clips"
    private_dir.mkdir()
    (private_dir / "secret.txt").write_text("private")
    clips_dir = rig.settings.data_dir / "clips"
    clips_dir.rmdir()
    clips_dir.symlink_to(private_dir, target_is_directory=True)

    response = client.get("/api/files/clips/secret.txt")

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid file path"}


def test_google_oauth_popup_uses_real_router_provider_and_transaction_store(rig):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={"access_token": "access-token", "token_type": "Bearer"},
            )
        return httpx.Response(404)

    settings = rig.settings.model_copy(
        update={
            "google_client_id": "client-id",
            "google_client_secret": "client-secret",
            "google_redirect_uri": "https://localhost/api/gallery/google/callback",
        }
    )
    token_store = MemoryTokenStore()
    upstream = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GooglePhotosPickerProvider(
        settings,
        client=upstream,
        token_store=token_store,
    )
    rig.context.settings = settings
    rig.context.oauth_provider = provider
    rig.context.oauth_transaction_store = OAuthTransactionStore()
    app = FastAPI()
    mount_ingestion_routers(app, rig.context)
    with TestClient(app, base_url="https://testserver") as browser:
        auth = browser.get("/api/gallery/google/auth-url")
        assert auth.status_code == 200
        parsed = urlparse(auth.json()["auth_url"])
        query = parse_qs(parsed.query)
        assert "photospicker.mediaitems.readonly" in query["scope"][0]
        state = query["state"][0]
        assert state
        assert browser.cookies.get("browser_cookie")

        callback = browser.get(
            "/api/gallery/google/callback",
            params={"code": "code-123", "state": state},
        )
        assert callback.status_code == 200
        assert "window.opener.postMessage" in callback.text
        assert "window.close()" in callback.text
        status = browser.get("/api/gallery/google/status")
        assert status.json()["connected"] is True
    assert len([request for request in requests if request.url.host == "oauth2.googleapis.com"]) == 1
    upstream.close()


def test_oauth_invalid_states_are_generic_single_use_and_browser_bound(rig):
    exchanges = []

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            exchanges.append(request)
            return httpx.Response(200, json={"access_token": "ok"})
        return httpx.Response(404)

    settings = rig.settings.model_copy(
        update={
            "google_client_id": "cid",
            "google_client_secret": "secret",
            "google_redirect_uri": "https://localhost/api/gallery/google/callback",
        }
    )
    upstream = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GooglePhotosPickerProvider(
        settings,
        client=upstream,
        token_store=MemoryTokenStore(),
    )
    rig.context.settings = settings
    rig.context.oauth_provider = provider
    rig.context.oauth_transaction_store = OAuthTransactionStore()
    app = FastAPI()
    mount_ingestion_routers(app, rig.context)
    with TestClient(app, base_url="https://testserver") as browser_a, TestClient(
        app, base_url="https://testserver"
    ) as browser_b:
        missing_state = browser_a.get(
            "/api/gallery/google/callback", params={"code": "code-123"}
        )
        auth_a = browser_a.get("/api/gallery/google/auth-url")
        auth_b = browser_b.get("/api/gallery/google/auth-url")
        state_a = parse_qs(urlparse(auth_a.json()["auth_url"]).query)["state"][0]

        foreign = browser_b.get(
            "/api/gallery/google/callback",
            params={"code": "code-123", "state": state_a},
        )
        assert foreign.status_code == missing_state.status_code
        assert foreign.content == missing_state.content
        assert exchanges == []

        valid = browser_a.get(
            "/api/gallery/google/callback",
            params={"code": "code-123", "state": state_a},
        )
        assert valid.status_code == 200
        replay = browser_a.get(
            "/api/gallery/google/callback",
            params={"code": "code-123", "state": state_a},
        )
        assert replay.status_code == missing_state.status_code
        assert replay.content == missing_state.content
        assert len(exchanges) == 1
        assert auth_b.status_code == 200
    upstream.close()


def test_google_picker_sessions_and_async_import_use_real_provider_and_job_manager(
    rig, client
):
    image = _image_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            return httpx.Response(
                200,
                json={
                    "id": "sess-1",
                    "pickerUri": "https://photos.google.com/picker/sess-1",
                },
            )
        if request.url.path.endswith("/v1/sessions/sess-1"):
            return httpx.Response(200, json={"mediaItemsSet": True})
        if request.url.path.endswith("/v1/mediaItems"):
            return httpx.Response(
                200,
                json={
                    "mediaItems": [
                        {
                            "id": "google-item-1",
                            "type": "PHOTO",
                            "mediaFile": {
                                "filename": "google.png",
                                "baseUrl": "https://lh3.googleusercontent.com/google-item",
                            },
                        }
                    ]
                },
            )
        if request.url.host == "lh3.googleusercontent.com":
            return httpx.Response(200, content=image)
        return httpx.Response(404)

    token_store = MemoryTokenStore({"access_token": "connected"})
    upstream = httpx.Client(transport=httpx.MockTransport(handler))

    def writer(path, content):
        with open_private_file(path) as output:
            output.write(content)

    picker = GooglePhotosPickerProvider(
        rig.settings,
        client=upstream,
        token_store=token_store,
        file_writer=writer,
        sleep=lambda _delay: None,
    )
    rig.context.gallery_provider = picker
    app = FastAPI()
    mount_ingestion_routers(app, rig.context)
    project = _project(client)
    with TestClient(app) as test_client:
        created = test_client.post("/api/gallery/google/sessions")
        assert created.status_code == 200
        assert created.json() == {
            "session_id": "sess-1",
            "picker_uri": "https://photos.google.com/picker/sess-1",
        }
        response = test_client.post(
            f"/api/gallery/google/sessions/sess-1/import?project_id={project['id']}"
        )
        assert response.status_code == 200
        job = rig.job_manager.wait(response.json()["id"], timeout=10)
        assert job.status == "completed"
        assets = rig.repository.list_media_assets(project["id"])
        assert len(assets) == 1
        assert assets[0].source == "google_photos"
        assert assets[0].width == 40
    assert any(request.url.host == "lh3.googleusercontent.com" for request in requests)
    upstream.close()


def test_google_photos_session_creation_reports_unavailable_and_sanitizes_failure(
    rig,
):
    unavailable_app = FastAPI()
    mount_ingestion_routers(
        unavailable_app,
        AppContext(settings=rig.settings, repository=rig.repository),
    )
    with TestClient(unavailable_app) as unavailable_client:
        unavailable = unavailable_client.post("/api/gallery/google/sessions")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Google Photos is unavailable"}

    class BrokenPicker:
        def create_session(self):
            raise RuntimeError("private token and upstream response")

    failed_app = FastAPI()
    mount_ingestion_routers(
        failed_app,
        AppContext(settings=rig.settings, gallery_provider=BrokenPicker()),
    )
    with TestClient(failed_app) as failed_client:
        failed = failed_client.post("/api/gallery/google/sessions")
    assert failed.status_code == 502
    assert failed.json() == {
        "detail": "Google Photos session creation failed"
    }
    assert "private token" not in failed.text
    assert "upstream response" not in failed.text


@pytest.mark.parametrize("missing", ["gallery_provider", "repository", "job_manager"])
def test_google_photos_import_reports_unavailable_collaborators(rig, client, missing):
    project = _project(client)

    class Picker:
        def download_session_items(self, _session_id, _output_dir):
            pytest.fail("unavailable import must not download items")

    context = AppContext(
        settings=rig.settings,
        repository=rig.repository,
        job_manager=rig.job_manager,
        gallery_provider=Picker(),
    )
    setattr(context, missing, None)
    app = FastAPI()
    mount_ingestion_routers(app, context)

    with TestClient(app) as test_client:
        response = test_client.post(
            f"/api/gallery/google/sessions/sess-1/import?project_id={project['id']}"
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Google Photos is unavailable"}


def test_google_photos_import_rejects_invalid_session_and_missing_project(rig, client):
    project = _project(client)
    downloads = []

    class Picker:
        def download_session_items(self, session_id, _output_dir):
            downloads.append(session_id)
            return []

    rig.context.gallery_provider = Picker()
    app = FastAPI()
    mount_ingestion_routers(app, rig.context)

    with TestClient(app) as test_client:
        invalid_session = test_client.post(
            f"/api/gallery/google/sessions/bad%21/import?project_id={project['id']}"
        )
        missing_project = test_client.post(
            "/api/gallery/google/sessions/sess-1/import?project_id=missing-project"
        )

    assert invalid_session.status_code == 400
    assert invalid_session.json() == {"detail": "Invalid session id"}
    assert missing_project.status_code == 404
    assert missing_project.json() == {"detail": "Project not found"}
    assert downloads == []


@pytest.mark.parametrize(
    "returned_item",
    ["empty", "missing_path", "missing_file", "directory", "outside"],
)
def test_google_photos_import_skips_or_sanitizes_unsafe_items_and_cleans_job_tmp(
    rig, client, tmp_path, returned_item
):
    project = _project(client)
    outside_path = tmp_path / "outside.png"
    outside_path.write_bytes(b"private external file")
    directory_path = rig.settings.data_dir / "tmp" / "not-a-media-file"
    directory_path.mkdir(parents=True, exist_ok=True)
    items = {
        "empty": [],
        "missing_path": [{"id": "missing-path"}],
        "missing_file": [{"id": "missing-file", "path": "assets/missing.png"}],
        "directory": [{"id": "directory", "path": str(directory_path)}],
        "outside": [{"id": "outside", "path": str(outside_path)}],
    }[returned_item]

    class Picker:
        def download_session_items(self, _session_id, _output_dir):
            return items

    rig.context.gallery_provider = Picker()
    app = FastAPI()
    mount_ingestion_routers(app, rig.context)

    with TestClient(app) as test_client:
        response = test_client.post(
            f"/api/gallery/google/sessions/sess-1/import?project_id={project['id']}"
        )
        assert response.status_code == 200
        job = rig.job_manager.wait(response.json()["id"], timeout=10)

    assert job is not None
    assert not rig.job_manager.tmp_dir(job.id).exists()
    assert rig.repository.list_media_assets(project["id"]) == []
    if returned_item == "outside":
        assert job.status == "failed"
        assert job.error == "Google Photos import failed"
        assert str(outside_path) not in job.error
        assert outside_path.read_bytes() == b"private external file"
    else:
        assert job.status == "completed"
        assert job.result == {"asset_ids": []}


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_detail"),
    [
        ("exception", 502, "Google Photos import could not be started"),
        ("none", 503, "Google Photos import could not be started"),
    ],
)
def test_google_photos_import_sanitizes_submit_failures(
    rig, client, outcome, expected_status, expected_detail
):
    project = _project(client)

    class Picker:
        def download_session_items(self, _session_id, _output_dir):
            pytest.fail("failed submission must not start a download")

    class BrokenJobManager:
        def submit(self, _project_id, _kind, _worker):
            if outcome == "exception":
                raise RuntimeError("private job manager detail")
            return None

    app = FastAPI()
    mount_ingestion_routers(
        app,
        AppContext(
            settings=rig.settings,
            repository=rig.repository,
            job_manager=BrokenJobManager(),
            gallery_provider=Picker(),
        ),
    )

    with TestClient(app) as test_client:
        response = test_client.post(
            f"/api/gallery/google/sessions/sess-1/import?project_id={project['id']}"
        )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "private job manager detail" not in response.text


def test_google_photos_import_sanitizes_download_failure_and_cleans_job_tmp(
    rig, client
):
    project = _project(client)

    class BrokenPicker:
        def download_session_items(self, _session_id, _output_dir):
            raise RuntimeError("private token and upstream response")

    rig.context.gallery_provider = BrokenPicker()
    app = FastAPI()
    mount_ingestion_routers(app, rig.context)

    with TestClient(app) as test_client:
        response = test_client.post(
            f"/api/gallery/google/sessions/sess-1/import?project_id={project['id']}"
        )
        assert response.status_code == 200
        job = rig.job_manager.wait(response.json()["id"], timeout=10)

    assert job is not None
    assert job.status == "failed"
    assert job.error == "Google Photos import failed"
    assert "private token" not in job.error
    assert "upstream response" not in job.error
    assert not rig.job_manager.tmp_dir(job.id).exists()
    assert rig.repository.list_media_assets(project["id"]) == []


def test_builtin_audio_search_import_persists_analyzed_asset_and_project_bpm(
    rig, client
):
    project = _project(client)
    search = client.get(
        "/api/audio-catalog/search?bpm_min=115&bpm_max=130&provider=builtin"
    )
    assert search.status_code == 200
    assert [track["id"] for track in search.json()] == ["builtin-124"]

    imported = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={"track_id": "builtin-124", "provider": "builtin"},
    )
    assert imported.status_code == 200, imported.text
    asset = imported.json()
    assert asset["kind"] == "audio"
    assert asset["source"] == "audio_catalog"
    assert asset["bpm"] == pytest.approx(124.0, abs=1.5)
    assert asset["beat_grid"]["beat_times"]
    assert (rig.settings.data_dir / asset["local_path"]).is_file()
    assert rig.repository.get_project(project["id"]).bpm == pytest.approx(
        124.0, abs=1.5
    )


@pytest.mark.parametrize(
    ("provider_name", "track_id"),
    [("jamendo", "jam-1"), ("freesound", "fs-1")],
)
def test_remote_audio_import_resolves_id_and_uses_stubbed_provider_download(
    rig, client, provider_name, track_id
):
    audio_bytes = _wav_bytes()
    requests = []
    if provider_name == "jamendo":
        rig.settings.jamendo_client_id = "dummy-client-id"

        def handler(request):
            requests.append(request)
            if request.url.host == "api.jamendo.com":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": track_id,
                                "name": "Provider Song",
                                "artist_name": "Provider Artist",
                                "bpm": 120,
                                "duration": 40,
                                "audio": "https://prod-1.storage.jamendo.com/preview",
                                "audiodownload": f"https://prod-1.storage.jamendo.com/{track_id}.wav",
                                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                            }
                        ]
                    },
                )
            if request.url.host == "prod-1.storage.jamendo.com":
                return httpx.Response(200, content=audio_bytes)
            return httpx.Response(404)

        provider = JamendoAudioProvider(
            rig.settings,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        expected_download = f"https://prod-1.storage.jamendo.com/{track_id}.wav"
    else:
        rig.settings.freesound_api_key = "dummy-api-key"

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/search/text/"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": track_id,
                                "name": "Provider Song",
                                "username": "Provider Artist",
                                "duration": 40,
                                "license": "https://creativecommons.org/publicdomain/zero/1.0/",
                                "previews": {
                                    "preview-hq-mp3": "https://downloads.freesound.org/preview"
                                },
                                "download": f"https://downloads.freesound.org/{track_id}.wav",
                            }
                        ]
                    },
                )
            if request.url.path.endswith(f"/sounds/{track_id}/"):
                return httpx.Response(
                    200,
                    json={
                        "id": track_id,
                        "download": f"https://downloads.freesound.org/{track_id}.wav",
                    },
                )
            if request.url.host == "downloads.freesound.org":
                return httpx.Response(200, content=audio_bytes)
            return httpx.Response(404)

        provider = FreesoundAudioProvider(
            rig.settings,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        expected_download = f"https://downloads.freesound.org/{track_id}.wav"

    rig.context.audio_providers.append(provider)
    project = _project(client)
    imported = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={
            "track_id": track_id,
            "provider": provider_name,
            "download_url": "https://attacker.invalid/ignore-me",
        },
    )
    assert imported.status_code == 200, imported.text
    asset = imported.json()
    assert asset["project_id"] == project["id"]
    assert asset["kind"] == "audio"
    assert asset["source"] == "audio_catalog"
    assert asset["metadata"]["license"]
    assert asset["metadata"]["attribution"]
    assert asset["beat_grid"]["beat_times"]
    assert asset["bpm"] == pytest.approx(120.0, abs=1.5)
    assert rig.repository.get_media_asset(asset["id"]) is not None
    assert rig.repository.get_project(project["id"]).bpm == pytest.approx(
        120.0, abs=1.5
    )
    assert any(str(request.url) == expected_download for request in requests)
    assert not any(request.url.host == "attacker.invalid" for request in requests)


def test_audio_import_passes_through_store_http_exception(rig, client, monkeypatch):
    from fastapi import HTTPException

    class StoreFailureProvider:
        name = "store-failure"

        def search(self):
            return [
                AudioCatalogTrack(
                    id="track-1",
                    provider=self.name,
                    title="Track",
                    artist="",
                    bpm=None,
                    duration_sec=0,
                    preview_url="",
                    download_url="",
                    license="CC0",
                    attribution="",
                )
            ]

        def download_track(self, _track_id, output_dir):
            path = Path(output_dir) / "track.wav"
            path.write_bytes(b"audio")
            return path

    def reject_store(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="Storage conflict")

    monkeypatch.setattr(media_api, "store_media_file", reject_store)
    rig.context.audio_providers.append(StoreFailureProvider())
    project = _project(client)

    response = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={"track_id": "track-1", "provider": "store-failure"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Storage conflict"}


def test_audio_provider_errors_are_sanitized_and_untrusted_urls_are_not_downloaded(
    rig, client
):
    class BrokenProvider:
        name = "broken"

        def search(self, **_kwargs):
            from app.models import AudioCatalogTrack

            return [
                AudioCatalogTrack(
                    id="safe-id",
                    provider="broken",
                    title="Broken",
                    artist="",
                    bpm=None,
                    duration_sec=0,
                    preview_url="",
                    download_url="https://attacker.invalid/private",
                    license="CC0",
                    attribution="",
                )
            ]

        def download_track(self, _track_id, _output_dir):
            raise RuntimeError("secret token and upstream response")

    rig.context.audio_providers.append(BrokenProvider())
    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={
            "track_id": "safe-id",
            "provider": "broken",
            "download_url": "https://attacker.invalid/override",
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Audio provider import failed"
    assert "secret token" not in response.text
    assert "attacker.invalid" not in response.text


def test_audio_import_reports_unavailable_without_exposing_internal_details(rig):
    app = FastAPI()
    mount_ingestion_routers(
        app,
        AppContext(
            settings=rig.settings,
            audio_providers=rig.audio_providers,
            analyzer=rig.analyzer,
        ),
    )
    with TestClient(app) as unavailable_client:
        response = unavailable_client.post(
            "/api/projects/project-1/audio-catalog/import",
            json={"provider": "builtin", "track_id": "builtin-124"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Audio import is unavailable"


def test_audio_import_reports_missing_project_safely(client):
    response = client.post(
        "/api/projects/missing-project/audio-catalog/import",
        json={"provider": "builtin", "track_id": "builtin-124"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found"


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "builtin"},
        {"provider": 12, "track_id": "builtin-124"},
        {"provider": "builtin", "track_id": "../outside"},
        {"provider": "builtin", "track_id": "x" * 129},
    ],
)
def test_audio_import_rejects_malformed_selection_safely(client, payload):
    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid audio track selection"


@pytest.mark.parametrize(
    ("provider_name", "track_id"),
    [("unknown", "track-1"), ("builtin", "unknown-track")],
)
def test_audio_import_returns_generic_not_found_for_unknown_provider_or_track(
    client, provider_name, track_id
):
    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={"provider": provider_name, "track_id": track_id},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Audio track not found"


def test_audio_import_sanitizes_provider_search_failure(rig, client):
    class SearchFailureProvider:
        name = "search-failure"

        def search(self):
            raise RuntimeError("private credential and upstream response")

    rig.context.audio_providers.append(SearchFailureProvider())
    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={"provider": "search-failure", "track_id": "track-1"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Audio provider request failed"
    assert "private credential" not in response.text
    assert "upstream response" not in response.text


@pytest.mark.parametrize("output_kind", ["outside", "missing", "directory"])
def test_audio_import_rejects_invalid_provider_output_paths_safely(
    rig, client, tmp_path, output_kind
):
    class InvalidOutputProvider:
        name = "invalid-output"

        def search(self):
            return [
                AudioCatalogTrack(
                    id="path-test",
                    provider=self.name,
                    title="Path Test",
                    artist="",
                    bpm=None,
                    duration_sec=0,
                    preview_url="",
                    download_url="https://provider.invalid/private",
                    license="CC0",
                    attribution="",
                )
            ]

        def download_track(self, _track_id, output_dir):
            if output_kind == "outside":
                path = tmp_path / "outside.wav"
                path.write_bytes(b"not imported")
                return path
            if output_kind == "directory":
                path = Path(output_dir) / "not-a-file.wav"
                path.mkdir()
                return path
            return Path(output_dir) / "missing.wav"

    rig.context.audio_providers.append(InvalidOutputProvider())
    project = _project(client)
    response = client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={"provider": "invalid-output", "track_id": "path-test"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Audio provider import failed"
    assert str(tmp_path) not in response.text
    assert "provider.invalid" not in response.text
