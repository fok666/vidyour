"""Royalty-free audio search and project-import routers."""

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api import AppContext, context_value
from app.config import Settings, ensure_private_directory, open_private_file
from app.providers.audio import get_audio_providers, search_audio_catalog

_TRACK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _secure_provider_writer(path: str | Path, content: bytes) -> None:
    with open_private_file(path) as output:
        output.write(content)


def create_audio_catalog_router(
    providers: list[Any] | None = None,
    repository: Any = None,
    analyzer: Any = None,
    app_settings: Any = None,
    filesystem: Any = None,
    *,
    context: AppContext | Any | None = None,
) -> APIRouter:
    api = APIRouter()
    repo = context_value(context, "repository", repository)
    configured_settings = context_value(context, "settings", app_settings) or Settings()
    audio_analyzer = context_value(context, "analyzer", analyzer)
    catalog_providers = context_value(context, "audio_providers", providers)
    if catalog_providers is None or catalog_providers == []:
        catalog_providers = get_audio_providers(configured_settings)

    @api.get("/audio-catalog/search")
    def search_audio(
        provider: str = "all",
        q: str = "",
        bpm_min: float | None = None,
        bpm_max: float | None = None,
    ) -> list[Any]:
        tracks = search_audio_catalog(
            provider=provider,
            q=q,
            bpm_min=bpm_min,
            bpm_max=bpm_max,
            providers=catalog_providers,
        )
        return [track.model_dump() for track in tracks]

    @api.post("/projects/{project_id}/audio-catalog/import")
    def import_audio_track(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            raise HTTPException(status_code=503, detail="Audio import is unavailable")
        if repo.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        provider_name = payload.get("provider")
        track_id = payload.get("track_id")
        if (
            not isinstance(provider_name, str)
            or not isinstance(track_id, str)
            or not _TRACK_ID.fullmatch(track_id)
        ):
            raise HTTPException(status_code=400, detail="Invalid audio track selection")

        selected = next(
            (
                item
                for item in catalog_providers
                if getattr(item, "name", "").lower() == provider_name.lower()
            ),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="Audio track not found")
        try:
            matches = selected.search()
            track = next(
                (item for item in matches if str(item.id) == track_id),
                None,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Audio provider request failed"
            ) from exc
        if track is None:
            raise HTTPException(status_code=404, detail="Audio track not found")

        data_dir = Path(configured_settings.data_dir).resolve()
        configured_settings.ensure_data_dirs()
        temp_dir = data_dir / "tmp" / f"audio-import-{uuid.uuid4()}"
        ensure_private_directory(temp_dir)
        old_writer = getattr(selected, "file_writer", None)
        try:
            # Providers receive only their own public ID. Client-supplied URLs
            # are neither accepted nor used to retrieve the selected media.
            if hasattr(selected, "file_writer") and old_writer is None:
                selected.file_writer = _secure_provider_writer
            output_path = Path(selected.download_track(track_id, temp_dir))
            resolved_output = output_path.resolve()
            if not resolved_output.is_relative_to(temp_dir.resolve()) or not resolved_output.is_file():
                raise ValueError("Provider returned an invalid local audio file")
            os.chmod(resolved_output, 0o600)
            from app.api.media import store_media_file

            asset = store_media_file(
                resolved_output,
                f"{track_id}{resolved_output.suffix.lower()}",
                project_id,
                repo,
                configured_settings,
                analyzer=audio_analyzer,
                source="audio_catalog",
                metadata={
                    "provider": provider_name,
                    "track_id": track_id,
                    "title": track.title,
                    "artist": track.artist,
                    "license": track.license,
                    "attribution": track.attribution,
                },
            )
            return asset.model_dump()
        except HTTPException:
            raise
        except Exception as exc:
            # Do not expose provider URLs, credentials, or raw upstream errors.
            raise HTTPException(
                status_code=502, detail="Audio provider import failed"
            ) from exc
        finally:
            if hasattr(selected, "file_writer") and old_writer is None:
                selected.file_writer = None
            shutil.rmtree(temp_dir, ignore_errors=True)

    return api


router = create_audio_catalog_router()
