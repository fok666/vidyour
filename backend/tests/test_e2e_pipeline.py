"""End-to-end tests for the production FastAPI composition and render pipeline."""

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
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors
from PIL import Image

from app.api.analysis import create_analysis_router
from app.api.compositions import create_compositions_router
from app.api.gallery_oauth import OAuthTransactionStore
from app.api.renders import create_renders_router
from app.config import Settings
from app.db import Database
from app.jobs import JobManager
from app.main import create_app
from app.models import (
    AIMoment,
    AIAnalysisResult,
    Composition,
    DetectedPart,
    Job,
    MediaAsset,
    StoredRender,
    TimelineVisualClip,
)
from app.providers.ai.gemini import GeminiProvider
from app.providers.ai.heuristic import HeuristicFallbackProvider
from app.providers.audio import BuiltinDemoAudioProvider
from app.providers.gallery.google_photos import GooglePhotosPickerProvider
from app.services.audio_analyzer import AudioAnalyzer
from app.services.scene_extractor import SceneExtractor


class MemoryTokenStore:
    def __init__(self) -> None:
        self.token = None

    def load(self):
        return self.token

    def save(self, token):
        self.token = dict(token)

    def clear(self):
        self.token = None


class Rig:
    """Create the production app with real services and a temporary SQLite DB."""

    def __init__(
        self,
        root: Path,
        *,
        oauth_client: httpx.Client | None = None,
        ai_provider=None,
        oauth_settings: bool = False,
        app_settings: Settings | None = None,
    ) -> None:
        self.settings = app_settings or Settings(
            _env_file=None,
            data_dir=root / "data",
            cors_origins="",
            ai_provider="gemini" if isinstance(ai_provider, GeminiProvider) else "local",
            gemini_api_key="test-key" if isinstance(ai_provider, GeminiProvider) else "",
            google_client_id="client-id" if oauth_settings else "",
            google_client_secret="client-secret" if oauth_settings else "",
            google_redirect_uri="http://localhost:8000/api/gallery/google/callback"
            if oauth_settings
            else "",
            jamendo_client_id="",
            freesound_api_key="",
            s3_bucket="",
        )
        self.settings.ensure_data_dirs()
        db_path = root / "integration.sqlite3"
        self.repository = Database(lambda: sqlite3.connect(db_path))
        self.repository.init_db()
        self.job_manager = JobManager(
            max_workers=1,
            data_dir=self.settings.data_dir,
            repository=self.repository,
        )
        self.analyzer = AudioAnalyzer(data_dir=self.settings.data_dir)
        self.audio_provider = BuiltinDemoAudioProvider(
            data_dir=self.settings.data_dir
        )
        self.token_store = MemoryTokenStore()
        self.gallery_provider = GooglePhotosPickerProvider(
            self.settings,
            client=oauth_client,
            token_store=self.token_store,
        )
        self.ai_provider = ai_provider or HeuristicFallbackProvider()
        self.app = create_app(
            self.settings,
            repository=self.repository,
            job_manager=self.job_manager,
            audio_providers=[self.audio_provider],
            gallery_provider=self.gallery_provider,
            oauth_provider=self.gallery_provider,
            oauth_transaction_store=OAuthTransactionStore(),
            ai_providers=self.ai_provider,
            analyzer=self.analyzer,
            scene_extractor=SceneExtractor(data_dir=self.settings.data_dir),
        )
        self.client = None

    def close(self) -> None:
        self.job_manager.shutdown()


@pytest.fixture
def rig(tmp_path):
    instance = Rig(tmp_path)
    try:
        with TestClient(instance.app, base_url="http://localhost") as client:
            instance.client = client
            yield instance
    finally:
        instance.close()


def _create_project(client: TestClient, name: str = "Weekend trip") -> dict:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def _image_bytes(fmt: str = "PNG", color=(180, 30, 50)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (160, 120), color).save(output, format=fmt)
    return output.getvalue()


def _video_file(path: Path, duration: float = 2.5, color: str = "red") -> bytes:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for end-to-end media tests")
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x120:r=24:d={duration}",
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


def _multipart_upload(client: TestClient, project_id: str, files: list[tuple[str, bytes, str]]):
    return client.post(
        f"/api/projects/{project_id}/media/upload",
        files=[("files", (name, data, mime)) for name, data, mime in files],
    )


def _wait_for_job(rig: Rig, job_id: str, timeout: float = 120.0) -> dict:
    result = rig.job_manager.wait(job_id, timeout=timeout)
    assert result is not None
    response = rig.client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


def _local_render_for_download(rig: Rig, content: bytes) -> str:
    project = _create_project(rig.client, "Download range test")
    queued = rig.job_manager.submit(
        project["id"],
        "render",
        lambda progress: {"storage_backend": "local"},
    )
    assert queued is not None
    job = rig.job_manager.wait(queued.id, timeout=5)
    assert job is not None
    assert job.status == "completed"
    output_path = rig.settings.data_dir / "renders" / f"{queued.id}.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return queued.id


def _click_wav_bytes(bpm: float = 110.0, duration: float = 12.0) -> bytes:
    sample_rate = 22050
    pcm = np.zeros(round(duration * sample_rate), dtype=np.float32)
    click_time = np.arange(round(0.04 * sample_rate)) / sample_rate
    click = np.sin(2 * np.pi * 1000 * click_time) * np.exp(-click_time * 100)
    beat = 0.25
    while beat < duration:
        start = round(beat * sample_rate)
        end = min(start + len(click), len(pcm))
        pcm[start:end] += click[: end - start].astype(np.float32)
        beat += 60.0 / bpm
    output = io.BytesIO()
    samples = np.clip(pcm, -1, 1)
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(np.round(samples * 32767).astype("<i2").tobytes())
    return output.getvalue()


def test_oauth_router_mounts_real_routes_and_consumes_state_once(tmp_path):
    exchanges = []

    def handle_token(request):
        exchanges.append(request)
        assert request.url.path == "/token"
        return httpx.Response(200, json={"access_token": "token-123", "expires_in": 3600})

    external_client = httpx.Client(
        transport=httpx.MockTransport(handle_token),
        base_url="https://oauth2.googleapis.com",
    )
    instance = Rig(
        tmp_path,
        oauth_client=external_client,
        oauth_settings=True,
    )
    try:
        with TestClient(instance.app, base_url="http://localhost") as client:
            auth_response = client.get("/api/gallery/google/auth-url")
            assert auth_response.status_code == 200
            state = parse_qs(urlparse(auth_response.json()["auth_url"]).query)["state"][0]
            assert "browser_cookie" in client.cookies

            callback = client.get(
                "/api/gallery/google/callback",
                params={"code": "code-123", "state": state},
            )
            assert callback.status_code == 200
            assert "text/html" in callback.headers["content-type"]
            assert "google-photos-connected" in callback.text
            assert len(exchanges) == 1

            replay = client.get(
                "/api/gallery/google/callback",
                params={"code": "code-123", "state": state},
            )
            missing = client.get(
                "/api/gallery/google/callback",
                params={"code": "code-123", "state": ""},
            )
            assert replay.status_code == missing.status_code
            assert replay.status_code >= 400
            assert len(exchanges) == 1
    finally:
        instance.close()
        external_client.close()


