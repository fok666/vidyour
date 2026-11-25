"""End-to-end media tests using the real FFmpeg toolchain."""

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import app.config as app_config
from app.models import (
    AIMoment,
    Composition,
    MediaAsset,
    Project,
    TimelineAudioClip,
    TimelineVisualClip,
)
from app.providers.ai.heuristic import HeuristicFallbackProvider
from app.providers.audio import BuiltinDemoAudioProvider
from app.services.audio_analyzer import AudioAnalyzer
from app.services.media_probe import (
    convert_image_to_jpeg,
    generate_thumbnail,
    probe_media,
)
from app.services.render_compiler import build_render_plan, execute_render_plan
from app.services.scene_extractor import SceneExtractor


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.fixture
def media_tools():
    if not FFMPEG or not FFPROBE:
        pytest.skip("FFmpeg and ffprobe are required for media integration tests")
    return FFMPEG, FFPROBE


def run_ffmpeg(*args):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    )


def ffprobe(path):
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    return json.loads(result.stdout)


def make_video(path: Path, duration: float, with_audio: bool = True):
    args = [
        "-f",
        "lavfi",
        "-i",
        f"color=c=red:s=320x240:r=30:d={duration}",
    ]
    if with_audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=44100:duration={duration}",
            "-shortest",
        ]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac"]
    args.append(path)
    run_ffmpeg(*args)


def make_asset(
    asset_id: str,
    kind: str,
    local_path: str,
    duration_sec: float,
    has_audio: bool = False,
) -> MediaAsset:
    return MediaAsset(
        id=asset_id,
        project_id="project-1",
        kind=kind,
        source="upload",
        filename=Path(local_path).name,
        local_path=local_path,
        still_path=None,
        thumbnail_path=None,
        duration_sec=duration_sec,
        width=320 if kind != "audio" else None,
        height=240 if kind != "audio" else None,
        fps=30.0 if kind == "video" else None,
        has_audio=has_audio,
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at="2026-11-24T00:00:00Z",
    )


def test_real_ffprobe_reports_synthetic_video_streams(tmp_path, media_tools):
    assets = tmp_path / "assets"
    assets.mkdir()
    video = assets / "probe.mp4"
    make_video(video, 2.0)

    result = probe_media("assets/probe.mp4", "video", data_dir=tmp_path)

    assert result.width == 320
    assert result.height == 240
    assert 1.8 <= result.duration_sec <= 2.2
    assert 29.0 <= result.fps <= 31.0
    assert result.has_audio is True


def test_real_ffmpeg_thumbnail_is_a_jpeg(tmp_path, media_tools):
    assets = tmp_path / "assets"
    thumbs = tmp_path / "thumbs"
    assets.mkdir()
    thumbs.mkdir()
    video = assets / "thumbnail.mp4"
    thumbnail = thumbs / "v.jpg"
    make_video(video, 2.0)

    output = generate_thumbnail(video, thumbnail, timestamp_sec=0.5)

    assert output == thumbnail
    assert thumbnail.is_file()
    with Image.open(thumbnail) as image:
        assert image.format == "JPEG"
        assert image.size == (320, 240)


def test_real_ffmpeg_converts_heic_photo_to_jpeg(tmp_path, media_tools):
    sips = shutil.which("sips")
    if not sips:
        pytest.skip("A local HEIC encoder is required to create the input fixture")
    assets = tmp_path / "assets"
    assets.mkdir()
    source = assets / "photo.heic"
    png = assets / "photo-source.png"
    Image.new("RGB", (64, 48), (210, 80, 30)).save(png)
    subprocess.run(
        [sips, "-s", "format", "heic", str(png), "--out", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )

    output = convert_image_to_jpeg(
        "assets/photo.heic", "assets/photo.jpg", data_dir=tmp_path
    )

    assert output == assets / "photo.jpg"
    assert output.is_file()
    with Image.open(output) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 48)


def test_builtin_audio_download_and_analyzer_use_real_ffmpeg(tmp_path, media_tools):
    assets = tmp_path / "assets"
    assets.mkdir()
    provider = BuiltinDemoAudioProvider(data_dir=tmp_path)

    track_path = provider.download_track("builtin-124", assets)

    assert track_path.is_file()
    assert track_path.stat().st_size > 0
    encoded = ffprobe(track_path)
    assert any(stream["codec_type"] == "audio" for stream in encoded["streams"])
    assert "mp3" in {stream["codec_name"] for stream in encoded["streams"]}

    analysis = AudioAnalyzer(data_dir=tmp_path).analyze_audio(
        "assets/builtin-124.mp3"
    )

    assert analysis is not None
    assert analysis.bpm == pytest.approx(124.0, abs=1.5)
    assert len(analysis.waveform) == 300
    assert all(
        left < right for left, right in zip(analysis.beat_times, analysis.beat_times[1:])
    )
    assert analysis.sections
    for section in analysis.sections:
        start_beat = (section.start_sec - analysis.beat_offset_sec) * analysis.bpm / 60
        end_beat = (section.end_sec - analysis.beat_offset_sec) * analysis.bpm / 60
        assert start_beat == pytest.approx(round(start_beat), abs=0.08)
        assert end_beat == pytest.approx(round(end_beat), abs=0.08)
        assert round(start_beat) % 4 == 0
        assert round(end_beat) % 4 == 0


