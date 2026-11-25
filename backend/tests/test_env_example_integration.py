"""Integration: the checked-in repository-root .env.example matches Spec Section 9
and loads through the real backend Settings."""

from pathlib import Path

import pytest

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

EXPECTED = {
    "DATA_DIR": "../data",
    "CORS_ORIGINS": "http://localhost:5173",
    "AI_PROVIDER": "auto",
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-2.5-flash",
    "AWS_REGION": "us-east-1",
    "BEDROCK_MODEL_ID": "amazon.nova-lite-v1:0",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "GOOGLE_REDIRECT_URI": "http://localhost:8000/api/gallery/google/callback",
    "JAMENDO_CLIENT_ID": "",
    "FREESOUND_API_KEY": "",
    "S3_BUCKET": "",
    "S3_REGION": "us-east-1",
    "S3_ENDPOINT_URL": "",
    "S3_PRESIGNED_URL_TTL": "86400",
}


def parse_env_file(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"not an assignment: {raw!r}"
        key, _, value = line.partition("=")
        key = key.strip()
        assert key not in parsed, f"duplicate key {key}"
        parsed[key] = value.strip()
    return parsed


def test_env_example_contains_exact_section_9_mapping():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert parse_env_file(text) == EXPECTED


def test_env_example_loads_through_real_settings(monkeypatch):
    for key in EXPECTED:
        monkeypatch.delenv(key, raising=False)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=ENV_EXAMPLE, _env_file_encoding="utf-8")

    assert s.data_dir == Path("../data")
    assert s.cors_origins == "http://localhost:5173"
    assert s.ai_provider == "auto"
    assert s.gemini_api_key == ""
    assert s.gemini_model == "gemini-2.5-flash"
    assert s.aws_region == "us-east-1"
    assert s.bedrock_model_id == "amazon.nova-lite-v1:0"
    assert s.google_client_id == ""
    assert s.google_client_secret == ""
    assert s.google_redirect_uri == "http://localhost:8000/api/gallery/google/callback"
    assert s.jamendo_client_id == ""
    assert s.freesound_api_key == ""
    assert s.s3_bucket == ""
    assert s.s3_region == "us-east-1"
    assert s.s3_endpoint_url == ""
    assert s.s3_presigned_url_ttl == 86400

    caps = s.capabilities()
    assert caps["active_ai_provider"] == "local"
    assert caps["storage_backend"] == "local"
    assert caps["gemini"] is False
    assert caps["s3"] is False
    assert caps["google_photos"] is False
