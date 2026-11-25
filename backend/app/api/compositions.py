"""Composition read/write and beat-aware auto-assembly API."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from app.models import Composition, TimelineAudioClip, TimelineVisualClip


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sequence_reference(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict):
        for key in ("part_id", "asset_id", "id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def create_compositions_router(
    repository: Any = None,
    app_settings: Any = None,
) -> APIRouter:
    api = APIRouter()

    @api.post("/projects/{project_id}/composition/auto-assemble")
    def auto_assemble(project_id: str) -> dict[str, Any]:
        if repository is None:
            raise HTTPException(status_code=503, detail="Composition is unavailable")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        parts = repository.list_detected_parts(project_id)
        visual_parts = [part for part in parts if part.part_type == "visual_clip"]
        parts_by_id = {part.id: part for part in visual_parts}
        parts_by_asset: dict[str, list[Any]] = {}
        for part in visual_parts:
            parts_by_asset.setdefault(part.asset_id, []).append(part)

        selected: list[Any] = []
        seen_parts: set[str] = set()
        for reference in project.suggested_sequence or []:
            value = _sequence_reference(reference)
            if value is None:
                continue
            matches = (
                [parts_by_id[value]]
                if value in parts_by_id
                else parts_by_asset.get(value, [])
            )
            for part in matches:
                if part.id not in seen_parts:
                    selected.append(part)
                    seen_parts.add(part.id)
        # An empty or stale AI sequence must not prevent a usable composition.
        if not selected:
            selected = visual_parts
        if not selected:
            raise HTTPException(status_code=422, detail="No visual parts to assemble")

        assets = {asset.id: asset for asset in repository.list_media_assets(project_id)}
        audio_asset = next((asset for asset in assets.values() if asset.kind == "audio"), None)
        audio_parts = [part for part in parts if part.part_type == "audio_segment"]
        if audio_asset is None and audio_parts:
            audio_asset = assets.get(audio_parts[0].asset_id)

        beat_sec = 60.0 / project.bpm if project.bpm > 0 else 0.5
        offset = max(0.0, float(project.beat_offset_sec))
        cursor = offset
        video_clips = []
        index = 0
        while index < 2000:
            part = selected[index % len(selected)]
            asset = assets.get(part.asset_id)
            if asset is not None:
                duration = max(0.0, float(part.end_sec) - float(part.start_sec))
                if duration > 0:
                    snapped_start = offset + round((cursor - offset) / beat_sec) * beat_sec
                    video_clips.append(
                        TimelineVisualClip(
                            id=f"V{index + 1}",
                            asset_id=asset.id,
                            part_id=part.id,
                            kind=asset.kind,
                            start_sec=snapped_start,
                            duration_sec=duration,
                            trim_start_sec=part.start_sec,
                            transition_in="none",
                            ken_burns="none",
                            color_preset="original",
                            volume=0.0 if audio_asset is not None else 1.0,
                            muted=audio_asset is not None,
                            fade_in_sec=0.0,
                            fade_out_sec=0.0,
                        )
                    )
                    cursor = snapped_start + duration
            index += 1
            if audio_asset is None or cursor >= audio_asset.duration_sec:
                break
        if not video_clips:
            raise HTTPException(status_code=422, detail="No usable visual parts")

        audio_clips = []
        if audio_asset is not None:
            audio_clips.append(
                TimelineAudioClip(
                    id="A1",
                    asset_id=audio_asset.id,
                    part_id=audio_parts[0].id if audio_parts else None,
                    track_index=0,
                    start_sec=0.0,
                    duration_sec=audio_asset.duration_sec,
                    trim_start_sec=0.0,
                    volume=1.0,
                    muted=False,
                )
            )
        composition = Composition(
            project_id=project_id,
            snap_subdivision="beat",
            video_clips=video_clips,
            audio_clips=audio_clips,
            updated_at=_now(),
        )
        saved = repository.save_composition(composition)
        if saved is None:
            raise HTTPException(status_code=500, detail="Composition could not be saved")
        return saved.model_dump()

    @api.get("/projects/{project_id}/composition")
    def get_composition(project_id: str) -> dict[str, Any]:
        if repository is None:
            raise HTTPException(status_code=503, detail="Composition is unavailable")
        if repository.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        composition = repository.get_composition(project_id)
        if composition is None:
            raise HTTPException(status_code=404, detail="Composition not found")
        return composition.model_dump()

    @api.put("/projects/{project_id}/composition")
    def save_composition(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if repository is None:
            raise HTTPException(status_code=503, detail="Composition is unavailable")
        if repository.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            composition = Composition.model_validate(
                {**payload, "project_id": project_id, "updated_at": _now()}
            )
            saved = repository.save_composition(composition)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Invalid composition") from exc
        if saved is None:
            raise HTTPException(status_code=500, detail="Composition could not be saved")
        return saved.model_dump()

    return api


router = create_compositions_router()
