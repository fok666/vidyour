import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.models import AIMoment, MediaAsset
from app.services.scene_extractor import clamp_moment_to_asset, extract_visual_part


DATA_DIR = Path("/virtual/data")
FIXED_NOW = "2026-01-01T00:00:00Z"


def make_asset(kind, local_path, duration_sec, asset_id="asset-1"):
    return MediaAsset(
        id=asset_id,
        project_id="project-1",
        kind=kind,
        source="local",
        filename=Path(local_path).name,
        local_path=local_path,
        still_path=None,
        thumbnail_path=None,
        duration_sec=duration_sec,
        width=1920,
        height=1080,
        fps=30.0 if kind == "video" else None,
        has_audio=kind == "video",
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at=FIXED_NOW,
    )


def make_moment(start_sec, end_sec, beat_length, suggested_effect="none", asset_id="asset-1"):
    return AIMoment(
        asset_id=asset_id,
        start_sec=start_sec,
        end_sec=end_sec,
        beat_length=beat_length,
        label="Highlight",
        score=0.9,
        reason="Strong motion",
        suggested_effect=suggested_effect,
    )


def runner_stub():
    runner = Mock()
    runner.return_value = Mock(returncode=0, stdout="", stderr="")
    return runner


def filesystem_stub():
    filesystem = Mock()
    filesystem.exists.return_value = False
    filesystem.is_file.return_value = True
    filesystem.mkdir.return_value = None
    filesystem.makedirs.return_value = None
    return filesystem


def commands_of(runner):
    commands = []
    for call in runner.call_args_list:
        command = call.args[0] if call.args else call.kwargs.get("args")
        commands.append([str(part) for part in command])
    return commands


def value_after(command, flag):
    return command[command.index(flag) + 1]


def has_pair(command, flag, value):
    return any(
        command[i] == flag and command[i + 1] == value for i in range(len(command) - 1)
    )


@pytest.fixture
def patched_subprocess(monkeypatch):
    runner = runner_stub()
    monkeypatch.setattr(subprocess, "run", runner)
    return runner


def run_extract(asset, moment, part_id, runner):
    return extract_visual_part(
        asset,
        moment,
        120.0,
        DATA_DIR,
        process_runner=runner,
        filesystem=filesystem_stub(),
        id_generator=lambda: part_id,
        clock=lambda: FIXED_NOW,
    )


def test_clamp_snaps_beat_length_down_to_fit_short_asset():
    # GIVEN a 3.0s asset at 120 BPM and a request for 8 beats (4.0s) at 0.5s
    # WHEN
    clamped = clamp_moment_to_asset(0.5, 8, 120.0, 3.0)
    # THEN it snaps down to 4 beats (2.0s) within the asset bounds
    assert clamped is not None
    assert clamped.beat_length == 4
    assert clamped.start_sec >= 0.0
    assert clamped.end_sec <= 3.0
    assert clamped.end_sec - clamped.start_sec == pytest.approx(2.0)


def test_clamp_shifts_start_so_window_ends_at_asset_end():
    # GIVEN a 10.0s asset at 120 BPM and a 4-beat (2.0s) request at 8.5s
    # WHEN
    clamped = clamp_moment_to_asset(8.5, 4, 120.0, 10.0)
    # THEN the window is pulled back to [8.0, 10.0] keeping 4 beats
    assert clamped is not None
    assert clamped.start_sec == pytest.approx(8.0)
    assert clamped.end_sec == pytest.approx(10.0)
    assert clamped.beat_length == 4


def test_extract_video_part_slices_preview_clip_and_mid_clip_thumbnail(patched_subprocess):
    # GIVEN a 5.0s video and a 4-beat moment at 1.0s with mocked ffmpeg
    asset = make_asset("video", "assets/clip.mp4", 5.0)
    moment = make_moment(1.0, 3.0, 4)
    runner = runner_stub()
    # WHEN
    part = run_extract(asset, moment, "part-1", runner)
    # THEN ffmpeg slices the clip with the preview encoding flags
    commands = commands_of(runner) + commands_of(patched_subprocess)
    slice_commands = [c for c in commands if c and c[-1].endswith("clips/part-1.mp4")]
    assert len(slice_commands) == 1, commands
    clip_cmd = slice_commands[0]
    assert clip_cmd[0] == "ffmpeg"
    for flag, value in [
        ("-ss", "1.0"),
        ("-t", "2.0"),
        ("-c:v", "libx264"),
        ("-preset", "ultrafast"),
        ("-crf", "24"),
        ("-c:a", "aac"),
        ("-movflags", "+faststart"),
    ]:
        assert has_pair(clip_cmd, flag, value), (flag, value, clip_cmd)
    # AND the thumbnail is extracted at the clip midpoint (2.0s)
    thumb_commands = [c for c in commands if c and c[-1].endswith("thumbs/part-1.jpg")]
    assert len(thumb_commands) == 1, commands
    assert float(value_after(thumb_commands[0], "-ss")) == pytest.approx(2.0)
    # AND the DetectedPart references relative clip and thumbnail paths
    assert part is not None
    assert part.id == "part-1"
    assert part.clip_path == "clips/part-1.mp4"
    assert part.thumbnail_path == "thumbs/part-1.jpg"
    assert part.start_sec == pytest.approx(1.0)
    assert part.end_sec == pytest.approx(3.0)
    assert part.beat_length == 4