def test_production_app_cors_allows_configured_origin_only(tmp_path):
    repository = Database(lambda: sqlite3.connect(tmp_path / "cors.sqlite3"))
    repository.init_db()
    jobs = JobManager(data_dir=tmp_path / "cors-data", repository=repository)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "cors-data",
        cors_origins="http://localhost:5173",
    )
    application = create_app(
        settings,
        repository=repository,
        job_manager=jobs,
        ai_providers=HeuristicFallbackProvider(),
        analyzer=AudioAnalyzer(data_dir=settings.data_dir),
        scene_extractor=SceneExtractor(data_dir=settings.data_dir),
        audio_providers=[BuiltinDemoAudioProvider(data_dir=settings.data_dir)],
    )
    try:
        with TestClient(application, base_url="http://localhost") as client:
            allowed = client.get(
                "/api/config/capabilities",
                headers={"Origin": "http://localhost:5173"},
            )
            denied = client.get(
                "/api/config/capabilities",
                headers={"Origin": "http://evil.example"},
            )
        assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert "access-control-allow-origin" not in denied.headers
    finally:
        jobs.shutdown()


def test_production_app_rejects_untrusted_host_header(rig):
    response = rig.client.get(
        "/api/config/capabilities",
        headers={"Host": "attacker.example"},
    )

    assert response.status_code == 400


def test_create_app_uses_injected_storage_provider(tmp_path, monkeypatch):
    class RenderCompiler:
        def build_render_plan(self, project, composition, assets, output_path):
            return SimpleNamespace(total_duration_sec=1.0)

        def execute_render_plan(self, plan, progress):
            return tmp_path / "compiled.mp4"

    class InlineJobManager:
        def tmp_dir(self, job_id):
            return tmp_path / "tmp" / job_id

        def submit(self, project_id, kind, worker):
            job_id = "custom-storage-job"
            result = worker(lambda *_args, **_kwargs: None, job_id)
            now = "2026-01-01T00:00:00Z"
            return Job(
                id=job_id,
                project_id=project_id,
                kind=kind,
                status="completed",
                progress=100,
                message="Completed",
                result=result,
                error=None,
                created_at=now,
                updated_at=now,
            )

    class CustomStorageProvider:
        def __init__(self):
            self.stored = []

        def store_render(
            self, source_path, project_id, job_id, project_name, duration_sec
        ):
            self.stored.append((source_path, project_id, job_id, project_name))
            return StoredRender(
                storage_backend="custom",
                s3_key=None,
                download_url="custom://render",
                duration_sec=duration_sec,
                size_bytes=123,
            )

    def unexpected_factory_call(_settings):
        pytest.fail("create_app should use the injected storage provider")

    monkeypatch.setattr("app.main.get_storage_provider", unexpected_factory_call)
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    repository = Database(lambda: sqlite3.connect(tmp_path / "storage.sqlite3"))
    repository.init_db()
    storage_provider = CustomStorageProvider()
    application = create_app(
        settings,
        repository=repository,
        job_manager=InlineJobManager(),
        render_compiler=RenderCompiler(),
        storage_provider=storage_provider,
    )

    media_path = settings.data_dir / "assets" / "clip.mp4"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"test media")
    with TestClient(application, base_url="http://localhost") as client:
        project = _create_project(client, "Custom storage")
        repository.create_media_asset(
            MediaAsset(
                id="custom-storage-asset",
                project_id=project["id"],
                kind="video",
                source="upload",
                filename="clip.mp4",
                local_path="assets/clip.mp4",
                still_path=None,
                thumbnail_path=None,
                duration_sec=1.0,
                width=160,
                height=120,
                fps=24.0,
                has_audio=False,
                bpm=None,
                beat_grid=None,
                metadata={},
                created_at=repository.clock(),
            )
        )
        repository.save_composition(
            Composition(
                project_id=project["id"],
                video_clips=[
                    TimelineVisualClip(
                        id="V1",
                        asset_id="custom-storage-asset",
                        part_id=None,
                        kind="video",
                        start_sec=0.0,
                        duration_sec=1.0,
                        trim_start_sec=0.0,
                        transition_in="none",
                        ken_burns="none",
                        color_preset="original",
                        muted=True,
                        fade_in_sec=0.0,
                        fade_out_sec=0.0,
                    )
                ],
                audio_clips=[],
                updated_at=repository.clock(),
            )
        )

        response = client.post(f"/api/projects/{project['id']}/render")

    assert response.status_code == 200
    assert response.json()["result"]["storage_backend"] == "custom"
    assert storage_provider.stored == [
        (
            tmp_path / "compiled.mp4",
            project["id"],
            "custom-storage-job",
            "Custom storage",
        )
    ]


def test_uploaded_audio_persists_detected_bpm_and_grid_through_api(rig):
    project = _create_project(rig.client)
    response = _multipart_upload(
        rig.client,
        project["id"],
        [("uploaded.wav", _click_wav_bytes(), "audio/wav")],
    )
    assert response.status_code == 200
    asset = response.json()[0]
    saved_asset = rig.repository.get_media_asset(asset["id"])
    saved_project = rig.repository.get_project(project["id"])

    assert saved_asset.kind == "audio"
    assert saved_asset.bpm == pytest.approx(110, abs=1.5)
    assert saved_asset.beat_grid is not None
    assert saved_asset.beat_grid["beat_times"]
    assert len(saved_asset.beat_grid["waveform"]) == 300
    assert saved_project.bpm == pytest.approx(saved_asset.bpm)
    assert saved_project.beat_offset_sec == pytest.approx(
        saved_asset.beat_grid["beat_offset_sec"]
    )


def test_running_job_status_is_read_from_real_sqlite_repository(rig):
    project = _create_project(rig.client)
    now = rig.repository.clock()
    rig.repository.create_job(
        Job(
            id="job-a1",
            project_id=project["id"],
            kind="analysis",
            status="running",
            progress=75,
            message="Extracting thumbnails",
            result={},
            error=None,
            created_at=now,
            updated_at=now,
        )
    )

    response = rig.client.get("/api/jobs/job-a1")

    assert response.status_code == 200
    assert {
        key: response.json()[key]
        for key in ("id", "status", "progress", "message")
    } == {
        "id": "job-a1",
        "status": "running",
        "progress": 75,
        "message": "Extracting thumbnails",
    }


@pytest.fixture
def analysis_runtime(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "analysis-data")
    settings.ensure_data_dirs()
    repository = Database(lambda: sqlite3.connect(tmp_path / "analysis.sqlite3"))
    repository.init_db()
    job_manager = JobManager(
        max_workers=1,
        data_dir=settings.data_dir,
        repository=repository,
    )
    try:
        yield settings, repository, job_manager
    finally:
        job_manager.shutdown()


def _analysis_test_client(
    repository,
    job_manager,
    settings,
    *,
    provider=None,
    analyzer=object(),
    scene_extractor=object(),
):
    application = FastAPI()
    application.include_router(
        create_analysis_router(
            repository=repository,
            job_manager=job_manager,
            analyzer=analyzer,
            scene_extractor=scene_extractor,
            providers=provider,
            app_settings=settings,
        ),
        prefix="/api",
    )
    return TestClient(application)


class _StaticAnalysisProvider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def analyze_project_media(
        self, assets, bpm, allowed_lengths, ai_context, result_target
    ):
        self.calls.append(
            {
                "assets": assets,
                "bpm": bpm,
                "allowed_lengths": allowed_lengths,
                "ai_context": ai_context,
                "result_target": result_target,
            }
        )
        return self.result


