"""Media probing and local-upload utility signatures."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


_MEDIA_KIND_BY_EXTENSION = {
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".mkv": "video",
    ".jpg": "photo",
    ".jpeg": "photo",
    ".png": "photo",
    ".webp": "photo",
    ".heic": "photo",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".ogg": "audio",
    ".flac": "audio",
}


@dataclass
class MediaProbeResult:
    width: int | None = None
    height: int | None = None
    duration_sec: float = 0.0
    fps: float | None = None
    has_audio: bool = False


def resolve_safe_path(data_dir: str | Path, relative_path: str | Path, filesystem: Any = None) -> Path:
    root = Path(data_dir).resolve()
    candidate = Path(relative_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path escapes data directory: {relative_path}")
    return candidate


def sanitize_filename(filename: str) -> str:
    basename = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    stem, suffix = Path(basename).stem, Path(basename).suffix
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"[^A-Za-z0-9_-]", "", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    if not stem:
        stem = "file"
    return f"{stem}{suffix}"


def classify_media_extension(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    media_kind = _MEDIA_KIND_BY_EXTENSION.get(extension)
    if media_kind is None:
        raise ValueError(f"Unsupported media extension: {extension or '(none)'}")
    return media_kind


def _media_path(path: str | Path, data_dir: str | Path | None) -> Path:
    if data_dir is None:
        return Path(path).resolve()
    return resolve_safe_path(data_dir, path)


def _run_process(process_runner: Callable[..., Any] | None, command: list[str]) -> Any:
    runner = process_runner or subprocess.run
    return runner(command, capture_output=True, text=True, check=True)


def _parse_rate(value: Any) -> float | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            rate = float(value)
        else:
            numerator, denominator = str(value).split("/", 1)
            rate = float(numerator) / float(denominator)
        return rate if rate > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(
    path: str | Path,
    kind: str,
    data_dir: str | Path | None = None,
    process_runner: Callable[..., Any] | None = None,
    image_module: Any = None,
    filesystem: Any = None,
) -> MediaProbeResult:
    media_path = _media_path(path, data_dir)
    if kind == "photo":
        if image_module is None:
            from PIL import Image

            image_module = Image
        with image_module.open(media_path) as image:
            width, height = image.size
        return MediaProbeResult(
            width=int(width), height=int(height), duration_sec=4.0
        )

    if kind not in {"video", "audio", "live_photo"}:
        raise ValueError(f"Unsupported media kind: {kind}")

    result = _run_process(
        process_runner,
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(media_path),
        ],
    )
    metadata = json.loads(result.stdout)
    streams = metadata.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), {}
    )
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    format_data = metadata.get("format", {})
    duration = format_data.get("duration")
    if duration is None:
        duration = next(
            (stream.get("duration") for stream in streams if stream.get("duration") is not None),
            0.0,
        )
    try:
        duration_sec = float(duration or 0.0)
    except (TypeError, ValueError):
        duration_sec = 0.0
    return MediaProbeResult(
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        duration_sec=duration_sec,
        fps=_parse_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        has_audio=has_audio,
    )


def convert_image_to_jpeg(
    source_path: str | Path,
    output_path: str | Path,
    data_dir: str | Path | None = None,
    process_runner: Callable[..., Any] | None = None,
    image_module: Any = None,
    filesystem: Any = None,
) -> Path:
    source = _media_path(source_path, data_dir)
    output = _media_path(output_path, data_dir)
    _run_process(
        process_runner,
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(output),
        ],
    )
    return output


def generate_thumbnail(
    source_path: str | Path,
    output_path: str | Path,
    data_dir: str | Path | None = None,
    timestamp_sec: float = 0.0,
    process_runner: Callable[..., Any] | None = None,
    image_module: Any = None,
    filesystem: Any = None,
) -> Path:
    source = _media_path(source_path, data_dir)
    output = _media_path(output_path, data_dir)
    _run_process(
        process_runner,
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(timestamp_sec),
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(output),
        ],
    )
    return output


def pair_live_photos(batch: list[Any], filesystem: Any = None) -> list[Any]:
    def get(entry: Any, name: str, default: Any = None) -> Any:
        if isinstance(entry, dict):
            return entry.get(name, default)
        return getattr(entry, name, default)

    photos = {
        Path(str(get(entry, "filename", ""))).stem.casefold(): index
        for index, entry in enumerate(batch)
        if get(entry, "kind") == "photo"
    }
    paired_video_indices: set[int] = set()
    paired_photos: dict[int, Any] = {}
    for index, entry in enumerate(batch):
        if get(entry, "kind") != "video":
            continue
        try:
            duration = float(get(entry, "duration_sec", 0))
        except (TypeError, ValueError):
            continue
        if duration > 5.0 or duration < 0:
            continue
        stem = Path(str(get(entry, "filename", ""))).stem.casefold()
        photo_index = photos.get(stem)
        if photo_index is None or photo_index in paired_photos:
            continue
        still = batch[photo_index]
        if isinstance(entry, dict):
            paired_photos[photo_index] = {
                **still,
                "kind": "live_photo",
                "motion": entry,
                "still": still,
            }
        else:
            from types import SimpleNamespace

            paired_photos[photo_index] = SimpleNamespace(
                **{
                    **vars(still),
                    "kind": "live_photo",
                    "motion": entry,
                    "still": still,
                }
            )
        paired_video_indices.add(index)

    return [
        paired_photos.get(index, entry)
        for index, entry in enumerate(batch)
        if index not in paired_video_indices
    ]
