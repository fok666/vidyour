"""Gemini multimodal analysis provider interface."""

import os
import subprocess
import uuid
from typing import Any, Callable

from google.genai import errors as genai_errors

from app.models import AIAnalysisResult, MediaAsset
from app.providers.ai.base import build_analysis_prompt
from app.services.media_probe import resolve_safe_path

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
PROXY_MAX_BYTES = 10 * 1024 * 1024
PROXY_MAX_SHORT_SIDE = 720


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        settings: Any,
        client: Any = None,
        media_probe: Any = None,
        process_runner: Callable[..., Any] | None = None,
        filesystem: Any = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.media_probe = media_probe
        self.process_runner = process_runner
        self.filesystem = filesystem

    def _needs_proxy(self, asset: MediaAsset, size: int) -> bool:
        short_side = min(asset.width or 0, asset.height or 0)
        return short_side > PROXY_MAX_SHORT_SIDE or size > PROXY_MAX_BYTES

    def _make_proxy(self, source: Any) -> Any:
        proxy = self.settings.data_dir / "tmp" / f"gemini-proxy-{uuid.uuid4().hex}.mp4"
        run = self.process_runner or subprocess.run
        run(
            ["ffmpeg", "-y", "-i", str(source), "-vf", "scale=-2:480", "-c:v", "libx264",
             "-preset", "veryfast", "-c:a", "aac", str(proxy)],
            check=True,
            capture_output=True,
            text=True,
        )
        return proxy

    def analyze_project_media(
        self,
        assets: list[MediaAsset],
        bpm: float,
        allowed_beat_lengths: list[int],
        ai_context: str,
        result_target: str,
    ) -> AIAnalysisResult | None:
        fs = self.filesystem or os
        local_proxies: list[Any] = []
        remote_files: list[Any] = []
        try:
            for asset in assets:
                path = resolve_safe_path(self.settings.data_dir, asset.local_path)
                if asset.kind == "video" and self._needs_proxy(asset, fs.stat(path).st_size):
                    path = self._make_proxy(path)
                    local_proxies.append(path)
                remote_files.append(self.client.files.upload(file=str(path)))
            prompt = build_analysis_prompt(assets, bpm, allowed_beat_lengths, ai_context, result_target)
            try:
                response = self.client.models.generate_content(
                    model=self.settings.gemini_model or DEFAULT_GEMINI_MODEL,
                    contents=[*remote_files, prompt],
                    config={"response_mime_type": "application/json", "response_schema": AIAnalysisResult},
                )
            except genai_errors.APIError as exc:
                raise RuntimeError(
                    f"Gemini analysis failed ({exc.code} {exc.status}): {exc.message}. "
                    "Check GEMINI_API_KEY and your Gemini quota, then retry."
                ) from exc
            if isinstance(response.parsed, AIAnalysisResult):
                return response.parsed
            return AIAnalysisResult.model_validate_json(response.text)
        finally:
            for remote in remote_files:
                self.client.files.delete(name=remote.name)
            for proxy in local_proxies:
                fs.unlink(proxy)