class _NoPartSceneExtractor:
    def __init__(self):
        self.calls = []

    def extract_visual_part(self, asset, moment, bpm, *, data_dir):
        self.calls.append((asset.id, moment.asset_id, bpm, data_dir))
        return None


class _ProjectDisappearsAfterRequest:
    def __init__(self, repository):
        self.repository = repository
        self.project_lookups = 0

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def get_project(self, project_id):
        self.project_lookups += 1
        if self.project_lookups == 1:
            return self.repository.get_project(project_id)
        return None


class _UnstartableJobManager:
    def __init__(self, outcome):
        self.outcome = outcome
        self.submissions = []

    def submit(self, project_id, kind, worker):
        self.submissions.append((project_id, kind))
        if self.outcome == "raise":
            raise RuntimeError("executor unavailable")
        return None


def _analysis_asset(project_id, asset_id, kind="video"):
    return MediaAsset(
        id=asset_id,
        project_id=project_id,
        kind=kind,
        source="upload",
        filename=f"{asset_id}.mp4",
        local_path=f"media/{asset_id}.mp4",
        still_path=None,
        thumbnail_path=None,
        duration_sec=2.0,
        width=320,
        height=240,
        fps=24.0,
        has_audio=False,
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_analysis_api_reports_unavailable_manager_and_parts_repository(analysis_runtime):
    settings, repository, job_manager = analysis_runtime
    project = repository.create_project("Analysis unavailable")

    with _analysis_test_client(
        repository, None, settings, analyzer=object(), scene_extractor=object()
    ) as client:
        analysis = client.post(f"/api/projects/{project.id}/analyze")

    with _analysis_test_client(
        None, None, settings, analyzer=object(), scene_extractor=object()
    ) as client:
        parts = client.get(f"/api/projects/{project.id}/parts")
        deletion = client.delete("/api/parts/missing-part")

    assert analysis.status_code == 503
    assert analysis.json()["detail"] == "Analysis is unavailable"
    assert parts.status_code == 503
    assert parts.json()["detail"] == "Parts are unavailable"
    assert deletion.status_code == 503
    assert deletion.json()["detail"] == "Parts are unavailable"
    assert job_manager.list_for_project(project.id) == []


@pytest.mark.parametrize("missing_service", ["analyzer", "scene_extractor"])
def test_analysis_api_reports_missing_analysis_service(
    analysis_runtime, missing_service
):
    settings, repository, job_manager = analysis_runtime
    project = repository.create_project("Analysis service unavailable")
    analyzer = None if missing_service == "analyzer" else object()
    scene_extractor = None if missing_service == "scene_extractor" else object()

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        analyzer=analyzer,
        scene_extractor=scene_extractor,
    ) as client:
        response = client.post(f"/api/projects/{project.id}/analyze")

    assert response.status_code == 503
    assert response.json()["detail"] == "Analysis is unavailable"
    assert job_manager.list_for_project(project.id) == []


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        ("return_none", "Analysis job could not be started"),
        ("raise", "Analysis job could not be started"),
    ],
)
def test_analysis_api_reports_job_submission_failure(
    analysis_runtime, outcome, detail
):
    settings, repository, _ = analysis_runtime
    project = repository.create_project("Analysis job cannot start")
    job_manager = _UnstartableJobManager(outcome)

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        analyzer=object(),
        scene_extractor=object(),
    ) as client:
        response = client.post(f"/api/projects/{project.id}/analyze")

    assert response.status_code == 503
    assert response.json()["detail"] == detail
    assert job_manager.submissions == [(project.id, "analysis")]


def test_analysis_api_rejects_missing_project_without_submitting(analysis_runtime):
    settings, repository, job_manager = analysis_runtime

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        analyzer=object(),
        scene_extractor=object(),
    ) as client:
        analysis = client.post("/api/projects/missing-project/analyze")
        parts = client.get("/api/projects/missing-project/parts")
        deletion = client.delete("/api/parts/missing-part")

    assert analysis.status_code == 404
    assert analysis.json()["detail"] == "Project not found"
    assert parts.status_code == 404
    assert parts.json()["detail"] == "Project not found"
    assert deletion.status_code == 404
    assert deletion.json()["detail"] == "Part not found"
    assert job_manager.list_for_project("missing-project") == []


def test_analysis_job_fails_if_project_disappears_after_request(analysis_runtime):
    settings, repository, job_manager = analysis_runtime
    project = repository.create_project("Project disappears")
    disappearing_repository = _ProjectDisappearsAfterRequest(repository)
    provider = _StaticAnalysisProvider(AIAnalysisResult(moments=[], suggested_sequence=[]))

    with _analysis_test_client(
        disappearing_repository,
        job_manager,
        settings,
        provider=provider,
    ) as client:
        response = client.post(f"/api/projects/{project.id}/analyze")
        assert response.status_code == 200
        job = job_manager.wait(response.json()["id"], timeout=5)

    assert job is not None
    assert job.status == "failed"
    assert job.error == "Project not found"
    assert disappearing_repository.project_lookups == 2
    assert provider.calls == []


def test_analysis_job_fails_when_provider_returns_no_result(analysis_runtime):
    settings, repository, job_manager = analysis_runtime
    project = repository.create_project("No analysis result")
    provider = _StaticAnalysisProvider(None)

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        provider=provider,
    ) as client:
        response = client.post(f"/api/projects/{project.id}/analyze")
        assert response.status_code == 200
        job = job_manager.wait(response.json()["id"], timeout=5)

    assert job is not None
    assert job.status == "failed"
    assert job.error == "Media analysis did not return a result"
    assert len(provider.calls) == 1
    assert provider.calls[0]["assets"] == []


@pytest.mark.parametrize("bpm", [None, 0])
def test_analysis_job_skips_audio_without_positive_bpm(analysis_runtime, bpm):
    settings, repository, job_manager = analysis_runtime
    project = repository.create_project("Audio without BPM")
    asset = _analysis_asset(project.id, f"audio-{bpm}", kind="audio").model_copy(
        update={"bpm": bpm}
    )
    repository.create_media_asset(asset)
    provider = _StaticAnalysisProvider(
        AIAnalysisResult(moments=[], suggested_sequence=[])
    )

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        provider=provider,
    ) as client:
        started = client.post(f"/api/projects/{project.id}/analyze")
        assert started.status_code == 200
        job = job_manager.wait(started.json()["id"], timeout=5)
        parts = client.get(f"/api/projects/{project.id}/parts")

    assert job is not None
    assert job.status == "completed"
    assert job.result == {
        "part_ids": [],
        "suggested_sequence": [],
        "visual_parts": 0,
        "audio_parts": 0,
    }
    assert parts.status_code == 200
    assert parts.json() == []


