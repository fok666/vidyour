"""Unit tests for the Google Photos OAuth router (auth-url, callback, status).

Collaborator contracts pinned here (owned by this module, no shared conftest):
- Router: ``create_oauth_router(settings, provider=..., transaction_store=..., clock=...)``
  mounted under ``/api`` on a FastAPI app.
- Provider fake: ``get_auth_url(state) -> str``, ``exchange_code(code)``,
  ``get_status() -> GooglePhotosStatus``.
- Clock: zero-argument callable returning numeric seconds.
- Transaction store: real ``OAuthTransactionStore``; a recording subclass captures
  each transaction dict returned by ``create`` (keys ``state``, ``created_at``,
  ``expires_at``; the browser binding is stored among its values).
- Cryptographic randomness: ``secrets.token_bytes`` (the primitive underneath
  ``secrets.token_urlsafe``/``token_hex``) is replaced by a spy returning
  deterministic distinct bytes.
- Browsers: one ``TestClient`` (own cookie jar) per browser, over HTTPS.
"""

import base64
import json
import queue
import secrets
import threading
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import gallery_oauth
from app.api.gallery_oauth import OAuthTransactionStore, create_oauth_router
from app.config import Settings
from app.providers.gallery.google_photos import GooglePhotosStatus

SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
AUTH_URL = "/api/gallery/google/auth-url"
CALLBACK_URL = "/api/gallery/google/callback"
STATUS_URL = "/api/gallery/google/status"
BASE_URL = "https://testserver"


class FakeClock:
    def __init__(self, now=1000):
        self.now = now

    def __call__(self):
        return self.now


class RandomSpy:
    """Deterministic stand-in for ``secrets.token_bytes`` that records requests."""

    def __init__(self):
        self.calls = []  # list of (requested_nbytes, output_bytes)

    def __call__(self, nbytes=None):
        n = 32 if nbytes is None else nbytes
        index = len(self.calls) + 1
        output = bytes([index % 256]) * n
        self.calls.append((n, output))
        return output

    def encodings(self, min_bytes=32):
        encoded = set()
        for n, output in self.calls:
            if n >= min_bytes:
                encoded.add(base64.urlsafe_b64encode(output).rstrip(b"=").decode("ascii"))
                encoded.add(output.hex())
        return encoded


class FakeProvider:
    def __init__(self, fail_exchange=False, exchange_gate=None):
        self.exchanged = []
        self.connected = False
        self.fail_exchange = fail_exchange
        self.exchange_gate = exchange_gate
        self.auth_states = []

    def get_auth_url(self, state):
        self.auth_states.append(state)
        query = urlencode({"scope": SCOPE, "state": state, "response_type": "code"})
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    def exchange_code(self, code):
        self.exchanged.append(code)
        if self.exchange_gate is not None:
            self.exchange_gate.wait(timeout=5)
        if self.fail_exchange:
            raise RuntimeError("token exchange failed")
        self.connected = True
        return {"access_token": "tok"}

    def get_status(self):
        return GooglePhotosStatus(configured=True, connected=self.connected)


class RecordingStore(OAuthTransactionStore):
    def __init__(self, clock=None):
        super().__init__(clock=clock)
        self.created = []

    def create(self, *args, **kwargs):
        transaction = super().create(*args, **kwargs)
        self.created.append(transaction)
        return transaction


@pytest.fixture(autouse=True)
def rng(monkeypatch):
    spy = RandomSpy()
    monkeypatch.setattr(secrets, "token_bytes", spy)
    monkeypatch.setattr(gallery_oauth, "token_bytes", spy, raising=False)
    return spy


@pytest.fixture
def clock():
    return FakeClock(1000)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        google_client_id="cid",
        google_client_secret="csec",
        google_redirect_uri=f"{BASE_URL}{CALLBACK_URL}",
    )


def build(settings, clock, provider=None):
    provider = provider or FakeProvider()
    store = RecordingStore(clock=clock)
    app = FastAPI()
    app.include_router(
        create_oauth_router(settings, provider=provider, transaction_store=store, clock=clock),
        prefix="/api",
    )
    return app, provider, store


def browser(app):
    return TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)


def start_auth(client):
    response = client.get(AUTH_URL)
    assert response.status_code == 200, response.text
    auth_url = response.json().get("auth_url", "")
    states = parse_qs(urlparse(auth_url).query).get("state", [])
    assert states and states[0], f"auth_url carries no state: {auth_url!r}"
    return states[0], response


def binding_cookie(response):
    headers = response.headers.get_list("set-cookie")
    assert headers, "auth-url set no browser binding cookie"
    parsed = SimpleCookie()
    parsed.load(headers[0])
    assert parsed, f"unparseable Set-Cookie: {headers[0]!r}"
    name = next(iter(parsed))
    return name, parsed[name], headers[0]


def callback(client, **params):
    return client.get(CALLBACK_URL, params=params)


