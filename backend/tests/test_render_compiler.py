import io
import subprocess
from pathlib import Path

import pytest

import app.config as app_config
from app.models import (
    Composition,
    MediaAsset,
    Project,
    TimelineAudioClip,
    TimelineVisualClip,
)
from app.services.render_compiler import build_render_plan, execute_render_plan


DATA_DIR = Path("/virtual/data")
OUTPUT_PATH = DATA_DIR / "renders" / "out.mp4"
NOW = "2026-11-22T00:00:00Z"


@pytest.fixture(autouse=True)
def virtual_data_dir(monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(DATA_DIR))
    monkeypatch.setattr(app_config.settings, "data_dir", DATA_DIR)
    return DATA_DIR


def make_project(result_target="high_energy", bpm=120.0):
    return Project(
        id="p1",
        name="Project",
        bpm=bpm,
        beat_offset_sec=0.0,
        resolution_w=1280,
        resolution_h=720,
        fps=30,
        ai_context="",
        result_target=result_target,
        suggested_sequence=[],
        created_at=NOW,
        updated_at=NOW,
    )


def make_asset(asset_id, kind="video", local_path=None, has_audio=False, duration_sec=10.0):
    extension = {"video": "mp4", "live_photo": "mov", "photo": "jpg", "audio": "mp3"}[kind]
    return MediaAsset(
        id=asset_id,
        project_id="p1",
        kind=kind,
        source="upload",
        filename=f"{asset_id}.{extension}",
        local_path=local_path or f"assets/{asset_id}.{extension}",
        still_path=None,
        thumbnail_path=None,
        duration_sec=duration_sec,
        width=1920 if kind != "audio" else None,
        height=1080 if kind != "audio" else None,
        fps=30.0 if kind in ("video", "live_photo") else None,
        has_audio=has_audio,
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at=NOW,
    )


def make_visual(
    clip_id,
    asset_id,
    start_sec,
    duration_sec,
    kind="video",
    speed=1.0,
    transition_in="none",
    transition_duration_sec=0.5,
    ken_burns="none",
    color_preset="original",
    volume=1.0,
    muted=False,
):
    return TimelineVisualClip(
        id=clip_id,
        asset_id=asset_id,
        part_id=None,
        kind=kind,
        start_sec=start_sec,
        duration_sec=duration_sec,
        trim_start_sec=0.0,
        speed=speed,
        transition_in=transition_in,
        transition_duration_sec=transition_duration_sec,
        ken_burns=ken_burns,
        color_preset=color_preset,
        volume=volume,
        muted=muted,
        fade_in_sec=0.0,
        fade_out_sec=0.0,
    )


def make_audio(
    clip_id,
    asset_id,
    start_sec,
    duration_sec,
    volume=1.0,
    muted=False,
    fade_in_sec=0.0,
    fade_out_sec=0.0,
):
    return TimelineAudioClip(
        id=clip_id,
        asset_id=asset_id,
        part_id=None,
        track_index=1,
        start_sec=start_sec,
        duration_sec=duration_sec,
        trim_start_sec=0.0,
        volume=volume,
        muted=muted,
        fade_in_sec=fade_in_sec,
        fade_out_sec=fade_out_sec,
    )


def make_composition(video_clips, audio_clips=()):
    return Composition(
        project_id="p1",
        snap_subdivision="beat",
        video_clips=list(video_clips),
        audio_clips=list(audio_clips),
        updated_at=NOW,
    )


def by_id(*assets):
    return {asset.id: asset for asset in assets}


def gap_video_photo_plan(video_color="warm", photo_color="bw", ken_burns="zoom_in"):
    assets = by_id(make_asset("v1", "video"), make_asset("ph1", "photo", duration_sec=0.0))
    composition = make_composition(
        [
            make_visual("c1", "v1", 1.0, 2.0, kind="video", speed=1.5, color_preset=video_color),
            make_visual(
                "c2", "ph1", 3.0, 2.0, kind="photo", ken_burns=ken_burns, color_preset=photo_color
            ),
        ]
    )
    return build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)


def two_adjacent_plan(result_target, transition_in="none", transition_duration_sec=0.5):
    assets = by_id(make_asset("v1", "video"), make_asset("v2", "video"))
    composition = make_composition(
        [
            make_visual("c1", "v1", 0.0, 2.0),
            make_visual(
                "c2",
                "v2",
                2.0,
                2.0,
                transition_in=transition_in,
                transition_duration_sec=transition_duration_sec,
            ),
        ]
    )
    return build_render_plan(make_project(result_target, bpm=120.0), composition, assets, OUTPUT_PATH)


