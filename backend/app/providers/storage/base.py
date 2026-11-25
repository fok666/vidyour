"""Storage provider contract."""

from pathlib import Path
from typing import Protocol

from app.models import StoredRender


def _safe_segment(value: str, label: str) -> str:
    """Keep identifiers as a single safe path/key component."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"Invalid {label}")
    return value


class StorageProvider(Protocol):
    def store_render(
        self,
        source_path: str | Path,
        project_id: str,
        job_id: str,
        project_name: str,
        duration_sec: float,
    ) -> StoredRender | None: ...
