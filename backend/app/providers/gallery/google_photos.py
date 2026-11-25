"""Google Photos Picker provider interface."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
PICKER_BASE = "https://photospicker.googleapis.com/v1"
POLL_INTERVAL_SECONDS = 1
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value: Any, fallback: str) -> str:
    """Reduce untrusted API metadata to a single, non-traversing path component."""
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME_CHARS.sub("_", name).strip("._")
    return name or fallback


@dataclass
class GooglePhotosStatus:
    configured: bool = False
    connected: bool = False


class GooglePhotosPickerProvider:
    def __init__(
        self,
        settings: Any,
        client: Any = None,
        token_store: Any = None,
        file_writer: Callable[..., Any] | None = None,
        clock: Callable[[], Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.token_store = token_store
        self.file_writer = file_writer
        self.clock = clock
        self.sleep = sleep

    def get_status(self) -> GooglePhotosStatus:
        configured = bool(
            self.settings.google_client_id
            and self.settings.google_client_secret
            and self.settings.google_redirect_uri
        )
        connected = bool(self.token_store.load()) if self.token_store else False
        return GooglePhotosStatus(configured=configured, connected=connected)

    def get_auth_url(self, state: str) -> str:
        if not self.settings.google_client_id:
            raise ValueError("Google Photos OAuth is not configured")
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        response = self.client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        token = response.json()
        self.token_store.save(token)
        return token

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        token = self.token_store.load() or {}
        headers = {"Authorization": f"Bearer {token.get('access_token', '')}"}
        kwargs["follow_redirects"] = False
        response = self.client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            self.token_store.clear()
            raise RuntimeError(
                "Google Photos authorization expired; please reconnect Google Photos."
            )
        response.raise_for_status()
        return response

    def create_session(self) -> dict[str, Any]:
        data = self._request("POST", f"{PICKER_BASE}/sessions", json={}).json()
        return {"session_id": data.get("id"), "picker_uri": data.get("pickerUri")}

    def download_session_items(self, session_id: str, output_dir: str | Path) -> list[Any]:
        self._wait_for_media_items(session_id)
        items = self._request(
            "GET", f"{PICKER_BASE}/mediaItems", params={"sessionId": session_id}
        ).json().get("mediaItems", [])
        return [self._download_item(item, output_dir) for item in items]

    def _wait_for_media_items(self, session_id: str) -> None:
        while True:
            session = self._request("GET", f"{PICKER_BASE}/sessions/{session_id}").json()
            if session.get("mediaItemsSet"):
                return
            self.sleep(POLL_INTERVAL_SECONDS)

    def _download_item(self, item: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
        media_file = item.get("mediaFile", {})
        base_url = media_file.get("baseUrl")
        try:
            parsed_url = urlsplit(base_url) if isinstance(base_url, str) else None
            valid_media_url = (
                parsed_url is not None
                and parsed_url.scheme == "https"
                and parsed_url.hostname == "lh3.googleusercontent.com"
                and parsed_url.username is None
                and parsed_url.password is None
                and parsed_url.port in (None, 443)
            )
        except ValueError:
            valid_media_url = False
        if not valid_media_url:
            raise ValueError("Invalid Google Photos media URL")

        suffix = "=dv" if item.get("type") == "VIDEO" else "=d"
        content = self._request("GET", base_url + suffix).content
        item_id = _safe_component(item.get("id"), "item")
        filename = _safe_component(media_file.get("filename"), "media")
        path = f"{output_dir}/{item_id}_{filename}"
        self.file_writer(path, content)
        return {"id": item["id"], "type": item.get("type"), "path": path}
