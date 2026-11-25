"""Unit tests for GooglePhotosPickerProvider.

Collaborator contracts pinned here (duck-typed, owned by this module):
- client: an ``httpx.Client`` (stubbed via ``httpx.MockTransport``).
- token_store: object with ``load() -> dict | None``, ``save(token: dict)`` and ``clear()``.
- file_writer: callable ``file_writer(path, content: bytes)``.
- sleep: callable ``sleep(seconds)`` used between ``mediaItemsSet`` polls.
"""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.providers.gallery.google_photos import GooglePhotosPickerProvider

REDIRECT_URI = "http://localhost:8000/api/gallery/google/callback"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
PICKER_BASE = "https://photospicker.googleapis.com/v1"


class FakeTokenStore:
    def __init__(self, token=None):
        self.token = token
        self.saved = []

    def load(self):
        return self.token

    def save(self, token):
        self.token = dict(token)
        self.saved.append(dict(token))

    def clear(self):
        self.token = None


class FakeFileWriter:
    def __init__(self):
        self.files = {}

    def __call__(self, path, content):
        self.files[str(path)] = bytes(content)


class SleepRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def make_settings(client_id="cid"):
    return Settings(
        _env_file=None,
        google_client_id=client_id,
        google_client_secret="csec",
        google_redirect_uri=REDIRECT_URI,
    )


def make_provider(handler, token=None, client_id="cid"):
    requests = []

    def recording_handler(request):
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    store = FakeTokenStore(token)
    writer = FakeFileWriter()
    sleep = SleepRecorder()
    provider = GooglePhotosPickerProvider(
        make_settings(client_id),
        client=client,
        token_store=store,
        file_writer=writer,
        sleep=sleep,
    )
    return provider, requests, store, writer, sleep


CONNECTED_TOKEN = {"access_token": "access-abc", "refresh_token": "refresh-xyz", "token_type": "Bearer"}


def no_network(request):
    raise AssertionError(f"unexpected request {request.method} {request.url}")


def descriptor_mentions(descriptor, value):
    return value in repr(descriptor)


# Behavior 1
def test_get_auth_url_contains_oauth_parameters_and_state():
    provider, _, _, _, _ = make_provider(no_network)

    url = provider.get_auth_url(state="state-example-123")

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert query.get("client_id") == ["cid"]
    assert query.get("redirect_uri") == [REDIRECT_URI]
    assert query.get("response_type") == ["code"]
    assert SCOPE in query.get("scope", [""])[0].split(" ")
    assert query.get("state") == ["state-example-123"]


# Behavior 2
def test_get_auth_url_raises_when_client_id_missing():
    provider, _, _, _, _ = make_provider(no_network, client_id="")

    with pytest.raises(ValueError, match="(?i)not configured"):
        provider.get_auth_url(state="state-example-123")


# Behavior 3
def test_exchange_code_saves_token_and_reports_connected():
    def handler(request):
        if request.method == "POST" and request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-abc",
                    "refresh_token": "refresh-xyz",
                    "expires_in": 3599,
                    "token_type": "Bearer",
                    "scope": SCOPE,
                },
            )
        return httpx.Response(404)

    provider, requests, store, _, _ = make_provider(handler)

    provider.exchange_code("code-123")

    assert store.token is not None and store.token.get("access_token") == "access-abc"
    token_requests = [r for r in requests if r.url.host == "oauth2.googleapis.com"]
    assert len(token_requests) == 1
    form = parse_qs(token_requests[0].content.decode())
    assert form.get("code") == ["code-123"]
    assert form.get("grant_type") == ["authorization_code"]
    status = provider.get_status()
    assert status.configured is True
    assert status.connected is True


