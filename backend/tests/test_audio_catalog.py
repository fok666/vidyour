"""Unit tests for audio catalog providers and built-in synthesis."""

from io import BytesIO
from pathlib import Path
import subprocess
import wave
from urllib.parse import parse_qs

import httpx
import numpy as np
import pytest

from app.config import Settings
from app.models import AudioCatalogTrack
from app.providers.audio import (
    BuiltinDemoAudioProvider,
    FreesoundAudioProvider,
    JamendoAudioProvider,
    get_audio_providers,
    search_audio_catalog,
    synthesize_track,
)


class FakeFileWriter:
    def __init__(self):
        self.files = {}

    def __call__(self, path, content):
        self.files[str(path)] = bytes(content)


def catalog_track_pairs(tracks):
    return [(track.title, track.bpm) for track in tracks]


@pytest.mark.parametrize(
    ("credential", "value", "expected_provider", "unconfigured_provider"),
    [
        (
            "jamendo_client_id",
            "test-client",
            JamendoAudioProvider,
            FreesoundAudioProvider,
        ),
        (
            "freesound_api_key",
            "test-key",
            FreesoundAudioProvider,
            JamendoAudioProvider,
        ),
    ],
)
def test_get_audio_providers_includes_remote_provider_when_credential_is_configured(
    credential, value, expected_provider, unconfigured_provider
):
    settings = Settings(_env_file=None, **{credential: value})

    providers = get_audio_providers(settings)

    assert any(isinstance(provider, expected_provider) for provider in providers)
    assert not any(isinstance(provider, unconfigured_provider) for provider in providers)


@pytest.mark.parametrize(
    ("bpm", "title"),
    [
        (90, "Chill Groove"),
        (110, "Sunset Pulse"),
        (124, "House Drive"),
        (140, "High-Energy Sprint"),
    ],
)
def test_builtin_catalog_search_returns_four_cc0_tracks(bpm, title):
    provider = BuiltinDemoAudioProvider()

    tracks = provider.search()

    assert len(tracks) == 4
    assert all(isinstance(track, AudioCatalogTrack) for track in tracks)
    assert catalog_track_pairs(tracks) == [
        ("Chill Groove", 90.0),
        ("Sunset Pulse", 110.0),
        ("House Drive", 124.0),
        ("High-Energy Sprint", 140.0),
    ]
    assert next(track for track in tracks if track.bpm == bpm).title == title
    assert all(track.license == "CC0" for track in tracks)


def test_builtin_catalog_search_filters_by_bpm_range():
    provider = BuiltinDemoAudioProvider()

    tracks = provider.search(bpm_min=115, bpm_max=130)

    assert [(track.id, track.bpm) for track in tracks] == [("builtin-124", 124.0)]


def test_builtin_catalog_search_filters_by_query():
    provider = BuiltinDemoAudioProvider()

    tracks = provider.search(q="Sprint")

    assert [(track.id, track.title, track.bpm) for track in tracks] == [
        ("builtin-140", "High-Energy Sprint", 140.0)
    ]


@pytest.mark.parametrize("bpm", [90, 110, 124, 140])
def test_synthesized_track_exposes_exact_section_boundaries(bpm):
    result = synthesize_track(bpm)
    bars = (4, 4, 8, 4)
    names = ("intro", "buildup", "drop", "outro")
    expected_lengths = [round(count * 4 * 60 / bpm * 44100) for count in bars]

    assert result.sample_rate == 44100
    assert tuple(section.name for section in result.sections) == names
    assert result.sections[0].start_sample == 0
    assert all(
        left.end_sample == right.start_sample
        for left, right in zip(result.sections, result.sections[1:])
    )
    assert result.sections[-1].end_sample == len(result.pcm)
    assert [
        section.end_sample - section.start_sample for section in result.sections
    ] == pytest.approx(expected_lengths, rel=0, abs=1)


