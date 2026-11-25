"""Project media upload, listing, deletion, and safe file streaming routers."""

import io
import mimetypes
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.api import AppContext, context_value
from app.config import (
    Settings,
    copy_private_file,
    ensure_private_directory,
    open_private_file,
)
from app.models import MediaAsset
from app.services.audio_analyzer import AudioAnalyzer
from app.services.media_probe import (
    classify_media_extension,
    convert_image_to_jpeg,
    generate_thumbnail,
    pair_live_photos,
    probe_media,
    resolve_safe_path,
    sanitize_filename,
)

_SERVABLE_DIRECTORIES = ("assets", "clips", "thumbs")


def _write_private(path: str | Path, content: bytes) -> None:
    with open_private_file(path) as output:
        output.write(content)


def _beat_grid(result: Any) -> dict[str, Any] | None:
    if result is None or not getattr(result, "bpm", 0):
        return None
    return {
        "bpm": float(result.bpm),
        "beat_offset_sec": float(result.beat_offset_sec),
        "beat_times": list(result.beat_times),
        "downbeats": list(result.downbeats),
        "waveform": list(result.waveform),
        "sections": [
            {
                "start_sec": float(section.start_sec),
                "end_sec": float(section.end_sec),
                "beat_length": float(section.beat_length),
                "label": str(section.label),
            }
            for section in result.sections
        ],
    }