def test_analysis_job_skips_audio_sections_with_nonpositive_duration(analysis_runtime):
    settings, repository, job_manager = analysis_runtime
    project = repository.create_project("Invalid audio sections")
    asset = _analysis_asset(project.id, "sectioned-audio", kind="audio").model_copy(
        update={
            "bpm": 120,
            "beat_grid": {
                "sections": [
                    {"start_sec": 0, "end_sec": 0, "label": "empty"},
                    {"start_sec": 2, "end_sec": 1, "label": "reversed"},
                    {"start_sec": 1, "end_sec": 2, "label": "valid"},
                ]
            },
        }
    )
    repository.create_media_asset(asset)
    provider = _StaticAnalysisProvider(
        AIAnalysisResult(moments=[], suggested_sequence=[])
    )

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        provider=provider,
    ) as client:
        started = client.post(f"/api/projects/{project.id}/analyze")
        assert started.status_code == 200
        job = job_manager.wait(started.json()["id"], timeout=5)
        parts = client.get(f"/api/projects/{project.id}/parts")

    assert job is not None
    assert job.status == "completed"
    assert job.result["audio_parts"] == 1
    assert len(job.result["part_ids"]) == 1
    assert parts.status_code == 200
    assert len(parts.json()) == 1
    assert parts.json()[0]["part_type"] == "audio_segment"
    assert parts.json()[0]["asset_id"] == asset.id
    assert parts.json()[0]["start_sec"] == 1
    assert parts.json()[0]["end_sec"] == 2
    assert parts.json()[0]["label"] == "valid"


def test_analysis_job_defaults_missing_audio_section_metadata(analysis_runtime):
    settings, repository, job_manager = analysis_runtime
    project = repository.create_project("Audio section defaults")
    asset = _analysis_asset(project.id, "section-defaults", kind="audio").model_copy(
        update={
            "bpm": 120,
            "beat_grid": {
                "sections": [
                    {
                        "start_sec": 1,
                        "end_sec": 3,
                        "beat_length": 0,
                        "label": "",
                    },
                    {"start_sec": 3, "end_sec": 5, "label": ""},
                ]
            },
        }
    )
    repository.create_media_asset(asset)
    provider = _StaticAnalysisProvider(
        AIAnalysisResult(moments=[], suggested_sequence=[])
    )

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        provider=provider,
    ) as client:
        started = client.post(f"/api/projects/{project.id}/analyze")
        assert started.status_code == 200
        job = job_manager.wait(started.json()["id"], timeout=5)
        parts = client.get(f"/api/projects/{project.id}/parts")

    assert job is not None
    assert job.status == "completed"
    assert job.result["audio_parts"] == 2
    assert parts.status_code == 200
    assert [part["beat_length"] for part in parts.json()] == [4.0, 4.0]
    assert [part["label"] for part in parts.json()] == ["Audio", "Audio"]


def test_analysis_job_handles_empty_and_unmatched_media(analysis_runtime):
    settings, repository, job_manager = analysis_runtime
    empty_project = repository.create_project("No media")
    empty_provider = _StaticAnalysisProvider(
        AIAnalysisResult(moments=[], suggested_sequence=[])
    )

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        provider=empty_provider,
    ) as client:
        empty_started = client.post(f"/api/projects/{empty_project.id}/analyze")
        assert empty_started.status_code == 200
        empty_job = job_manager.wait(empty_started.json()["id"], timeout=5)

    assert empty_job is not None
    assert empty_job.status == "completed"
    assert empty_job.result == {
        "part_ids": [],
        "suggested_sequence": [],
        "visual_parts": 0,
        "audio_parts": 0,
    }
    assert empty_provider.calls[0]["assets"] == []
    assert repository.get_project(empty_project.id).suggested_sequence == []

    unmatched_project = repository.create_project("Unmatched media")
    unmatched = _analysis_asset(unmatched_project.id, "unmatched-clip")
    matched_but_unextractable = _analysis_asset(
        unmatched_project.id, "unextractable-clip"
    )
    repository.create_media_asset(unmatched)
    repository.create_media_asset(matched_but_unextractable)
    old_part = DetectedPart(
        id="previous-part",
        project_id=unmatched_project.id,
        asset_id=unmatched.id,
        part_type="visual_clip",
        start_sec=0,
        end_sec=1,
        beat_length=2,
        label="previous",
        score=1,
        ai_reason="previous analysis",
        clip_path=None,
        thumbnail_path=None,
        created_at=repository.clock(),
    )
    repository.create_detected_part(old_part)
    provider = _StaticAnalysisProvider(
        AIAnalysisResult(
            moments=[
                AIMoment(
                    asset_id="different-asset",
                    start_sec=0,
                    end_sec=1,
                    beat_length=2,
                    label="unmatched",
                    score=1,
                    reason="unmatched",
                    suggested_effect="none",
                ),
                AIMoment(
                    asset_id=matched_but_unextractable.id,
                    start_sec=0,
                    end_sec=1,
                    beat_length=2,
                    label="no scene",
                    score=1,
                    reason="not extractable",
                    suggested_effect="none",
                ),
            ],
            suggested_sequence=[],
        )
    )
    scene_extractor = _NoPartSceneExtractor()
    expected_sequence = sorted([unmatched.id, matched_but_unextractable.id])

    with _analysis_test_client(
        repository,
        job_manager,
        settings,
        provider=provider,
        scene_extractor=scene_extractor,
    ) as client:
        started = client.post(f"/api/projects/{unmatched_project.id}/analyze")
        assert started.status_code == 200
        job = job_manager.wait(started.json()["id"], timeout=5)

    assert job is not None
    assert job.status == "completed"
    assert job.result == {
        "part_ids": [],
        "suggested_sequence": expected_sequence,
        "visual_parts": 0,
        "audio_parts": 0,
    }
    provider_assets = provider.calls[0]["assets"]
    assert [asset.id for asset in provider_assets] == expected_sequence
    assert all(
        Path(asset.local_path).is_absolute() for asset in provider_assets
    )
    assert scene_extractor.calls == [
        (
            matched_but_unextractable.id,
            matched_but_unextractable.id,
            repository.get_project(unmatched_project.id).bpm,
            settings.data_dir,
        )
    ]
    assert repository.list_detected_parts(unmatched_project.id) == []
    assert repository.get_project(unmatched_project.id).suggested_sequence == [
        *expected_sequence,
    ]


class _FakeGeminiClient:
    def __init__(self, error):
        self.error = error
        self.uploaded = []
        self.deleted = []

        class Files:
            def __init__(inner):
                inner.owner = self

            def upload(inner, file):
                self.uploaded.append(file)
                return SimpleNamespace(name=f"remote-{len(self.uploaded)}")

            def delete(inner, name):
                self.deleted.append(name)

        class Models:
            def generate_content(inner, **kwargs):
                raise self.error

        self.files = Files()
        self.models = Models()


def test_gemini_rate_limit_fails_analysis_job_without_crashing_server(tmp_path):
    error = genai_errors.ClientError(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "Resource has been exhausted.",
            }
        },
    )
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        ai_provider="gemini",
        gemini_api_key="test-key",
    )
    provider = GeminiProvider(settings, client=_FakeGeminiClient(error))
    instance = Rig(tmp_path, ai_provider=provider, app_settings=settings)
    try:
        with TestClient(instance.app, base_url="http://localhost") as client:
            instance.client = client
            project = _create_project(client)
            video = _video_file(tmp_path / "gemini.mp4", duration=2.0)
            uploaded = _multipart_upload(
                client,
                project["id"],
                [("gemini.mp4", video, "video/mp4")],
            )
            assert uploaded.status_code == 200

            started = client.post(f"/api/projects/{project['id']}/analyze")
            assert started.status_code == 200
            job = _wait_for_job(instance, started.json()["id"])
            health = client.get("/api/config/capabilities")

        assert health.status_code == 200
        assert job["status"] == "failed"
        assert "Gemini" in job["error"]
        assert "429 RESOURCE_EXHAUSTED" in job["error"]
        assert provider.client.uploaded
        assert provider.client.deleted == ["remote-1"]
    finally:
        instance.close()