@pytest.mark.parametrize(
    ("bpm", "sample_rate"),
    [(0, 44100), (-1, 44100), (120, 0), (120, -1)],
)
def test_synthesize_track_rejects_nonpositive_tempo_or_sample_rate(
    bpm, sample_rate
):
    with pytest.raises(ValueError, match="bpm and sample_rate must be positive"):
        synthesize_track(bpm, sample_rate)


@pytest.mark.parametrize("bpm", [90, 110, 124, 140])
def test_synthesized_track_sections_have_distinct_nonzero_energy(bpm):
    result = synthesize_track(bpm)

    energies = [
        float(np.sqrt(np.mean(result.pcm[span.start_sample : span.end_sample] ** 2)))
        for span in result.sections
    ]

    assert len(energies) == 4
    assert all(energy > 0 for energy in energies)
    assert all(
        abs(left - right) / max(left, right) >= 0.10
        for index, left in enumerate(energies)
        for right in energies[index + 1 :]
    )


@pytest.mark.parametrize("bpm", [90, 110, 124, 140])
def test_synthesized_track_kicks_follow_only_the_beat_grid(bpm):
    result = synthesize_track(bpm)
    window = round(0.05 * 44100)

    assert result.kick_onsets == tuple(
        round(beat * 60 / bpm * 44100) for beat in range(80)
    )
    assert all(
        abs((right - left) - 60 / bpm * 44100) <= 1
        for left, right in zip(result.kick_onsets, result.kick_onsets[1:])
    )
    for onset in result.kick_onsets[1:]:
        before = float(np.sqrt(np.mean(result.pcm[onset - window : onset] ** 2)))
        after = float(np.sqrt(np.mean(result.pcm[onset : onset + window] ** 2)))
        assert after >= 1.5 * max(before, 1e-6)

    for beat in range(79):
        midpoint = round((beat + 0.5) * 60 / bpm * 44100)
        before = float(np.sqrt(np.mean(result.pcm[midpoint - window : midpoint] ** 2)))
        after = float(np.sqrt(np.mean(result.pcm[midpoint : midpoint + window] ** 2)))
        assert after < 1.5 * max(before, 1e-6)


@pytest.mark.parametrize("bpm", [90, 110, 124, 140])
def test_builtin_download_encodes_exact_synthesized_pcm_as_mono_wav_input(bpm):
    writer = FakeFileWriter()
    process_calls = []

    def process_runner(*args, **kwargs):
        process_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    provider = BuiltinDemoAudioProvider(
        data_dir="/virtual/data",
        process_runner=process_runner,
        file_writer=writer,
    )

    output = provider.download_track(f"builtin-{bpm}", "/virtual/data/assets")

    expected_path = Path(f"/virtual/data/assets/builtin-{bpm}.mp3")
    assert output == expected_path
    assert len(process_calls) == 1
    args, kwargs = process_calls[0]
    command = args[0]
    assert command[-1] == str(expected_path)
    input_index = command.index("-i") + 1
    input_arg = command[input_index]
    if input_arg == "-":
        wav_bytes = kwargs["input"]
    else:
        wav_bytes = writer.files[str(input_arg)]

    with wave.open(BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 44100
        actual_samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")

    expected_samples = np.round(synthesize_track(bpm).pcm * 32767).astype(np.int16)
    assert len(actual_samples) == len(expected_samples)
    assert np.max(np.abs(actual_samples.astype(np.int32) - expected_samples)) <= 1


def test_builtin_download_returns_empty_path_for_unknown_track_without_side_effects():
    writer = FakeFileWriter()
    process_calls = []

    def process_runner(*args, **kwargs):
        process_calls.append((args, kwargs))

    provider = BuiltinDemoAudioProvider(
        data_dir="/virtual/data",
        process_runner=process_runner,
        file_writer=writer,
    )

    output = provider.download_track("unknown", "/virtual/data/assets")

    assert output == Path()
    assert writer.files == {}
    assert process_calls == []


def test_jamendo_search_sends_license_and_client_filters_and_maps_track():
    requests = []
    payload = {
        "results": [
            {
                "id": "jam-1",
                "name": "Chill Loop",
                "artist_name": "Example Artist",
                "duration": 12,
                "audio": "https://prod-1.storage.jamendo.com/preview.mp3",
                "audiodownload": "https://prod-1.storage.jamendo.com/download.mp3",
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "bpm": 100,
            }
        ]
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id="test-client"), client=client
        )

        tracks = provider.search(q="chill", bpm_min=85, bpm_max=115)
    finally:
        client.close()

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path.rstrip("/").endswith("/tracks")
    params = dict(request.url.params)
    assert params["client_id"] == "test-client"
    assert "license" in params
    assert "cc" in params["license"].lower() or "creative" in params["license"].lower()
    assert len(tracks) == 1
    track = tracks[0]
    assert (
        track.provider,
        track.title,
        track.artist,
        track.bpm,
        track.duration_sec,
        track.preview_url,
        track.download_url,
        track.license,
    ) == (
        "jamendo",
        "Chill Loop",
        "Example Artist",
        100.0,
        12.0,
        "https://prod-1.storage.jamendo.com/preview.mp3",
        "https://prod-1.storage.jamendo.com/download.mp3",
        "https://creativecommons.org/licenses/by/4.0/",
    )


