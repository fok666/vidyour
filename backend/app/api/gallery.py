"""Google Photos Picker session and import routers."""

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api import AppContext, context_value
from app.config import Settings, ensure_private_directory, open_private_file
from app.services.media_probe import resolve_safe_path

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _write_private(path: str | Path, content: bytes) -> None:
    with open_private_file(path) as output:
        output.write(content)


def create_gallery_router(
    provider: Any = None,
    repository: Any = None,
    job_manager: Any = None,
    app_settings: Any = None,
    *,
    context: AppContext | Any | None = None,
) -> APIRouter:
    api = APIRouter()
    picker = context_value(context, "gallery_provider", provider)
    repo = context_value(context, "repository", repository)
    manager = context_value(context, "job_manager", job_manager)
    configured_settings = context_value(context, "settings", app_settings) or Settings()
    analyzer = context_value(context, "analyzer")
    if picker is not None and hasattr(picker, "file_writer") and picker.file_writer is None:
        picker.file_writer = _write_private

    @api.post("/gallery/google/sessions")
    def create_session() -> dict[str, Any]:
        if picker is None:
            raise HTTPException(status_code=503, detail="Google Photos is unavailable")
        try:
            return picker.create_session()
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Google Photos session creation failed"
            ) from exc

    @api.post("/gallery/google/sessions/{session_id}/import")
    def import_session(session_id: str, project_id: str) -> dict[str, Any]:
        if picker is None or repo is None or manager is None:
            raise HTTPException(status_code=503, detail="Google Photos is unavailable")
        if not _SESSION_ID.fullmatch(session_id):
            raise HTTPException(status_code=400, detail="Invalid session id")
        if repo.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")

        data_dir = Path(configured_settings.data_dir).resolve()
        configured_settings.ensure_data_dirs()

        def worker(progress: Any, job_id: str | None = None) -> dict[str, Any]:
            output_dir = Path(manager.tmp_dir(job_id)) if job_id else data_dir / "tmp" / session_id
            ensure_private_directory(output_dir)
            try:
                downloaded = picker.download_session_items(session_id, output_dir)
                assets = []
                from app.api.media import store_media_file

                for index, item in enumerate(downloaded):
                    path_value = item.get("path") if isinstance(item, dict) else None
                    if not path_value:
                        continue
                    path = resolve_safe_path(data_dir, path_value)
                    if not path.is_file():
                        continue
                    asset = store_media_file(
                        path,
                        path.name,
                        project_id,
                        repo,
                        configured_settings,
                        analyzer=analyzer,
                        source="google_photos",
                        metadata={"google_photos_item_id": str(item.get("id", ""))},
                    )
                    assets.append(asset)
                    progress(
                        int((index + 1) * 100 / max(1, len(downloaded))),
                        "Importing Google Photos media",
                    )
                return {"asset_ids": [asset.id for asset in assets]}
            except Exception:
                raise RuntimeError("Google Photos import failed") from None

        try:
            job = manager.submit(project_id, "import", worker)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Google Photos import could not be started"
            ) from exc
        if job is None:
            raise HTTPException(
                status_code=503, detail="Google Photos import could not be started"
            )
        return job.model_dump()

    return api


router = create_gallery_router()
