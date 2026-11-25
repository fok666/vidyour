"""S3 render storage interface."""

import re
from pathlib import Path
from typing import Any, Callable

from botocore.exceptions import ClientError

from app.models import StoredRender
from app.providers.storage.base import _safe_segment


def _safe_filename(value: str) -> str:
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return filename or "render"


class S3StorageProvider:
    def __init__(
        self,
        settings: Any,
        client: Any = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory
        if client is not None:
            self.client = client
        else:
            if client_factory is None:
                import boto3

                client_factory = boto3.client
            self.client_factory = client_factory
            self.client = client_factory(
                "s3",
                region_name=settings.s3_region,
                endpoint_url=settings.s3_endpoint_url or None,
            )

    def store_render(
        self,
        source_path: str | Path,
        project_id: str,
        job_id: str,
        project_name: str,
        duration_sec: float,
    ) -> StoredRender | None:
        project_id = _safe_segment(project_id, "project id")
        job_id = _safe_segment(job_id, "job id")
        source = Path(source_path)
        key = f"renders/{project_id}/{job_id}.mp4"
        try:
            self.client.upload_file(
                str(source),
                self.settings.s3_bucket,
                key,
                ExtraArgs={
                    "ContentType": "video/mp4",
                    "ContentDisposition": (
                        f'attachment; filename="{_safe_filename(project_name)}.mp4"'
                    ),
                },
            )
            size_bytes = source.stat().st_size
            download_url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.settings.s3_bucket, "Key": key},
                ExpiresIn=self.settings.s3_presigned_url_ttl,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "Unknown")
            raise RuntimeError(f"S3 operation failed: {code}") from error

        return StoredRender(
            storage_backend="s3",
            s3_key=key,
            download_url=download_url,
            duration_sec=duration_sec,
            size_bytes=size_bytes,
        )