def test_jamendo_without_client_id_skips_search_and_download_requests():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id=""), client=client
        )

        tracks = provider.search()
        output = provider.download_track("jam-1", "/virtual/data/assets")
    finally:
        client.close()

    assert tracks == []
    assert output == Path()
    assert requests == []


def test_jamendo_search_keeps_inclusive_bpm_bounds_and_skips_invalid_results():
    requests = []
    payload = {
        "results": [
            {
                "id": "at-lower-bound",
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "bpm": 85,
            },
            {
                "id": "at-upper-bound",
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "bpm": 115,
            },
            {
                "id": "below-bound",
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "bpm": 84.9,
            },
            {
                "id": "above-bound",
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "bpm": 115.1,
            },
            {
                "id": "malformed-bpm",
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "bpm": "not-a-number",
            },
            {"id": "missing-license", "bpm": 100},
            {"id": "empty-license", "license_ccurl": "", "bpm": 100},
        ]
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id="test-client"), client=client
        )

        tracks = provider.search(bpm_min=85, bpm_max=115)
    finally:
        client.close()

    assert len(requests) == 1
    params = dict(requests[0].url.params)
    assert params["client_id"] == "test-client"
    assert params["license"] == "cc"
    assert params["license_cc"] == "1"
    assert params["license_cc_nc"] == "0"
    assert params["license_cc_nd"] == "0"
    assert [(track.id, track.bpm) for track in tracks] == [
        ("at-lower-bound", 85.0),
        ("at-upper-bound", 115.0),
    ]


@pytest.mark.parametrize(
    "bpm",
    ["not-a-number", {"unexpected": "shape"}],
    ids=["invalid-number", "invalid-type"],
)
def test_jamendo_search_keeps_tracks_with_malformed_optional_bpm_as_unknown(bpm):
    payload = {
        "results": [
            {
                "id": "jam-malformed-bpm",
                "name": "Unknown Tempo",
                "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
                "bpm": bpm,
            }
        ]
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id="test-client"), client=client
        )

        tracks = provider.search()
    finally:
        client.close()

    assert [(track.id, track.bpm) for track in tracks] == [("jam-malformed-bpm", None)]


@pytest.mark.parametrize(
    "payload",
    [
        {"results": []},
        {"results": [{"id": "jam-1"}]},
    ],
    ids=["empty-results", "missing-download-url"],
)
def test_jamendo_download_without_result_url_does_not_fetch_audio(payload):
    requests = []
    writer = FakeFileWriter()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id="test-client"),
            client=client,
            file_writer=writer,
        )

        output = provider.download_track("jam-1", "/virtual/data/assets")
    finally:
        client.close()

    assert output == Path()
    assert len(requests) == 1
    assert requests[0].url.path.rstrip("/").endswith("/tracks")
    assert requests[0].url.params["id"] == "jam-1"
    assert writer.files == {}