# Behavior 1: gaps, video/photo normalization, Ken Burns, color presets.
def test_gap_video_speed_photo_kenburns_normalization_in_filter_complex():
    plan = gap_video_photo_plan()

    fc = plan.filter_complex
    assert "color=c=black:s=1280x720:r=30:d=1.0" in fc, fc
    assert "setpts=(PTS-STARTPTS)/1.5" in fc, fc
    zoom_in_expression = "zoompan=z='min(1+0.2*on/60,1.2)'"
    zoom_out_expression = "zoompan=z='max(1.2-0.2*on/60,1)'"
    assert zoom_in_expression in fc, fc

    zoom_out_fc = gap_video_photo_plan(ken_burns="zoom_out").filter_complex
    assert zoom_out_expression in zoom_out_fc, zoom_out_fc
    assert zoom_out_expression not in fc, fc
    assert "format=yuv420p" in fc, fc
    assert "setsar=1" in fc, fc


def test_warm_and_bw_color_presets_add_distinct_color_filters():
    original = gap_video_photo_plan(video_color="original", photo_color="original").filter_complex
    warm_only = gap_video_photo_plan(video_color="warm", photo_color="original").filter_complex
    bw_only = gap_video_photo_plan(video_color="original", photo_color="bw").filter_complex

    assert original != "", "original-preset plan produced an empty filter_complex"
    warm_filter = "colorbalance=rm=0.08:gm=0.02:bm=-0.08"
    bw_filter = "hue=s=0"
    assert warm_filter in warm_only, (warm_filter, warm_only)
    assert bw_filter in bw_only, (bw_filter, bw_only)
    assert bw_filter not in warm_only, (bw_filter, warm_only)
    assert warm_filter not in bw_only, (warm_filter, bw_only)
    assert warm_only != original, "warm preset added no color filter"
    assert bw_only != original, "bw preset added no color filter"


@pytest.mark.parametrize(
    ("color_preset", "expected_filter"),
    [
        ("cool", "colorbalance=rm=-0.08:bm=0.08"),
        ("vivid", "eq=saturation=1.4:contrast=1.05"),
        ("vignette", "vignette=PI/5"),
    ],
)
def test_cool_vivid_and_vignette_color_presets_emit_exact_filters(color_preset, expected_filter):
    filter_complex = gap_video_photo_plan(
        video_color=color_preset, photo_color="original"
    ).filter_complex

    assert expected_filter in filter_complex, (expected_filter, filter_complex)


# Behavior 2: explicit wipe_left transition.
def test_wipe_left_transition_emits_wipeleft_xfade():
    plan = two_adjacent_plan("high_energy", transition_in="wipe_left", transition_duration_sec=0.5)

    assert "xfade=transition=wipeleft:duration=0.5:offset=1.5" in plan.filter_complex, plan.filter_complex


def test_transition_duration_is_clamped_to_shorter_adjacent_clip():
    assets = by_id(make_asset("v1", "video"), make_asset("v2", "video"))
    composition = make_composition(
        [
            make_visual("c1", "v1", 0.0, 0.5),
            make_visual(
                "c2",
                "v2",
                0.5,
                2.0,
                transition_in="wipe_left",
                transition_duration_sec=1.25,
            ),
        ]
    )

    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)

    assert "tpad=stop_mode=clone:stop_duration=1.0" in plan.filter_complex, plan.filter_complex
    assert "trim=duration=1.0" in plan.filter_complex, plan.filter_complex
    assert "xfade=transition=wipeleft:duration=0.5:offset=0.5" in plan.filter_complex


def test_transition_at_or_below_epsilon_is_rendered_as_a_hard_cut():
    plan = two_adjacent_plan(
        "high_energy", transition_in="wipe_left", transition_duration_sec=0.0005
    )

    assert "concat=n=2:v=1:a=0" in plan.filter_complex, plan.filter_complex
    assert "xfade" not in plan.filter_complex, plan.filter_complex


def test_unknown_visual_asset_is_rejected_before_render_command_is_built():
    composition = make_composition([make_visual("c1", "missing", 0.0, 2.0)])

    with pytest.raises(ValueError, match="Unknown asset: missing"):
        build_render_plan(make_project("high_energy"), composition, {}, OUTPUT_PATH)


