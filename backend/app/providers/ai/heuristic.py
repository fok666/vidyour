"""Deterministic local analysis provider interface."""

import re
import subprocess
from itertools import combinations
from typing import Any, Callable

from app.models import AIAnalysisResult, AIMoment, MediaAsset
from app.providers.ai.base import beats_to_seconds

_PTS_TIME = re.compile(r"pts_time:([\d.]+)")
_MEAN_Y = re.compile(r"mean:\[([\d.]+)")
_STDEV_Y = re.compile(r"stdev:\[([\d.]+)")
_SIGNALSTAT = re.compile(r"lavfi\.signalstats\.(YAVG|YDIF)=([\d.]+)")


class HeuristicFallbackProvider:
    name = "local"

    def __init__(
        self,
        scene_detector: Callable[..., Any] | None = None,
        media_probe: Any = None,
        process_runner: Callable[..., Any] | None = None,
        filesystem: Any = None,
    ) -> None:
        self.scene_detector = scene_detector
        self.media_probe = media_probe
        self.process_runner = process_runner
        self.filesystem = filesystem

    def _scene_frames(self, asset: MediaAsset) -> list[tuple[float, float, float, float]]:
        run = self.process_runner or subprocess.run
        completed = run(
            ["ffmpeg", "-hide_banner", "-i", asset.local_path,
             "-vf", "select='gt(scene,0.28)+eq(n,0)',signalstats,metadata=print,showinfo",
             "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        frames: dict[float, dict[str, float]] = {}
        current_time: float | None = None
        for line in (completed.stderr or "").splitlines():
            pts = _PTS_TIME.search(line)
            if pts:
                current_time = float(pts.group(1))
            if current_time is None:
                continue
            frame = frames.setdefault(current_time, {})
            mean, stdev, signalstat = (
                _MEAN_Y.search(line),
                _STDEV_Y.search(line),
                _SIGNALSTAT.search(line),
            )
            if mean:
                frame["luminance"] = float(mean.group(1))
            if stdev:
                frame["contrast"] = float(stdev.group(1)) ** 2
            if signalstat:
                key, value = signalstat.groups()
                frame["luminance" if key == "YAVG" else "activity"] = float(value)

        ordered = sorted(frames.items())
        parsed: list[tuple[float, float, float, float]] = []
        previous_luminance: float | None = None
        for timestamp, features in ordered:
            luminance = features.get("luminance", 0.0)
            contrast = features.get("contrast", 0.0)
            activity = features.get("activity", 0.0)
            if not activity and previous_luminance is not None:
                activity = abs(luminance - previous_luminance)
            parsed.append((timestamp, luminance, contrast, activity))
            previous_luminance = luminance
        return parsed

    @staticmethod
    def _activity_score(
        frame: tuple[float, float, float, float],
        previous: tuple[float, float, float, float] | None,
        max_luminance_change: float,
        max_contrast: float,
        max_activity: float,
    ) -> float:
        _, luminance, contrast, activity = frame
        luminance_change = abs(luminance - previous[1]) if previous else 0.0
        return (
            (luminance_change / max_luminance_change if max_luminance_change else 0.0)
            + (contrast / max_contrast if max_contrast else 0.0)
            + (activity / max_activity if max_activity else 0.0)
        )

    def analyze_project_media(
        self,
        assets: list[MediaAsset],
        bpm: float,
        allowed_beat_lengths: list[int],
        ai_context: str,
        result_target: str,
    ) -> AIAnalysisResult | None:
        lengths = allowed_beat_lengths
        if result_target == "high_energy":
            lengths = [beats for beats in allowed_beat_lengths if beats <= 4]
        effect = "crossfade" if result_target == "seamless_flow" else "cut"
        moments: list[AIMoment] = []
        visual_signatures: dict[str, tuple[float, float]] = {}
        for index, asset in enumerate(assets):
            beats = lengths[index % len(lengths)]
            start = 0.0
            if asset.kind == "video":
                frames = self._scene_frames(asset)
                if frames:
                    visual_signatures[asset.id] = (
                        sum(frame[1] for frame in frames) / len(frames),
                        sum(frame[2] for frame in frames) / len(frames),
                    )
                    luminance_changes = [
                        abs(frame[1] - previous[1])
                        for previous, frame in zip(frames, frames[1:])
                    ]
                    max_luminance_change = max(luminance_changes, default=0.0)
                    max_contrast = max((frame[2] for frame in frames), default=0.0)
                    max_activity = max((frame[3] for frame in frames), default=0.0)
                    candidates = list(enumerate(frames))
                    if len(candidates) > 1:
                        candidates = candidates[1:]
                    if result_target == "high_energy":
                        start = max(
                            candidates,
                            key=lambda item: self._activity_score(
                                item[1],
                                frames[item[0] - 1] if item[0] else None,
                                max_luminance_change,
                                max_contrast,
                                max_activity,
                            ),
                        )[1][0]
                    else:
                        start = candidates[0][1][0]
            end = start + beats_to_seconds(beats, bpm)
            if asset.duration_sec:
                end = min(end, asset.duration_sec)
            moments.append(
                AIMoment(
                    asset_id=asset.id, start_sec=start, end_sec=end, beat_length=beats,
                    label="scene_change" if asset.kind == "video" else "photo",
                    score=0.5, reason="Local scene-change heuristic", suggested_effect=effect,
                )
            )
        sequence = [asset.id for asset in assets]
        if result_target == "perfect_loop" and len(visual_signatures) >= 2:
            first, last = min(
                combinations(visual_signatures, 2),
                key=lambda pair: sum(
                    abs(a - b) / scale
                    for a, b, scale in zip(
                        visual_signatures[pair[0]],
                        visual_signatures[pair[1]],
                        (255.0, 255.0**2),
                    )
                ),
            )
            sequence = [first] + [a for a in sequence if a not in (first, last)] + [last]
        return AIAnalysisResult(moments=moments, suggested_sequence=sequence)