def test_jamendo_download_uses_default_filesystem_writer(tmp_path):
    audio_content = b"jamendo audio bytes"

    def handler(request):
        if request.url.path.rstrip("/").endswith("/tracks"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "jam-1",
                            "audiodownload": "https://prod-1.storage.jamendo.com/jam-1.mp3",
                        }
                    ]
                },
            )
        return httpx.Response(200, content=audio_content)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    output_dir = tmp_path / "nested" / "assets"
    try:
        provider = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id="test-client"), client=client
        )

        output = provider.download_track("jam-1", output_dir)
    finally:
        client.close()

    assert output == output_dir / "jam-1.mp3"
    assert output.read_bytes() == audio_content


@pytest.mark.parametrize(
    ("provider_type", "settings_values", "api_host"),
    [
        (
            JamendoAudioProvider,
            {"jamendo_client_id": "test-client"},
            "api.jamendo.com",
        ),
        (
            FreesoundAudioProvider,
            {"freesound_api_key": "test-key"},
            "freesound.org",
        ),
    ],
)
def test_audio_provider_rejects_malicious_download_url_before_fetching(
    provider_type, settings_values, api_host
):
    requests = []
    malicious_url = "https://attacker.example/track.mp3"

    def handler(request):
        requests.append(request)
        if provider_type is JamendoAudioProvider:
            return httpx.Response(
                200, json={"results": [{"id": "jam-1", "audiodownload": malicious_url}]}
            )
        return httpx.Response(200, json={"id": 42, "download": malicious_url})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = provider_type(
            Settings(_env_file=None, **settings_values),
            client=client,
            file_writer=FakeFileWriter(),
        )

        with pytest.raises(httpx.InvalidURL):
            provider.download_track(
                "jam-1" if provider_type is JamendoAudioProvider else "42",
                "/virtual/data/assets",
            )
    finally:
        client.close()

    assert len(requests) == 1
    assert requests[0].url.host == api_host
    assert requests[0].url.host != httpx.URL(malicious_url).host


@pytest.mark.parametrize(
    ("provider_type", "settings_values", "api_host", "allowed_download_url"),
    [
        (
            JamendoAudioProvider,
            {"jamendo_client_id": "test-client"},
            "api.jamendo.com",
            "https://prod-1.storage.jamendo.com/track.mp3",
        ),
        (
            FreesoundAudioProvider,
            {"freesound_api_key": "test-key"},
            "freesound.org",
            "https://downloads.freesound.org/track.mp3",
        ),
    ],
)
def test_audio_provider_rejects_untrusted_redirect_before_fetching(
    provider_type, settings_values, api_host, allowed_download_url
):
    requests = []
    malicious_url = "https://attacker.example/redirected.mp3"

    def handler(request):
        requests.append(request)
        if request.url.host == api_host:
            if provider_type is JamendoAudioProvider:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"id": "jam-1", "audiodownload": allowed_download_url}
                        ]
                    },
                )
            return httpx.Response(
                200, json={"id": 42, "download": allowed_download_url}
            )
        return httpx.Response(302, headers={"location": malicious_url})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = provider_type(
            Settings(_env_file=None, **settings_values),
            client=client,
            file_writer=FakeFileWriter(),
        )

        with pytest.raises(httpx.InvalidURL):
            provider.download_track(
                "jam-1" if provider_type is JamendoAudioProvider else "42",
                "/virtual/data/assets",
            )
    finally:
        client.close()

    assert [request.url.host for request in requests] == [
        api_host,
        httpx.URL(allowed_download_url).host,
    ]
    assert all(request.url.host != httpx.URL(malicious_url).host for request in requests)


