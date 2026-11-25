"""Beat-aligned visual part extraction interfaces."""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, get_args

from app.models import AIMoment, DetectedPart, MediaAsset, SuggestedEffect
from app.services.media_probe import _run_process, generate_thumbnail, resolve_safe_path


def _seconds_per_beat(bpm: float) -> float:
    return 60.0 / bpm


def _clip_slice_command(
    source: Path, output: Path, start_sec: float, duration_sec: float
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-ss",
        str(start_sec),
        "-i",
        str(source),
        "-t",
        str(duration_sec),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "24",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output),
    ]


def clamp_moment_to_asset(
    start_sec: float,
    beat_length: float,
    bpm: float,
    duration_sec: float,
) -> AIMoment | None:
    beat_sec = _seconds_per_beat(bpm)
    beats = beat_length
    while beats > 1 and beats * beat_sec > duration_sec:
        beats = beats / 2
    target_dur = beats * beat_sec
    start = min(max(start_sec, 0.0), max(duration_sec - target_dur, 0.0))
    return AIMoment(
        asset_id="",
        start_sec=start,
        end_sec=start + target_dur,
        beat_length=beats,
        label="",
        score=0.0,
        reason="",
        suggested_effect="none",
    )


def extract_visual_part(
    asset: MediaAsset,
    moment: AIMoment,
    bpm: float,
    data_dir: str | Path,
    process_runner: Callable[..., Any] | None = None,
    filesystem: Any = None,
    id_generator: Callable[[], str] | None = None,
    clock: Callable[[], str] | None = None,
) -> DetectedPart | None:
    source = resolve_safe_path(data_dir, asset.local_path)
    part_id = id_generator() if id_generator else str(uuid.uuid4())
    created_at = clock() if clock else datetime.now(timezone.utc).isoformat()
    thumbnail_path = f"thumbs/{part_id}.jpg"
    clip_path: str | None = None
    if asset.kind == "photo":
        start_sec = 0.0
        beat_length = moment.beat_length
        end_sec = beat_length * _seconds_per_beat(bpm)
        generate_thumbnail(
            source, thumbnail_path, data_dir=data_dir, process_runner=process_runner
        )
    else:
        clamped = clamp_moment_to_asset(
            moment.start_sec, moment.beat_length, bpm, asset.duration_sec
        )
        start_sec, end_sec, beat_length = (
            clamped.start_sec,
            clamped.end_sec,
            clamped.beat_length,
        )
        duration = end_sec - start_sec
        clip_path = f"clips/{part_id}.mp4"
        clip_output = resolve_safe_path(data_dir, clip_path)
        _run_process(
            process_runner,
            _clip_slice_command(source, clip_output, start_sec, duration),
        )
        generate_thumbnail(
            source,
            thumbnail_path,
            data_dir=data_dir,
            timestamp_sec=start_sec + duration / 2,
            process_runner=process_runner,
        )
    return DetectedPart(
        id=part_id,
        project_id=asset.project_id,
        asset_id=asset.id,
        part_type="visual_clip",
        start_sec=start_sec,
        end_sec=end_sec,
        beat_length=beat_length,
        label=moment.label,
        score=moment.score,
        ai_reason=moment.reason,
        suggested_effect=(
            moment.suggested_effect
            if moment.suggested_effect in get_args(SuggestedEffect)
            else "none"
        ),
        clip_path=clip_path,
        thumbnail_path=thumbnail_path,
        created_at=created_at,
    )


class SceneExtractor:
    def __init__(
        self,
        data_dir: str | Path = Path(),
        process_runner: Callable[..., Any] | None = None,
        filesystem: Any = None,
        id_generator: Callable[[], str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.process_runner = process_runner
        self.filesystem = filesystem
        self.id_generator = id_generator
        self.clock = clock

    def extract_visual_part(
        self,
        asset: MediaAsset,
        moment: AIMoment,
        bpm: float,
        data_dir: str | Path | None = None,
    ) -> DetectedPart | None:
        return extract_visual_part(
            asset,
            moment,
            bpm,
            data_dir if data_dir is not None else self.data_dir,
            process_runner=self.process_runner,
            filesystem=self.filesystem,
            id_generator=self.id_generator,
            clock=self.clock,
        )