def baseline_rejection(app):
    return callback(browser(app), code="code-123")


def assert_generic_rejection(response, baseline):
    assert not 200 <= baseline.status_code < 300, "missing-state callback was accepted"
    assert not 200 <= response.status_code < 300, (
        f"invalid callback accepted with {response.status_code}"
    )
    assert response.status_code == baseline.status_code
    assert response.content == baseline.content


def recorded(store, state):
    matches = [t for t in store.created if isinstance(t, dict) and t.get("state") == state]
    assert matches, f"no stored transaction for state {state!r}: {store.created!r}"
    return matches[0]


# Behavior 1
def test_auth_url_returns_state_and_sets_binding_cookie(settings, clock):
    app, provider, _ = build(settings, clock)
    client = browser(app)

    response = client.get(AUTH_URL)

    assert response.status_code == 200
    auth_url = response.json().get("auth_url", "")
    query = parse_qs(urlparse(auth_url).query)
    assert query.get("scope") == [SCOPE]
    state = (query.get("state") or [""])[0]
    assert state != ""
    assert provider.auth_states == [state]
    name, morsel, raw = binding_cookie(response)
    lowered = raw.lower()
    assert morsel.value and morsel.value != state
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "secure" in lowered
    assert "domain=" not in lowered
    assert morsel["path"] in ("/", "/api", "/api/", "/api/gallery", "/api/gallery/",
                              "/api/gallery/google", "/api/gallery/google/")
    # Top-level redirect back from Google: a fresh request carrying only the cookie.
    returning = browser(app)
    returning.cookies.set(name, morsel.value)
    assert callback(returning, code="code-123", state=state).status_code == 200


# Behavior 2
def test_auth_url_stores_transaction_with_bounded_ttl(settings, clock):
    app, _, store = build(settings, clock)
    client = browser(app)

    state, response = start_auth(client)

    _, morsel, _ = binding_cookie(response)
    transaction = recorded(store, state)
    assert morsel.value in transaction.values()
    assert transaction["created_at"] == 1000
    ttl = transaction["expires_at"] - transaction["created_at"]
    assert 0 < ttl <= 600
    assert transaction["expires_at"] <= 1600


# Behavior 3
def test_each_transaction_gets_distinct_strong_random_state(settings, clock, rng):
    app, provider, _ = build(settings, clock)
    client = browser(app)

    state1, response1 = start_auth(client)
    state2, _ = start_auth(client)

    cookie_value = client.cookies.get(binding_cookie(response1)[0])
    strong = rng.encodings(min_bytes=32)
    assert state1 in strong
    assert state2 in strong
    assert state1 != state2
    assert cookie_value not in (state1, state2)
    assert callback(client, code="code-1", state=state1).status_code == 200
    assert callback(client, code="code-2", state=state2).status_code == 200
    assert provider.exchanged == ["code-1", "code-2"]


# Behavior 4
def test_valid_callback_exchanges_code_and_connects(settings, clock):
    app, provider, store = build(settings, clock)
    client = browser(app)
    state, _ = start_auth(client)
    transaction = recorded(store, state)
    clock.now = transaction["created_at"]
    assert clock.now < transaction["expires_at"]

    response = callback(client, code="code-123", state=state)

    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/html")
    body = response.text
    assert "opener" in body and "postMessage" in body
    assert "close(" in body
    assert provider.exchanged == ["code-123"]
    assert client.get(STATUS_URL).json().get("connected") is True


@pytest.mark.parametrize(
    ("cors_origins", "target_origin"),
    [
        ("https://studio.example.test,https://other.example.test", "https://studio.example.test"),
        ("", "http://localhost:5173"),
        ("*", "http://localhost:5173"),
    ],
)
def test_success_callback_posts_to_frontend_origin(settings, clock, cors_origins, target_origin):
    settings.cors_origins = cors_origins
    app, _, _ = build(settings, clock)
    client = browser(app)
    state, _ = start_auth(client)

    response = callback(client, code="code-123", state=state)

    assert response.status_code == 200
    assert (
        f"postMessage({{type: 'google-photos-connected'}}, {json.dumps(target_origin)})"
        in response.text
    )


@pytest.mark.parametrize(
    ("cors_origins", "target_origin"),
    [
        ("http://localhost:bad,https://studio.example.test", "https://studio.example.test"),
        ("http://localhost:bad", "http://localhost:5173"),
    ],
)
def test_malformed_configured_origin_port_is_skipped(
    settings, clock, cors_origins, target_origin
):
    settings.cors_origins = cors_origins
    app, _, _ = build(settings, clock)
    client = browser(app)
    state, _ = start_auth(client)

    response = callback(client, code="code-123", state=state)

    assert response.status_code == 200
    assert (
        f"postMessage({{type: 'google-photos-connected'}}, {json.dumps(target_origin)})"
        in response.text
    )


