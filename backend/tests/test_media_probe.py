import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.media_probe import (
    MediaProbeResult,
    classify_media_extension,
    convert_image_to_jpeg,
    generate_thumbnail,
    pair_live_photos,
    probe_media,
    resolve_safe_path,
    sanitize_filename,
)


DATA_DIR = Path("/virtual/data")


class StubImage:
    width = 640
    height = 480
    size = (640, 480)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def image_module():
    module = Mock()
    module.open.return_value = StubImage()
    return module


def filesystem_stub():
    filesystem = Mock()
    filesystem.exists.return_value = False
    filesystem.is_file.return_value = True
    filesystem.mkdir.return_value = None
    filesystem.makedirs.return_value = None
    return filesystem


def exception_from(call):
    try:
        call()
    except Exception as error:
        return error
    return None


def field(entry, name):
    if isinstance(entry, dict):
        return entry[name]
    return getattr(entry, name)


def entry_filename(entry):
    return field(entry, "filename")


def test_resolve_safe_path_rejects_traversal_outside_data_dir():
    result = exception_from(
        lambda: resolve_safe_path(DATA_DIR, "../etc/passwd", filesystem_stub())
    )

    assert isinstance(result, ValueError)


def test_resolve_safe_path_returns_normalized_path_inside_data_dir():
    result = resolve_safe_path(DATA_DIR, "assets/video.mp4", filesystem_stub())

    assert result == Path("/virtual/data/assets/video.mp4")


def test_resolve_safe_path_collapses_safe_dot_dot_segments():
    result = resolve_safe_path(
        DATA_DIR, "assets/tmp/../video.mp4", filesystem_stub()
    )

    assert result == Path("/virtual/data/assets/video.mp4")


def test_sanitize_filename_returns_safe_basename_and_preserves_extension_case():
    result = sanitize_filename("../../My Clip (1)!.MP4")

    assert result == "My_Clip_1.MP4"


def test_sanitize_filename_uses_file_when_name_has_no_safe_stem_characters():
    result = sanitize_filename("!!!.MP4")

    assert result == "file.MP4"


def test_classify_media_extension_accepts_every_supported_upload_type():
    filenames_and_kinds = [
        ("clip.mp4", "video"),
        ("clip.mov", "video"),
        ("clip.webm", "video"),
        ("clip.mkv", "video"),
        ("photo.jpg", "photo"),
        ("photo.jpeg", "photo"),
        ("photo.png", "photo"),
        ("photo.webp", "photo"),
        ("photo.heic", "photo"),
        ("track.mp3", "audio"),
        ("track.wav", "audio"),
        ("track.m4a", "audio"),
        ("track.ogg", "audio"),
        ("track.flac", "audio"),
    ]

    result = [
        (filename, classify_media_extension(filename))
        for filename, _expected_kind in filenames_and_kinds
    ]

    assert result == filenames_and_kinds


def test_classify_media_extension_reports_unsupported_extension():
    error = exception_from(lambda: classify_media_extension("malware.exe"))

    assert isinstance(error, ValueError) and ".exe" in str(error)


def test_probe_media_extracts_video_metadata_from_ffprobe_json():
    ffprobe_json = {
        "format": {"duration": "2.0"},
        "streams": [
            {
                "codec_type": "video",
                "width": 320,
                "height": 240,
                "r_frame_rate": "30/1",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(ffprobe_json), stderr=""
        )
    )

    result = probe_media(
        DATA_DIR / "assets/v.mp4",
        kind="video",
        data_dir=DATA_DIR,
        process_runner=runner,
        filesystem=filesystem_stub(),
    )

    assert result == MediaProbeResult(
        width=320,
        height=240,
        duration_sec=2.0,
        fps=30.0,
        has_audio=True,
    )


def test_probe_media_accepts_numeric_frame_rate():
    ffprobe_json = {
        "format": {"duration": "2.0"},
        "streams": [{"codec_type": "video", "r_frame_rate": 29.97}],
    }
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(ffprobe_json), stderr=""
        )
    )

    result = probe_media(
        DATA_DIR / "assets/v.mp4",
        kind="video",
        data_dir=DATA_DIR,
        process_runner=runner,
        filesystem=filesystem_stub(),
    )

    assert result.fps == 29.97