def test_real_scene_detection_returns_moments_for_synthetic_scenes(
    tmp_path, media_tools
):
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    path = assets_dir / "scenes.mp4"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=320x240:r=30:d=1",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x240:r=30:d=1",
        "-f",
        "lavfi",
        "-i",
        "color=c=yellow:s=320x240:r=30:d=1",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        path,
    )
    asset = make_asset("scenes", "video", str(path), 3.0)

    result = HeuristicFallbackProvider().analyze_project_media(
        [asset],
        bpm=120.0,
        allowed_beat_lengths=[2, 4, 8],
        ai_context="",
        result_target="seamless_flow",
    )

    assert result is not None
    assert result.moments
    assert result.moments[0].beat_length in {2, 4, 8}
    assert result.suggested_sequence
    assert result.suggested_sequence[0] == asset.id


def test_real_ffmpeg_scene_extractor_writes_valid_clip_and_thumbnail(
    tmp_path, media_tools
):
    assets_dir = tmp_path / "assets"
    (tmp_path / "clips").mkdir()
    (tmp_path / "thumbs").mkdir()
    assets_dir.mkdir()
    video = assets_dir / "source.mp4"
    make_video(video, 5.0)
    asset = make_asset("source", "video", "assets/source.mp4", 5.0, has_audio=True)
    moment = AIMoment(
        asset_id=asset.id,
        start_sec=1.0,
        end_sec=3.0,
        beat_length=4,
        label="scene",
        score=0.9,
        reason="synthetic test scene",
        suggested_effect="none",
    )

    part = SceneExtractor(
        data_dir=tmp_path,
        id_generator=lambda: "part-1",
        clock=lambda: "2026-11-24T00:00:00Z",
    ).extract_visual_part(asset, moment, bpm=120.0)

    assert part is not None
    clip = tmp_path / part.clip_path
    thumbnail = tmp_path / part.thumbnail_path
    assert clip.is_file()
    streams = ffprobe(clip)["streams"]
    assert any(
        stream["codec_type"] == "video" and stream["codec_name"] == "h264"
        for stream in streams
    )
    assert any(
        stream["codec_type"] == "audio" and stream["codec_name"] == "aac"
        for stream in streams
    )
    duration = float(ffprobe(clip)["format"]["duration"])
    assert 1.85 <= duration <= 2.15
    with Image.open(thumbnail) as image:
        assert image.format == "JPEG"


def test_real_ffmpeg_render_plan_writes_h264_aac_and_reports_progress(
    tmp_path, media_tools, monkeypatch
):
    assets_dir = tmp_path / "assets"
    renders_dir = tmp_path / "renders"
    assets_dir.mkdir()
    renders_dir.mkdir()
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    video_path = assets_dir / "video.mp4"
    photo_path = assets_dir / "photo.png"
    audio_path = assets_dir / "audio.wav"
    make_video(video_path, 1.0)
    Image.new("RGB", (320, 240), (40, 100, 220)).save(photo_path)
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=220:sample_rate=44100:duration=2",
        "-c:a",
        "pcm_s16le",
        audio_path,
    )

    project = Project(
        id="project-1",
        name="Integration render",
        bpm=120.0,
        beat_offset_sec=0.0,
        resolution_w=320,
        resolution_h=240,
        fps=30,
        ai_context="",
        result_target="high_energy",
        suggested_sequence=[],
        created_at="2026-11-24T00:00:00Z",
        updated_at="2026-11-24T00:00:00Z",
    )
    video_asset = make_asset(
        "video", "video", "assets/video.mp4", 1.0, has_audio=True
    )
    photo_asset = make_asset("photo", "photo", "assets/photo.png", 1.0)
    audio_asset = make_asset("audio", "audio", "assets/audio.wav", 2.0)
    composition = Composition(
        project_id=project.id,
        snap_subdivision="beat",
        video_clips=[
            TimelineVisualClip(
                id="video-clip",
                asset_id=video_asset.id,
                part_id=None,
                kind="video",
                start_sec=0.0,
                duration_sec=1.0,
                trim_start_sec=0.0,
                transition_in="none",
                ken_burns="none",
                color_preset="original",
                volume=0.0,
                muted=True,
                fade_in_sec=0.0,
                fade_out_sec=0.0,
            ),
            TimelineVisualClip(
                id="photo-clip",
                asset_id=photo_asset.id,
                part_id=None,
                kind="photo",
                start_sec=1.0,
                duration_sec=1.0,
                trim_start_sec=0.0,
                transition_in="none",
                ken_burns="zoom_in",
                color_preset="original",
                volume=0.0,
                muted=True,
                fade_in_sec=0.0,
                fade_out_sec=0.0,
            ),
        ],
        audio_clips=[
            TimelineAudioClip(
                id="audio-clip",
                asset_id=audio_asset.id,
                part_id=None,
                track_index=0,
                start_sec=0.0,
                duration_sec=2.0,
                trim_start_sec=0.0,
                volume=0.8,
                muted=False,
                fade_in_sec=0.5,
                fade_out_sec=0.5,
            )
        ],
        updated_at="2026-11-24T00:00:00Z",
    )
    plan = build_render_plan(
        project,
        composition,
        {asset.id: asset for asset in (video_asset, photo_asset, audio_asset)},
        "renders/final.mp4",
    )
    progress = []

    output = execute_render_plan(plan, lambda percent, _message: progress.append(percent))

    assert output == renders_dir / "final.mp4"
    assert output.is_file()
    streams = ffprobe(output)["streams"]
    assert any(
        stream["codec_type"] == "video" and stream["codec_name"] == "h264"
        for stream in streams
    )
    assert any(
        stream["codec_type"] == "audio" and stream["codec_name"] == "aac"
        for stream in streams
    )
    assert progress
    assert all(5 <= percent <= 90 for percent in progress)
    assert progress == sorted(progress)