def test_full_api_analyze_assemble_edit_render_and_download_round_trip(rig, tmp_path):
    project = _create_project(rig.client)
    response = rig.client.patch(
        f"/api/projects/{project['id']}",
        json={"resolution_w": 320, "resolution_h": 240, "fps": 12},
    )
    assert response.status_code == 200

    files = [
        ("trip.mp4", _video_file(tmp_path / "trip.mp4", 2.5, "red"), "video/mp4"),
        ("friends.png", _image_bytes("PNG"), "image/png"),
        ("live.jpg", _image_bytes("JPEG", (15, 140, 50)), "image/jpeg"),
        ("live.mov", _video_file(tmp_path / "live.mov", 1.5, "blue"), "video/quicktime"),
    ]
    uploaded = _multipart_upload(rig.client, project["id"], files)
    assert uploaded.status_code == 200
    assets = uploaded.json()
    assert {asset["kind"] for asset in assets} == {"video", "photo", "live_photo"}
    live = next(asset for asset in assets if asset["kind"] == "live_photo")
    assert live["still_path"] and live["thumbnail_path"]

    imported = rig.client.post(
        f"/api/projects/{project['id']}/audio-catalog/import",
        json={"provider": "builtin", "track_id": "builtin-110"},
    )
    assert imported.status_code == 200
    audio_asset = imported.json()
    saved_audio = rig.repository.get_media_asset(audio_asset["id"])
    assert saved_audio.bpm == pytest.approx(110, abs=1.5)
    assert saved_audio.beat_grid and saved_audio.beat_grid["beat_times"]

    started = rig.client.post(f"/api/projects/{project['id']}/analyze")
    assert started.status_code == 200
    analysis_job = _wait_for_job(rig, started.json()["id"])
    assert analysis_job["status"] == "completed"
    parts_response = rig.client.get(f"/api/projects/{project['id']}/parts")
    assert parts_response.status_code == 200
    parts = parts_response.json()
    visual_parts = [part for part in parts if part["part_type"] == "visual_clip"]
    audio_parts = [part for part in parts if part["part_type"] == "audio_segment"]
    assert visual_parts and audio_parts
    assert all(part["thumbnail_path"] for part in visual_parts)
    assert all(part["clip_path"] for part in visual_parts if part["clip_path"])
    assert all(part["clip_path"] == saved_audio.local_path for part in audio_parts)
    assert rig.repository.get_project(project["id"]).suggested_sequence

    # Include stale references and an empty reference to prove the assembler
    # skips them while snapping the valid parts to the configured beat offset.
    current_project = rig.repository.get_project(project["id"])
    rig.repository.update_project(
        project["id"],
        beat_offset_sec=0.173,
        suggested_sequence=[
            None,
            "",
            "unknown-asset",
            *current_project.suggested_sequence,
        ],
    )
    assembled = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )
    assert assembled.status_code == 200
    composition = assembled.json()
    assert composition["video_clips"]
    assert composition["audio_clips"]
    assert composition["video_clips"][0]["id"] == "V1"
    assert composition["audio_clips"][0]["id"] == "A1"
    assert composition["video_clips"][0]["start_sec"] == pytest.approx(0.173)
    project_after_analysis = rig.repository.get_project(project["id"])
    beat_sec = 60 / project_after_analysis.bpm
    for clip in composition["video_clips"]:
        beat_position = (clip["start_sec"] - 0.173) / beat_sec
        assert beat_position == pytest.approx(round(beat_position), abs=1e-6)
        assert clip["muted"] is True
        assert clip["volume"] == 0.0

    # An entirely stale sequence also falls back to the real detected parts.
    rig.repository.update_project(
        project["id"], suggested_sequence=["", "unknown-asset"]
    )
    fallback = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )
    assert fallback.status_code == 200
    assert fallback.json()["video_clips"]

    composition = fallback.json()
    composition["video_clips"][0].update(
        {
            "fade_in_sec": 0.5,
            "fade_out_sec": 0.5,
            "transition_in": "crossfade",
            "ken_burns": "zoom_in",
            "color_preset": "warm",
        }
    )
    saved = rig.client.put(
        f"/api/projects/{project['id']}/composition", json=composition
    )
    assert saved.status_code == 200
    loaded = rig.client.get(f"/api/projects/{project['id']}/composition")
    assert loaded.status_code == 200
    edited = loaded.json()["video_clips"][0]
    assert edited["fade_in_sec"] == 0.5
    assert edited["fade_out_sec"] == 0.5
    assert edited["transition_in"] == "crossfade"
    assert edited["ken_burns"] == "zoom_in"
    assert edited["color_preset"] == "warm"

    render_started = rig.client.post(f"/api/projects/{project['id']}/render")
    assert render_started.status_code == 200
    render_job = _wait_for_job(rig, render_started.json()["id"], timeout=180)
    assert render_job["status"] == "completed", render_job.get("error")

    listed_jobs = rig.client.get(f"/api/projects/{project['id']}/jobs")
    assert listed_jobs.status_code == 200
    rendered = next(
        job for job in listed_jobs.json() if job["id"] == render_job["id"]
    )
    assert rendered["status"] == "completed"
    assert rendered["download_url"]

    ranged = rig.client.get(
        f"/api/renders/{render_job['id']}/download",
        headers={"Range": "bytes=0-99"},
    )
    assert ranged.status_code == 206
    assert ranged.headers["content-range"].startswith("bytes 0-99/")
    assert len(ranged.content) == 100
    attachment = rig.client.get(
        f"/api/renders/{render_job['id']}/download?download=1"
    )
    assert attachment.status_code == 200
    assert attachment.headers["content-disposition"].startswith("attachment;")

    output_path = rig.settings.data_dir / "renders" / f"{render_job['id']}.mp4"
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(probe.stdout)
    streams = {stream["codec_type"]: stream for stream in metadata["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    rendered_duration = float(metadata["format"]["duration"])
    assert rendered_duration == pytest.approx(saved_audio.duration_sec, abs=3.0)

    deleted = rig.client.delete(f"/api/parts/{visual_parts[0]['id']}")
    assert deleted.status_code == 204
    remaining = rig.client.get(f"/api/projects/{project['id']}/parts").json()
    assert visual_parts[0]["id"] not in {part["id"] for part in remaining}


def test_composition_routes_report_unavailable_repository():
    application = FastAPI()
    application.include_router(create_compositions_router(), prefix="/api")

    with TestClient(application) as client:
        assembled = client.post(
            "/api/projects/project-1/composition/auto-assemble"
        )
        loaded = client.get("/api/projects/project-1/composition")
        saved = client.put(
            "/api/projects/project-1/composition",
            json={"video_clips": [], "audio_clips": []},
        )

    assert assembled.status_code == 503
    assert assembled.json()["detail"] == "Composition is unavailable"
    assert loaded.status_code == 503
    assert loaded.json()["detail"] == "Composition is unavailable"
    assert saved.status_code == 503
    assert saved.json()["detail"] == "Composition is unavailable"


def test_composition_routes_report_missing_project_without_persisting(rig):
    assembled = rig.client.post(
        "/api/projects/missing-project/composition/auto-assemble"
    )
    loaded = rig.client.get("/api/projects/missing-project/composition")
    saved = rig.client.put(
        "/api/projects/missing-project/composition",
        json={"video_clips": [], "audio_clips": []},
    )

    assert assembled.status_code == 404
    assert assembled.json()["detail"] == "Project not found"
    assert loaded.status_code == 404
    assert loaded.json()["detail"] == "Project not found"
    assert saved.status_code == 404
    assert saved.json()["detail"] == "Project not found"
    assert rig.repository.get_composition("missing-project") is None


def test_get_composition_returns_empty_timeline_for_new_project(rig):
    project = _create_project(rig.client)

    loaded = rig.client.get(f"/api/projects/{project['id']}/composition")

    assert loaded.status_code == 200
    body = loaded.json()
    assert (body["project_id"], body["video_clips"], body["audio_clips"]) == (project["id"], [], [])
    assert rig.repository.get_composition(project["id"]) is None  # read does not persist


def test_auto_assemble_rejects_missing_and_unusable_visual_parts(rig, monkeypatch):
    project = _create_project(rig.client)

    no_parts = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )
    assert no_parts.status_code == 422
    assert no_parts.json()["detail"] == "No visual parts to assemble"
    assert rig.repository.get_composition(project["id"]) is None

    unusable_part = SimpleNamespace(
        id="part-without-asset",
        part_type="visual_clip",
        asset_id="missing-asset",
        start_sec=0.0,
        end_sec=1.0,
    )
    monkeypatch.setattr(
        rig.repository, "list_detected_parts", lambda project_id: [unusable_part]
    )
    monkeypatch.setattr(rig.repository, "list_media_assets", lambda project_id: [])

    unusable = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )
    assert unusable.status_code == 422
    assert unusable.json()["detail"] == "No usable visual parts"
    assert rig.repository.get_composition(project["id"]) is None