def test_probe_media_rejects_an_unsupported_kind_before_running_ffprobe():
    runner = Mock()

    error = exception_from(
        lambda: probe_media(
            DATA_DIR / "assets/v.mp4",
            kind="document",
            data_dir=DATA_DIR,
            process_runner=runner,
            filesystem=filesystem_stub(),
        )
    )

    assert isinstance(error, ValueError)
    runner.assert_not_called()


def test_probe_media_falls_back_to_stream_duration_when_format_duration_is_missing():
    ffprobe_json = {
        "format": {},
        "streams": [{"codec_type": "video", "duration": "3.5"}],
    }
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(ffprobe_json), stderr=""
        )
    )

    result = probe_media(
        DATA_DIR / "assets/v.mp4",
        kind="video",
        data_dir=DATA_DIR,
        process_runner=runner,
        filesystem=filesystem_stub(),
    )

    assert result.duration_sec == 3.5


def test_probe_media_uses_zero_duration_when_duration_cannot_be_parsed():
    ffprobe_json = {"format": {"duration": "not-a-number"}, "streams": []}
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(ffprobe_json), stderr=""
        )
    )

    result = probe_media(
        DATA_DIR / "assets/v.mp4",
        kind="video",
        data_dir=DATA_DIR,
        process_runner=runner,
        filesystem=filesystem_stub(),
    )

    assert result.duration_sec == 0.0


@pytest.mark.parametrize("frame_rate", ["not-a-rate", "30/0"])
def test_probe_media_ignores_unparseable_or_zero_divisor_frame_rate(frame_rate):
    ffprobe_json = {
        "format": {"duration": "2.0"},
        "streams": [{"codec_type": "video", "r_frame_rate": frame_rate}],
    }
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(ffprobe_json), stderr=""
        )
    )

    result = probe_media(
        DATA_DIR / "assets/v.mp4",
        kind="video",
        data_dir=DATA_DIR,
        process_runner=runner,
        filesystem=filesystem_stub(),
    )

    assert result.fps is None


def test_probe_media_reads_photo_dimensions_from_pillow():
    result = probe_media(
        DATA_DIR / "assets/p.png",
        kind="photo",
        data_dir=DATA_DIR,
        image_module=image_module(),
        filesystem=filesystem_stub(),
    )

    assert result == MediaProbeResult(
        width=640,
        height=480,
        duration_sec=4.0,
        fps=None,
        has_audio=False,
    )


def test_probe_media_reports_missing_ffprobe_executable():
    runner = Mock(side_effect=FileNotFoundError("ffprobe"))

    error = exception_from(
        lambda: probe_media(
            DATA_DIR / "assets/v.mp4",
            kind="video",
            data_dir=DATA_DIR,
            process_runner=runner,
            filesystem=filesystem_stub(),
        )
    )

    assert isinstance(error, FileNotFoundError)


def test_probe_media_preserves_ffprobe_failure_details():
    failure = subprocess.CalledProcessError(
        1, ["ffprobe"], stderr="invalid media container"
    )
    runner = Mock(side_effect=failure)

    error = exception_from(
        lambda: probe_media(
            DATA_DIR / "assets/v.mp4",
            kind="video",
            data_dir=DATA_DIR,
            process_runner=runner,
            filesystem=filesystem_stub(),
        )
    )

    assert error is failure