def test_empty_composition_is_rejected():
    composition = make_composition([])

    with pytest.raises(ValueError, match="^Composition is empty$"):
        build_render_plan(make_project("high_energy"), composition, {}, OUTPUT_PATH)


def test_photo_without_ken_burns_uses_static_scale_crop_and_fps_filters():
    photo = make_asset("ph1", "photo", duration_sec=0.0)
    composition = make_composition(
        [make_visual("c1", "ph1", 0.0, 2.0, kind="photo", ken_burns="none")]
    )

    plan = build_render_plan(
        make_project("high_energy"), composition, by_id(photo), OUTPUT_PATH
    )

    assert plan.inputs == [str(DATA_DIR / "assets" / "ph1.jpg")]
    assert "-loop" in plan.command
    assert "-filter_complex" in plan.command
    assert "scale=1280:720:force_original_aspect_ratio=increase" in plan.filter_complex
    assert "crop=1280:720" in plan.filter_complex
    assert "fps=30" in plan.filter_complex
    assert "zoompan" not in plan.filter_complex


@pytest.mark.parametrize("kind", ["video", "live_photo"])
def test_video_and_live_photo_fill_and_crop_without_padding(kind):
    asset = make_asset("v1", kind)
    composition = make_composition([make_visual("c1", "v1", 0.0, 2.0, kind=kind)])

    plan = build_render_plan(make_project("high_energy"), composition, by_id(asset), OUTPUT_PATH)

    fc = plan.filter_complex
    assert "scale=1280:720:force_original_aspect_ratio=increase" in fc, fc
    assert "crop=1280:720" in fc, fc
    assert "force_original_aspect_ratio=decrease" not in fc, fc
    filters = (item.strip() for item in fc.replace(";", ",").split(","))
    assert not any(item.startswith("pad=") for item in filters), fc


def test_photo_pan_lr_uses_horizontal_zoompan_instead_of_static_photo_filters():
    photo = make_asset("ph1", "photo", duration_sec=0.0)
    composition = make_composition(
        [make_visual("c1", "ph1", 0.0, 2.0, kind="photo", ken_burns="pan_lr")]
    )

    plan = build_render_plan(
        make_project("high_energy"), composition, by_id(photo), OUTPUT_PATH
    )

    expected_pan_filter = (
        "zoompan=z='1.2':x='(iw-iw/zoom)*on/60':"
        "y='ih/2-(ih/zoom/2)':d=1:s=1280x720:fps=30"
    )
    assert expected_pan_filter in plan.filter_complex, plan.filter_complex
    assert "scale=1280:720:force_original_aspect_ratio=increase" not in plan.filter_complex
    assert "crop=1280:720" not in plan.filter_complex


# Behavior 3: seamless_flow auto half-beat crossfade.
def test_seamless_flow_auto_inserts_half_beat_crossfade():
    plan = two_adjacent_plan("seamless_flow", transition_in="none")

    assert "xfade=transition=fade:duration=0.25:offset=1.75" in plan.filter_complex, plan.filter_complex


# Behavior 4: hard cuts use concat.
def test_high_energy_hard_cut_uses_concat_without_xfade():
    plan = two_adjacent_plan("high_energy", transition_in="none")

    fc = plan.filter_complex
    assert "concat=n=2:v=1:a=0" in fc, fc
    assert "xfade" not in fc, fc


# Behavior 5: multi-track audio graph.
def test_audio_graph_trims_tempos_fades_delays_mixes_and_limits():
    assets = by_id(
        make_asset("v1", "video", has_audio=True),
        make_asset("a1", "audio", has_audio=True, duration_sec=30.0),
        make_asset("a2", "audio", has_audio=True, duration_sec=30.0),
    )
    composition = make_composition(
        [make_visual("c1", "v1", 0.0, 4.0, speed=1.5)],
        [
            make_audio("ac1", "a1", 1.0, 3.0, volume=0.8, fade_in_sec=0.5, fade_out_sec=0.75),
            make_audio("ac2", "a2", 2.5, 3.0),
        ],
    )

    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)

    fc = plan.filter_complex
    for expected in (
        "atrim",
        "atempo=1.5",
        "volume=0.8",
        "afade=t=in:st=0:d=0.5",
        "afade=t=out:st=2.25:d=0.75",
        "adelay=1000:all=1",
        "adelay=2500:all=1",
        "amix=inputs=3",
        "alimiter=limit=0.95",
    ):
        assert expected in fc, (expected, fc)


