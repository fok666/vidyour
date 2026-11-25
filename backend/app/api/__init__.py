"""FastAPI API router modules and their shared application dependencies."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppContext:
    """Dependencies shared by API routers.

    Routers accept this object explicitly so tests and alternate application
    entry points use the same real collaborators as the production app. The
    optional fields keep narrow routers usable without unrelated services.
    """

    settings: Any = None
    repository: Any = None
    job_manager: Any = None
    audio_providers: list[Any] = field(default_factory=list)
    gallery_provider: Any = None
    analyzer: Any = None
    oauth_provider: Any = None
    oauth_transaction_store: Any = None


def context_value(context: AppContext | Any | None, name: str, fallback: Any = None) -> Any:
    """Return one dependency from an AppContext, with legacy factory fallback."""
    if context is not None:
        value = getattr(context, name, None)
        if value is not None:
            return value
    return fallback


def mount_ingestion_routers(application: Any, context: AppContext) -> None:
    """Mount the ingestion API using only collaborators from ``context``."""
    from app.api.audio_catalog import create_audio_catalog_router
    from app.api.config_api import create_config_router
    from app.api.gallery import create_gallery_router
    from app.api.media import create_media_router
    from app.api.projects import create_projects_router

    application.state.context = context
    application.include_router(create_config_router(context=context), prefix="/api")
    application.include_router(create_projects_router(context=context), prefix="/api")
    application.include_router(create_media_router(context=context), prefix="/api")
    application.include_router(create_gallery_router(context=context), prefix="/api")
    application.include_router(create_audio_catalog_router(context=context), prefix="/api")
    if context.oauth_provider is not None:
        from app.api.gallery_oauth import create_oauth_router

        application.include_router(
            create_oauth_router(
                context.settings,
                provider=context.oauth_provider,
                transaction_store=context.oauth_transaction_store,
            ),
            prefix="/api",
        )