# Behavior 5
def test_missing_state_is_generic_rejection_without_exchange(settings, clock):
    app, provider, _ = build(settings, clock)
    client = browser(app)

    response = callback(client, code="code-123")

    assert not 200 <= response.status_code < 300
    assert "missing" not in response.text.lower()
    assert response.content == baseline_rejection(app).content
    assert provider.exchanged == []


# Behavior 6
def test_mismatched_state_rejected_and_correct_state_still_usable(settings, clock):
    app, provider, _ = build(settings, clock)
    client = browser(app)
    state_a, _ = start_auth(client)
    baseline = baseline_rejection(app)

    response = callback(client, code="code-123", state="wrong-state")

    assert_generic_rejection(response, baseline)
    assert provider.exchanged == []
    assert callback(client, code="code-123", state=state_a).status_code == 200


# Behavior 7
def test_foreign_browser_cannot_redeem_state(settings, clock):
    app, provider, _ = build(settings, clock)
    browser_a = browser(app)
    browser_b = browser(app)
    state_a, response_a = start_auth(browser_a)
    _, response_b = start_auth(browser_b)
    assert binding_cookie(response_a)[1].value != binding_cookie(response_b)[1].value
    baseline = baseline_rejection(app)

    response = callback(browser_b, code="code-123", state=state_a)

    assert_generic_rejection(response, baseline)
    assert provider.exchanged == []
    assert callback(browser_a, code="code-123", state=state_a).status_code == 200


# Behavior 8
def test_state_rejected_at_its_recorded_expiry(settings, clock):
    app, provider, store = build(settings, clock)
    client = browser(app)
    state, _ = start_auth(client)
    transaction = recorded(store, state)
    baseline = baseline_rejection(app)
    clock.now = transaction["expires_at"]

    response = callback(client, code="code-123", state=state)

    assert_generic_rejection(response, baseline)
    assert provider.exchanged == []


# Behavior 9
def test_replayed_state_rejected_after_success(settings, clock):
    app, provider, _ = build(settings, clock)
    client = browser(app)
    state, _ = start_auth(client)
    assert callback(client, code="code-123", state=state).status_code == 200
    baseline = baseline_rejection(app)

    response = callback(client, code="code-123", state=state)

    assert_generic_rejection(response, baseline)
    assert provider.exchanged == ["code-123"]


# Behavior 10
def test_concurrent_callbacks_only_one_consumes_state(settings, clock):
    gate = threading.Event()
    app, provider, _ = build(settings, clock, FakeProvider(exchange_gate=gate))
    client = browser(app)
    state, response = start_auth(client)
    name = binding_cookie(response)[0]
    cookie_value = client.cookies.get(name)
    baseline = baseline_rejection(app)
    results = queue.Queue()
    start = threading.Barrier(2)

    def race():
        racer = browser(app)
        racer.cookies.set(name, cookie_value)
        start.wait(timeout=5)
        results.put(callback(racer, code="code-123", state=state))

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    first = [results.get(timeout=10)]
    gate.set()
    for thread in threads:
        thread.join(timeout=10)
    responses = first + [results.get(timeout=10)]

    accepted = [r for r in responses if 200 <= r.status_code < 300]
    rejected = [r for r in responses if not 200 <= r.status_code < 300]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert_generic_rejection(rejected[0], baseline)
    assert len(provider.exchanged) <= 1


# Behavior 11
def test_retry_after_failed_exchange_is_rejected(settings, clock):
    app, provider, _ = build(settings, clock, FakeProvider(fail_exchange=True))
    client = browser(app)
    state, _ = start_auth(client)
    callback(client, code="code-123", state=state)
    assert provider.exchanged == ["code-123"]
    baseline = baseline_rejection(app)

    response = callback(client, code="code-123", state=state)

    assert_generic_rejection(response, baseline)
    assert provider.exchanged == ["code-123"]


# Behavior 12
def test_later_state_redeemed_leaves_earlier_state_pending(settings, clock):
    app, provider, _ = build(settings, clock)
    client = browser(app)
    state_a1, _ = start_auth(client)
    state_a2, _ = start_auth(client)

    response = callback(client, code="code-A2", state=state_a2)

    assert response.status_code == 200
    assert provider.exchanged == ["code-A2"]
    assert callback(client, code="code-A1", state=state_a1).status_code == 200


# Behavior 13
def test_earlier_state_redeemed_after_later_state(settings, clock):
    app, provider, _ = build(settings, clock)
    client = browser(app)
    state_a1, _ = start_auth(client)
    state_a2, _ = start_auth(client)
    assert callback(client, code="code-A2", state=state_a2).status_code == 200
    assert provider.exchanged == ["code-A2"]

    response = callback(client, code="code-A1", state=state_a1)

    assert response.status_code == 200
    assert provider.exchanged == ["code-A2", "code-A1"]
