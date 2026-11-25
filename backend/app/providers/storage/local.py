"""Local filesystem render storage interface."""

from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from app.config import copy_private_file, ensure_private_directory
from app.models import StoredRender
from app.providers.storage.base import _safe_segment


class LocalStorageProvider:
    def __init__(
        self,
        data_dir: str | Path = Path(),
        base_url: str = "",
        copy_file: Callable[..., Any] | None = None,
        stat_file: Callable[..., Any] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.base_url = base_url
        self.copy_file = copy_file
        self.stat_file = stat_file

    def store_render(
        self,
        source_path: str | Path,
        project_id: str,
        job_id: str,
        project_name: str,
        duration_sec: float,
    ) -> StoredRender | None:
        job_id = _safe_segment(job_id, "job id")
        source = Path(source_path)
        destination = Path(self.data_dir) / "renders" / f"{job_id}.mp4"
        stat_file = self.stat_file or Path.stat

        # A supplied copier usually targets a virtual/test filesystem; avoid
        # touching the host filesystem in that case.
        if self.copy_file is None:
            ensure_private_directory(destination.parent)
            copy_private_file(source, destination)
        else:
            self.copy_file(source, destination)
        size_bytes = stat_file(destination).st_size

        download_url = f"/api/renders/{quote(job_id, safe='-_.~')}/download"
        if self.base_url:
            download_url = f"{self.base_url.rstrip('/')}{download_url}"
        return StoredRender(
            storage_backend="local",
            s3_key=None,
            download_url=download_url,
            duration_sec=duration_sec,
            size_bytes=size_bytes,
        )