def test_auto_assemble_resolves_dictionary_references_and_suppresses_duplicates(rig):
    project = _create_project(rig.client)
    first_asset = _analysis_asset(project["id"], "first-asset")
    second_asset = _analysis_asset(project["id"], "second-asset")
    audio_asset = _analysis_asset(
        project["id"], "assembly-audio", kind="audio"
    ).model_copy(update={"duration_sec": 8.0})
    for asset in (first_asset, second_asset, audio_asset):
        rig.repository.create_media_asset(asset)

    for part_id, asset in (
        ("first-part", first_asset),
        ("second-part", second_asset),
    ):
        rig.repository.create_detected_part(
            DetectedPart(
                id=part_id,
                project_id=project["id"],
                asset_id=asset.id,
                part_type="visual_clip",
                start_sec=0.0,
                end_sec=1.0,
                beat_length=1.0,
                label=part_id,
                score=1.0,
                ai_reason="test fixture",
                clip_path=None,
                thumbnail_path=None,
                created_at=rig.repository.clock(),
            )
        )

    rig.repository.update_project(
        project["id"],
        bpm=60,
        suggested_sequence=[
            {"part_id": "first-part"},
            {"asset_id": "second-asset"},
            {"id": "second-part"},
            {"part_id": "first-part"},
        ],
    )

    assembled = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )

    assert assembled.status_code == 200
    clips = assembled.json()["video_clips"]
    assert len(clips) == 8
    assert [clip["part_id"] for clip in clips] == [
        "first-part",
        "second-part",
    ] * 4


@pytest.mark.parametrize(
    ("start_sec", "end_sec"),
    [(0.0, 0.0), (1.0, 0.5)],
    ids=("zero-duration", "negative-duration"),
)
def test_auto_assemble_skips_nonpositive_visual_part_durations(
    rig, start_sec, end_sec
):
    project = _create_project(rig.client)
    asset = _analysis_asset(project["id"], "nonpositive-duration-asset")
    rig.repository.create_media_asset(asset)
    rig.repository.create_detected_part(
        DetectedPart(
            id="nonpositive-duration-part",
            project_id=project["id"],
            asset_id=asset.id,
            part_type="visual_clip",
            start_sec=start_sec,
            end_sec=end_sec,
            beat_length=1.0,
            label="nonpositive duration",
            score=1.0,
            ai_reason="test fixture",
            clip_path=None,
            thumbnail_path=None,
            created_at=rig.repository.clock(),
        )
    )

    assembled = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )

    assert assembled.status_code == 422
    assert assembled.json()["detail"] == "No usable visual parts"
    assert rig.repository.get_composition(project["id"]) is None


def test_auto_assemble_stops_after_2000_visual_iterations(rig):
    project = _create_project(rig.client)
    visual_asset = _analysis_asset(project["id"], "cap-visual-asset")
    audio_asset = _analysis_asset(
        project["id"], "cap-audio-asset", kind="audio"
    ).model_copy(update={"duration_sec": 3000.0})
    for asset in (visual_asset, audio_asset):
        rig.repository.create_media_asset(asset)
    rig.repository.create_detected_part(
        DetectedPart(
            id="cap-visual-part",
            project_id=project["id"],
            asset_id=visual_asset.id,
            part_type="visual_clip",
            start_sec=0.0,
            end_sec=1.0,
            beat_length=1.0,
            label="one-second visual",
            score=1.0,
            ai_reason="test fixture",
            clip_path=None,
            thumbnail_path=None,
            created_at=rig.repository.clock(),
        )
    )
    rig.repository.update_project(project["id"], bpm=60)

    assembled = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )

    assert assembled.status_code == 200
    clips = assembled.json()["video_clips"]
    assert len(clips) == 2000
    assert clips[0]["id"] == "V1"
    assert clips[-1]["id"] == "V2000"
    assert clips[-1]["start_sec"] == pytest.approx(1999.0)


def test_auto_assemble_ignores_audio_segment_with_missing_non_audio_asset(rig, monkeypatch):
    project = _create_project(rig.client)
    visual_asset = _analysis_asset(project["id"], "orphan-audio-visual-asset")
    rig.repository.create_media_asset(visual_asset)
    visual_part = DetectedPart(
        id="orphan-audio-visual-part",
        project_id=project["id"],
        asset_id=visual_asset.id,
        part_type="visual_clip",
        start_sec=0.0,
        end_sec=1.0,
        beat_length=1.0,
        label="visual",
        score=1.0,
        ai_reason="test fixture",
        clip_path=None,
        thumbnail_path=None,
        created_at=rig.repository.clock(),
    )
    rig.repository.create_detected_part(visual_part)
    list_detected_parts = rig.repository.list_detected_parts
    orphan_audio_part = SimpleNamespace(
        id="orphan-audio-part",
        part_type="audio_segment",
        asset_id="missing-audio-asset",
    )
    monkeypatch.setattr(
        rig.repository,
        "list_detected_parts",
        lambda project_id: [*list_detected_parts(project_id), orphan_audio_part],
    )

    assembled = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )

    assert assembled.status_code == 200
    composition = assembled.json()
    assert [clip["part_id"] for clip in composition["video_clips"]] == [
        visual_part.id
    ]
    assert composition["video_clips"][0]["muted"] is False
    assert composition["video_clips"][0]["volume"] == 1.0
    assert composition["audio_clips"] == []