def test_freesound_search_filters_licenses_and_maps_preview_and_download():
    requests = []
    payload = {
        "results": [
            {
                "id": 42,
                "name": "Loop",
                "username": "Sound Maker",
                "duration": 8.5,
                "license": "https://creativecommons.org/publicdomain/zero/1.0/",
                "previews": {"preview-hq-mp3": "https://cdn.freesound.org/preview.mp3"},
                "download": "https://downloads.freesound.org/download.mp3",
            }
        ]
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = FreesoundAudioProvider(
            Settings(_env_file=None, freesound_api_key="test-key"), client=client
        )

        tracks = provider.search(q="loop")
    finally:
        client.close()

    assert len(requests) == 1
    request = requests[0]
    assert "freesound.org" in request.url.host
    assert "search" in request.url.path
    query = " ".join(
        value
        for values in parse_qs(request.url.query.decode()).values()
        for value in values
    ).lower()
    assert "loop" in query
    assert "creative commons 0" in query or "cc0" in query
    assert "attribution" in query or "cc-by" in query
    assert len(tracks) == 1
    track = tracks[0]
    assert (
        track.id,
        track.provider,
        track.title,
        track.artist,
        track.duration_sec,
        track.preview_url,
        track.download_url,
        track.license,
    ) == (
        "42",
        "freesound",
        "Loop",
        "Sound Maker",
        8.5,
        "https://cdn.freesound.org/preview.mp3",
        "https://downloads.freesound.org/download.mp3",
        "https://creativecommons.org/publicdomain/zero/1.0/",
    )


def test_freesound_search_filters_analysis_bpm_inclusively_and_skips_invalid_bpm():
    license_url = "https://creativecommons.org/publicdomain/zero/1.0/"
    payload = {
        "results": [
            {"id": "lower", "license": license_url, "analysis": {"rhythm": {"bpm": 100}}},
            {"id": "upper", "license": license_url, "analysis": {"rhythm": {"bpm": 120}}},
            {"id": "below", "license": license_url, "analysis": {"rhythm": {"bpm": 99.9}}},
            {"id": "above", "license": license_url, "analysis": {"rhythm": {"bpm": 120.1}}},
            {"id": "missing", "license": license_url, "analysis": {"rhythm": {}}},
            {
                "id": "malformed",
                "license": license_url,
                "analysis": {"rhythm": {"bpm": "not-a-number"}},
            },
        ]
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = FreesoundAudioProvider(
            Settings(_env_file=None, freesound_api_key="test-key"), client=client
        )

        tracks = provider.search(q="loop", bpm_min=100, bpm_max=120)
    finally:
        client.close()

    assert [(track.id, track.bpm) for track in tracks] == [
        ("lower", 100.0),
        ("upper", 120.0),
    ]


def test_freesound_without_api_key_skips_search_and_download_requests():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = FreesoundAudioProvider(
            Settings(_env_file=None, freesound_api_key=""), client=client
        )

        tracks = provider.search()
        output = provider.download_track("42", "/virtual/data/assets")
    finally:
        client.close()

    assert tracks == []
    assert output == Path()
    assert requests == []


def test_freesound_search_rejects_unlicensed_items_and_falls_back_to_preview():
    payload = {
        "results": [
            {
                "id": 41,
                "license": "https://example.com/all-rights-reserved",
                "previews": {"preview-hq-mp3": "https://cdn.freesound.org/rejected.mp3"},
            },
            {
                "id": 42,
                "name": "Low Quality Preview",
                "license": "https://creativecommons.org/licenses/by/4.0/",
                "previews": {"preview-lq-mp3": "https://cdn.freesound.org/preview-lq.mp3"},
            },
        ]
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = FreesoundAudioProvider(
            Settings(_env_file=None, freesound_api_key="test-key"), client=client
        )

        tracks = provider.search()
    finally:
        client.close()

    assert [(track.id, track.preview_url) for track in tracks] == [
        ("42", "https://cdn.freesound.org/preview-lq.mp3")
    ]


def test_freesound_download_without_url_does_not_fetch_or_write_audio():
    requests = []
    writer = FakeFileWriter()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 42, "download": ""})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = FreesoundAudioProvider(
            Settings(_env_file=None, freesound_api_key="test-key"),
            client=client,
            file_writer=writer,
        )

        output = provider.download_track("42", "/virtual/data/assets")
    finally:
        client.close()

    assert output == Path()
    assert len(requests) == 1
    assert requests[0].url.path.rstrip("/").endswith("/42")
    assert writer.files == {}


def test_freesound_download_uses_default_file_writer(tmp_path):
    audio_content = b"freesound audio bytes"

    def handler(request):
        if request.url.path.rstrip("/").endswith("/42"):
            return httpx.Response(
                200, json={"id": 42, "download": "https://downloads.freesound.org/42.mp3"}
            )
        return httpx.Response(200, content=audio_content)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        provider = FreesoundAudioProvider(
            Settings(_env_file=None, freesound_api_key="test-key"), client=client
        )

        output = provider.download_track("42", tmp_path)
    finally:
        client.close()

    assert output == tmp_path / "42.mp3"
    assert output.read_bytes() == audio_content


def test_catalog_keeps_builtin_tracks_when_jamendo_returns_http_500():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500, text="provider unavailable")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        builtin = BuiltinDemoAudioProvider()
        jamendo = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id="test-client"), client=client
        )

        tracks = search_audio_catalog(
            provider="all", providers=[builtin, jamendo]
        )
    finally:
        client.close()

    assert catalog_track_pairs(tracks) == [
        ("Chill Groove", 90.0),
        ("Sunset Pulse", 110.0),
        ("House Drive", 124.0),
        ("High-Energy Sprint", 140.0),
    ]