def test_convert_image_to_jpeg_invokes_ffmpeg_with_safe_input_and_output_paths():
    runner = Mock()
    source = DATA_DIR / "assets/IMG_1.heic"
    output = DATA_DIR / "assets/IMG_1.jpg"

    result = convert_image_to_jpeg(
        source,
        output,
        data_dir=DATA_DIR,
        process_runner=runner,
        image_module=image_module(),
        filesystem=filesystem_stub(),
    )

    command = runner.call_args.args[0] if runner.call_args else []
    input_args = (
        command[command.index("-i") : command.index("-i") + 2]
        if "-i" in command
        else []
    )
    frame_args = (
        command[command.index("-frames:v") : command.index("-frames:v") + 2]
        if "-frames:v" in command
        else []
    )
    assert (
        result,
        command[0] if command else None,
        input_args,
        frame_args,
        command[-1] if command else None,
    ) == (
        output,
        "ffmpeg",
        ["-i", str(source)],
        ["-frames:v", "1"],
        str(output),
    )


def test_generate_thumbnail_invokes_ffmpeg_with_safe_normalized_paths():
    runner = Mock()
    source = DATA_DIR / "assets/v.mp4"
    output = DATA_DIR / "thumbs/v.jpg"

    result = generate_thumbnail(
        source,
        output,
        data_dir=DATA_DIR,
        timestamp_sec=1.0,
        process_runner=runner,
        filesystem=filesystem_stub(),
    )

    command = runner.call_args.args[0] if runner.call_args else []
    seek_args = (
        command[command.index("-ss") : command.index("-ss") + 2]
        if "-ss" in command
        else []
    )
    input_args = (
        command[command.index("-i") : command.index("-i") + 2]
        if "-i" in command
        else []
    )
    frame_args = (
        command[command.index("-frames:v") : command.index("-frames:v") + 2]
        if "-frames:v" in command
        else []
    )
    assert (
        result,
        command[0] if command else None,
        seek_args,
        input_args,
        frame_args,
        command[-1] if command else None,
    ) == (
        output,
        "ffmpeg",
        ["-ss", "1.0"],
        ["-i", str(source)],
        ["-frames:v", "1"],
        str(output),
    )


def test_pair_live_photos_pairs_only_matching_short_motion_assets():
    batch = [
        {"filename": "IMG_1042.HEIC", "kind": "photo"},
        {"filename": "img_1042.mov", "kind": "video", "duration_sec": 2.5},
        {"filename": "boundary.jpg", "kind": "photo"},
        {"filename": "boundary.mov", "kind": "video", "duration_sec": 5.0},
        {"filename": "over_limit.jpg", "kind": "photo"},
        {"filename": "over_limit.mov", "kind": "video", "duration_sec": 5.01},
        {"filename": "solo.png", "kind": "photo"},
    ]

    result = pair_live_photos(batch, filesystem_stub())

    live_photos = [entry for entry in result if field(entry, "kind") == "live_photo"]
    paired_names = {
        (
            entry_filename(field(entry, "motion")),
            entry_filename(field(entry, "still")),
        )
        for entry in live_photos
    }
    unpaired = {
        (field(entry, "kind"), entry_filename(entry))
        for entry in result
        if field(entry, "kind") != "live_photo"
    }
    assert (len(live_photos), paired_names, unpaired) == (
        2,
        {
            ("img_1042.mov", "IMG_1042.HEIC"),
            ("boundary.mov", "boundary.jpg"),
        },
        {
            ("photo", "over_limit.jpg"),
            ("video", "over_limit.mov"),
            ("photo", "solo.png"),
        },
    )


def test_pair_live_photos_pairs_object_backed_entries():
    still = SimpleNamespace(filename="IMG_2048.HEIC", kind="photo")
    motion = SimpleNamespace(
        filename="img_2048.mov", kind="video", duration_sec=2.5
    )

    result = pair_live_photos([still, motion], filesystem_stub())

    assert len(result) == 1
    assert field(result[0], "kind") == "live_photo"
    assert field(result[0], "motion") is motion
    assert field(result[0], "still") is still


def test_pair_live_photos_skips_malformed_motion_duration_metadata():
    photo = {"filename": "IMG_2048.HEIC", "kind": "photo"}
    video = {
        "filename": "img_2048.mov",
        "kind": "video",
        "duration_sec": "not-a-duration",
    }

    result = pair_live_photos([photo, video], filesystem_stub())

    assert result == [photo, video]
