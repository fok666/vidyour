"""Analysis job and detected-parts API."""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from app.models import DetectedPart
from app.providers.ai import select_ai_provider
from app.providers.ai.base import compute_allowed_beat_lengths
from app.services.media_probe import resolve_safe_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_asset(asset: Any, data_dir: Path) -> Any:
    """Give path-based providers an absolute, still-rooted asset path."""
    return asset.model_copy(
        update={"local_path": str(resolve_safe_path(data_dir, asset.local_path))}
    )


def _audio_parts(asset: Any, repository: Any) -> list[DetectedPart]:
    grid = asset.beat_grid or {}
    bpm = float(grid.get("bpm") or asset.bpm or 0)
    if bpm <= 0:
        return []
    parts = []
    for section in grid.get("sections", []):
        start = float(section.get("start_sec", 0))
        end = float(section.get("end_sec", 0))
        if end <= start:
            continue
        parts.append(
            DetectedPart(
                id=str(uuid.uuid4()),
                project_id=asset.project_id,
                asset_id=asset.id,
                part_type="audio_segment",
                start_sec=start,
                end_sec=end,
                beat_length=float(
                    section.get("beat_length")
                    or (end - start) * bpm / 60.0
                ),
                label=str(section.get("label") or "Audio"),
                score=1.0,
                ai_reason="Audio energy section",
                clip_path=asset.local_path,
                thumbnail_path=asset.thumbnail_path,
                created_at=repository.clock(),
            )
        )
    return parts


def run_analysis(
    project_id: str,
    repository: Any,
    analyzer: Any,
    scene_extractor: Any,
    providers: Any,
    data_dir: str | Path,
    progress: Any,
    app_settings: Any,
) -> dict[str, Any]:
    """Run AI analysis and extract previewable visual/audio parts."""
    project = repository.get_project(project_id)
    if project is None:
        raise ValueError("Project not found")

    assets = repository.list_media_assets(project_id)
    visual_assets = [
        asset for asset in assets if asset.kind in {"video", "photo", "live_photo"}
    ]
    audio_assets = [asset for asset in assets if asset.kind == "audio"]
    ai_provider = providers or select_ai_provider(app_settings)
    allowed_lengths = compute_allowed_beat_lengths(project.bpm)
    progress(10, "Analyzing project media")
    result = ai_provider.analyze_project_media(
        [_relative_asset(asset, Path(data_dir)) for asset in visual_assets],
        project.bpm,
        allowed_lengths,
        project.ai_context,
        project.result_target,
    )
    if result is None:
        raise RuntimeError("Media analysis did not return a result")

    for previous in repository.list_detected_parts(project_id):
        repository.delete_detected_part(previous.id)

    visual_parts = []
    moments = {moment.asset_id: moment for moment in result.moments}
    for asset in visual_assets:
        moment = moments.get(asset.id)
        if moment is None:
            continue
        part = scene_extractor.extract_visual_part(
            asset, moment, project.bpm, data_dir=data_dir
        )
        if part is not None:
            repository.create_detected_part(part)
            visual_parts.append(part)

    audio_parts = []
    for asset in audio_assets:
        for part in _audio_parts(asset, repository):
            repository.create_detected_part(part)
            audio_parts.append(part)

    sequence = [
        item
        for item in (result.suggested_sequence or [])
        if isinstance(item, (str, dict))
    ]
    if not sequence:
        sequence = [asset.id for asset in visual_assets]
    repository.update_project(project_id, suggested_sequence=sequence)
    progress(95, "Saving detected parts")
    return {
        "part_ids": [part.id for part in [*visual_parts, *audio_parts]],
        "suggested_sequence": sequence,
        "visual_parts": len(visual_parts),
        "audio_parts": len(audio_parts),
    }


def create_analysis_router(
    repository: Any = None,
    job_manager: Any = None,
    analyzer: Any = None,
    scene_extractor: Any = None,
    providers: Any = None,
    app_settings: Any = None,
) -> APIRouter:
    api = APIRouter()

    @api.post("/projects/{project_id}/analyze")
    def analyze_project(project_id: str) -> dict[str, Any]:
        if repository is None or job_manager is None:
            raise HTTPException(status_code=503, detail="Analysis is unavailable")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if analyzer is None or scene_extractor is None:
            raise HTTPException(status_code=503, detail="Analysis is unavailable")
        data_dir = Path(app_settings.data_dir).resolve()

        def worker(progress: Any, job_id: str | None = None) -> dict[str, Any]:
            return run_analysis(
                project_id,
                repository,
                analyzer,
                scene_extractor,
                providers,
                data_dir,
                progress,
                app_settings,
            )

        try:
            job = job_manager.submit(project_id, "analysis", worker)
        except Exception:
            raise HTTPException(
                status_code=503, detail="Analysis job could not be started"
            ) from None
        if job is None:
            raise HTTPException(
                status_code=503, detail="Analysis job could not be started"
            )
        return job.model_dump()

    @api.get("/projects/{project_id}/parts")
    def list_parts(project_id: str) -> list[Any]:
        if repository is None:
            raise HTTPException(status_code=503, detail="Parts are unavailable")
        if repository.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return [
            part.model_dump()
            for part in repository.list_detected_parts(project_id)
        ]

    @api.delete("/parts/{part_id}", status_code=204)
    def delete_part(part_id: str) -> Response:
        if repository is None:
            raise HTTPException(status_code=503, detail="Parts are unavailable")
        if not repository.delete_detected_part(part_id):
            raise HTTPException(status_code=404, detail="Part not found")
        return Response(status_code=204)

    return api


router = create_analysis_router()