# Behavior 6: silent fallback.
def test_all_muted_audio_inserts_anullsrc():
    assets = by_id(
        make_asset("v1", "video", has_audio=True),
        make_asset("a1", "audio", has_audio=True, duration_sec=30.0),
    )
    composition = make_composition(
        [make_visual("c1", "v1", 0.0, 4.0, muted=True)],
        [make_audio("ac1", "a1", 0.0, 4.0, muted=True)],
    )

    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)

    assert "anullsrc" in plan.filter_complex, plan.filter_complex


# Behavior 7: perfect loop post-pass.
def test_perfect_loop_blends_tail_into_head_and_shortens_duration():
    assets = by_id(
        make_asset("v1", "video", has_audio=True),
        make_asset("v2", "video", has_audio=True),
    )
    composition = make_composition(
        [
            make_visual("c1", "v1", 0.0, 2.0),
            make_visual("c2", "v2", 2.0, 2.0),
        ]
    )

    plan = build_render_plan(make_project("perfect_loop", bpm=120.0), composition, assets, OUTPUT_PATH)

    fc = plan.filter_complex
    assert "split" in fc, fc
    assert "[lvtail][lvhead]xfade=transition=fade:duration=0.25:offset=0[lvblend]" in fc, fc
    assert "[lvbody][lvblend]concat=n=2:v=1:a=0[lvout]" in fc, fc
    assert "acrossfade=d=0.25" in fc, fc
    assert "[labody][lablend]concat=n=2:v=0:a=1[laout]" in fc, fc
    assert plan.total_duration_sec == pytest.approx(3.75)


def test_perfect_loop_overlap_uses_half_beat_at_non_120_bpm():
    assets = by_id(
        make_asset("v1", "video", has_audio=True),
        make_asset("v2", "video", has_audio=True),
    )
    composition = make_composition(
        [
            make_visual("c1", "v1", 0.0, 2.0),
            make_visual("c2", "v2", 2.0, 2.0),
        ]
    )

    plan = build_render_plan(make_project("perfect_loop", bpm=100.0), composition, assets, OUTPUT_PATH)

    fc = plan.filter_complex
    assert "xfade=transition=fade:duration=0.3:offset=0" in fc, fc
    assert "acrossfade=d=0.3" in fc, fc
    assert plan.total_duration_sec == pytest.approx(3.7)


def test_perfect_loop_skips_post_pass_just_below_two_seconds():
    assets = by_id(make_asset("v1", "video", has_audio=True))
    composition = make_composition([make_visual("c1", "v1", 0.0, 1.99)])

    plan = build_render_plan(make_project("perfect_loop"), composition, assets, OUTPUT_PATH)

    fc = plan.filter_complex
    assert "split=3" not in fc, fc
    assert "xfade=transition=fade" not in fc, fc
    assert "acrossfade=" not in fc, fc
    assert plan.total_duration_sec == pytest.approx(1.99)


def test_perfect_loop_applies_post_pass_at_exactly_two_seconds():
    assets = by_id(make_asset("v1", "video", has_audio=True))
    composition = make_composition([make_visual("c1", "v1", 0.0, 2.0)])

    plan = build_render_plan(make_project("perfect_loop"), composition, assets, OUTPUT_PATH)

    fc = plan.filter_complex
    assert "split=3" in fc, fc
    assert "[lvtail][lvhead]xfade=transition=fade:duration=0.25:offset=0[lvblend]" in fc, fc
    assert "acrossfade=d=0.25" in fc, fc
    assert plan.total_duration_sec == pytest.approx(1.75)


# Behavior 8: path traversal rejected.
def test_asset_outside_data_dir_is_rejected():
    assets = by_id(make_asset("v1", "video", local_path="../../etc/passwd"))
    composition = make_composition([make_visual("c1", "v1", 0.0, 2.0)])

    with pytest.raises(ValueError):
        build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)


