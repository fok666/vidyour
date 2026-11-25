"""Shared audio catalog provider contract."""

from pathlib import Path
from typing import Any, Protocol

from app.models import AudioCatalogTrack


class AudioCatalogProvider(Protocol):
    """Search and download tracks from an audio catalog."""

    name: str

    def search(
        self,
        q: str = "",
        bpm_min: float | None = None,
        bpm_max: float | None = None,
    ) -> list[AudioCatalogTrack]: ...

    def download_track(self, track_id: str, output_dir: str | Path) -> Path: ...


def search_audio_catalog(
    provider: str = "all",
    q: str = "",
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    providers: list[AudioCatalogProvider] | None = None,
) -> list[AudioCatalogTrack]:
    """Search selected providers and combine their available results."""
    selected = list(get_audio_providers() if providers is None else providers)
    if provider.strip().lower() not in ("", "all"):
        selected = [item for item in selected if item.name.lower() == provider.strip().lower()]

    tracks: list[AudioCatalogTrack] = []
    for item in selected:
        try:
            tracks.extend(item.search(q=q, bpm_min=bpm_min, bpm_max=bpm_max))
        except Exception:
            # Catalogs are independent: an outage must not hide other sources.
            continue
    return tracks


def get_audio_providers(settings: Any = None) -> list[AudioCatalogProvider]:
    """Build the always-available local catalog and configured remote catalogs."""
    if settings is None:
        from app.config import settings as app_settings

        settings = app_settings

    from app.providers.audio.builtin import BuiltinDemoAudioProvider
    from app.providers.audio.freesound import FreesoundAudioProvider
    from app.providers.audio.jamendo import JamendoAudioProvider

    providers: list[AudioCatalogProvider] = [BuiltinDemoAudioProvider(settings.data_dir)]
    if settings.jamendo_client_id:
        providers.append(JamendoAudioProvider(settings))
    if settings.freesound_api_key:
        providers.append(FreesoundAudioProvider(settings))
    return providers
