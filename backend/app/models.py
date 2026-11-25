"""Pydantic API and domain models."""

from typing import Any, Literal

from pydantic import BaseModel, Field

SuggestedEffect = Literal[
    "none",
    "zoom_in",
    "zoom_out",
    "pan_lr",
    "crossfade",
    "fade_black",
    "wipe_left",
]


class Project(BaseModel):
    id: str
    name: str
    bpm: float = 120.0
    beat_offset_sec: float = 0.0
    resolution_w: int = 1280
    resolution_h: int = 720
    fps: int = 30
    ai_context: str = ""
    result_target: str = "seamless_flow"
    suggested_sequence: list[Any] = Field(default_factory=list)
    created_at: str
    updated_at: str


class MediaAsset(BaseModel):
    id: str
    project_id: str
    kind: str
    source: str
    filename: str
    local_path: str
    still_path: str | None
    thumbnail_path: str | None
    duration_sec: float
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool
    bpm: float | None
    beat_grid: dict[str, Any] | None
    metadata: dict[str, Any]
    created_at: str


class DetectedPart(BaseModel):
    id: str
    project_id: str
    asset_id: str
    part_type: str
    start_sec: float
    end_sec: float
    beat_length: float
    label: str
    score: float
    ai_reason: str
    suggested_effect: SuggestedEffect = "none"
    clip_path: str | None
    thumbnail_path: str | None
    created_at: str


class AIMoment(BaseModel):
    asset_id: str
    start_sec: float
    end_sec: float
    beat_length: float
    label: str
    score: float
    reason: str
    suggested_effect: str


class TimelineVisualClip(BaseModel):
    id: str
    asset_id: str
    part_id: str | None
    kind: str
    start_sec: float
    duration_sec: float
    trim_start_sec: float
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    transition_in: Literal["none", "crossfade", "fade_black", "wipe_left"]
    transition_duration_sec: float = 0.5
    ken_burns: Literal["none", "zoom_in", "zoom_out", "pan_lr"]
    color_preset: Literal["original", "warm", "cool", "bw", "vivid", "vignette"]
    volume: float = Field(default=0.0, ge=0.0, le=1.5)
    muted: bool
    fade_in_sec: float
    fade_out_sec: float


class TimelineAudioClip(BaseModel):
    id: str
    asset_id: str
    part_id: str | None
    track_index: Literal[0, 1]
    start_sec: float
    duration_sec: float
    trim_start_sec: float
    volume: float = Field(default=1.0, ge=0.0, le=1.5)
    muted: bool
    fade_in_sec: float = 0.0
    fade_out_sec: float = 0.0


class Composition(BaseModel):
    project_id: str
    snap_subdivision: str = "beat"
    video_clips: list[TimelineVisualClip]
    audio_clips: list[TimelineAudioClip]
    updated_at: str


class Job(BaseModel):
    id: str
    project_id: str
    kind: str
    status: str
    progress: int
    message: str
    result: dict[str, Any]
    error: str | None
    created_at: str
    updated_at: str


class AIAnalysisResult(BaseModel):
    moments: list[AIMoment]
    suggested_sequence: list[Any]


class AudioCatalogTrack(BaseModel):
    id: str
    provider: str
    title: str
    artist: str
    bpm: float | None
    duration_sec: float
    preview_url: str
    download_url: str
    license: str
    attribution: str


class StoredRender(BaseModel):
    storage_backend: str
    s3_key: str | None
    download_url: str
    duration_sec: float
    size_bytes: int
