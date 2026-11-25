"""Procedural built-in audio catalog provider interfaces."""

import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
import wave

import numpy as np

from app.models import AudioCatalogTrack


@dataclass(frozen=True)
class SectionSpan:
    name: str
    start_sample: int
    end_sample: int


@dataclass(frozen=True)
class SynthesizedTrack:
    pcm: np.ndarray
    sample_rate: int
    sections: tuple[SectionSpan, ...]
    kick_onsets: tuple[int, ...]


_TRACK_INFO = (
    ("builtin-90", "Chill Groove", 90.0),
    ("builtin-110", "Sunset Pulse", 110.0),
    ("builtin-124", "House Drive", 124.0),
    ("builtin-140", "High-Energy Sprint", 140.0),
)

BUILTIN_TRACKS: tuple[AudioCatalogTrack, ...] = tuple(
    AudioCatalogTrack(
        id=track_id,
        provider="builtin",
        title=title,
        artist="VidYour",
        bpm=bpm,
        duration_sec=80 * 60 / bpm,
        preview_url="",
        download_url="",
        license="CC0",
        attribution="",
    )
    for track_id, title, bpm in _TRACK_INFO
)


def synthesize_track(
    bpm: float, sample_rate: int = 44100
) -> SynthesizedTrack:
    """Return a deterministic synthetic track and its musical section spans."""
    if bpm <= 0 or sample_rate <= 0:
        raise ValueError("bpm and sample_rate must be positive")

    beats_per_section = (16, 16, 32, 16)
    names = ("intro", "buildup", "drop", "outro")
    levels = (0.09, 0.16, 0.30, 0.12)
    cumulative = [0]
    for beat_count in beats_per_section:
        cumulative.append(cumulative[-1] + beat_count)
    boundaries = [
        round(beat * 60 / bpm * sample_rate) for beat in cumulative
    ]
    pcm = np.zeros(boundaries[-1], dtype=np.float32)
    sections = tuple(
        SectionSpan(name, boundaries[index], boundaries[index + 1])
        for index, name in enumerate(names)
    )

    # Each section has a gentle tonal bed at its own level, giving the
    # arrangement clear, repeatable energy changes without sharp transients.
    for section, level in zip(sections, levels):
        indexes = np.arange(section.end_sample - section.start_sample)
        time = indexes / sample_rate
        bed = (
            0.65 * np.sin(2 * np.pi * 110 * time)
            + 0.25 * np.sin(2 * np.pi * 165 * time)
            + 0.10 * np.sin(2 * np.pi * 220 * time)
        )
        pcm[section.start_sample : section.end_sample] = (level * bed).astype(
            np.float32
        )

    kick_onsets = tuple(round(beat * 60 / bpm * sample_rate) for beat in range(80))
    kick_length = max(1, round(0.05 * sample_rate))
    kick_time = np.arange(kick_length, dtype=np.float32) / sample_rate
    kick = (3.2 * np.exp(-kick_time / 0.018) * np.sin(2 * np.pi * 58 * kick_time))
    for section_index, (start_beat, end_beat) in enumerate(
        zip(cumulative, cumulative[1:])
    ):
        for onset in kick_onsets[start_beat:end_beat]:
            end = min(onset + kick_length, len(pcm))
            pcm[onset:end] += (
                levels[section_index] / levels[2] * kick[: end - onset]
            ).astype(np.float32)

    np.clip(pcm, -1.0, 1.0, out=pcm)
    return SynthesizedTrack(pcm, sample_rate, sections, kick_onsets)


class BuiltinDemoAudioProvider:
    name = "builtin"

    def __init__(
        self,
        data_dir: str | Path = Path(),
        process_runner: Callable[..., Any] | None = None,
        file_writer: Callable[..., Any] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.process_runner = process_runner or subprocess.run
        self.file_writer = file_writer

    def search(
        self,
        q: str = "",
        bpm_min: float | None = None,
        bpm_max: float | None = None,
    ) -> list[AudioCatalogTrack]:
        query = q.strip().lower()
        return [
            track
            for track in BUILTIN_TRACKS
            if (bpm_min is None or (track.bpm is not None and track.bpm >= bpm_min))
            and (bpm_max is None or (track.bpm is not None and track.bpm <= bpm_max))
            and (not query or query in track.title.lower() or query in track.artist.lower())
        ]

    def download_track(self, track_id: str, output_dir: str | Path) -> Path:
        track = next((item for item in BUILTIN_TRACKS if item.id == track_id), None)
        if track is None or track.bpm is None:
            return Path()
        output = Path(output_dir) / f"{track.id}.mp3"
        pcm = synthesize_track(track.bpm)
        wav_buffer = BytesIO()
        samples = np.round(pcm.pcm * 32767).astype("<i2")
        with wave.open(wav_buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(pcm.sample_rate)
            wav.writeframes(samples.tobytes())
        wav_path = Path(self.data_dir) / "tmp" / f"{track.id}.wav"
        wav_bytes = wav_buffer.getvalue()
        if self.file_writer is None:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(wav_bytes)
        else:
            self.file_writer(wav_path, wav_bytes)
        if self.file_writer is None:
            output.parent.mkdir(parents=True, exist_ok=True)
        self.process_runner(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(wav_path),
                "-codec:a",
                "libmp3lame",
                "-qscale:a",
                "2",
                str(output),
            ],
            check=False,
            capture_output=True,
        )
        return output