def store_media_file(
    source_path: str | Path,
    filename: str,
    project_id: str,
    repository: Any,
    app_settings: Any,
    *,
    process_runner: Any = None,
    analyzer: Any = None,
    kind: str | None = None,
    still_source_path: str | Path | None = None,
    source: str = "upload",
    metadata: dict[str, Any] | None = None,
) -> MediaAsset:
    """Persist one already-downloaded local media file using private paths."""
    data_dir = Path(app_settings.data_dir).resolve()
    app_settings.ensure_data_dirs()
    media_kind = kind or classify_media_extension(filename)
    if media_kind not in {"photo", "video", "audio", "live_photo"}:
        raise ValueError("Unsupported media kind")

    asset_id = str(uuid.uuid4())
    safe_name = sanitize_filename(filename)
    original_suffix = Path(safe_name).suffix.lower()
    source_file = Path(source_path)
    created_paths: list[Path] = []
    temporary_paths: list[Path] = []
    try:
        if original_suffix == ".heic":
            converted_path = resolve_safe_path(data_dir, f"tmp/{asset_id}_converted.jpg")
            temporary_paths.append(converted_path)
            convert_image_to_jpeg(source_file, converted_path, data_dir, process_runner=process_runner)
            source_file = converted_path
            safe_name = f"{Path(safe_name).stem}.jpg"
            original_suffix = ".jpg"

        relative_local_path = f"assets/{asset_id}{original_suffix}"
        local_path = resolve_safe_path(data_dir, relative_local_path)
        copy_private_file(source_file, local_path)
        created_paths.append(local_path)

        relative_still_path = None
        still_path = None
        if still_source_path is not None:
            still_suffix = Path(still_source_path).suffix.lower()
            if still_suffix == ".heic":
                converted_still = resolve_safe_path(
                    data_dir, f"tmp/{asset_id}_still.jpg"
                )
                temporary_paths.append(converted_still)
                convert_image_to_jpeg(
                    still_source_path,
                    converted_still,
                    data_dir,
                    process_runner=process_runner,
                )
                still_source_path = converted_still
                still_suffix = ".jpg"
            relative_still_path = f"assets/{asset_id}_still{still_suffix}"
            still_path = resolve_safe_path(data_dir, relative_still_path)
            copy_private_file(still_source_path, still_path)
            created_paths.append(still_path)

        probe_path = local_path if media_kind != "live_photo" else local_path
        probe_kind = "video" if media_kind == "live_photo" else media_kind
        probed = probe_media(
            probe_path,
            probe_kind,
            data_dir,
            process_runner=process_runner,
        )

        thumbnail_path = None
        relative_thumbnail_path = None
        if media_kind in {"photo", "live_photo"}:
            image_source = still_path if still_path is not None else local_path
            image = None
            try:
                from PIL import Image

                with Image.open(image_source) as opened:
                    image = opened.convert("RGB")
                thumbnail_bytes = io.BytesIO()
                image.thumbnail((512, 512))
                image.save(thumbnail_bytes, format="JPEG", optimize=True)
                relative_thumbnail_path = f"thumbs/{asset_id}.jpg"
                thumbnail_path = resolve_safe_path(data_dir, relative_thumbnail_path)
                _write_private(thumbnail_path, thumbnail_bytes.getvalue())
                created_paths.append(thumbnail_path)
            finally:
                if image is not None:
                    image.close()
        elif media_kind == "video":
            thumb_temp = resolve_safe_path(data_dir, f"tmp/{asset_id}_thumb.jpg")
            temporary_paths.append(thumb_temp)
            try:
                generate_thumbnail(
                    local_path,
                    thumb_temp,
                    data_dir,
                    process_runner=process_runner,
                )
                relative_thumbnail_path = f"thumbs/{asset_id}.jpg"
                thumbnail_path = resolve_safe_path(data_dir, relative_thumbnail_path)
                copy_private_file(thumb_temp, thumbnail_path)
                created_paths.append(thumbnail_path)
            except Exception:
                # A preview failure should not discard an otherwise valid video.
                thumbnail_path = None
                relative_thumbnail_path = None

        analysis = None
        beat_grid = None
        bpm = None
        if media_kind == "audio":
            audio_analyzer = analyzer or AudioAnalyzer(data_dir=data_dir)
            analysis = audio_analyzer.analyze_audio(local_path)
            beat_grid = _beat_grid(analysis)
            if beat_grid is not None:
                bpm = beat_grid["bpm"]
                has_persisted_audio_analysis = any(
                    asset.kind == "audio" and asset.beat_grid is not None
                    for asset in repository.list_media_assets(project_id)
                )
                if not has_persisted_audio_analysis:
                    repository.update_project(
                        project_id,
                        bpm=bpm,
                        beat_offset_sec=beat_grid["beat_offset_sec"],
                    )

        asset = MediaAsset(
            id=asset_id,
            project_id=project_id,
            kind=media_kind,
            source=source,
            filename=safe_name,
            local_path=relative_local_path,
            still_path=relative_still_path,
            thumbnail_path=relative_thumbnail_path,
            duration_sec=probed.duration_sec,
            width=probed.width,
            height=probed.height,
            fps=probed.fps,
            has_audio=probed.has_audio,
            bpm=bpm,
            beat_grid=beat_grid,
            metadata=dict(metadata or {}),
            created_at=repository.clock(),
        )
        repository.create_media_asset(asset)
        return asset
    except Exception:
        for path in created_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        for path in temporary_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _allowed_file_path(data_dir: Path, rel_path: str) -> Path:
    normalized = rel_path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not candidate.parts or candidate.parts[0] not in _SERVABLE_DIRECTORIES:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        resolved = resolve_safe_path(data_dir, Path(*candidate.parts))
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid file path") from None
    category_path = data_dir / candidate.parts[0]
    allowed_root = category_path.resolve()
    if category_path.is_symlink() or allowed_root != category_path:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not resolved.is_relative_to(allowed_root):
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


