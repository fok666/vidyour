"""FastAPI application factory and production dependency wiring."""

import sqlite3
import threading
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import AppContext
from app.api.analysis import create_analysis_router
from app.api.audio_catalog import create_audio_catalog_router
from app.api.compositions import create_compositions_router
from app.api.config_api import create_config_router
from app.api.gallery import create_gallery_router
from app.api.gallery_oauth import OAuthTransactionStore, create_oauth_router
from app.api.media import create_media_router
from app.api.projects import create_projects_router
from app.api.renders import create_renders_router
from app.config import Settings, settings
from app.db import Database
from app.jobs import JobManager
from app.providers.ai import select_ai_provider
from app.providers.audio import get_audio_providers
from app.providers.gallery.google_photos import GooglePhotosPickerProvider
from app.providers.storage import get_storage_provider
from app.services.audio_analyzer import AudioAnalyzer
from app.services.scene_extractor import SceneExtractor


class _InMemoryGooglePhotosTokenStore:
    """Keep Google OAuth tokens in process memory for the app lifetime."""

    def __init__(self) -> None:
        self._token: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._token) if self._token is not None else None

    def save(self, token: dict[str, Any]) -> None:
        with self._lock:
            self._token = dict(token)

    def clear(self) -> None:
        with self._lock:
            self._token = None


def _create_google_photos_http_client() -> httpx.Client:
    """Create the app-scoped client used for Google OAuth and Picker requests."""
    return httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0))


def create_app(
    app_settings: Settings | None = None,
    repository: Any = None,
    job_manager: Any = None,
    audio_providers: list[Any] | None = None,
    gallery_provider: Any = None,
    ai_providers: Any = None,
    analyzer: Any = None,
    scene_extractor: Any = None,
    render_compiler: Any = None,
    storage_provider: Any = None,
    oauth_provider: Any = None,
    oauth_transaction_store: Any = None,
) -> FastAPI:
    configured_settings = app_settings or settings
    configured_settings.ensure_data_dirs()
    owned_google_photos_client = None

    if repository is None:
        db_path = Path(configured_settings.data_dir) / "vidyour.db"
        repository = Database(lambda: sqlite3.connect(db_path))
        repository.init_db()
    if job_manager is None:
        job_manager = JobManager(
            data_dir=configured_settings.data_dir,
            repository=repository,
        )
    if analyzer is None:
        analyzer = AudioAnalyzer(data_dir=configured_settings.data_dir)
    if scene_extractor is None:
        scene_extractor = SceneExtractor(data_dir=configured_settings.data_dir)
    if audio_providers is None:
        audio_providers = get_audio_providers(configured_settings)
    if gallery_provider is None:
        owned_google_photos_client = _create_google_photos_http_client()
        gallery_provider = GooglePhotosPickerProvider(
            configured_settings,
            client=owned_google_photos_client,
            token_store=_InMemoryGooglePhotosTokenStore(),
        )
    if oauth_provider is None:
        oauth_provider = gallery_provider
    if oauth_transaction_store is None:
        oauth_transaction_store = OAuthTransactionStore()
    if ai_providers is None:
        ai_providers = select_ai_provider(configured_settings)
    if storage_provider is None:
        storage_provider = get_storage_provider(configured_settings)

    context = AppContext(
        settings=configured_settings,
        repository=repository,
        job_manager=job_manager,
        audio_providers=audio_providers,
        gallery_provider=gallery_provider,
        analyzer=analyzer,
        oauth_provider=oauth_provider,
        oauth_transaction_store=oauth_transaction_store,
    )
    application = FastAPI(title="VidYour")
    if owned_google_photos_client is not None:
        application.router.on_shutdown.append(owned_google_photos_client.close)
    application.state.settings = configured_settings
    application.state.repository = repository
    application.state.job_manager = job_manager
    application.state.context = context

    allowed_origins = [
        origin.strip()
        for origin in str(configured_settings.cors_origins or "").split(",")
        if origin.strip() and origin.strip() != "*"
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1"],
    )
    application.include_router(create_config_router(configured_settings), prefix="/api")
    application.include_router(create_projects_router(repository), prefix="/api")
    application.include_router(
        create_media_router(context=context),
        prefix="/api",
    )
    application.include_router(create_gallery_router(context=context), prefix="/api")
    application.include_router(
        create_audio_catalog_router(context=context), prefix="/api"
    )
    application.include_router(
        create_analysis_router(
            repository,
            job_manager,
            analyzer,
            scene_extractor,
            ai_providers,
            configured_settings,
        ),
        prefix="/api",
    )
    application.include_router(
        create_compositions_router(repository, configured_settings), prefix="/api"
    )
    application.include_router(
        create_renders_router(
            repository,
            job_manager,
            render_compiler,
            storage_provider,
            configured_settings,
        ),
        prefix="/api",
    )
    application.include_router(
        create_oauth_router(
            configured_settings,
            provider=oauth_provider,
            transaction_store=oauth_transaction_store,
        ),
        prefix="/api",
    )
    return application


app = create_app()