def test_extract_video_part_propagates_clip_slice_failure():
    # GIVEN ffmpeg fails while slicing the preview clip
    asset = make_asset("video", "assets/clip.mp4", 5.0)
    moment = make_moment(1.0, 3.0, 4)
    failure = subprocess.CalledProcessError(
        1, ["ffmpeg"], stderr="clip encoding failed"
    )
    runner = Mock(side_effect=failure)

    # WHEN / THEN the process failure propagates without attempting the thumbnail
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_extract(asset, moment, "part-clip-failure", runner)

    assert caught.value is failure
    runner.assert_called_once()
    command = commands_of(runner)[0]
    assert command[0] == "ffmpeg"
    assert command[-1].endswith("clips/part-clip-failure.mp4")
    assert runner.call_args.kwargs == {
        "capture_output": True,
        "text": True,
        "check": True,
    }


def test_extract_video_part_propagates_thumbnail_generation_failure():
    # GIVEN the clip slice succeeds but ffmpeg fails while extracting its thumbnail
    asset = make_asset("video", "assets/clip.mp4", 5.0)
    moment = make_moment(1.0, 3.0, 4)
    failure = subprocess.CalledProcessError(
        1, ["ffmpeg"], stderr="thumbnail extraction failed"
    )
    runner = Mock(
        side_effect=[
            Mock(returncode=0, stdout="", stderr=""),
            failure,
        ]
    )

    # WHEN / THEN the thumbnail process failure propagates instead of returning a part
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_extract(asset, moment, "part-thumbnail-failure", runner)

    assert caught.value is failure
    assert runner.call_count == 2
    commands = commands_of(runner)
    assert commands[0][-1].endswith("clips/part-thumbnail-failure.mp4")
    assert commands[1][-1].endswith("thumbs/part-thumbnail-failure.jpg")
    assert all(call.kwargs.get("check") is True for call in runner.call_args_list)


def test_extract_photo_part_creates_thumbnail_only_still_part(patched_subprocess):
    # GIVEN a photo asset and a 4-beat zoom_in moment with mocked ffmpeg
    asset = make_asset("photo", "assets/pic.jpg", 4.0)
    moment = make_moment(0.0, 2.0, 4, suggested_effect="zoom_in")
    runner = runner_stub()
    # WHEN
    part = run_extract(asset, moment, "part-2", runner)
    # THEN only a thumbnail is generated, no video slicing occurs
    commands = commands_of(runner) + commands_of(patched_subprocess)
    assert any(c and c[-1].endswith("thumbs/part-2.jpg") for c in commands), commands
    assert not any("libx264" in c or "-t" in c for c in commands), commands
    assert not any(c and c[-1].endswith(".mp4") for c in commands), commands
    # AND the DetectedPart is a still part with no clip
    assert part is not None
    assert part.start_sec == 0.0
    assert part.end_sec == pytest.approx(2.0)
    assert part.beat_length == 4
    assert part.suggested_effect == "zoom_in"
    assert part.clip_path is None
    assert part.thumbnail_path == "thumbs/part-2.jpg"


def test_extract_rejects_path_traversal_without_running_ffmpeg(patched_subprocess):
    # GIVEN an asset whose local_path escapes DATA_DIR
    asset = make_asset("video", "../../etc/passwd", 5.0)
    moment = make_moment(1.0, 3.0, 4)
    runner = runner_stub()
    # WHEN / THEN it raises ValueError and never invokes a subprocess
    with pytest.raises(ValueError):
        run_extract(asset, moment, "part-3", runner)
    assert runner.call_count == 0
    assert patched_subprocess.call_count == 0