def test_save_composition_rejects_invalid_payload_without_persisting(rig):
    project = _create_project(rig.client)

    response = rig.client.put(
        f"/api/projects/{project['id']}/composition",
        json={"video_clips": [{"id": "incomplete"}], "audio_clips": []},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid composition"
    assert rig.repository.get_composition(project["id"]) is None


def test_composition_routes_report_save_failure_without_persisting(rig, monkeypatch):
    project = _create_project(rig.client)
    asset = _analysis_asset(project["id"], "assembly-asset")
    rig.repository.create_media_asset(asset)
    rig.repository.create_detected_part(
        DetectedPart(
            id="assembly-part",
            project_id=project["id"],
            asset_id=asset.id,
            part_type="visual_clip",
            start_sec=0.0,
            end_sec=1.0,
            beat_length=2.0,
            label="usable",
            score=1.0,
            ai_reason="test fixture",
            clip_path=None,
            thumbnail_path=None,
            created_at=rig.repository.clock(),
        )
    )
    monkeypatch.setattr(rig.repository, "save_composition", lambda composition: None)

    assembled = rig.client.post(
        f"/api/projects/{project['id']}/composition/auto-assemble"
    )
    assert assembled.status_code == 500
    assert assembled.json()["detail"] == "Composition could not be saved"
    assert rig.repository.get_composition(project["id"]) is None

    saved = rig.client.put(
        f"/api/projects/{project['id']}/composition",
        json={"video_clips": [], "audio_clips": []},
    )
    assert saved.status_code == 500
    assert saved.json()["detail"] == "Composition could not be saved"
    assert rig.repository.get_composition(project["id"]) is None


def test_render_rejects_path_traversal_before_job_is_queued(rig):
    project = _create_project(rig.client)
    malicious = MediaAsset(
        id="unsafe-asset",
        project_id=project["id"],
        kind="video",
        source="upload",
        filename="unsafe.mp4",
        local_path="../../etc/passwd",
        still_path=None,
        thumbnail_path=None,
        duration_sec=2.0,
        width=320,
        height=240,
        fps=24.0,
        has_audio=False,
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at=rig.repository.clock(),
    )
    rig.repository.create_media_asset(malicious)
    rig.repository.save_composition(
        Composition(
            project_id=project["id"],
            video_clips=[
                TimelineVisualClip(
                    id="V1",
                    asset_id=malicious.id,
                    part_id=None,
                    kind="video",
                    start_sec=0,
                    duration_sec=1,
                    trim_start_sec=0,
                    transition_in="none",
                    ken_burns="none",
                    color_preset="original",
                    muted=True,
                    fade_in_sec=0,
                    fade_out_sec=0,
                )
            ],
            audio_clips=[],
            updated_at=rig.repository.clock(),
        )
    )

    response = rig.client.post(f"/api/projects/{project['id']}/render")

    assert response.status_code == 400
    assert "unsafe" in response.json()["detail"].lower() or "path" in response.json()["detail"].lower()
    assert rig.repository.list_jobs(project["id"]) == []


def test_render_worker_sanitizes_compiler_errors_and_download_is_not_exposed(rig):
    project = _create_project(rig.client)
    invalid_media = rig.settings.data_dir / "assets" / "invalid.mp4"
    invalid_media.write_bytes(b"not a video")
    asset = MediaAsset(
        id="invalid-image",
        project_id=project["id"],
        kind="video",
        source="upload",
        filename="invalid.mp4",
        local_path="assets/invalid.mp4",
        still_path=None,
        thumbnail_path=None,
        duration_sec=1,
        width=10,
        height=10,
        fps=None,
        has_audio=False,
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at=rig.repository.clock(),
    )
    rig.repository.create_media_asset(asset)
    rig.repository.save_composition(
        Composition(
            project_id=project["id"],
            video_clips=[
                TimelineVisualClip(
                    id="V1",
                    asset_id=asset.id,
                    part_id=None,
                    kind="video",
                    start_sec=0,
                    duration_sec=1,
                    trim_start_sec=0,
                    transition_in="none",
                    ken_burns="none",
                    color_preset="original",
                    muted=True,
                    fade_in_sec=0,
                    fade_out_sec=0,
                )
            ],
            audio_clips=[],
            updated_at=rig.repository.clock(),
        )
    )

    started = rig.client.post(f"/api/projects/{project['id']}/render")
    assert started.status_code == 200
    failed = _wait_for_job(rig, started.json()["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "Render failed"
    assert str(rig.settings.data_dir) not in failed["error"]
    unavailable_download = rig.client.get(
        f"/api/renders/{failed['id']}/download"
    )
    assert unavailable_download.status_code == 404
    assert unavailable_download.json() == {"detail": "Render not found"}


def test_render_routes_report_unavailable_dependencies():
    application = FastAPI()
    application.include_router(create_renders_router(), prefix="/api")

    with TestClient(application) as client:
        render = client.post("/api/projects/project-a/render")
        job = client.get("/api/jobs/job-a")
        jobs = client.get("/api/projects/project-a/jobs")
        download = client.get("/api/renders/job-a/download")

    assert render.status_code == 503
    assert render.json() == {"detail": "Rendering is unavailable"}
    assert job.status_code == 503
    assert job.json() == {"detail": "Jobs are unavailable"}
    assert jobs.status_code == 503
    assert jobs.json() == {"detail": "Jobs are unavailable"}
    assert download.status_code == 404
    assert download.json() == {"detail": "Render not found"}


class _RenderRouteRepository:
    def __init__(self):
        self.project = SimpleNamespace(id="project-a", name="Test project")
        self.composition = SimpleNamespace(video_clips=[], audio_clips=[])
        self.assets = []

    def get_project(self, project_id):
        return self.project

    def get_composition(self, project_id):
        return self.composition

    def list_media_assets(self, project_id):
        return self.assets


class _RenderRouteJob:
    def __init__(self, *, status, result=None, error=None):
        self.status = status
        self.result = result
        self.error = error

    def model_dump(self):
        return {
            "id": "job-a",
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


class _InlineRenderJobManager:
    def __init__(self, tmp_path, *, mode="run"):
        self.tmp_path = tmp_path
        self.mode = mode
        self.worker = None
        self.worker_error = None

    def tmp_dir(self, job_id):
        return self.tmp_path / "jobs" / job_id

    def submit(self, project_id, kind, worker):
        if self.mode == "raise":
            raise RuntimeError("private executor details")
        if self.mode == "none":
            return None
        if self.mode == "capture":
            self.worker = worker
            return _RenderRouteJob(status="queued")
        try:
            result = worker(lambda *_args, **_kwargs: None, "job-a")
        except RuntimeError as exc:
            self.worker_error = str(exc)
            return _RenderRouteJob(status="failed", error=str(exc))
        return _RenderRouteJob(status="completed", result=result)


def _render_route_client(repository, job_manager, tmp_path, *, compiler=None, storage=None):
    application = FastAPI()
    application.include_router(
        create_renders_router(
            repository=repository,
            job_manager=job_manager,
            render_compiler=compiler,
            storage_provider=storage,
            app_settings=SimpleNamespace(data_dir=tmp_path),
        ),
        prefix="/api",
    )
    return TestClient(application)


@pytest.mark.parametrize(
    ("missing", "status", "detail"),
    [
        ("project", 404, "Project not found"),
        ("composition", 422, "Composition is required"),
        ("media", 400, "Invalid or unsafe composition media path"),
    ],
)
def test_render_creation_rejects_missing_project_composition_or_media(
    tmp_path, missing, status, detail
):
    repository = _RenderRouteRepository()
    if missing == "project":
        repository.project = None
    elif missing == "composition":
        repository.composition = None
    else:
        repository.composition.video_clips = [
            SimpleNamespace(asset_id="missing-asset")
        ]
    job_manager = _InlineRenderJobManager(tmp_path)

    with _render_route_client(repository, job_manager, tmp_path) as client:
        response = client.post("/api/projects/project-a/render")

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert job_manager.worker is None


def test_render_worker_requires_a_job_id(tmp_path):
    repository = _RenderRouteRepository()
    job_manager = _InlineRenderJobManager(tmp_path, mode="capture")

    with _render_route_client(repository, job_manager, tmp_path) as client:
        response = client.post("/api/projects/project-a/render")

    assert response.status_code == 200
    with pytest.raises(RuntimeError, match="^Render failed$"):
        job_manager.worker(lambda *_args, **_kwargs: None)


def test_render_creation_uses_injected_compiler_and_stores_render(tmp_path):
    repository = _RenderRouteRepository()

    class Compiler:
        def __init__(self):
            self.built = []
            self.executed = []

        def build_render_plan(self, project, composition, assets, output_path):
            self.built.append((project, composition, assets, output_path))
            return SimpleNamespace(total_duration_sec=2.5)

        def execute_render_plan(self, plan, progress):
            self.executed.append((plan, progress))
            return tmp_path / "compiled.mp4"

    class Storage:
        def __init__(self):
            self.stored = []

        def store_render(
            self, source_path, project_id, job_id, project_name, duration_sec
        ):
            self.stored.append(
                (source_path, project_id, job_id, project_name, duration_sec)
            )
            return StoredRender(
                storage_backend="custom",
                s3_key=None,
                download_url="custom://render",
                duration_sec=duration_sec,
                size_bytes=123,
            )

    compiler = Compiler()
    storage = Storage()
    job_manager = _InlineRenderJobManager(tmp_path)
    with _render_route_client(
        repository, job_manager, tmp_path, compiler=compiler, storage=storage
    ) as client:
        response = client.post("/api/projects/project-a/render")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["result"]["storage_backend"] == "custom"
    assert len(compiler.built) == 1
    assert len(compiler.executed) == 1
    assert storage.stored == [
        (
            tmp_path / "compiled.mp4",
            "project-a",
            "job-a",
            "Test project",
            2.5,
        )
    ]


def test_render_worker_reports_empty_storage_result_as_sanitized_failure(tmp_path):
    repository = _RenderRouteRepository()

    class Compiler:
        def build_render_plan(self, project, composition, assets, output_path):
            return SimpleNamespace(total_duration_sec=1.0)

        def execute_render_plan(self, plan, progress):
            return tmp_path / "compiled.mp4"

    class EmptyStorage:
        def store_render(self, *args):
            return None

    job_manager = _InlineRenderJobManager(tmp_path)
    with _render_route_client(
        repository,
        job_manager,
        tmp_path,
        compiler=Compiler(),
        storage=EmptyStorage(),
    ) as client:
        response = client.post("/api/projects/project-a/render")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "Render failed"
    assert job_manager.worker_error == "Render failed"


@pytest.mark.parametrize("mode", ["raise", "none"])
def test_render_creation_reports_submission_failure(tmp_path, mode):
    repository = _RenderRouteRepository()
    job_manager = _InlineRenderJobManager(tmp_path, mode=mode)

    with _render_route_client(repository, job_manager, tmp_path) as client:
        response = client.post("/api/projects/project-a/render")

    assert response.status_code == 503
    assert response.json() == {"detail": "Render job could not be started"}


@pytest.mark.parametrize(
    ("range_header", "status", "content_range", "content"),
    [
        ("not-a-range", 416, "bytes */10", b""),
        ("bytes=abc-def", 416, "bytes */10", b""),
        ("bytes=0", 416, "bytes */10", b""),
        ("bytes=0-1,4-5", 416, "bytes */10", b""),
        ("bytes=10-", 416, "bytes */10", b""),
        ("bytes=-4", 206, "bytes 6-9/10", b"6789"),
        ("bytes=2-", 206, "bytes 2-9/10", b"23456789"),
    ],
)
def test_render_download_validates_and_serves_byte_ranges(
    rig, range_header, status, content_range, content
):
    job_id = _local_render_for_download(rig, b"0123456789")

    response = rig.client.get(
        f"/api/renders/{job_id}/download",
        headers={"Range": range_header},
    )

    assert response.status_code == status
    assert response.content == content
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == content_range
    if status == 206:
        assert response.headers["content-length"] == str(len(content))
        assert response.headers["content-type"] == "video/mp4"
    else:
        assert response.headers["content-length"] == "0"
        assert "content-type" not in response.headers


@pytest.mark.parametrize(
    ("range_header", "content_range"),
    [
        ("bytes=0-0", "bytes */0"),
        ("bytes=-1", "bytes */0"),
    ],
)
def test_render_download_rejects_ranges_for_empty_files(
    rig, range_header, content_range
):
    job_id = _local_render_for_download(rig, b"")

    response = rig.client.get(
        f"/api/renders/{job_id}/download",
        headers={"Range": range_header},
    )

    assert response.status_code == 416
    assert response.content == b""
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == "0"
    assert "content-type" not in response.headers


def test_render_download_returns_empty_file_without_range(rig):
    job_id = _local_render_for_download(rig, b"")

    response = rig.client.get(f"/api/renders/{job_id}/download")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "0"
    assert response.headers["content-type"] == "video/mp4"
    assert "content-range" not in response.headers


@pytest.mark.parametrize("job_id", ["missing-job-id", "invalid.job.id"])
def test_render_download_hides_missing_jobs_and_invalid_ids(rig, job_id):
    response = rig.client.get(f"/api/renders/{job_id}/download")

    assert response.status_code == 404
    assert response.json() == {"detail": "Render not found"}


def test_render_download_hides_nonlocal_storage(rig):
    project = _create_project(rig.client, "Nonlocal render test")
    queued = rig.job_manager.submit(
        project["id"],
        "render",
        lambda progress: {"storage_backend": "s3"},
    )
    assert queued is not None
    job = rig.job_manager.wait(queued.id, timeout=5)
    assert job is not None
    assert job.status == "completed"

    response = rig.client.get(f"/api/renders/{queued.id}/download")

    assert response.status_code == 404
    assert response.json() == {"detail": "Render not found"}


def test_render_download_hides_unsafe_storage_path(rig):
    job_id = _local_render_for_download(rig, b"render")
    renders_dir = rig.settings.data_dir / "renders"
    (renders_dir / f"{job_id}.mp4").unlink()
    renders_dir.rmdir()
    outside_dir = rig.settings.data_dir.parent / "outside-renders"
    outside_dir.mkdir()
    (outside_dir / f"{job_id}.mp4").write_bytes(b"outside")
    renders_dir.symlink_to(outside_dir, target_is_directory=True)

    response = rig.client.get(f"/api/renders/{job_id}/download")

    assert response.status_code == 404
    assert response.json() == {"detail": "Render not found"}


def test_render_download_hides_missing_local_file(rig):
    job_id = _local_render_for_download(rig, b"render")
    (rig.settings.data_dir / "renders" / f"{job_id}.mp4").unlink()

    response = rig.client.get(f"/api/renders/{job_id}/download")

    assert response.status_code == 404
    assert response.json() == {"detail": "Render not found"}

