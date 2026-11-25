"""Jamendo v3 audio catalog provider interface."""

from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from app.models import AudioCatalogTrack

_API_URL = "https://api.jamendo.com/v3.0"
_DOWNLOAD_HOSTS = frozenset(
    {
        "prod-1.storage.jamendo.com",
        "prod-2.storage.jamendo.com",
        "prod-3.storage.jamendo.com",
        "prod-4.storage.jamendo.com",
        "mp3d.jamendo.com",
    }
)
_MAX_DOWNLOAD_REDIRECTS = 5


class JamendoAudioProvider:
    name = "jamendo"

    def __init__(
        self,
        settings: Any,
        client: Any = None,
        file_writer: Any = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.file_writer = file_writer

    def search(
        self,
        q: str = "",
        bpm_min: float | None = None,
        bpm_max: float | None = None,
    ) -> list[AudioCatalogTrack]:
        client_id = getattr(self.settings, "jamendo_client_id", "")
        if not client_id:
            return []
        client = self.client or httpx.Client(timeout=15)
        params = {
            "client_id": client_id,
            "format": "json",
            "limit": 50,
            "audioformat": "mp31",
            "include": "musicinfo",
            "license": "cc",
            "license_cc": "1",
            "license_cc_nc": "0",
            "license_cc_nd": "0",
        }
        if q.strip():
            params["search"] = q.strip()
        response = client.get(f"{_API_URL}/tracks", params=params)
        response.raise_for_status()
        tracks: list[AudioCatalogTrack] = []
        for item in response.json().get("results", []):
            license_url = str(item.get("license_ccurl") or "")
            if not license_url:
                continue
            bpm = _optional_float(item.get("bpm"))
            if bpm_min is not None and (bpm is None or bpm < bpm_min):
                continue
            if bpm_max is not None and (bpm is None or bpm > bpm_max):
                continue
            tracks.append(
                AudioCatalogTrack(
                    id=str(item.get("id", "")),
                    provider=self.name,
                    title=str(item.get("name") or ""),
                    artist=str(item.get("artist_name") or ""),
                    bpm=bpm,
                    duration_sec=float(item.get("duration") or 0),
                    preview_url=str(item.get("audio") or ""),
                    download_url=str(item.get("audiodownload") or ""),
                    license=license_url,
                    attribution=str(item.get("artist_name") or ""),
                )
            )
        return tracks

    def download_track(self, track_id: str, output_dir: str | Path) -> Path:
        client_id = getattr(self.settings, "jamendo_client_id", "")
        if not client_id:
            return Path()
        client = self.client or httpx.Client(timeout=30, follow_redirects=False)
        response = client.get(
            f"{_API_URL}/tracks",
            params={
                "client_id": client_id,
                "format": "json",
                "id": track_id,
                "audioformat": "mp31",
            },
            follow_redirects=False,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if not results or not results[0].get("audiodownload"):
            return Path()
        audio_response = _get_download_response(client, results[0]["audiodownload"])
        output = Path(output_dir) / f"{track_id}.mp3"
        _write_file(self.file_writer, output, audio_response.content)
        return output


def _validate_download_url(value: str) -> str:
    if not isinstance(value, str):
        raise httpx.InvalidURL("Invalid Jamendo download URL")
    url = httpx.URL(value)
    if (
        url.scheme != "https"
        or url.host not in _DOWNLOAD_HOSTS
        or url.username
        or url.password
        or url.port not in (None, 443)
    ):
        raise httpx.InvalidURL("Invalid Jamendo download URL")
    return str(url)


def _get_download_response(client: Any, download_url: str) -> httpx.Response:
    current_url = _validate_download_url(download_url)
    for _ in range(_MAX_DOWNLOAD_REDIRECTS + 1):
        response = client.get(current_url, follow_redirects=False)
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                response.raise_for_status()
            current_url = _validate_download_url(urljoin(current_url, location))
            continue
        response.raise_for_status()
        return response
    raise httpx.TooManyRedirects(
        "Too many Jamendo download redirects", request=response.request
    )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_file(writer: Any, path: Path, content: bytes) -> None:
    if writer is not None:
        writer(path, content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
