"""AI analysis provider contract and shared result declarations."""

from typing import Protocol

from app.models import AIAnalysisResult, MediaAsset

BEAT_LENGTHS = (2, 4, 8, 16)
MIN_MOMENT_SEC = 0.75
MAX_MOMENT_SEC = 12.0

TARGET_INSTRUCTIONS = {
    "perfect_loop": (
        "Prefer opening and closing clips that match in visual appearance and motion "
        "so the last moment loops back seamlessly into the first."
    ),
    "seamless_flow": "Favor continuous motion and color continuity between clips.",
}


class AIProvider(Protocol):
    name: str

    def analyze_project_media(
        self,
        assets: list[MediaAsset],
        bpm: float,
        allowed_beat_lengths: list[int],
        ai_context: str,
        result_target: str,
    ) -> AIAnalysisResult | None: ...


def beats_to_seconds(beats: int, bpm: float) -> float:
    return beats * 60.0 / bpm


def compute_allowed_beat_lengths(bpm: float) -> list[int]:
    return [
        beats
        for beats in BEAT_LENGTHS
        if MIN_MOMENT_SEC <= beats_to_seconds(beats, bpm) <= MAX_MOMENT_SEC
    ]


def build_analysis_prompt(
    assets: list[MediaAsset],
    bpm: float,
    allowed_beat_lengths: list[int],
    ai_context: str,
    result_target: str,
) -> str:
    durations = ", ".join(f"{beats} beats = {beats_to_seconds(beats, bpm):.1f}s" for beats in allowed_beat_lengths)
    asset_lines = "\n".join(
        f"- asset_id={asset.id} kind={asset.kind} duration={asset.duration_sec}s" for asset in assets
    )
    return (
        "Identify the key visual moments in the attached media for a music-synced video.\n"
        f"Project tempo: {bpm:g} BPM. Each moment must last exactly one of: {durations}.\n"
        f"User context: {ai_context}\n"
        f"Result target: {result_target}. {TARGET_INSTRUCTIONS.get(result_target, '')}\n"
        f"Assets in attachment order:\n{asset_lines}\n"
        "Return moments and a suggested_sequence of asset ids as JSON."
    )
