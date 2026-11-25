"""Decode audio and derive its beat grid, waveform, and energy sections."""

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Any, Callable

import numpy as np
from scipy.signal import correlate, find_peaks

from app.services.media_probe import resolve_safe_path


_SAMPLE_RATE = 22050
_HOP = 44


@dataclass
class AudioSection:
    start_sec: float = 0.0
    end_sec: float = 0.0
    beat_length: float = 0.0
    label: str = ""


@dataclass
class AudioAnalysisResult:
    bpm: float = 0.0
    beat_offset_sec: float = 0.0
    beat_times: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    waveform: list[float] = field(default_factory=list)
    sections: list[AudioSection] = field(default_factory=list)


def _waveform_peaks(pcm: np.ndarray) -> list[float]:
    """Normalize the peak magnitude of each of 300 waveform buckets."""
    buckets = np.array_split(np.abs(pcm), 300)
    peaks = np.array(
        [float(bucket.max()) if len(bucket) else 0.0 for bucket in buckets]
    )
    if peaks.max() > 0:
        peaks /= peaks.max()
    return peaks.tolist()


def _estimate_beat_period(onset: np.ndarray) -> float | None:
    """Score beat-period candidates using autocorrelation harmonics."""
    autocorrelation = correlate(onset, onset, mode="full", method="fft")[
        len(onset) - 1:
    ]
    autocorrelation = np.convolve(autocorrelation, [0.25, 0.5, 0.25], mode="same")
    min_lag = max(1, int(np.ceil(60 * _SAMPLE_RATE / (180 * _HOP))))
    max_lag = min(len(onset) - 1, int(np.floor(60 * _SAMPLE_RATE / (60 * _HOP))))
    if min_lag > max_lag:
        return None
    lags = np.arange(min_lag, max_lag + 1, 0.25)
    scores = np.zeros(len(lags))
    for harmonic in range(1, 5):
        scores += np.interp(
            harmonic * lags, np.arange(len(autocorrelation)), autocorrelation,
            left=0, right=0,
        ) / harmonic
    candidate_bpms = 60 * _SAMPLE_RATE / (_HOP * lags)
    preferred = (candidate_bpms >= 85) & (candidate_bpms <= 155)
    scores[preferred] *= 1.05
    best_lag = float(lags[np.argmax(scores)])
    best_bpm = 60 * _SAMPLE_RATE / (_HOP * best_lag)
    if not 85 <= best_bpm <= 155:
        # Prefer the in-range tempo octave over its out-of-range half/double.
        octave_lag = best_lag / 2 if best_bpm < 85 else best_lag * 2
        window = (lags >= octave_lag - 1) & (lags <= octave_lag + 1)
        if np.any(window & preferred):
            candidates = np.flatnonzero(window & preferred)
            best_lag = float(lags[candidates[np.argmax(scores[candidates])]])
    return best_lag * _HOP / _SAMPLE_RATE


def _align_beat_grid(onset: np.ndarray, period: float) -> tuple[float, float] | None:
    """Find the onset phase and refine the period with aligned onset peaks."""
    indices, _ = find_peaks(
        onset,
        height=onset.max() * 0.05,
        distance=max(1, int(period * _SAMPLE_RATE / _HOP / 2)),
    )
    if onset[0] > onset.max() * 0.05:
        indices = np.insert(indices, 0, 0)
    if not len(indices):
        return None
    times = (indices + 0.5) * _HOP / _SAMPLE_RATE
    weights = onset[indices]
    # Circular averaging estimates phase without a discontinuity at one period.
    phase = (
        np.angle(np.sum(weights * np.exp(2j * np.pi * times / period)))
        * period / (2 * np.pi)
    )
    beat_numbers = np.rint((times - phase) / period)
    aligned = np.abs(times - (phase + beat_numbers * period)) < period / 4
    if np.count_nonzero(aligned) > 1:
        # Fit onset time against beat number, weighted by onset strength.
        aligned_beats = beat_numbers[aligned]
        aligned_times = times[aligned]
        aligned_weights = weights[aligned]
        mean_beat = np.average(aligned_beats, weights=aligned_weights)
        mean_time = np.average(aligned_times, weights=aligned_weights)
        variance = np.sum(aligned_weights * (aligned_beats - mean_beat) ** 2)
        if variance > 0:
            refined = float(
                np.sum(
                    aligned_weights * (aligned_beats - mean_beat)
                    * (aligned_times - mean_time)
                ) / variance
            )
            if 60 / 180 <= refined <= 1:
                period = refined
                phase = float(mean_time - period * mean_beat)
    offset = float(phase % period)
    if period - offset < _HOP / _SAMPLE_RATE:
        offset = 0.0
    return period, offset


