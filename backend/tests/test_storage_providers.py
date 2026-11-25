from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.models import StoredRender
from app.providers.storage import get_storage_provider
from app.providers.storage.local import LocalStorageProvider
from app.providers.storage.s3 import S3StorageProvider


INVALID_STORAGE_IDENTIFIERS = ("", ".", "..", "/", "\\", "\x00", "\x1f", "\x7f")


def test_storage_provider_factory_selects_s3_for_configured_bucket():
    settings = Settings(
        _env_file=None,
        s3_bucket="vidyour-renders",
        s3_region="eu-west-1",
    )

    with patch("boto3.client"):
        provider = get_storage_provider(settings)

    assert isinstance(provider, S3StorageProvider)


def test_storage_provider_factory_selects_local_for_empty_bucket():
    settings = Settings(_env_file=None, s3_bucket="")

    provider = get_storage_provider(settings)

    assert isinstance(provider, LocalStorageProvider)


def test_s3_provider_uses_default_boto3_client_factory_with_configured_endpoint():
    settings = Settings(
        _env_file=None,
        s3_bucket="vidyour-renders",
        s3_region="eu-west-1",
        s3_endpoint_url="http://localhost:9000",
    )

    client = Mock()
    with patch("boto3.client", return_value=client) as client_factory:
        provider = S3StorageProvider(settings)

    client_factory.assert_called_once_with(
        "s3",
        region_name="eu-west-1",
        endpoint_url="http://localhost:9000",
    )
    assert provider.client is client


def test_s3_provider_uses_custom_client_factory_override():
    settings = Settings(
        _env_file=None,
        s3_bucket="vidyour-renders",
        s3_region="eu-west-1",
        s3_endpoint_url="http://custom-s3:9000",
    )
    client = Mock()
    client_factory = Mock(return_value=client)

    provider = S3StorageProvider(settings, client_factory=client_factory)

    client_factory.assert_called_once_with(
        "s3",
        region_name="eu-west-1",
        endpoint_url="http://custom-s3:9000",
    )
    assert provider.client is client


def test_s3_provider_passes_none_for_unset_endpoint_url():
    settings = Settings(
        _env_file=None,
        s3_bucket="vidyour-renders",
        s3_region="us-east-1",
        s3_endpoint_url="",
    )

    with patch("boto3.client", return_value=Mock()) as client_factory:
        S3StorageProvider(settings)

    client_factory.assert_called_once_with(
        "s3",
        region_name="us-east-1",
        endpoint_url=None,
    )


def test_s3_store_uses_default_presigned_url_ttl_and_returns_url(tmp_path):
    settings = Settings(
        _env_file=None,
        s3_bucket="vidyour-renders",
    )
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"render")
    client = Mock()
    client.generate_presigned_url.return_value = "https://s3.example/render"
    provider = S3StorageProvider(settings, client=client)

    result = provider.store_render(
        source_path,
        project_id="p1",
        job_id="j1",
        project_name="Summer Trip!",
        duration_sec=4.0,
    )

    client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "vidyour-renders", "Key": "renders/p1/j1.mp4"},
        ExpiresIn=86400,
    )
    assert result == StoredRender(
        storage_backend="s3",
        s3_key="renders/p1/j1.mp4",
        download_url="https://s3.example/render",
        duration_sec=4.0,
        size_bytes=6,
    )


