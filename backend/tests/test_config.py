import os
from pathlib import Path
from stat import S_IMODE

import pytest

import app.config as config
from app.config import Settings, open_private_file


SETTING_ENV_VARS = (
    "DATA_DIR",
    "CORS_ORIGINS",
    "AI_PROVIDER",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "AWS_REGION",
    "BEDROCK_MODEL_ID",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
    "JAMENDO_CLIENT_ID",
    "FREESOUND_API_KEY",
    "S3_BUCKET",
    "S3_REGION",
    "S3_ENDPOINT_URL",
    "S3_PRESIGNED_URL_TTL",
)


def clear_settings_env(monkeypatch):
    for name in SETTING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_settings_provider_and_s3_defaults(monkeypatch):
    clear_settings_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert (
        settings.ai_provider,
        settings.aws_region,
        settings.s3_region,
        settings.s3_endpoint_url,
        settings.s3_presigned_url_ttl,
    ) == ("auto", "us-east-1", "us-east-1", "", 86400)


def test_capabilities_use_local_defaults_when_external_providers_are_unconfigured(
    monkeypatch,
):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("S3_BUCKET", "")
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "")
    monkeypatch.setenv("FREESOUND_API_KEY", "")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("AI_PROVIDER", "auto")

    settings = Settings(_env_file=None)

    assert settings.capabilities() == {
        "google_photos": False,
        "gemini": False,
        "bedrock": False,
        "jamendo": False,
        "freesound": False,
        "s3": False,
        "active_ai_provider": "local",
        "storage_backend": "local",
    }


def test_capabilities_report_configured_provider_integrations(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setenv("S3_BUCKET", "vidyour-renders")
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "jc")
    monkeypatch.setenv("FREESOUND_API_KEY", "fk")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsec")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "http://localhost:8000/api/gallery/google/callback",
    )

    settings = Settings(_env_file=None)

    expected = {
        "google_photos": True,
        "gemini": True,
        "jamendo": True,
        "freesound": True,
        "s3": True,
        "active_ai_provider": "gemini",
        "storage_backend": "s3",
    }
    capabilities = settings.capabilities()

    assert {key: capabilities.get(key) for key in expected} == expected


def test_capabilities_select_bedrock_when_requested_with_credentials(monkeypatch):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("AI_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")

    settings = Settings(_env_file=None)

    capabilities = settings.capabilities()

    assert (capabilities.get("bedrock"), capabilities.get("active_ai_provider")) == (
        True,
        "bedrock",
    )


def test_capabilities_auto_select_bedrock_without_gemini_when_credentials_present(
    monkeypatch,
):
    clear_settings_env(monkeypatch)
    monkeypatch.setenv("AI_PROVIDER", "auto")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")

    settings = Settings(_env_file=None)

    capabilities = settings.capabilities()

    assert (capabilities.get("bedrock"), capabilities.get("active_ai_provider")) == (
        True,
        "bedrock",
    )


def test_ensure_data_dirs_creates_private_root_and_required_subdirectories(
    monkeypatch, tmp_path
):
    clear_settings_env(monkeypatch)
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, _env_file=None)

    settings.ensure_data_dirs()

    directories = [
        data_dir,
        data_dir / "assets",
        data_dir / "clips",
        data_dir / "thumbs",
        data_dir / "renders",
        data_dir / "tmp",
    ]
    assert all(directory.is_dir() for directory in directories)
    assert all(S_IMODE(directory.stat().st_mode) == 0o700 for directory in directories)


def test_open_private_file_closes_descriptor_when_fchmod_fails(monkeypatch, tmp_path):
    target = tmp_path / "private.bin"
    opened = []
    closed = []
    real_open = os.open
    real_close = os.close

    def recording_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def recording_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    def fail_fchmod(_descriptor, _mode):
        raise PermissionError("cannot set private file permissions")

    monkeypatch.setattr(config.os, "open", recording_open)
    monkeypatch.setattr(config.os, "close", recording_close)
    monkeypatch.setattr(config.os, "fchmod", fail_fchmod)

    with pytest.raises(PermissionError, match="cannot set private file permissions"):
        with open_private_file(target):
            pytest.fail("the file stream must not be yielded when fchmod fails")

    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_open_private_file_works_without_o_nofollow(monkeypatch, tmp_path):
    monkeypatch.delattr(config.os, "O_NOFOLLOW", raising=False)
    real_open = os.open
    opened_flags = []

    def recording_open(path, flags, mode):
        opened_flags.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(config.os, "open", recording_open)
    target = tmp_path / "private.bin"

    with open_private_file(target) as stream:
        stream.write(b"private data")

    assert opened_flags == [os.O_WRONLY | os.O_CREAT | os.O_TRUNC]
    assert target.read_bytes() == b"private data"


def test_ensure_data_dirs_uses_injected_mkdir_for_subdirectories(
    monkeypatch, tmp_path
):
    clear_settings_env(monkeypatch)
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, _env_file=None)
    calls = []

    def recording_mkdir(directory, **kwargs):
        calls.append((directory, kwargs))
        directory.mkdir(**kwargs)

    settings.ensure_data_dirs(mkdir=recording_mkdir)

    expected_directories = [
        data_dir / name for name in ("assets", "clips", "thumbs", "renders", "tmp")
    ]
    assert [directory for directory, _kwargs in calls] == expected_directories
    assert all(
        kwargs == {"parents": True, "exist_ok": True, "mode": 0o700}
        for _directory, kwargs in calls
    )
    assert all(directory.is_dir() for directory in [data_dir, *expected_directories])