def _energy_sections(
    pcm: np.ndarray, duration: float, period: float, offset: float
) -> list[AudioSection]:
    """Group complete bars and label the highest-energy group as the peak."""
    bar_period = 4 * period
    bars = int(np.floor((duration - offset) / bar_period + 1e-8))
    sections = []
    section_energy = []
    for first_bar in range(0, bars, 4):
        last_bar = min(first_bar + 4, bars)
        start = offset + first_bar * bar_period
        end = offset + last_bar * bar_period
        samples = pcm[int(start * _SAMPLE_RATE):int(end * _SAMPLE_RATE)]
        section_energy.append(float(np.mean(samples.astype(np.float64) ** 2)))
        sections.append(
            AudioSection(start, end, (last_bar - first_bar) * 4, "Verse / Groove")
        )
    if sections:
        # Peak takes precedence even when it is also the intro or outro.
        sections[0].label = "Intro"
        sections[-1].label = "Outro"
        sections[int(np.argmax(section_energy))].label = "Peak / Drop"
    return sections


def analyze_audio(
    audio_path: str | Path,
    data_dir: str | Path | None = None,
    process_runner: Callable[..., Any] | None = None,
) -> AudioAnalysisResult | None:
    path = (
        resolve_safe_path(data_dir, audio_path)
        if data_dir is not None
        else Path(audio_path).resolve()
    )
    runner = process_runner or subprocess.run
    decoded = runner(
        [
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
            "-ac", "1", "-ar", str(_SAMPLE_RATE), "-f", "f32le", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    pcm = np.frombuffer(decoded.stdout, dtype="<f4")
    if not len(pcm):
        return None

    waveform = _waveform_peaks(pcm)

    frame_starts = np.arange(0, len(pcm), _HOP)
    energy = np.sqrt(
        np.add.reduceat(pcm.astype(np.float64) ** 2, frame_starts) / _HOP
    )
    onset = np.maximum(0.0, np.diff(energy, prepend=0.0))
    if not np.any(onset):
        return AudioAnalysisResult(waveform=waveform)

    period = _estimate_beat_period(onset)
    if period is None:
        return AudioAnalysisResult(waveform=waveform)

    grid = _align_beat_grid(onset, period)
    if grid is None:
        return AudioAnalysisResult(waveform=waveform)
    period, offset = grid
    duration = len(pcm) / _SAMPLE_RATE
    beat_count = max(0, int(np.ceil((duration - offset) / period)))
    beat_times = (offset + np.arange(beat_count) * period).tolist()
    bpm = 60.0 / period

    sections = _energy_sections(pcm, duration, period, offset)
    return AudioAnalysisResult(
        bpm=bpm,
        beat_offset_sec=offset,
        beat_times=beat_times,
        downbeats=beat_times[::4],
        waveform=waveform,
        sections=sections,
    )


class AudioAnalyzer:
    def __init__(
        self,
        data_dir: str | Path = Path(),
        process_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.process_runner = process_runner

    def analyze_audio(self, audio_path: str | Path) -> AudioAnalysisResult | None:
        return analyze_audio(audio_path, self.data_dir, self.process_runner)
