import subprocess
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from app.services.audio_analyzer import analyze_audio


DATA_DIR = Path("/virtual/data")
SAMPLE_RATE = 22050


def click_track(bpm, duration=32.0, phase=0.0, section_levels=None):
    """Make mono float32 PCM with a short, decaying click on every beat."""
    pcm = np.zeros(round(duration * SAMPLE_RATE), dtype=np.float32)
    if section_levels:
        times = np.arange(len(pcm)) / SAMPLE_RATE
        levels = np.select(
            [times < 8, times < 16, times < 24],
            section_levels[:3],
            default=section_levels[3],
        )
        pcm += (0.04 * levels * np.sin(2 * np.pi * 220 * times)).astype(
            np.float32
        )

    click_time = np.arange(round(0.04 * SAMPLE_RATE)) / SAMPLE_RATE
    click = np.sin(2 * np.pi * 1000 * click_time) * np.exp(-click_time * 100)
    beat = phase
    while beat < duration:
        start = round(beat * SAMPLE_RATE)
        end = min(start + len(click), len(pcm))
        level = section_levels[min(int(beat // 8), 3)] if section_levels else 1.0
        pcm[start:end] += (level * click[: end - start]).astype(np.float32)
        beat += 60.0 / bpm
    return pcm


def decoder_for(pcm):
    return Mock(
        return_value=subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout=pcm.tobytes(), stderr=b""
        )
    )


def analyze_pcm(pcm):
    return analyze_audio(
        DATA_DIR / "track.wav", data_dir=DATA_DIR, process_runner=decoder_for(pcm)
    )


def test_decoder_requests_mono_22050_hz_float32_pcm():
    pcm = click_track(120)
    runner = decoder_for(pcm)

    analyze_audio(DATA_DIR / "track.wav", data_dir=DATA_DIR, process_runner=runner)

    call = runner.call_args
    command_args = (call.args[0] if call.args else call.kwargs.get("args", [])) if call else []
    command = [str(arg) for arg in command_args]
    assert (
        len(command) > 0
        and Path(command[0]).name == "ffmpeg"
        and any(
            command[i : i + 2] == ["-i", str(DATA_DIR / "track.wav")]
            for i in range(len(command) - 1)
        )
        and any(
            command[i : i + 2] == ["-ac", "1"]
            and "-i" in command[:i]
            and "-i" not in command[i + 2 : -1]
            and not any(
                option == "-ac" or option.startswith("-ac:")
                for option in command[i + 2 : -1]
            )
            for i in range(len(command) - 1)
        )
        and any(
            command[i : i + 2] == ["-ar", "22050"]
            and "-i" in command[:i]
            and "-i" not in command[i + 2 : -1]
            and not any(
                option == "-ar" or option.startswith("-ar:")
                for option in command[i + 2 : -1]
            )
            for i in range(len(command) - 1)
        )
        and command[-1] in ("-", "pipe:1", "pipe:")
        and any(
            command[i : i + 2] == ["-f", "f32le"]
            and "-i" in command[:i]
            and "-f" not in command[i + 2 : -1]
            for i in range(len(command) - 1)
        )
    )


@pytest.mark.parametrize("bpm", [90, 120, 140])
def test_click_track_tempo_is_estimated_within_tolerance(bpm):
    pcm = click_track(bpm)

    result = analyze_pcm(pcm)

    assert getattr(result, "bpm", None) == pytest.approx(bpm, abs=1.5)


@pytest.mark.parametrize(("input_bpm", "expected_bpm"), [(70, 140), (160, 160)])
def test_click_track_tempo_octave_handling(input_bpm, expected_bpm):
    result = analyze_pcm(click_track(input_bpm))

    assert result.bpm == pytest.approx(expected_bpm, abs=1.5)


def test_shifted_click_track_has_strictly_increasing_beat_times():
    pcm = click_track(120, phase=0.25)

    result = analyze_pcm(pcm)

    beats = getattr(result, "beat_times", [])
    assert len(beats) > 1 and all(a < b for a, b in zip(beats, beats[1:]))


def test_shifted_click_track_has_half_second_median_beat_interval():
    pcm = click_track(120, phase=0.25)

    result = analyze_pcm(pcm)

    beats = getattr(result, "beat_times", [])
    interval = float(np.median(np.diff(beats))) if len(beats) > 1 else None
    assert interval == pytest.approx(0.5, abs=0.02)


def test_shifted_click_track_beat_grid_spans_track_interior():
    pcm = click_track(120, phase=0.25)

    result = analyze_pcm(pcm)

    beats = getattr(result, "beat_times", [])
    # Allow generous gaps and avoid relying on the first or last detected beat.
    assert all(
        any(abs(beat - checkpoint) <= 1.0 for beat in beats)
        for checkpoint in range(4, 32, 4)
    )


@pytest.mark.parametrize("input_phase", [0.1, 0.25, 0.4])
def test_shifted_click_track_recovers_phase_modulo_beat_period(input_phase):
    pcm = click_track(120, phase=input_phase)

    result = analyze_pcm(pcm)

    offset = getattr(result, "beat_offset_sec", None)
    phase = offset % 0.5 if offset is not None else None
    assert phase == pytest.approx(input_phase, abs=0.04)


@pytest.mark.parametrize("input_phase", [0.1, 0.25, 0.4])
def test_shifted_click_track_beat_times_align_with_estimated_grid(input_phase):
    pcm = click_track(120, phase=input_phase)

    result = analyze_pcm(pcm)

    beats = getattr(result, "beat_times", [])
    bpm = getattr(result, "bpm", 0)
    offset = getattr(result, "beat_offset_sec", None)
    assert beats and bpm > 0 and offset is not None
    period = 60.0 / bpm
    assert beats and all(
        abs((beat - offset + period / 2) % period - period / 2) <= 0.04
        for beat in beats
    )


def test_shifted_click_track_marks_every_fourth_beat_as_downbeat():
    pcm = click_track(120, phase=0.25)

    result = analyze_pcm(pcm)

    beats = getattr(result, "beat_times", [])
    downbeats = getattr(result, "downbeats", [])
    assert len(beats) > 0 and downbeats == beats[::4]


def test_empty_decode_returns_none():
    runner = decoder_for(np.array([], dtype=np.float32))

    result = analyze_audio(
        DATA_DIR / "track.wav", data_dir=DATA_DIR, process_runner=runner
    )

    assert result is None
    runner.assert_called_once()


def test_decoder_failure_propagates():
    runner = Mock(
        side_effect=subprocess.CalledProcessError(
            returncode=1, cmd=["ffmpeg"], stderr=b"decoder failed"
        )
    )

    with pytest.raises(subprocess.CalledProcessError):
        analyze_audio(
            DATA_DIR / "track.wav", data_dir=DATA_DIR, process_runner=runner
        )

    runner.assert_called_once()


def test_silent_audio_has_waveform_but_no_bpm_or_beat_grid():
    result = analyze_pcm(np.zeros(2 * SAMPLE_RATE, dtype=np.float32))

    assert result is not None
    assert len(result.waveform) == 300 and all(peak == 0 for peak in result.waveform)
    assert result.bpm == 0
    assert result.beat_times == [] and result.downbeats == [] and result.sections == []


def test_short_audio_without_beat_period_has_no_bpm_or_beat_grid():
    result = analyze_pcm(np.ones(100, dtype=np.float32))

    assert result is not None
    assert len(result.waveform) == 300 and max(result.waveform) == 1.0
    assert result.bpm == 0
    assert result.beat_times == [] and result.downbeats == [] and result.sections == []


def test_non_silent_audio_produces_300_normalized_waveform_peaks():
    pcm = click_track(120)
    peaks = np.max(np.abs(pcm.reshape(300, -1)), axis=1)
    expected = peaks / peaks.max()

    result = analyze_pcm(pcm)

    waveform = getattr(result, "waveform", [])
    assert (
        len(waveform) == 300
        and all(0.0 <= peak <= 1.0 for peak in waveform)
        and max(waveform) == 1.0
    )
    # Check silent and prominent buckets without prescribing output precision.
    assert all(waveform[i] == 0 for i in np.flatnonzero(expected == 0))
    assert all(waveform[i] >= 0.5 for i in np.flatnonzero(expected >= 0.9))


def test_energy_sections_are_non_overlapping_and_bar_aligned():
    pcm = click_track(120, section_levels=[0.1, 0.4, 1.0, 0.1])

    result = analyze_pcm(pcm)

    sections = getattr(result, "sections", [])
    ordered = sorted(sections, key=lambda section: section.start_sec)
    assert (
        len(sections) > 0
        and all(section.beat_length > 0 for section in sections)
        and all(
            section.beat_length / 4 == pytest.approx(round(section.beat_length / 4))
            for section in sections
        )
        and all(
            first.end_sec <= second.start_sec
            for first, second in zip(ordered, ordered[1:])
        )
    )

    beats = getattr(result, "beat_times", [])
    bpm = getattr(result, "bpm", 0)
    assert beats and bpm > 0
    beat_period = 60.0 / bpm
    bar_period = 4 * beat_period
    for section in sections:
        for boundary in (section.start_sec, section.end_sec):
            nearest_bar = beats[0] + round((boundary - beats[0]) / bar_period) * bar_period
            assert boundary == pytest.approx(nearest_bar, abs=0.04)
        assert section.end_sec - section.start_sec == pytest.approx(
            section.beat_length * beat_period, abs=0.04
        )


def test_valid_beat_grid_has_no_sections_without_a_complete_bar():
    result = analyze_pcm(click_track(120, duration=1.5, phase=0.25))

    assert result is not None
    assert result.bpm == pytest.approx(120, abs=0.1)
    assert result.beat_times == pytest.approx([0.25, 0.75, 1.25], abs=0.01)
    assert result.sections == []


ENERGY_CASES = [
    ([0.1, 0.4, 1.0, 0.1], 16.0, 0.0),
    ([0.1, 1.0, 0.4, 0.1], 8.0, 24.0),
    ([1.0, 0.1, 0.1, 0.1], 0.0, 24.0),
]


@pytest.mark.parametrize("levels,peak_start,quiet_start", ENERGY_CASES)
def test_peak_drop_tracks_loudest_energy_region(levels, peak_start, quiet_start):
    result = analyze_pcm(click_track(120, section_levels=levels))

    sections = getattr(result, "sections", [])
    assert any(
        section.label == "Peak / Drop"
        and section.start_sec <= peak_start + 0.25
        and section.end_sec >= peak_start + 8.0 - 0.25
        for section in sections
    ) and sum(
        section.start_sec < 32.0 and section.end_sec > 0.0
        for section in sections
    ) > 1


@pytest.mark.parametrize("levels,peak_start,quiet_start", ENERGY_CASES)
def test_peak_drop_does_not_cover_distant_quiet_region(levels, peak_start, quiet_start):
    result = analyze_pcm(click_track(120, section_levels=levels))

    sections = getattr(result, "sections", [])
    peaks = [section for section in sections if section.label == "Peak / Drop"]
    assert peaks and not any(
        section.start_sec <= quiet_start + 0.25
        and section.end_sec >= quiet_start + 8.0 - 0.25
        for section in peaks
    )


def test_path_traversal_is_rejected_before_starting_ffmpeg():
    runner = Mock()

    with pytest.raises(ValueError):
        analyze_audio(
            "/virtual/data/../../etc/passwd", data_dir=DATA_DIR, process_runner=runner
        )

    runner.assert_not_called()