def _parse_range(value: str, size: int) -> tuple[int, int]:
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("Invalid byte range")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("Invalid byte range")
    first, last = spec.split("-", 1)
    if not first:
        suffix_length = int(last)
        if suffix_length <= 0 or size <= 0:
            raise ValueError("Invalid byte range")
        return max(0, size - suffix_length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError("Invalid byte range")
    return start, min(end, size - 1)


def create_media_router(
    repository: Any = None,
    app_settings: Any = None,
    process_runner: Any = None,
    filesystem: Any = None,
    *,
    context: AppContext | Any | None = None,
) -> APIRouter:
    api = APIRouter()
    repo = context_value(context, "repository", repository)
    configured_settings = context_value(context, "settings", app_settings) or Settings()
    analyzer = context_value(context, "analyzer")

    @api.post("/projects/{project_id}/media/upload")
    def upload_media(
        project_id: str,
        files: list[UploadFile] = File(default=[]),
    ) -> list[Any]:
        if repo is None:
            raise HTTPException(status_code=503, detail="Media repository unavailable")
        if repo.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if not files:
            raise HTTPException(status_code=400, detail="No media files provided")

        for upload in files:
            try:
                classify_media_extension(upload.filename or "")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        data_dir = Path(configured_settings.data_dir).resolve()
        configured_settings.ensure_data_dirs()
        batch_dir = resolve_safe_path(data_dir, f"tmp/upload-{uuid.uuid4()}")
        ensure_private_directory(batch_dir)
        staged: list[dict[str, Any]] = []
        created_assets: list[MediaAsset] = []
        try:
            for upload in files:
                filename = sanitize_filename(upload.filename or "file")
                kind = classify_media_extension(filename)
                path = resolve_safe_path(data_dir, f"tmp/upload-{uuid.uuid4()}{Path(filename).suffix.lower()}")
                _write_private(path, upload.file.read())
                entry: dict[str, Any] = {
                    "filename": filename,
                    "kind": kind,
                    "path": path,
                    "duration_sec": 0.0,
                }
                if kind in {"video", "audio"}:
                    probe = probe_media(
                        path, kind, data_dir, process_runner=process_runner
                    )
                    entry["duration_sec"] = probe.duration_sec
                staged.append(entry)

            paired = pair_live_photos(staged)
            for item in paired:
                if item.get("kind") == "live_photo":
                    still = item["still"]
                    motion = item["motion"]
                    asset = store_media_file(
                        motion["path"],
                        still["filename"],
                        project_id,
                        repo,
                        configured_settings,
                        process_runner=process_runner,
                        analyzer=analyzer,
                        kind="live_photo",
                        still_source_path=still["path"],
                    )
                    # Preserve the motion duration in the returned live-photo record.
                    asset.duration_sec = float(motion.get("duration_sec", 0.0))
                else:
                    asset = store_media_file(
                        item["path"],
                        item["filename"],
                        project_id,
                        repo,
                        configured_settings,
                        process_runner=process_runner,
                        analyzer=analyzer,
                    )
                created_assets.append(asset)
            return [asset.model_dump() for asset in created_assets]
        except HTTPException:
            raise
        except Exception as exc:
            for asset in created_assets:
                repo.delete_media_asset(asset.id)
                _remove_asset_files(configured_settings, asset)
            raise HTTPException(
                status_code=400, detail="Unable to process uploaded media"
            ) from exc
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)
            for item in staged:
                item["path"].unlink(missing_ok=True)

    @api.get("/projects/{project_id}/media")
    def list_media(project_id: str) -> list[Any]:
        if repo is None:
            raise HTTPException(status_code=503, detail="Media repository unavailable")
        if repo.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return [asset.model_dump() for asset in repo.list_media_assets(project_id)]

    @api.delete("/media/{asset_id}", status_code=204)
    def delete_media(asset_id: str) -> Response:
        if repo is None:
            raise HTTPException(status_code=503, detail="Media repository unavailable")
        asset = repo.get_media_asset(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="Media not found")
        repo.delete_media_asset(asset_id)
        _remove_asset_files(configured_settings, asset)
        return Response(status_code=204)

    @api.get("/files/{rel_path:path}")
    def get_file(request: Request, rel_path: str) -> Response:
        data_dir = Path(configured_settings.data_dir).resolve()
        try:
            path = _allowed_file_path(data_dir, rel_path)
        except HTTPException:
            raise
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_header = request.headers.get("range")
        headers = {"Accept-Ranges": "bytes"}
        if not range_header:
            return Response(path.read_bytes(), media_type=content_type, headers=headers)
        try:
            start, end = _parse_range(range_header, size)
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
            media_type=content_type,
            headers=headers,
        )

    return api


def _remove_asset_files(app_settings: Any, asset: MediaAsset) -> None:
    data_dir = Path(app_settings.data_dir).resolve()
    for relative in (
        asset.local_path,
        asset.still_path,
        asset.thumbnail_path,
    ):
        if not relative:
            continue
        try:
            path = _allowed_file_path(data_dir, relative)
        except HTTPException:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


router = create_media_router()
