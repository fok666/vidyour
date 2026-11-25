"""Application settings and provider capability declarations."""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_DIR_MODE = 0o700
DATA_FILE_MODE = 0o600


def ensure_private_directory(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=DATA_DIR_MODE)
    os.chmod(directory, DATA_DIR_MODE)
    return directory


@contextmanager
def open_private_file(path: str | Path) -> Iterator[Any]:
    target = Path(path)
    ensure_private_directory(target.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, DATA_FILE_MODE)
    try:
        os.fchmod(descriptor, DATA_FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def copy_private_file(source: str | Path, destination: str | Path) -> None:
    with Path(source).open("rb") as source_file, open_private_file(destination) as target:
        while chunk := source_file.read(1024 * 1024):
            target.write(chunk)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Fail closed: when DATA_DIR is unset, use the dedicated repository-level
    # ``data`` directory (the spec's ``../data`` relative to ``backend/``),
    # anchored to this file so it never resolves to the process cwd or to any
    # directory holding a ``.env`` file.
    data_dir: Path = DEFAULT_DATA_DIR
    cors_origins: str = ""
    ai_provider: str = "auto"
    gemini_api_key: str = ""
    gemini_model: str = ""
    aws_region: str = "us-east-1"
    bedrock_model_id: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    jamendo_client_id: str = ""
    freesound_api_key: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""
    s3_presigned_url_ttl: int = 86400

    def capabilities(self) -> dict[str, Any]:
        google_photos = bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_redirect_uri
        )
        gemini = bool(self.gemini_api_key)
        bedrock = bool(
            self.aws_region and self.aws_access_key_id and self.aws_secret_access_key
        )
        jamendo = bool(self.jamendo_client_id)
        freesound = bool(self.freesound_api_key)
        s3 = bool(self.s3_bucket)

        requested_provider = self.ai_provider.strip().lower()
        active_ai_provider = "local"
        if requested_provider == "auto":
            if gemini:
                active_ai_provider = "gemini"
            elif bedrock:
                active_ai_provider = "bedrock"
        elif requested_provider == "gemini" and gemini:
            active_ai_provider = "gemini"
        elif requested_provider == "bedrock" and bedrock:
            active_ai_provider = "bedrock"

        return {
            "google_photos": google_photos,
            "gemini": gemini,
            "bedrock": bedrock,
            "jamendo": jamendo,
            "freesound": freesound,
            "s3": s3,
            "active_ai_provider": active_ai_provider,
            "storage_backend": "s3" if s3 else "local",
        }

    def ensure_data_dirs(self, mkdir: Any = None) -> None:
        ensure_private_directory(self.data_dir)
        for name in ("assets", "clips", "thumbs", "renders", "tmp"):
            directory = self.data_dir / name
            if mkdir is None:
                ensure_private_directory(directory)
            else:
                mkdir(directory, parents=True, exist_ok=True, mode=DATA_DIR_MODE)
                os.chmod(directory, DATA_DIR_MODE)


settings = Settings()
