"""AWS Bedrock analysis provider interface."""

import json
import os
import subprocess
import uuid
from typing import Any, Callable

from botocore.exceptions import ClientError

from app.models import AIAnalysisResult, MediaAsset
from app.providers.ai.base import build_analysis_prompt
from app.services.media_probe import resolve_safe_path

DEFAULT_BEDROCK_MODEL_ID = "amazon.nova-lite-v1:0"
MAX_KEYFRAMES = 12


class BedrockProvider:
    name = "bedrock"

    def __init__(
        self,
        settings: Any,
        client: Any = None,
        frame_extractor: Callable[..., Any] | None = None,
        filesystem: Any = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.frame_extractor = frame_extractor
        self.filesystem = filesystem

    def _extract_keyframes(
        self, asset: MediaAsset, source: Any, temp_files: list[Any]
    ) -> list[tuple[float, Any]]:
        run = self.frame_extractor or subprocess.run
        duration = asset.duration_sec or 0.0
        frames = []
        for index in range(MAX_KEYFRAMES):
            timestamp = index * duration / MAX_KEYFRAMES
            frame = self.settings.data_dir / "tmp" / f"bedrock-kf-{uuid.uuid4().hex}.jpg"
            run(
                ["ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", str(source),
                 "-frames:v", "1", "-q:v", "2", str(frame)],
                check=True,
                capture_output=True,
                text=True,
            )
            temp_files.append(frame)
            frames.append((timestamp, frame))
        return frames

    def analyze_project_media(
        self,
        assets: list[MediaAsset],
        bpm: float,
        allowed_beat_lengths: list[int],
        ai_context: str,
        result_target: str,
    ) -> AIAnalysisResult | None:
        fs = self.filesystem or _OsFilesystem()
        temp_files: list[Any] = []
        try:
            content: list[dict[str, Any]] = []
            for asset in assets:
                path = resolve_safe_path(self.settings.data_dir, asset.local_path)
                if asset.kind == "video":
                    images = self._extract_keyframes(asset, path, temp_files)
                elif asset.kind == "live_photo":
                    images = (
                        [(None, resolve_safe_path(self.settings.data_dir, asset.still_path))]
                        if asset.still_path
                        else []
                    )
                    images.extend(self._extract_keyframes(asset, path, temp_files))
                else:
                    images = [(None, path)]
                for timestamp, image in images:
                    frame_label = f"asset_id={asset.id}"
                    if timestamp is None:
                        frame_label += " (photo still; no video timestamp)"
                    else:
                        frame_label += f" timestamp={timestamp:.3f}s"
                    content.append({"text": frame_label})
                    content.append({"image": {"format": "jpeg", "source": {"bytes": fs.read_bytes(image)}}})
            content.append({"text": build_analysis_prompt(assets, bpm, allowed_beat_lengths, ai_context, result_target)})
            try:
                response = self.client.converse(
                    modelId=self.settings.bedrock_model_id or DEFAULT_BEDROCK_MODEL_ID,
                    messages=[{"role": "user", "content": content}],
                    outputConfig={
                        "textFormat": {
                            "type": "json_schema",
                            "structure": {
                                "jsonSchema": {
                                    "name": "AIAnalysisResult",
                                    "schema": json.dumps(AIAnalysisResult.model_json_schema()),
                                }
                            },
                        }
                    },
                )
            except ClientError as exc:
                error = exc.response.get("Error", {})
                raise RuntimeError(
                    f"Bedrock analysis failed ({error.get('Code')}): {error.get('Message')}. "
                    "Check AWS credentials, model access and Bedrock quotas, then retry."
                ) from exc
            text = next(b["text"] for b in response["output"]["message"]["content"] if "text" in b)
            return AIAnalysisResult.model_validate_json(text)
        finally:
            for temp_file in temp_files:
                fs.unlink(temp_file)


class _OsFilesystem:
    def read_bytes(self, path: Any) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def unlink(self, path: Any) -> None:
        os.unlink(path)