# Behavior 4
def test_create_session_posts_to_picker_and_returns_session_info():
    def handler(request):
        if request.method == "POST" and str(request.url) == f"{PICKER_BASE}/sessions":
            return httpx.Response(
                200,
                json={
                    "id": "sess-1",
                    "pickerUri": "https://photos.google.com/picker/sess-1",
                    "mediaItemsSet": False,
                },
            )
        return httpx.Response(404)

    provider, requests, _, _, _ = make_provider(handler, token=dict(CONNECTED_TOKEN))

    result = provider.create_session()

    assert result.get("session_id") == "sess-1"
    assert result.get("picker_uri") == "https://photos.google.com/picker/sess-1"
    posts = [r for r in requests if r.method == "POST" and str(r.url) == f"{PICKER_BASE}/sessions"]
    assert len(posts) == 1
    assert posts[0].headers.get("authorization") == "Bearer access-abc"


def picker_handler(session_id, session_polls, items, downloads):
    """Stub Picker API: session GET replies come from ``session_polls`` in order."""
    poll_iter = iter(session_polls)

    def handler(request):
        url = str(request.url)
        if request.method == "GET" and url == f"{PICKER_BASE}/sessions/{session_id}":
            return httpx.Response(
                200,
                json={
                    "id": session_id,
                    "mediaItemsSet": next(poll_iter),
                    "pollingConfig": {"pollInterval": "1s"},
                },
            )
        if request.method == "GET" and request.url.path == "/v1/mediaItems":
            assert request.url.params.get("sessionId") == session_id
            return httpx.Response(200, json={"mediaItems": items})
        if request.method == "GET" and url in downloads:
            return httpx.Response(200, content=downloads[url])
        return httpx.Response(404)

    return handler


# Behavior 5
def test_download_session_items_downloads_video_and_photo_with_correct_suffixes():
    items = [
        {
            "id": "gp-video-1",
            "type": "VIDEO",
            "mediaFile": {
                "baseUrl": "https://lh3.googleusercontent.com/v1",
                "mimeType": "video/mp4",
                "filename": "v1.mp4",
            },
        },
        {
            "id": "gp-photo-1",
            "type": "PHOTO",
            "mediaFile": {
                "baseUrl": "https://lh3.googleusercontent.com/p1",
                "mimeType": "image/jpeg",
                "filename": "p1.jpg",
            },
        },
    ]
    downloads = {
        "https://lh3.googleusercontent.com/v1=dv": b"VIDEO-BYTES",
        "https://lh3.googleusercontent.com/p1=d": b"PHOTO-BYTES",
    }
    provider, requests, _, writer, _ = make_provider(
        picker_handler("sess-1", [True], items, downloads), token=dict(CONNECTED_TOKEN)
    )

    result = provider.download_session_items("sess-1", "/virtual/data/assets")

    assert len(result) == 2
    assert any(descriptor_mentions(d, "gp-video-1") for d in result)
    assert any(descriptor_mentions(d, "gp-photo-1") for d in result)
    fetched = [str(r.url) for r in requests]
    assert "https://lh3.googleusercontent.com/v1=dv" in fetched
    assert "https://lh3.googleusercontent.com/p1=d" in fetched
    assert sorted(writer.files.values()) == [b"PHOTO-BYTES", b"VIDEO-BYTES"]
    assert all(path.startswith("/virtual/data/assets") for path in writer.files)


# Behavior 6
def test_download_session_items_polls_until_media_items_set():
    items = [
        {
            "id": "gp-photo-2",
            "type": "PHOTO",
            "mediaFile": {
                "baseUrl": "https://lh3.googleusercontent.com/p2",
                "mimeType": "image/jpeg",
                "filename": "p2.jpg",
            },
        },
    ]
    downloads = {"https://lh3.googleusercontent.com/p2=d": b"PHOTO2-BYTES"}
    provider, requests, _, writer, sleep = make_provider(
        picker_handler("sess-2", [False, True], items, downloads), token=dict(CONNECTED_TOKEN)
    )

    result = provider.download_session_items("sess-2", "/virtual/data/assets")

    session_polls = [
        r for r in requests if r.method == "GET" and str(r.url) == f"{PICKER_BASE}/sessions/sess-2"
    ]
    assert len(session_polls) == 2
    assert len(sleep.calls) == 1
    assert "https://lh3.googleusercontent.com/p2=d" in [str(r.url) for r in requests]
    assert list(writer.files.values()) == [b"PHOTO2-BYTES"]
    assert len(result) == 1
    assert descriptor_mentions(result[0], "gp-photo-2")


