"""Storage provider selection interface."""

from typing import Any

from app.providers.storage.local import LocalStorageProvider
from app.providers.storage.s3 import S3StorageProvider


def get_storage_provider(settings: Any) -> Any:
    if getattr(settings, "s3_bucket", ""):
        return S3StorageProvider(settings)
    return LocalStorageProvider(data_dir=settings.data_dir)
