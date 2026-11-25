"""Runtime capability endpoint."""

from typing import Any

from fastapi import APIRouter

from app.api import AppContext, context_value
from app.config import Settings


router = APIRouter()


@router.get("/config/capabilities")
def get_capabilities(app_settings: Settings | None = None) -> dict[str, Any]:
    return (app_settings or Settings()).capabilities()


def create_config_router(
    app_settings: Settings | None = None,
    *,
    context: AppContext | Any | None = None,
) -> APIRouter:
    api = APIRouter()
    configured_settings = context_value(context, "settings", app_settings) or Settings()

    @api.get("/config/capabilities")
    def capabilities() -> dict[str, Any]:
        return configured_settings.capabilities()

    return api