def test_download_session_items_does_not_send_bearer_token_to_redirect_target():
    media_url = "https://lh3.googleusercontent.com/p3=d"
    redirect_target = "https://attacker.example/collect"
    items = [
        {
            "id": "gp-photo-3",
            "type": "PHOTO",
            "mediaFile": {
                "baseUrl": "https://lh3.googleusercontent.com/p3",
                "mimeType": "image/jpeg",
                "filename": "p3.jpg",
            },
        },
    ]

    def handler(request):
        if request.method == "GET" and str(request.url) == f"{PICKER_BASE}/sessions/sess-3":
            return httpx.Response(200, json={"id": "sess-3", "mediaItemsSet": True})
        if request.method == "GET" and request.url.path == "/v1/mediaItems":
            return httpx.Response(200, json={"mediaItems": items})
        if str(request.url) == media_url:
            return httpx.Response(302, headers={"Location": redirect_target})
        if str(request.url) == redirect_target:
            return httpx.Response(200, content=b"should not be fetched")
        return httpx.Response(404)

    provider, requests, _, _, _ = make_provider(handler, token=dict(CONNECTED_TOKEN))

    with pytest.raises(httpx.HTTPStatusError):
        provider.download_session_items("sess-3", "/virtual/data/assets")

    media_requests = [r for r in requests if str(r.url) == media_url]
    assert len(media_requests) == 1
    assert media_requests[0].headers.get("authorization") == "Bearer access-abc"
    assert not any(str(r.url) == redirect_target for r in requests)


@pytest.mark.parametrize(
    "media_url",
    [
        "http://lh3.googleusercontent.com/p4",
        "https://attacker.example/collect",
        "https://user:password@lh3.googleusercontent.com/p4",
        "https://lh3.googleusercontent.com:444/p4",
        "https://[invalid",
        "https://lh3.googleusercontent.com:not-a-port/p4",
    ],
)
def test_download_session_items_rejects_invalid_media_urls_without_sending_bearer(
    media_url,
):
    items = [
        {
            "id": "gp-invalid-url",
            "type": "PHOTO",
            "mediaFile": {
                "baseUrl": media_url,
                "mimeType": "image/jpeg",
                "filename": "invalid.jpg",
            },
        },
    ]
    provider, requests, _, writer, _ = make_provider(
        picker_handler("sess-invalid-url", [True], items, {}),
        token=dict(CONNECTED_TOKEN),
    )

    with pytest.raises(ValueError, match="Invalid Google Photos media URL"):
        provider.download_session_items("sess-invalid-url", "/virtual/data/assets")

    assert len(requests) == 2
    assert all(request.url.host == "photospicker.googleapis.com" for request in requests)
    assert all(
        request.headers.get("authorization") == "Bearer access-abc"
        for request in requests
    )
    assert writer.files == {}


# Behavior 7
def test_create_session_clears_token_and_raises_on_401():
    def handler(request):
        return httpx.Response(401, content=json.dumps({"error": {"code": 401, "status": "UNAUTHENTICATED"}}))

    provider, _, store, _, _ = make_provider(handler, token=dict(CONNECTED_TOKEN))

    with pytest.raises(RuntimeError) as excinfo:
        provider.create_session()

    message = str(excinfo.value)
    assert "Google Photos" in message
    assert "expired" in message.lower()
    assert store.token is None
    assert provider.get_status().connected is False
