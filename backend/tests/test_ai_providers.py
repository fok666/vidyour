"""Unit tests for AI key-moment providers: beat lengths, provider selection,
Gemini, Bedrock and the deterministic heuristic fallback.

External collaborators (google-genai client, boto3 bedrock-runtime client,
ffmpeg subprocess calls, filesystem I/O) are replaced by in-memory fakes owned
by this module. Settings, domain models and the media-probe path guard are real.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError as BotocoreClientError
from google.genai import errors as genai_errors

from app.config import Settings
from app.models import AIAnalysisResult, AIMoment, MediaAsset
from app.providers.ai import select_ai_provider
from app.providers.ai.base import compute_allowed_beat_lengths
from app.providers.ai.bedrock import BedrockProvider
from app.providers.ai.gemini import GeminiProvider
from app.providers.ai.heuristic import HeuristicFallbackProvider

DATA_DIR = Path("/virtual/vidyour-data")
TMP_DIR = DATA_DIR / "tmp"
MIB = 1024 * 1024

_RELEVANT_ENV = (
    "AI_PROVIDER",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "BEDROCK_MODEL_ID",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "DATA_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RELEVANT_ENV:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def _settings(**kwargs: Any) -> Settings:
    kwargs.setdefault("data_dir", DATA_DIR)
    return Settings(_env_file=None, **kwargs)


# --------------------------------------------------------------------------- fakes


class FakeFilesystem:
    """In-memory filesystem: stat/size/read/write/exists/glob/unlink/rmtree/mkdir."""

    def __init__(self, files: dict[Path, bytes] | None = None, sizes: dict[Path, int] | None = None) -> None:
        self.files: dict[Path, bytes] = {self._n(p): d for p, d in (files or {}).items()}
        self.sizes: dict[Path, int] = {self._n(p): s for p, s in (sizes or {}).items()}
        self.deleted: list[Path] = []
        self.created_dirs: list[Path] = []

    @staticmethod
    def _n(path: str | Path) -> Path:
        return Path(path).resolve()

    # --- size / stat
    def _size(self, path: str | Path) -> int:
        target = self._n(path)
        if target in self.sizes:
            return self.sizes[target]
        if target in self.files:
            return len(self.files[target])
        raise FileNotFoundError(str(target))

    def stat(self, path: str | Path) -> Any:
        return SimpleNamespace(st_size=self._size(path))

    stat_file = stat

    def getsize(self, path: str | Path) -> int:
        return self._size(path)

    file_size = getsize
    size = getsize

    # --- existence / dirs
    def exists(self, path: str | Path) -> bool:
        target = self._n(path)
        return target in self.files or any(p.is_relative_to(target) for p in self.files)

    def is_file(self, path: str | Path) -> bool:
        return self._n(path) in self.files

    def mkdir(self, path: str | Path, *args: Any, **kwargs: Any) -> None:
        self.created_dirs.append(self._n(path))

    makedirs = mkdir

    # --- content
    def read_bytes(self, path: str | Path) -> bytes:
        target = self._n(path)
        if target not in self.files:
            raise FileNotFoundError(str(target))
        return self.files[target]

    def write_bytes(self, path: str | Path, data: bytes) -> None:
        self.files[self._n(path)] = data

    # --- listing
    def glob(self, directory: str | Path, pattern: str = "*") -> list[Path]:
        root = self._n(directory)
        return sorted(p for p in self.files if p.parent == root and fnmatch.fnmatch(p.name, pattern))

    def list_dir(self, directory: str | Path) -> list[Path]:
        return self.glob(directory, "*")

    iterdir = list_dir
    listdir = list_dir

    # --- deletion
    def unlink(self, path: str | Path, *args: Any, **kwargs: Any) -> None:
        target = self._n(path)
        self.deleted.append(target)
        self.files.pop(target, None)
        self.sizes.pop(target, None)

    remove = unlink
    delete = unlink

    def rmtree(self, path: str | Path, *args: Any, **kwargs: Any) -> None:
        root = self._n(path)
        for p in [p for p in self.files if p == root or p.is_relative_to(root)]:
            self.unlink(p)
        self.deleted.append(root)

    def was_deleted(self, path: str | Path) -> bool:
        target = self._n(path)
        return any(target == d or target.is_relative_to(d) for d in self.deleted)


def _cmd_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[str]:
    cmd = args[0] if args else kwargs.get("args")
    return [str(part) for part in cmd]


def _flag(cmd: list[str], flag: str) -> str | None:
    for index in range(len(cmd) - 1):
        if cmd[index] == flag:
            return cmd[index + 1]
    return None


class FakeFfmpegRunner:
    """subprocess.run-compatible fake. ffmpeg outputs are written into the fake filesystem.

    For output patterns containing '%' (e.g. frame_%03d.jpg) it writes
    `-frames:v` frames (or one per second of source duration when absent).
    `stderr_by_name` maps input basenames to canned stderr (showinfo output).
    """

    def __init__(
        self,
        filesystem: FakeFilesystem | None = None,
        stderr_by_name: dict[str, str] | None = None,
        durations_by_name: dict[str, float] | None = None,
    ) -> None:
        self.filesystem = filesystem
        self.stderr_by_name = stderr_by_name or {}
        self.durations_by_name = durations_by_name or {}
        self.calls: list[list[str]] = []
        self.frame_counter = 0

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = _cmd_of(args, kwargs)
        self.calls.append(cmd)
        source = _flag(cmd, "-i")
        name = Path(source).name if source else ""
        if Path(cmd[0]).name.startswith("ffprobe"):
            duration = self.durations_by_name.get(Path(cmd[-1]).name, 10.0)
            stdout = json.dumps(
                {
                    "streams": [{"codec_type": "video", "width": 1280, "height": 720, "avg_frame_rate": "30/1"}],
                    "format": {"duration": str(duration)},
                }
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        output = cmd[-1]
        if self.filesystem is not None and output not in ("-", "pipe:1") and "null" not in output:
            if "%" in output:
                frames_flag = _flag(cmd, "-frames:v") or _flag(cmd, "-vframes")
                count = int(frames_flag) if frames_flag else int(self.durations_by_name.get(name, 20.0))
                for number in range(1, count + 1):
                    self.frame_counter += 1
                    self.filesystem.write_bytes(output % number, f"jpeg-frame-{self.frame_counter}".encode())
            else:
                self.frame_counter += 1
                self.filesystem.write_bytes(output, f"ffmpeg-output-{self.frame_counter}".encode())
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=self.stderr_by_name.get(name, ""))

    def ffmpeg_calls(self) -> list[list[str]]:
        return [c for c in self.calls if Path(c[0]).name.startswith("ffmpeg")]


def _asset(
    asset_id: str,
    filename: str,
    kind: str = "video",
    *,
    duration_sec: float = 10.0,
    width: int | None = 1280,
    height: int | None = 720,
) -> MediaAsset:
    return MediaAsset(
        id=asset_id,
        project_id="proj-1",
        kind=kind,
        source="upload",
        filename=filename,
        local_path=f"assets/{filename}",
        still_path=None,
        thumbnail_path=None,
        duration_sec=duration_sec if kind != "photo" else 4.0,
        width=width,
        height=height,
        fps=30.0 if kind != "photo" else None,
        has_audio=kind != "photo",
        bpm=None,
        beat_grid=None,
        metadata={},
        created_at="2026-11-22T10:00:00Z",
    )


def _expected_result(video_id: str = "vid-1", photo_id: str = "pho-1") -> AIAnalysisResult:
    return AIAnalysisResult(
        moments=[
            AIMoment(
                asset_id=video_id,
                start_sec=1.5,
                end_sec=3.5,
                beat_length=4,
                label="smile",
                score=0.92,
                reason="Warm golden-hour smile",
                suggested_effect="crossfade",
            ),
            AIMoment(
                asset_id=photo_id,
                start_sec=0.0,
                end_sec=1.0,
                beat_length=2,
                label="portrait",
                score=0.81,
                reason="Backlit portrait",
                suggested_effect="zoom_in",
            ),
        ],
        suggested_sequence=[video_id, photo_id],
    )


# ---------------------------------------------------------------- gemini helpers


def _gemini_client(
    result: AIAnalysisResult | None = None, error: Exception | None = None
) -> tuple[MagicMock, list[str], list[str], list[str]]:
    uploaded_paths: list[str] = []
    uploaded_names: list[str] = []
    deleted_names: list[str] = []
    by_name: dict[str, Any] = {}

    def upload(*args: Any, **kwargs: Any) -> Any:
        path = kwargs.get("file", kwargs.get("path", args[0] if args else None))
        uploaded_paths.append(str(path))
        name = f"files/remote-{len(uploaded_names) + 1}"
        uploaded_names.append(name)
        remote = SimpleNamespace(
            name=name,
            uri=f"https://generativelanguage.googleapis.com/v1beta/{name}",
            mime_type="video/mp4" if str(path).lower().endswith(".mp4") else "image/jpeg",
            state=SimpleNamespace(name="ACTIVE"),
        )
        by_name[name] = remote
        return remote

    def get(*args: Any, **kwargs: Any) -> Any:
        return by_name[kwargs.get("name", args[0] if args else None)]

    def delete(*args: Any, **kwargs: Any) -> None:
        deleted_names.append(str(kwargs.get("name", args[0] if args else None)))

    client = MagicMock()
    client.files.upload.side_effect = upload
    client.files.get.side_effect = get
    client.files.delete.side_effect = delete
    if error is not None:
        client.models.generate_content.side_effect = error
    else:
        payload = result or _expected_result()
        client.models.generate_content.return_value = SimpleNamespace(
            text=payload.model_dump_json(), parsed=payload
        )
    return client, uploaded_paths, uploaded_names, deleted_names


def _texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _texts(v)]
    found: list[str] = []
    text = getattr(value, "text", None)
    if isinstance(text, str):
        found.append(text)
    parts = getattr(value, "parts", None)
    if isinstance(parts, (list, tuple)):
        found.extend(_texts(parts))
    return found


def _cfg(config: Any, key: str) -> Any:
    if isinstance(config, dict):
        return config.get(key)
    return getattr(config, key, None)


def _gemini_prompt(call: Any) -> str:
    fragments = _texts(call.kwargs.get("contents"))
    fragments.extend(_texts(list(call.args)))
    fragments.extend(_texts(_cfg(call.kwargs.get("config"), "system_instruction")))
    return "\n".join(fragments)


def _gemini_fs(video_name: str, video_size: int, photo_name: str | None = None) -> FakeFilesystem:
    files = {DATA_DIR / "assets" / video_name: b"video-bytes"}
    if photo_name:
        files[DATA_DIR / "assets" / photo_name] = b"photo-bytes"
    return FakeFilesystem(files=files, sizes={DATA_DIR / "assets" / video_name: video_size})


def _run_gemini(
    provider: GeminiProvider, assets: list[MediaAsset], result_target: str = "perfect_loop"
) -> AIAnalysisResult | None:
    return provider.analyze_project_media(
        assets,
        bpm=120.0,
        allowed_beat_lengths=[2, 4, 8, 16],
        ai_context="golden hour smiles",
        result_target=result_target,
    )


def _single_generate_call(client: MagicMock) -> Any:
    assert client.models.generate_content.call_count == 1, "generate_content was not called exactly once"
    return client.models.generate_content.call_args


def _assert_proxy_flow(
    runner: FakeFfmpegRunner, filesystem: FakeFilesystem, uploaded_paths: list[str], source: Path
) -> Path:
    proxy_calls = [c for c in runner.ffmpeg_calls() if _flag(c, "-i") and Path(_flag(c, "-i")).resolve() == source]
    assert len(proxy_calls) == 1, f"expected one ffmpeg proxy transcode of {source}, got {runner.calls}"
    cmd = proxy_calls[0]
    proxy = Path(cmd[-1]).resolve()
    assert proxy.is_relative_to(TMP_DIR), f"proxy {proxy} is not under {TMP_DIR}"
    assert any(re.search(r"(?<!\d)480(?!\d)", part) for part in cmd), f"ffmpeg command lacks 480p scale: {cmd}"
    uploaded = [Path(p).resolve() for p in uploaded_paths]
    assert proxy in uploaded, f"proxy {proxy} was not uploaded; uploads={uploaded}"
    assert source not in uploaded, "original source was uploaded instead of the proxy"
    assert filesystem.was_deleted(proxy), "local temporary proxy was not deleted"
    assert proxy not in filesystem.files
    return proxy


# ------------------------------------------------------------------ beat lengths


def test_compute_allowed_beat_lengths_at_120_bpm_returns_all_four_lengths() -> None:
    # GIVEN project BPM 120 (0.5s per beat)
    # WHEN
    lengths = compute_allowed_beat_lengths(120.0)
    # THEN
    assert lengths == [2, 4, 8, 16]


def test_compute_allowed_beat_lengths_at_60_bpm_excludes_16_beats_over_12_seconds() -> None:
    # GIVEN project BPM 60 (16 beats = 16s > 12s)
    # WHEN
    lengths = compute_allowed_beat_lengths(60.0)
    # THEN
    assert lengths == [2, 4, 8]


def test_compute_allowed_beat_lengths_at_40_bpm_includes_8_beats_at_12_seconds() -> None:
    # GIVEN project BPM 40 (8 beats = exactly 12s)
    # WHEN
    lengths = compute_allowed_beat_lengths(40.0)
    # THEN
    assert lengths == [2, 4, 8]


def test_compute_allowed_beat_lengths_at_160_bpm_includes_exact_075_second_boundary() -> None:
    # GIVEN project BPM 160 (2 beats = exactly 0.75s)
    # WHEN
    lengths = compute_allowed_beat_lengths(160.0)
    # THEN
    assert lengths == [2, 4, 8, 16]


def test_compute_allowed_beat_lengths_just_above_160_bpm_excludes_2_beats() -> None:
    # GIVEN project BPM just above 160 (2 beats < 0.75s)
    # WHEN
    lengths = compute_allowed_beat_lengths(160.01)
    # THEN
    assert lengths == [4, 8, 16]


# ------------------------------------------------------------- provider selection


@pytest.mark.parametrize(
    ("settings_kwargs", "expected_cls"),
    [
        ({"ai_provider": "auto", "gemini_api_key": "gk"}, GeminiProvider),
        (
            {
                "ai_provider": "bedrock",
                "aws_access_key_id": "test-access-key",
                "aws_secret_access_key": "test-secret-key",
            },
            BedrockProvider,
        ),
        ({"ai_provider": "local"}, HeuristicFallbackProvider),
    ],
    ids=["auto-gemini", "bedrock", "local"],
)
def test_select_ai_provider_returns_provider_for_auto_bedrock_and_local(
    settings_kwargs: dict[str, Any], expected_cls: type
) -> None:
    # GIVEN
    settings = _settings(**settings_kwargs)
    # WHEN
    provider = select_ai_provider(settings)
    # THEN
    assert isinstance(provider, expected_cls), f"got {provider!r}"


def test_select_ai_provider_returns_gemini_when_explicitly_requested_with_key() -> None:
    # GIVEN
    settings = _settings(ai_provider="gemini", gemini_api_key="gk")
    # WHEN
    provider = select_ai_provider(settings)
    # THEN
    assert isinstance(provider, GeminiProvider), f"got {provider!r}"


def test_select_ai_provider_constructs_bedrock_runtime_client_and_passes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GIVEN
    settings = _settings(
        ai_provider="bedrock",
        aws_region="eu-west-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    bedrock_client = MagicMock()
    boto3_client = MagicMock(return_value=bedrock_client)
    monkeypatch.setattr("boto3.client", boto3_client)

    # WHEN
    provider = select_ai_provider(settings)

    # THEN
    boto3_client.assert_called_once_with(
        "bedrock-runtime",
        region_name="eu-west-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    assert isinstance(provider, BedrockProvider)
    assert provider.client is bedrock_client


def test_select_ai_provider_auto_without_any_keys_returns_heuristic_fallback() -> None:
    # GIVEN
    settings = _settings(ai_provider="auto")
    # WHEN
    provider = select_ai_provider(settings)
    # THEN
    assert isinstance(provider, HeuristicFallbackProvider), f"got {provider!r}"


def test_select_ai_provider_bedrock_without_aws_credentials_falls_back_to_local() -> None:
    # GIVEN
    settings = _settings(ai_provider="bedrock")
    # WHEN
    provider = select_ai_provider(settings)
    # THEN
    assert isinstance(provider, HeuristicFallbackProvider), f"got {provider!r}"
    assert not isinstance(provider, BedrockProvider)
    capabilities = settings.capabilities()
    assert capabilities["bedrock"] is False
    assert capabilities["active_ai_provider"] == "local"


# ------------------------------------------------------------------ gemini model


@pytest.mark.parametrize(
    ("model_kwargs", "expected_model"),
    [({}, "gemini-2.5-flash"), ({"gemini_model": "gemini-configured-test"}, "gemini-configured-test")],
    ids=["default", "configured"],
)
def test_gemini_provider_passes_default_or_configured_model(
    model_kwargs: dict[str, Any], expected_model: str
) -> None:
    # GIVEN
    settings = _settings(gemini_api_key="gk", **model_kwargs)
    client, _, _, _ = _gemini_client()
    filesystem = _gemini_fs("clip.mp4", 2 * MIB, "photo.jpg")
    provider = GeminiProvider(
        settings, client=client, process_runner=FakeFfmpegRunner(filesystem), filesystem=filesystem
    )
    assets = [_asset("vid-1", "clip.mp4"), _asset("pho-1", "photo.jpg", "photo")]
    # WHEN
    _run_gemini(provider, assets)
    # THEN
    call = _single_generate_call(client)
    assert call.kwargs.get("model") == expected_model


# ------------------------------------------------------------------ gemini prompt


def test_gemini_provider_builds_perfect_loop_prompt_with_json_schema_cleans_up_and_parses() -> None:
    # GIVEN
    settings = _settings(gemini_api_key="gk")
    expected = _expected_result()
    client, _, uploaded_names, deleted_names = _gemini_client(expected)
    filesystem = _gemini_fs("clip.mp4", 2 * MIB, "photo.jpg")
    provider = GeminiProvider(
        settings, client=client, process_runner=FakeFfmpegRunner(filesystem), filesystem=filesystem
    )
    assets = [_asset("vid-1", "clip.mp4"), _asset("pho-1", "photo.jpg", "photo")]
    # WHEN
    result = _run_gemini(provider, assets, "perfect_loop")
    # THEN
    call = _single_generate_call(client)
    prompt = _gemini_prompt(call)
    lowered = prompt.lower()
    assert "120" in prompt
    for beats, seconds in ((2, 1), (4, 2), (8, 4), (16, 8)):
        assert re.search(rf"\b{beats} beats?\s*=\s*{seconds}(\.0+)?\s*s", prompt), (
            f"prompt lacks exact duration '{beats} beats = {seconds}.0s'"
        )
    assert "golden hour smiles" in prompt
    assert "perfect_loop" in prompt
    for word in ("opening", "closing", "match", "visual", "motion"):
        assert word in lowered, f"perfect_loop instruction lacks '{word}'"
    config = call.kwargs.get("config")
    assert _cfg(config, "response_mime_type") == "application/json"
    assert _cfg(config, "response_schema") is AIAnalysisResult
    assert uploaded_names, "no media was uploaded via files.upload"
    assert sorted(deleted_names) == sorted(uploaded_names)
    assert isinstance(result, AIAnalysisResult)
    assert result.moments == expected.moments
    assert result.suggested_sequence == expected.suggested_sequence


def test_gemini_provider_parses_json_when_response_parsed_is_not_analysis_result() -> None:
    # GIVEN
    settings = _settings(gemini_api_key="gk")
    expected = _expected_result()
    client, _, _, _ = _gemini_client(expected)
    client.models.generate_content.return_value = SimpleNamespace(
        text=expected.model_dump_json(), parsed={"unexpected": "parsed response"}
    )
    filesystem = _gemini_fs("clip.mp4", 2 * MIB)
    provider = GeminiProvider(
        settings, client=client, process_runner=FakeFfmpegRunner(filesystem), filesystem=filesystem
    )
    assets = [_asset("vid-1", "clip.mp4")]
    # WHEN
    result = _run_gemini(provider, assets)
    # THEN
    assert isinstance(result, AIAnalysisResult)
    assert result.moments == expected.moments
    assert result.suggested_sequence == expected.suggested_sequence


def test_gemini_provider_seamless_flow_prompt_favors_continuous_motion_and_color() -> None:
    # GIVEN
    settings = _settings(gemini_api_key="gk")
    client, _, _, _ = _gemini_client()
    filesystem = _gemini_fs("clip.mp4", 2 * MIB, "photo.jpg")
    provider = GeminiProvider(
        settings, client=client, process_runner=FakeFfmpegRunner(filesystem), filesystem=filesystem
    )
    assets = [_asset("vid-1", "clip.mp4"), _asset("pho-1", "photo.jpg", "photo")]
    # WHEN
    _run_gemini(provider, assets, "seamless_flow")
    # THEN
    prompt = _gemini_prompt(_single_generate_call(client))
    lowered = prompt.lower()
    assert "seamless_flow" in prompt
    assert re.search(r"continuous\s+motion", lowered), "prompt lacks continuous motion instruction"
    assert re.search(r"colou?r\s+continuity", lowered), "prompt lacks color continuity instruction"


# ------------------------------------------------------------------- gemini proxy


def test_gemini_provider_transcodes_1080p_video_to_480p_tmp_proxy_and_deletes_it() -> None:
    # GIVEN
    settings = _settings(gemini_api_key="gk")
    client, uploaded_paths, _, _ = _gemini_client()
    filesystem = _gemini_fs("big.mp4", 2 * MIB)
    runner = FakeFfmpegRunner(filesystem)
    provider = GeminiProvider(settings, client=client, process_runner=runner, filesystem=filesystem)
    assets = [_asset("vid-1", "big.mp4", width=1920, height=1080)]
    # WHEN
    _run_gemini(provider, assets)
    # THEN
    _assert_proxy_flow(runner, filesystem, uploaded_paths, (DATA_DIR / "assets" / "big.mp4").resolve())


def test_gemini_provider_transcodes_small_resolution_video_over_10_mib_to_proxy() -> None:
    # GIVEN a 640x480 video whose actual size is 10 MiB + 1 byte
    settings = _settings(gemini_api_key="gk")
    client, uploaded_paths, _, _ = _gemini_client()
    filesystem = _gemini_fs("heavy.mp4", 10 * MIB + 1)
    runner = FakeFfmpegRunner(filesystem)
    provider = GeminiProvider(settings, client=client, process_runner=runner, filesystem=filesystem)
    assets = [_asset("vid-1", "heavy.mp4", width=640, height=480)]
    # WHEN
    _run_gemini(provider, assets)
    # THEN
    _assert_proxy_flow(runner, filesystem, uploaded_paths, (DATA_DIR / "assets" / "heavy.mp4").resolve())


def test_gemini_provider_cleans_up_and_raises_actionable_runtime_error_on_429() -> None:
    # GIVEN
    settings = _settings(gemini_api_key="gk")
    error = genai_errors.ClientError(
        429,
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Resource has been exhausted (e.g. check quota)."}},
    )
    client, uploaded_paths, uploaded_names, deleted_names = _gemini_client(error=error)
    filesystem = _gemini_fs("big.mp4", 2 * MIB)
    runner = FakeFfmpegRunner(filesystem)
    provider = GeminiProvider(settings, client=client, process_runner=runner, filesystem=filesystem)
    assets = [_asset("vid-1", "big.mp4", width=1920, height=1080)]
    # WHEN
    with pytest.raises(RuntimeError) as exc_info:
        _run_gemini(provider, assets)
    # THEN
    message = str(exc_info.value)
    assert "Gemini" in message
    assert "429 RESOURCE_EXHAUSTED" in message
    assert uploaded_names, "no media was uploaded before the failure"
    assert sorted(deleted_names) == sorted(uploaded_names)
    _assert_proxy_flow(runner, filesystem, uploaded_paths, (DATA_DIR / "assets" / "big.mp4").resolve())


# ------------------------------------------------------------------------ bedrock


def _converse_response(result: AIAnalysisResult) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": result.model_dump_json()}]}},
        "stopReason": "end_turn",
    }


def _bedrock_fs() -> FakeFilesystem:
    return FakeFilesystem(
        files={
            DATA_DIR / "assets" / "clip20.mp4": b"video-bytes",
            DATA_DIR / "assets" / "photo.jpg": b"photo-bytes",
        }
    )


def _run_bedrock(provider: BedrockProvider, assets: list[MediaAsset]) -> AIAnalysisResult | None:
    return provider.analyze_project_media(
        assets,
        bpm=120.0,
        allowed_beat_lengths=[2, 4, 8, 16],
        ai_context="golden hour smiles",
        result_target="perfect_loop",
    )


def _single_converse_call(client: MagicMock) -> Any:
    assert client.converse.call_count == 1, "converse was not called exactly once"
    return client.converse.call_args


@pytest.mark.parametrize(
    ("model_kwargs", "expected_model_id"),
    [
        ({}, "amazon.nova-lite-v1:0"),
        (
            {"bedrock_model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0"},
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
        ),
    ],
    ids=["default", "configured"],
)
def test_bedrock_provider_passes_default_or_configured_model_id(
    model_kwargs: dict[str, Any], expected_model_id: str
) -> None:
    # GIVEN
    settings = _settings(
        ai_provider="bedrock", aws_access_key_id="test-access-key", aws_secret_access_key="test-secret-key", **model_kwargs
    )
    client = MagicMock()
    client.converse.return_value = _converse_response(_expected_result())
    filesystem = _bedrock_fs()
    provider = BedrockProvider(
        settings, client=client, frame_extractor=FakeFfmpegRunner(filesystem), filesystem=filesystem
    )
    # WHEN
    _run_bedrock(provider, [_asset("pho-1", "photo.jpg", "photo")])
    # THEN
    call = _single_converse_call(client)
    assert call.kwargs.get("modelId") == expected_model_id


def test_bedrock_provider_sends_at_most_12_even_keyframes_with_shared_schema_and_parses() -> None:
    # GIVEN a 20s video + photo, mocked converse returning structured JSON
    settings = _settings(
        ai_provider="bedrock", aws_access_key_id="test-access-key", aws_secret_access_key="test-secret-key"
    )
    expected = _expected_result()
    client = MagicMock()
    client.converse.return_value = _converse_response(expected)
    filesystem = _bedrock_fs()
    extractor = FakeFfmpegRunner(filesystem, durations_by_name={"clip20.mp4": 20.0})
    provider = BedrockProvider(settings, client=client, frame_extractor=extractor, filesystem=filesystem)
    assets = [_asset("vid-1", "clip20.mp4", duration_sec=20.0), _asset("pho-1", "photo.jpg", "photo")]
    # WHEN
    result = _run_bedrock(provider, assets)
    # THEN keyframes extracted from the video, at most 12, evenly spaced
    call = _single_converse_call(client)
    blocks = [b for m in call.kwargs.get("messages", []) for b in m.get("content", [])]
    image_bytes = [b["image"]["source"]["bytes"] for b in blocks if isinstance(b, dict) and "image" in b]
    frame_bytes = [data for data in image_bytes if data.startswith(b"jpeg-frame-") or data.startswith(b"ffmpeg-output-")]
    assert 2 <= len(frame_bytes) <= 12, f"expected 2..12 video keyframes, got {len(frame_bytes)}"
    assert b"photo-bytes" in image_bytes, "photo still not passed as an image block"
    photo_indexes = [
        index
        for index, block in enumerate(blocks)
        if isinstance(block, dict)
        and "image" in block
        and block["image"]["source"]["bytes"] == b"photo-bytes"
    ]
    assert len(photo_indexes) == 1, f"expected one photo still image block, got {photo_indexes}"
    assert photo_indexes[0] > 0
    assert blocks[photo_indexes[0] - 1] == {
        "text": "asset_id=pho-1 (photo still; no video timestamp)"
    }
    assert all(b["image"].get("format") == "jpeg" for b in blocks if isinstance(b, dict) and "image" in b)
    video_cmds = [c for c in extractor.ffmpeg_calls() if _flag(c, "-i") and Path(_flag(c, "-i")).name == "clip20.mp4"]
    seeks = sorted(float(_flag(c, "-ss")) for c in video_cmds if _flag(c, "-ss") is not None)
    keyframe_blocks = [
        (index, block)
        for index, block in enumerate(blocks)
        if isinstance(block, dict)
        and "image" in block
        and (
            block["image"]["source"]["bytes"].startswith(b"jpeg-frame-")
            or block["image"]["source"]["bytes"].startswith(b"ffmpeg-output-")
        )
    ]
    assert len(keyframe_blocks) == len(seeks), (
        f"expected one image block for each extracted keyframe: {len(keyframe_blocks)} != {len(seeks)}"
    )
    for (block_index, _), timestamp in zip(keyframe_blocks, seeks, strict=True):
        assert block_index > 0
        assert blocks[block_index - 1] == {
            "text": f"asset_id=vid-1 timestamp={timestamp:.3f}s"
        }, f"keyframe at {timestamp:.3f}s lacks its matching asset/timestamp label"
    if len(seeks) >= 2:
        gaps = [b - a for a, b in zip(seeks, seeks[1:])]
        assert max(gaps) - min(gaps) < 0.05, f"keyframe timestamps not evenly spaced: {seeks}"
        assert 0.0 <= seeks[0] and seeks[-1] < 20.0
    else:
        assert any("fps=" in part or "select=" in part for c in video_cmds for part in c), (
            f"no evenly spaced keyframe selection in {video_cmds}"
        )
    # THEN the Converse request carries the same AIAnalysisResult JSON schema
    output_config = call.kwargs.get("outputConfig") or {}
    schema = (
        output_config.get("textFormat", {}).get("structure", {}).get("jsonSchema", {}).get("schema")
    )
    assert schema is not None, f"outputConfig lacks textFormat.structure.jsonSchema.schema: {output_config}"
    decoded = json.loads(schema) if isinstance(schema, str) else schema
    assert decoded == AIAnalysisResult.model_json_schema()
    # THEN response is parsed
    assert isinstance(result, AIAnalysisResult)
    assert result.moments == expected.moments
    assert result.suggested_sequence == expected.suggested_sequence


def test_bedrock_default_filesystem_reads_and_cleans_real_keyframes(tmp_path: Path) -> None:
    # GIVEN a real data directory and an ffmpeg stub that writes real keyframe files
    data_dir = tmp_path / "data"
    assets_dir = data_dir / "assets"
    temp_dir = data_dir / "tmp"
    assets_dir.mkdir(parents=True)
    temp_dir.mkdir()
    (assets_dir / "clip.mp4").write_bytes(b"video-bytes")
    settings = _settings(
        data_dir=data_dir,
        ai_provider="bedrock",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    client = MagicMock()
    client.converse.return_value = _converse_response(_expected_result())
    written_frames: list[Path] = []

    def write_keyframe(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        frame_path = Path(command[-1])
        frame_bytes = f"real-keyframe-{len(written_frames)}".encode()
        frame_path.write_bytes(frame_bytes)
        written_frames.append(frame_path)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    # Deliberately omit the filesystem argument to exercise Bedrock's production default.
    provider = BedrockProvider(settings, client=client, frame_extractor=write_keyframe)

    # WHEN Bedrock analyzes the video
    result = _run_bedrock(provider, [_asset("vid-1", "clip.mp4", duration_sec=20.0)])

    # THEN image blocks contain bytes read from the real keyframe files, which are cleaned up.
    call = _single_converse_call(client)
    blocks = [block for message in call.kwargs["messages"] for block in message["content"]]
    sent_frame_bytes = [
        block["image"]["source"]["bytes"]
        for block in blocks
        if isinstance(block, dict) and "image" in block
    ]
    assert len(written_frames) == 12
    assert sent_frame_bytes == [f"real-keyframe-{index}".encode() for index in range(12)]
    assert all(frame.parent == temp_dir for frame in written_frames)
    assert all(not frame.exists() for frame in written_frames), "Bedrock left temporary keyframes behind"
    assert isinstance(result, AIAnalysisResult)


def test_bedrock_provider_sends_live_photo_still_and_labeled_motion_frames() -> None:
    # GIVEN a Live Photo with a still image and an extracted motion component
    settings = _settings(
        ai_provider="bedrock", aws_access_key_id="test-access-key", aws_secret_access_key="test-secret-key"
    )
    client = MagicMock()
    client.converse.return_value = _converse_response(_expected_result())
    filesystem = FakeFilesystem(
        files={
            DATA_DIR / "assets" / "live-motion.mov": b"motion-bytes",
            DATA_DIR / "assets" / "live-still.jpg": b"still-bytes",
        }
    )
    extractor = FakeFfmpegRunner(filesystem, durations_by_name={"live-motion.mov": 8.0})
    provider = BedrockProvider(settings, client=client, frame_extractor=extractor, filesystem=filesystem)
    live_photo = _asset("live-1", "live-motion.mov", "live_photo", duration_sec=8.0).model_copy(
        update={"still_path": "assets/live-still.jpg"}
    )

    # WHEN Bedrock analyzes the Live Photo
    _run_bedrock(provider, [live_photo])

    # THEN the still and every extracted motion frame are image blocks with labels
    call = _single_converse_call(client)
    blocks = [block for message in call.kwargs.get("messages", []) for block in message.get("content", [])]
    still_blocks = [
        (index, block)
        for index, block in enumerate(blocks)
        if isinstance(block, dict)
        and block.get("image", {}).get("source", {}).get("bytes") == b"still-bytes"
    ]
    assert len(still_blocks) == 1, f"expected one Live Photo still image block, got {still_blocks}"
    still_index, still_block = still_blocks[0]
    assert still_block["image"]["format"] == "jpeg"
    assert blocks[still_index - 1] == {
        "text": "asset_id=live-1 (photo still; no video timestamp)"
    }

    frame_commands = [
        command
        for command in extractor.ffmpeg_calls()
        if _flag(command, "-i") and Path(_flag(command, "-i")).name == "live-motion.mov"
    ]
    assert frame_commands, "Live Photo motion component was not extracted"
    frame_blocks = [
        (index, block)
        for index, block in enumerate(blocks)
        if isinstance(block, dict)
        and block.get("image", {}).get("source", {}).get("bytes", b"").startswith(
            (b"jpeg-frame-", b"ffmpeg-output-")
        )
    ]
    assert len(frame_blocks) == len(frame_commands), (
        f"expected an image block for each extracted motion frame: {len(frame_blocks)} != {len(frame_commands)}"
    )
    for (frame_index, frame_block), command in zip(frame_blocks, frame_commands, strict=True):
        timestamp = float(_flag(command, "-ss"))
        assert frame_block["image"]["format"] == "jpeg"
        assert blocks[frame_index - 1] == {
            "text": f"asset_id=live-1 timestamp={timestamp:.3f}s"
        }, f"motion frame at {timestamp:.3f}s lacks its Live Photo asset/timestamp label"


def test_bedrock_provider_wraps_throttling_client_error_in_actionable_runtime_error() -> None:
    # GIVEN
    settings = _settings(
        ai_provider="bedrock", aws_access_key_id="test-access-key", aws_secret_access_key="test-secret-key"
    )
    client = MagicMock()
    client.converse.side_effect = BotocoreClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "Converse"
    )
    filesystem = _bedrock_fs()
    provider = BedrockProvider(
        settings, client=client, frame_extractor=FakeFfmpegRunner(filesystem), filesystem=filesystem
    )
    # WHEN
    with pytest.raises(RuntimeError) as exc_info:
        _run_bedrock(provider, [_asset("pho-1", "photo.jpg", "photo")])
    # THEN
    message = str(exc_info.value)
    assert "Bedrock" in message
    assert "ThrottlingException" in message


# ---------------------------------------------------------------------- heuristic


def _showinfo(frames: list[tuple[float, int, float]]) -> str:
    lines = []
    for number, (pts_time, mean_y, stdev_y) in enumerate(frames):
        lines.append(
            f"[Parsed_showinfo_1 @ 0x7f8b1c004a00] n:{number:4d} pts:{int(pts_time * 15360):8d} "
            f"pts_time:{pts_time:g} duration:512 duration_time:0.0333333 fmt:yuv420p cl:left "
            f"sar:1/1 s:1280x720 i:P iskey:0 type:P checksum:DA7B6DD5 plane_checksum:[DA7B6DD5] "
            f"mean:[{mean_y} 128 128] stdev:[{stdev_y:.1f} 10.0 10.0]"
        )
    return "\n".join(lines) + "\n"


def _showinfo_with_activity(frames: list[tuple[float, int, float, float]]) -> str:
    lines = []
    for pts_time, mean_y, stdev_y, activity in frames:
        lines.extend(_showinfo([(pts_time, mean_y, stdev_y)]).splitlines())
        lines.append(f"[Parsed_metadata_2 @ 0x7f8b1c004a00] lavfi.signalstats.YDIF={activity}")
    return "\n".join(lines) + "\n"


def _showinfo_with_signalstats(
    frames: list[tuple[float, int, float, float, float]],
) -> str:
    lines = []
    for pts_time, mean_y, stdev_y, signal_luminance, activity in frames:
        lines.extend(_showinfo([(pts_time, mean_y, stdev_y)]).splitlines())
        lines.append(
            f"[Parsed_metadata_2 @ 0x7f8b1c004a00] "
            f"lavfi.signalstats.YAVG={signal_luminance:g}"
        )
        lines.append(
            f"[Parsed_metadata_2 @ 0x7f8b1c004a00] lavfi.signalstats.YDIF={activity:g}"
        )
    return "\n".join(lines) + "\n"


def _heuristic(stderr_by_name: dict[str, str]) -> tuple[HeuristicFallbackProvider, FakeFfmpegRunner]:
    filesystem = FakeFilesystem(files={Path("assets") / name: b"x" for name in stderr_by_name})
    runner = FakeFfmpegRunner(filesystem, stderr_by_name=stderr_by_name)
    return HeuristicFallbackProvider(process_runner=runner, filesystem=filesystem), runner


def _resolve_sequence_asset(entry: Any, moments: list[AIMoment]) -> str:
    if isinstance(entry, int):
        assert 0 <= entry < len(moments), f"sequence index {entry} out of range"
        return moments[entry].asset_id
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        if "asset_id" in entry:
            return str(entry["asset_id"])
        for key in ("moment_index", "index"):
            if key in entry:
                return moments[int(entry[key])].asset_id
    raise AssertionError(f"unrecognized suggested_sequence entry {entry!r}")


def test_heuristic_high_energy_uses_only_2_or_4_beat_lengths() -> None:
    # GIVEN videos whose luminance, contrast, and activity make the middle
    # scene-change candidate uniquely highest-energy, plus one photo
    provider, _ = _heuristic(
        {
            "run.mp4": _showinfo_with_activity(
                [(0.0, 10, 5.0, 1.0), (2.1, 20, 10.0, 10.0), (5.3, 100, 30.0, 90.0), (8.0, 110, 20.0, 20.0)]
            ),
            "jump.mp4": _showinfo_with_activity(
                [(0.0, 30, 4.0, 1.0), (3.4, 40, 8.0, 5.0), (6.9, 130, 25.0, 80.0), (9.2, 150, 18.0, 15.0)]
            ),
        }
    )
    assets = [
        _asset("vid-a", "run.mp4"),
        _asset("vid-b", "jump.mp4"),
        _asset("pho-c", "still.jpg", "photo"),
    ]
    # WHEN
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4, 8, 16], ai_context="", result_target="high_energy"
    )
    # THEN
    assert isinstance(result, AIAnalysisResult)
    assert result.moments, "no moments returned"
    assert all(m.beat_length in (2, 4) for m in result.moments), [m.beat_length for m in result.moments]
    assert {m.asset_id: m.start_sec for m in result.moments} == {
        "vid-a": 5.3,
        "vid-b": 6.9,
        "pho-c": 0.0,
    }


def test_heuristic_high_energy_zero_features_selects_first_candidate() -> None:
    # GIVEN multiple frames with no luminance change, contrast, or activity
    provider, _ = _heuristic(
        {
            "still.mp4": _showinfo_with_activity(
                [(0.0, 0, 0.0, 0.0), (1.5, 0, 0.0, 0.0), (3.0, 0, 0.0, 0.0)]
            ),
        }
    )
    assets = [_asset("vid-zero", "still.mp4")]
    # WHEN analyzed through the public provider method
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4], ai_context="", result_target="high_energy"
    )
    # THEN all three zero-denominator fallbacks yield a valid, deterministic result
    assert isinstance(result, AIAnalysisResult)
    assert len(result.moments) == 1
    assert result.moments[0].asset_id == "vid-zero"
    assert result.moments[0].start_sec == 1.5


def test_heuristic_does_not_clamp_moment_for_zero_duration_asset() -> None:
    # GIVEN a video whose probed duration is zero and no parsed scene frames
    provider, _ = _heuristic({"zero-duration.mp4": ""})
    asset = _asset("vid-zero-duration", "zero-duration.mp4", duration_sec=0.0)
    # WHEN analyzed with a two-beat moment at 120 BPM
    result = provider.analyze_project_media(
        [asset], bpm=120.0, allowed_beat_lengths=[2], ai_context="", result_target="high_energy"
    )
    # THEN the falsy duration skips clamping, preserving the computed 1-second end
    assert isinstance(result, AIAnalysisResult)
    assert result.moments[0].asset_id == "vid-zero-duration"
    assert result.moments[0].beat_length == 2
    assert result.moments[0].end_sec == 1.0
    assert result.moments[0].end_sec > asset.duration_sec


def test_heuristic_high_energy_retains_single_frame_candidate() -> None:
    # GIVEN a video with one parsed scene-change frame
    provider, _ = _heuristic({"single.mp4": _showinfo([(3.25, 80, 20.0)])})
    assets = [_asset("vid-single", "single.mp4")]
    # WHEN high-energy analysis selects from its candidates
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4], ai_context="", result_target="high_energy"
    )
    # THEN the sole candidate is retained and selected
    assert isinstance(result, AIAnalysisResult)
    assert len(result.moments) == 1
    assert result.moments[0].asset_id == "vid-single"
    assert result.moments[0].start_sec == 3.25


@pytest.mark.parametrize(
    ("signal_luminance", "activity", "expected_start"),
    [
        # Showinfo's mean is flat; the YAVG metadata change identifies 2.0s.
        ([20, 20, 180, 180], [10, 10, 10, 10], 2.0),
        # YAVG and showinfo's mean are flat; YDIF metadata identifies 2.0s.
        ([20, 20, 20, 20], [10, 10, 100, 10], 2.0),
    ],
)
def test_heuristic_high_energy_uses_signalstats_luminance_and_activity(
    signal_luminance: list[int], activity: list[int], expected_start: float
) -> None:
    # GIVEN signalstats metadata where each case makes one parsed feature
    # uniquely select the 2.0s scene over the other candidates.
    frames = [
        (float(index), 50, 10.0, yavg, ydif)
        for index, (yavg, ydif) in enumerate(zip(signal_luminance, activity))
    ]
    provider, _ = _heuristic({"signalstats.mp4": _showinfo_with_signalstats(frames)})
    assets = [_asset("vid-signalstats", "signalstats.mp4")]
    # WHEN high-energy selection analyzes the metadata-backed frames
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4], ai_context="", result_target="high_energy"
    )
    # THEN selection follows YAVG luminance changes and YDIF activity, not
    # showinfo's intentionally flat mean/contrast values.
    assert isinstance(result, AIAnalysisResult)
    assert result.moments[0].start_sec == expected_start


def test_heuristic_seamless_flow_suggests_crossfade_effect() -> None:
    # GIVEN two videos with canned scene changes
    provider, _ = _heuristic(
        {
            "run.mp4": _showinfo([(0.0, 90, 40.0), (2.1, 140, 55.0), (5.3, 110, 48.0)]),
            "jump.mp4": _showinfo([(0.0, 70, 35.0), (3.4, 160, 60.0), (6.9, 120, 50.0)]),
        }
    )
    assets = [_asset("vid-a", "run.mp4"), _asset("vid-b", "jump.mp4")]
    # WHEN
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4, 8, 16], ai_context="", result_target="seamless_flow"
    )
    # THEN
    assert isinstance(result, AIAnalysisResult)
    assert result.moments, "no moments returned"
    assert all(m.suggested_effect == "crossfade" for m in result.moments)


def test_heuristic_perfect_loop_places_most_similar_clips_first_and_last() -> None:
    # GIVEN three clips; clip 0 and clip 2 share near-identical luminance, clip 1 differs strongly
    provider, _ = _heuristic(
        {
            "dusk_a.mp4": _showinfo([(0.0, 60, 20.0), (3.0, 62, 21.0), (6.0, 61, 20.5)]),
            "noon.mp4": _showinfo([(0.0, 210, 70.0), (3.0, 215, 72.0), (6.0, 205, 68.0)]),
            "dusk_b.mp4": _showinfo([(0.0, 61, 20.2), (3.0, 63, 21.2), (6.0, 60, 20.4)]),
        }
    )
    assets = [_asset("clip-1", "noon.mp4"), _asset("clip-0", "dusk_a.mp4"), _asset("clip-2", "dusk_b.mp4")]
    # WHEN
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4, 8, 16], ai_context="", result_target="perfect_loop"
    )
    # THEN
    assert isinstance(result, AIAnalysisResult)
    assert len(result.suggested_sequence) >= 3, f"sequence too short: {result.suggested_sequence}"
    first = _resolve_sequence_asset(result.suggested_sequence[0], result.moments)
    last = _resolve_sequence_asset(result.suggested_sequence[-1], result.moments)
    assert {first, last} == {"clip-0", "clip-2"}
    assert result.suggested_sequence[1:-1] == ["clip-1"]


def test_heuristic_high_energy_handles_video_with_no_parsed_frames() -> None:
    # GIVEN a video whose ffmpeg output contains no parsed scene frames
    provider, runner = _heuristic({"empty.mp4": ""})
    assets = [_asset("vid-empty", "empty.mp4")]
    # WHEN high-energy analysis receives the empty frame list
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4], ai_context="", result_target="high_energy"
    )
    # THEN it returns the default-timed moment rather than failing to select a frame
    assert isinstance(result, AIAnalysisResult)
    assert len(runner.calls) == 1
    assert len(result.moments) == 1
    assert result.moments[0].asset_id == "vid-empty"
    assert result.moments[0].start_sec == 0.0
    assert result.moments[0].end_sec == 1.0
    assert result.suggested_sequence == ["vid-empty"]


def test_heuristic_perfect_loop_preserves_order_with_fewer_than_two_video_signatures() -> None:
    # GIVEN two videos but only one has parsed frames to produce a visual signature
    provider, _ = _heuristic(
        {
            "parsed.mp4": _showinfo([(0.0, 60, 20.0), (3.0, 62, 21.0)]),
            "empty.mp4": "",
        }
    )
    assets = [_asset("vid-parsed", "parsed.mp4"), _asset("vid-empty", "empty.mp4")]
    # WHEN perfect-loop analysis has fewer than two signatures to compare
    result = provider.analyze_project_media(
        assets, bpm=120.0, allowed_beat_lengths=[2, 4], ai_context="", result_target="perfect_loop"
    )
    # THEN it leaves the original sequence unchanged
    assert isinstance(result, AIAnalysisResult)
    assert len(result.moments) == 2
    assert result.suggested_sequence == ["vid-parsed", "vid-empty"]
