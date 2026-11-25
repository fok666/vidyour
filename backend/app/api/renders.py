"""Render job creation, polling, and local download API."""

import mimetypes
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.services.media_probe import resolve_safe_path

_RENDER_PLAN_LOCK = threading.Lock()
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _byte_range(value: str, size: int) -> tuple[int, int]:
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("Invalid byte range")
    first, separator, last = value[6:].partition("-")
    if not separator:
        raise ValueError("Invalid byte range")
    if not first:
        suffix = int(last)
        if suffix <= 0 or size <= 0:
            raise ValueError("Invalid byte range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError("Invalid byte range")
    return start, min(end, size - 1)


def _job_json(job: Any) -> dict[str, Any]:
    payload = job.model_dump()
    download_url = job.result.get("download_url") if job.result else None
    if job.status == "completed" and download_url:
        payload["download_url"] = download_url
    return payload


def create_renders_router(
    repository: Any = None,
    job_manager: Any = None,
    render_compiler: Any = None,
    storage_provider: Any = None,
    app_settings: Any = None,
) -> APIRouter:
    api = APIRouter()

    @api.post("/projects/{project_id}/render")
    def create_render(project_id: str) -> dict[str, Any]:
        if repository is None or job_manager is None:
            raise HTTPException(status_code=503, detail="Rendering is unavailable")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        composition = repository.get_composition(project_id)
        if composition is None:
            raise HTTPException(status_code=422, detail="Composition is required")
        assets = {asset.id: asset for asset in repository.list_media_assets(project_id)}
        data_dir = Path(app_settings.data_dir).resolve()
        try:
            for clip in [*composition.video_clips, *composition.audio_clips]:
                asset = assets.get(clip.asset_id)
                if asset is None:
                    raise ValueError("Unknown composition asset")
                path = resolve_safe_path(data_dir, asset.local_path)
                if not path.is_file():
                    raise ValueError("Composition asset is unavailable")
        except (OSError, ValueError):
            raise HTTPException(
                status_code=400, detail="Invalid or unsafe composition media path"
            ) from None

        def worker(progress: Any, job_id: str | None = None) -> dict[str, Any]:
            if not job_id:
                raise RuntimeError("Render failed")
            try:
                tmp_dir = Path(job_manager.tmp_dir(job_id))
                tmp_dir.mkdir(parents=True, exist_ok=True)
                output_path = resolve_safe_path(
                    data_dir, Path("tmp") / job_id / "render.mp4"
                )
                if render_compiler is None:
                    from app import config as app_config
                    from app.services import render_compiler as compiler

                    # The compiler's public API uses app.config.settings for
                    # path resolution. Scope its data root while constructing
                    # the immutable plan, then restore it before running FFmpeg.
                    with _RENDER_PLAN_LOCK:
                        previous_settings = app_config.settings
                        app_config.settings = app_settings
                        try:
                            plan = compiler.build_render_plan(
                                project, composition, assets, output_path
                            )
                        finally:
                            app_config.settings = previous_settings
                    output_file = compiler.execute_render_plan(plan, progress)
                    duration_sec = plan.total_duration_sec
                else:
                    plan = render_compiler.build_render_plan(
                        project, composition, assets, output_path
                    )
                    output_file = render_compiler.execute_render_plan(plan, progress)
                    duration_sec = plan.total_duration_sec

                stored = storage_provider.store_render(
                    output_file,
                    project_id,
                    job_id,
                    project.name,
                    duration_sec,
                )
                if stored is None:
                    raise RuntimeError("Storage did not return a render")
                return stored.model_dump()
            except Exception:
                # Never place local paths, command arguments, or provider
                # responses in a user-visible render job error.
                raise RuntimeError("Render failed") from None

        try:
            job = job_manager.submit(project_id, "render", worker)
        except Exception:
            raise HTTPException(
                status_code=503, detail="Render job could not be started"
            ) from None
        if job is None:
            raise HTTPException(
                status_code=503, detail="Render job could not be started"
            )
        return job.model_dump()

    @api.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        if job_manager is None:
            raise HTTPException(status_code=503, detail="Jobs are unavailable")
        job = job_manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _job_json(job)

    @api.get("/projects/{project_id}/jobs")
    def list_jobs(project_id: str) -> list[Any]:
        if repository is None or job_manager is None:
            raise HTTPException(status_code=503, detail="Jobs are unavailable")
        if repository.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return [_job_json(job) for job in job_manager.list_for_project(project_id)]

    @api.get("/renders/{job_id}/download")
    def download_render(
        job_id: str,
        request: Request,
        download: int = 0,
    ) -> Response:
        if not _JOB_ID.fullmatch(job_id):
            raise HTTPException(status_code=404, detail="Render not found")
        job = job_manager.get(job_id) if job_manager is not None else None
        if job is None or job.status != "completed":
            raise HTTPException(status_code=404, detail="Render not found")
        if not job.result or job.result.get("storage_backend") != "local":
            raise HTTPException(status_code=404, detail="Render not found")
        try:
            path = resolve_safe_path(
                app_settings.data_dir, Path("renders") / f"{job_id}.mp4"
            )
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Render not found") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Render not found")

        size = path.stat().st_size
        headers = {"Accept-Ranges": "bytes"}
        if download:
            headers["Content-Disposition"] = (
                f'attachment; filename="{quote(path.name, safe=".-_")}"'
            )
        range_header = request.headers.get("range")
        if not range_header:
            return Response(
                path.read_bytes(),
                media_type=mimetypes.guess_type(path.name)[0] or "video/mp4",
                headers=headers,
            )
        try:
            start, end = _byte_range(range_header, size)
        except (ValueError, OverflowError):
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        with path.open("rb") as stream:
            stream.seek(start)
            content = stream.read(end - start + 1)
        headers.update(
            {
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(len(content)),
            }
        )
        return Response(
            content,
            status_code=206,
            media_type=mimetypes.guess_type(path.name)[0] or "video/mp4",
            headers=headers,
        )

    return api


router = create_renders_router()
