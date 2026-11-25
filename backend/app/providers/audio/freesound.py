"""Freesound v2 audio catalog provider interface."""

import math
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from app.models import AudioCatalogTrack

_API_URL = "https://freesound.org/apiv2"
_DOWNLOAD_HOSTS = frozenset(
    {"freesound.org", "cdn.freesound.org", "downloads.freesound.org"}
)
_MAX_DOWNLOAD_REDIRECTS = 5


class FreesoundAudioProvider:
    name = "freesound"

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
        api_key = getattr(self.settings, "freesound_api_key", "")
        if not api_key:
            return []
        client = self.client or httpx.Client(timeout=15)
        response = client.get(
            f"{_API_URL}/search/text/",
            params={
                "query": q.strip(),
                "filter": 'license:"Creative Commons 0" OR license:"Attribution"',
                "fields": "id,name,username,duration,license,previews,download,analysis",
                "page_size": 50,
            },
            headers={"Authorization": f"Token {api_key}"},
        )
        response.raise_for_status()
        tracks: list[AudioCatalogTrack] = []
        for item in response.json().get("results", []):
            license_url = str(item.get("license") or "")
            if not _is_allowed_license(license_url):
                continue
            bpm = _optional_float(_extract_bpm(item))
            if bpm_min is not None and (bpm is None or bpm < bpm_min):
                continue
            if bpm_max is not None and (bpm is None or bpm > bpm_max):
                continue
            previews = item.get("previews") or {}
            tracks.append(
                AudioCatalogTrack(
                    id=str(item.get("id", "")),
                    provider=self.name,
                    title=str(item.get("name") or ""),
                    artist=str(item.get("username") or ""),
                    bpm=bpm,
                    duration_sec=float(item.get("duration") or 0),
                    preview_url=str(
                        previews.get("preview-hq-mp3")
                        or previews.get("preview-lq-mp3")
                        or ""
                    ),
                    download_url=str(item.get("download") or ""),
                    license=license_url,
                    attribution=str(item.get("username") or ""),
                )
            )
        return tracks

    def download_track(self, track_id: str, output_dir: str | Path) -> Path:
        api_key = getattr(self.settings, "freesound_api_key", "")
        if not api_key:
            return Path()
        client = self.client or httpx.Client(timeout=30, follow_redirects=False)
        headers = {"Authorization": f"Token {api_key}"}
        response = client.get(
            f"{_API_URL}/sounds/{quote(str(track_id), safe='')}/",
            params={"fields": "id,download"},
            headers=headers,
            follow_redirects=False,
        )
        response.raise_for_status()
        download_url = response.json().get("download")
        if not download_url:
            return Path()
        # Never forward the API credential to the arbitrary media URL.
        audio_response = _get_download_response(client, download_url)
        output = Path(output_dir) / f"{track_id}.mp3"
        _write_file(self.file_writer, output, audio_response.content)
        return output


def _validate_download_url(value: str) -> str:
    if not isinstance(value, str):
        raise httpx.InvalidURL("Invalid Freesound download URL")
    url = httpx.URL(value)
    if (
        url.scheme != "https"
        or url.host not in _DOWNLOAD_HOSTS
        or url.username
        or url.password
        or url.port not in (None, 443)
    ):
        raise httpx.InvalidURL("Invalid Freesound download URL")
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
        "Too many Freesound download redirects", request=response.request
    )


def _is_allowed_license(value: str) -> bool:
    normalized = value.lower()
    return (
        "publicdomain/zero" in normalized
        or "creativecommons.org/licenses/by/" in normalized
        or "creative commons 0" in normalized
        or "cc-by" in normalized
    )


def _extract_bpm(item: dict[str, Any]) -> Any:
    bpm = item.get("bpm")
    if bpm is not None:
        return bpm
    analysis = item.get("analysis")
    if isinstance(analysis, dict):
        rhythm = analysis.get("rhythm")
        if isinstance(rhythm, dict):
            return rhythm.get("bpm")
    return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        bpm = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return bpm if math.isfinite(bpm) else None


def _write_file(writer: Any, path: Path, content: bytes) -> None:
    if writer is not None:
        writer(path, content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