def test_s3_store_uploads_safe_attachment_metadata_and_returns_render(monkeypatch):
    settings = Settings(
        _env_file=None,
        s3_bucket="vidyour-renders",
        s3_region="eu-west-1",
        s3_endpoint_url="http://localhost:9000",
        s3_presigned_url_ttl=3600,
    )
    source_path = Path("/virtual/source.mp4")
    client = Mock()
    client.generate_presigned_url.return_value = "https://s3.example/render"
    provider = S3StorageProvider(settings, client=client)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *args, **kwargs: SimpleNamespace(st_size=2048),
    )

    result = provider.store_render(
        source_path,
        project_id="p1",
        job_id="j1",
        project_name="Summer Trip!",
        duration_sec=4.0,
    )

    client.upload_file.assert_called_once_with(
        str(source_path),
        "vidyour-renders",
        "renders/p1/j1.mp4",
        ExtraArgs={
            "ContentType": "video/mp4",
            "ContentDisposition": 'attachment; filename="Summer_Trip.mp4"',
        },
    )
    client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "vidyour-renders", "Key": "renders/p1/j1.mp4"},
        ExpiresIn=3600,
    )
    assert result == StoredRender(
        storage_backend="s3",
        s3_key="renders/p1/j1.mp4",
        download_url="https://s3.example/render",
        duration_sec=4.0,
        size_bytes=2048,
    )


def test_s3_store_translates_client_error_to_runtime_error_with_s3_context(
    tmp_path,
):
    settings = Settings(_env_file=None, s3_bucket="vidyour-renders")
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"render")
    client = Mock()
    client.upload_file.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "PutObject",
    )
    provider = S3StorageProvider(settings, client=client)

    with pytest.raises(RuntimeError, match="S3.*AccessDenied"):
        provider.store_render(
            source_path,
            project_id="p1",
            job_id="j1",
            project_name="Demo",
            duration_sec=4.0,
        )


@pytest.mark.parametrize(
    ("base_url", "download_url"),
    (
        ("", "/api/renders/job-123/download"),
        (
            "https://storage.example/",
            "https://storage.example/api/renders/job-123/download",
        ),
    ),
)
def test_local_store_uses_injected_filesystem_and_returns_local_render(
    base_url, download_url
):
    settings = Settings(_env_file=None, data_dir=Path("/virtual/data"))
    source_path = Path("/virtual/source.mp4")
    destination = Path("/virtual/data/renders/job-123.mp4")

    class FakeFilesystem:
        def __init__(self):
            self.files = {source_path: b"r" * 1024}

        def copy_file(self, source, target):
            self.files[target] = self.files[source]

        def stat_file(self, path):
            return SimpleNamespace(st_size=len(self.files[path]))

    filesystem = FakeFilesystem()
    provider = LocalStorageProvider(
        data_dir=settings.data_dir,
        base_url=base_url,
        copy_file=filesystem.copy_file,
        stat_file=filesystem.stat_file,
    )

    result = provider.store_render(
        source_path,
        project_id="p1",
        job_id="job-123",
        project_name="Demo",
        duration_sec=4.0,
    )

    assert filesystem.files.get(destination) == b"r" * 1024
    assert result == StoredRender(
        storage_backend="local",
        s3_key=None,
        download_url=download_url,
        duration_sec=4.0,
        size_bytes=1024,
    )


@pytest.mark.parametrize("identifier_field", ("project_id", "job_id"))
@pytest.mark.parametrize("identifier", INVALID_STORAGE_IDENTIFIERS)
def test_s3_store_rejects_unsafe_identifiers(identifier_field, identifier):
    settings = Settings(_env_file=None, s3_bucket="vidyour-renders")
    client = Mock()
    provider = S3StorageProvider(settings, client=client)
    identifiers = {"project_id": "p1", "job_id": "j1"}
    identifiers[identifier_field] = identifier

    with pytest.raises(ValueError, match="Invalid"):
        provider.store_render(
            "/virtual/source.mp4",
            **identifiers,
            project_name="Demo",
            duration_sec=4.0,
        )

    client.upload_file.assert_not_called()


@pytest.mark.parametrize("identifier", INVALID_STORAGE_IDENTIFIERS)
def test_local_store_rejects_unsafe_job_identifier(tmp_path, identifier):
    provider = LocalStorageProvider(data_dir=tmp_path)

    with pytest.raises(ValueError, match="Invalid"):
        provider.store_render(
            tmp_path / "source.mp4",
            project_id="p1",
            job_id=identifier,
            project_name="Demo",
            duration_sec=4.0,
        )