def test_builtin_catalog_selection_does_not_request_jamendo():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        builtin = BuiltinDemoAudioProvider()
        jamendo = JamendoAudioProvider(
            Settings(_env_file=None, jamendo_client_id="test-client"), client=client
        )

        tracks = search_audio_catalog(
            provider="builtin", providers=[builtin, jamendo]
        )
    finally:
        client.close()

    assert catalog_track_pairs(tracks) == [
        ("Chill Groove", 90.0),
        ("Sunset Pulse", 110.0),
        ("House Drive", 124.0),
        ("High-Energy Sprint", 140.0),
    ]
    assert requests == []


def test_unknown_catalog_selection_returns_no_tracks_without_searching_providers():
    class SearchSpy:
        def __init__(self, name):
            self.name = name
            self.search_calls = 0

        def search(self, **kwargs):
            self.search_calls += 1
            return []

    providers = [SearchSpy("builtin"), SearchSpy("jamendo")]

    tracks = search_audio_catalog(provider="unknown", providers=providers)

    assert tracks == []
    assert [item.search_calls for item in providers] == [0, 0]


def test_freesound_search_and_track_lookup_use_authorization_header_not_url_secret():
    requests = []

    def handler(request):
        requests.append(request)
        if "search" in request.url.path:
            return httpx.Response(200, json={"results": []})
        if request.url.path.rstrip("/").endswith("/42"):
            return httpx.Response(
                200, json={"id": 42, "download": "https://freesound.org/42/download/"}
            )
        return httpx.Response(200, content=b"audio")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    writer = FakeFileWriter()
    try:
        provider = FreesoundAudioProvider(
            Settings(_env_file=None, freesound_api_key="test-key"),
            client=client,
            file_writer=writer,
        )

        provider.search(q="loop")
        provider.download_track("42", "/virtual/data/assets")
    finally:
        client.close()

    api_requests = [
        request
        for request in requests
        if "search" in request.url.path or request.url.path.rstrip("/").endswith("/42")
    ]
    assert len(api_requests) == 2
    assert all("test-key" not in str(request.url) for request in requests)
    assert all(
        request.headers.get("Authorization") == "Token test-key"
        for request in api_requests
    )
    media_requests = [request for request in requests if request not in api_requests]
    assert len(media_requests) == 1
    assert media_requests[0].headers.get("Authorization") is None