# Behavior 9: execute_render_plan command and progress parsing.
class FakeProcess:
    def __init__(self, command, lines, returncode=0):
        self.args = command
        self.stdout = io.StringIO("".join(f"{line}\n" for line in lines))
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self.pid = 4242

    def wait(self, *args, **kwargs):
        return self.returncode

    def poll(self):
        return 0

    def communicate(self, *args, **kwargs):
        return ("", "")

    def kill(self):
        return None

    def terminate(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakePopen:
    def __init__(self, calls, lines, returncode=0):
        self.calls = calls
        self.lines = lines
        self.returncode = returncode

    def __call__(self, command, *args, **kwargs):
        self.calls.append(list(command))
        return FakeProcess(command, self.lines, self.returncode)


def test_execute_render_plan_runs_ffmpeg_and_reports_scaled_progress(monkeypatch):
    assets = by_id(make_asset("v1", "video", has_audio=True))
    composition = make_composition([make_visual("c1", "v1", 0.0, 4.0)])
    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)
    plan.total_duration_sec = 4.0

    calls = []
    fake_popen = FakePopen(
        calls, ["out_time_us=1000000", "out_time_us=2000000", "out_time_us=4000000"]
    )
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    progress = []

    execute_render_plan(plan, lambda pct, msg: progress.append(pct), process_factory=fake_popen)

    assert len(calls) == 1, calls
    command = calls[0]

    def has_pair(flag, value):
        return any(command[i] == flag and command[i + 1] == value for i in range(len(command) - 1))

    for flag, value in (
        ("-c:v", "libx264"),
        ("-preset", "fast"),
        ("-crf", "20"),
        ("-pix_fmt", "yuv420p"),
        ("-c:a", "aac"),
        ("-b:a", "192k"),
        ("-ar", "48000"),
        ("-ac", "2"),
        ("-movflags", "+faststart"),
        ("-progress", "pipe:1"),
    ):
        assert has_pair(flag, value), (flag, value, command)

    assert progress, "progress_cb was never called"
    assert all(5 <= value <= 90 for value in progress), progress
    assert all(a < b for a, b in zip(progress, progress[1:])), progress
    assert progress[-1] == 90, progress


def test_execute_render_plan_raises_with_last_twenty_ffmpeg_output_lines():
    assets = by_id(make_asset("v1", "video"))
    composition = make_composition([make_visual("c1", "v1", 0.0, 4.0)])
    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)
    diagnostics = [f"ffmpeg diagnostic {i:02}" for i in range(22)]
    fake_popen = FakePopen([], diagnostics, returncode=7)

    with pytest.raises(RuntimeError) as exc_info:
        execute_render_plan(plan, process_factory=fake_popen)

    assert str(exc_info.value) == f"ffmpeg failed (7): {' '.join(diagnostics[-20:])}"


def test_execute_render_plan_ignores_non_progress_lines_and_invalid_times():
    assets = by_id(make_asset("v1", "video"))
    composition = make_composition([make_visual("c1", "v1", 0.0, 4.0)])
    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)
    plan.total_duration_sec = 4.0
    fake_popen = FakePopen(
        [],
        [
            "",
            "ffmpeg diagnostic without equals",
            "out_time_us=not-an-integer",
            "out_time_us=1000000",
            "progress=end",
        ],
    )
    progress = []

    result = execute_render_plan(
        plan,
        lambda pct, message: progress.append((pct, message)),
        process_factory=fake_popen,
    )

    assert result == OUTPUT_PATH
    assert progress == [(26, "Rendering"), (90, "Rendering")]


def test_execute_render_plan_accepts_progress_output_without_callback():
    assets = by_id(make_asset("v1", "video"))
    composition = make_composition([make_visual("c1", "v1", 0.0, 4.0)])
    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)
    fake_popen = FakePopen([], ["out_time_us=2000000", "progress=end"])

    result = execute_render_plan(plan, process_factory=fake_popen)

    assert result == OUTPUT_PATH


def test_audio_outlasting_video_adds_black_filler_to_timeline_end():
    assets = by_id(
        make_asset("v1", "video"),
        make_asset("a1", "audio", duration_sec=10.0),
    )
    composition = make_composition(
        [make_visual("c1", "v1", 0.0, 2.0)],
        [make_audio("ac1", "a1", 0.0, 5.0)],
    )

    plan = build_render_plan(make_project("high_energy"), composition, assets, OUTPUT_PATH)

    assert plan.total_duration_sec == 5.0
    graph_parts = plan.filter_complex.split(";")
    assert (
        "color=c=black:s=1280x720:r=30:d=3.0,format=yuv420p,setsar=1,settb=AVTB[v1]"
        in graph_parts
    )
    assert "[v0][v1]concat=n=2:v=1:a=0[vj1]" in graph_parts
