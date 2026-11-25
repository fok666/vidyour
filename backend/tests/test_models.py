import pytest
from pydantic import ValidationError

from app.models import Composition, Project, TimelineAudioClip, TimelineVisualClip


def project_fields():
    return {
        "id": "p1",
        "name": "Demo",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def valid_composition_payload():
    return {
        "project_id": "p1",
        "video_clips": [
            {
                "id": "visual-1",
                "asset_id": "asset-1",
                "part_id": None,
                "kind": "video",
                "start_sec": 0.0,
                "duration_sec": 4.0,
                "trim_start_sec": 0.0,
                "speed": 1.5,
                "transition_in": "crossfade",
                "ken_burns": "zoom_in",
                "color_preset": "warm",
                "volume": 0.5,
                "muted": False,
                "fade_in_sec": 0.0,
                "fade_out_sec": 0.0,
            }
        ],
        "audio_clips": [
            {
                "id": "audio-1",
                "asset_id": "asset-2",
                "part_id": None,
                "track_index": 0,
                "start_sec": 0.0,
                "duration_sec": 8.0,
                "trim_start_sec": 0.0,
                "volume": 1.0,
                "muted": False,
                "fade_in_sec": 0.5,
                "fade_out_sec": 0.75,
            }
        ],
        "updated_at": "2026-01-01T00:00:00Z",
    }


def build_valid_composition(*, snap_subdivision=None, transition_duration_sec=None):
    payload = valid_composition_payload()
    if snap_subdivision is not None:
        payload["snap_subdivision"] = snap_subdivision
    if transition_duration_sec is not None:
        payload["video_clips"][0]["transition_duration_sec"] = (
            transition_duration_sec
        )

    try:
        return Composition(**payload)
    except ValidationError as error:
        assert False, f"valid Composition payload should construct successfully: {error}"


def test_project_applies_declared_defaults():
    try:
        project = Project(**project_fields())
    except ValidationError as error:
        assert False, f"minimal Project payload should construct successfully: {error}"

    assert (
        project.bpm,
        project.beat_offset_sec,
        project.resolution_w,
        project.resolution_h,
        project.fps,
        project.ai_context,
        project.result_target,
        project.suggested_sequence,
    ) == (120.0, 0.0, 1280, 720, 30, "", "seamless_flow", [])


def test_composition_and_clips_apply_declared_defaults_and_preserve_fields():
    composition = build_valid_composition()

    assert composition.project_id == "p1"
    assert composition.snap_subdivision == "beat"
    visual_clip = composition.video_clips[0]
    assert (
        visual_clip.speed,
        visual_clip.transition_in,
        visual_clip.ken_burns,
        visual_clip.color_preset,
        visual_clip.volume,
        visual_clip.muted,
        visual_clip.transition_duration_sec,
    ) == (1.5, "crossfade", "zoom_in", "warm", 0.5, False, 0.5)
    audio_clip = composition.audio_clips[0]
    assert (
        audio_clip.track_index,
        audio_clip.volume,
        audio_clip.fade_in_sec,
        audio_clip.fade_out_sec,
    ) == (0, 1.0, 0.5, 0.75)


def test_composition_json_roundtrip_preserves_values_and_defaults():
    payload = valid_composition_payload()
    visual_clip = TimelineVisualClip.model_construct(**payload["video_clips"][0])
    audio_clip = TimelineAudioClip.model_construct(**payload["audio_clips"][0])
    composition = Composition.model_construct(
        **{
            **payload,
            "video_clips": [visual_clip],
            "audio_clips": [audio_clip],
        }
    )

    try:
        round_tripped = Composition.model_validate_json(composition.model_dump_json())
    except ValidationError as error:
        assert False, f"Composition JSON round-trip should validate successfully: {error}"

    assert round_tripped == composition
    assert round_tripped.project_id == "p1"
    assert round_tripped.snap_subdivision == "beat"
    assert (
        round_tripped.video_clips[0].speed,
        round_tripped.video_clips[0].transition_in,
        round_tripped.video_clips[0].ken_burns,
        round_tripped.video_clips[0].color_preset,
        round_tripped.video_clips[0].volume,
        round_tripped.video_clips[0].muted,
        round_tripped.video_clips[0].transition_duration_sec,
    ) == (1.5, "crossfade", "zoom_in", "warm", 0.5, False, 0.5)
    assert (
        round_tripped.audio_clips[0].track_index,
        round_tripped.audio_clips[0].volume,
        round_tripped.audio_clips[0].fade_in_sec,
        round_tripped.audio_clips[0].fade_out_sec,
    ) == (0, 1.0, 0.5, 0.75)


def visual_clip_payload(**overrides):
    payload = {
        "id": "visual-1",
        "asset_id": "asset-1",
        "part_id": None,
        "kind": "video",
        "start_sec": 0.0,
        "duration_sec": 4.0,
        "trim_start_sec": 0.0,
        "speed": 1.0,
        "transition_in": "cut",
        "transition_duration_sec": 0.5,
        "ken_burns": "none",
        "color_preset": "none",
        "volume": 1.0,
        "muted": False,
        "fade_in_sec": 0.0,
        "fade_out_sec": 0.0,
    }
    payload.update(overrides)
    return payload


def test_visual_clip_rejects_speed_above_supported_range():
    payload = visual_clip_payload(speed=2.5)

    with pytest.raises(ValidationError) as error:
        TimelineVisualClip.model_validate(payload)

    assert "speed" in str(error.value)


def test_visual_clip_rejects_speed_below_supported_range():
    payload = visual_clip_payload(
        speed=0.49,
        transition_in="none",
        color_preset="original",
    )

    with pytest.raises(ValidationError) as error:
        TimelineVisualClip.model_validate(payload)

    assert [detail["loc"] for detail in error.value.errors()] == [("speed",)]


def test_visual_clip_defaults_volume_to_zero():
    payload = visual_clip_payload(transition_in="none", color_preset="original")
    payload.pop("volume")

    visual_clip = TimelineVisualClip.model_validate(payload)

    assert visual_clip.volume == 0.0


def test_visual_clip_rejects_volume_above_supported_range():
    payload = visual_clip_payload(
        volume=1.51,
        transition_in="none",
        color_preset="original",
    )

    with pytest.raises(ValidationError) as error:
        TimelineVisualClip.model_validate(payload)

    assert [detail["loc"] for detail in error.value.errors()] == [("volume",)]


def test_audio_clip_rejects_volume_above_supported_range():
    payload = {
        "id": "audio-1",
        "asset_id": "asset-2",
        "part_id": None,
        "track_index": 0,
        "start_sec": 0.0,
        "duration_sec": 8.0,
        "trim_start_sec": 0.0,
        "volume": 2.0,
        "muted": False,
        "fade_in_sec": 0.0,
        "fade_out_sec": 0.0,
    }

    with pytest.raises(ValidationError) as error:
        TimelineAudioClip.model_validate(payload)

    assert "volume" in str(error.value)


def test_visual_clip_rejects_unsupported_transition_and_color_preset():
    payload = visual_clip_payload(transition_in="star_wipe", color_preset="neon")

    with pytest.raises(ValidationError) as error:
        TimelineVisualClip.model_validate(payload)

    error_text = str(error.value)
    assert "transition_in" in error_text
    assert "color_preset" in error_text
