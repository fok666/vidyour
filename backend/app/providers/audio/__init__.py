"""Audio catalog provider interfaces and implementations."""

from app.providers.audio.base import (
    AudioCatalogProvider,
    get_audio_providers,
    search_audio_catalog,
)
from app.providers.audio.builtin import (
    BUILTIN_TRACKS,
    BuiltinDemoAudioProvider,
    SectionSpan,
    SynthesizedTrack,
    synthesize_track,
)
from app.providers.audio.freesound import FreesoundAudioProvider
from app.providers.audio.jamendo import JamendoAudioProvider

__all__ = [
    "AudioCatalogProvider",
    "BUILTIN_TRACKS",
    "BuiltinDemoAudioProvider",
    "FreesoundAudioProvider",
    "get_audio_providers",
    "JamendoAudioProvider",
    "SectionSpan",
    "SynthesizedTrack",
    "search_audio_catalog",
    "synthesize_track",
]
