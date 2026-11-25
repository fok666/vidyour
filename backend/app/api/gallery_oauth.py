"""Google Photos OAuth transaction store and router interfaces."""

import json
import secrets
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Request, Response
from fastapi.responses import HTMLResponse

TRANSACTION_TTL_SECONDS = 600
BROWSER_COOKIE_NAME = "browser_cookie"
STATE_BYTES = 32

_REJECTION_HTML = "<!doctype html><html><body>Google Photos authorization failed.</body></html>"


def _frontend_origin(settings: Any) -> str:
    """Use the first configured CORS origin, or the local studio default."""
    for candidate in str(getattr(settings, "cors_origins", "") or "").split(","):
        candidate = candidate.strip()
        if not candidate or candidate == "*":
            continue
        try:
            parsed = urlsplit(candidate)
            parsed.port  # Validate any configured port.
        except ValueError:
            continue
        if (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        ):
            return f"{parsed.scheme}://{parsed.netloc}"
    return "http://localhost:5173"


def _success_html(target_origin: str) -> str:
    # Keep configured values inside a JavaScript string literal and prevent
    # any value from terminating the containing script element.
    js_origin = json.dumps(target_origin).replace("<", "\\u003c")
    return (
        "<!doctype html><html><body><script>"
        "if (window.opener) { window.opener.postMessage("
        "{type: 'google-photos-connected'}, " + js_origin + "); }"
        "window.close();"
        "</script>Google Photos connected.</body></html>"
    )


def _new_token() -> str:
    return secrets.token_urlsafe(STATE_BYTES)


class OAuthTransactionStore:
    def __init__(self, clock: Callable[[], Any] | None = None) -> None:
        self.clock = clock or time.time
        self._transactions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(
        self,
        browser_cookie: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        created_at = self.clock()
        transaction = {
            "state": state or _new_token(),
            "browser_cookie": browser_cookie,
            "created_at": created_at,
            "expires_at": created_at + TRANSACTION_TTL_SECONDS,
        }
        with self._lock:
            self._transactions[transaction["state"]] = transaction
        return transaction

    def consume(
        self,
        state: str,
        browser_cookie: str | None,
        now: datetime | None = None,
    ) -> Any:
        if not state or not browser_cookie:
            return None
        current = self.clock() if now is None else now
        with self._lock:
            transaction = self._transactions.get(state)
            if transaction is None or not secrets.compare_digest(
                transaction["browser_cookie"] or "", browser_cookie
            ):
                return None
            # Matching browser: the transaction is single-use, even if expired.
            del self._transactions[state]
        if current >= transaction["expires_at"]:
            return None
        return transaction


def create_oauth_router(
    app_settings: Any,
    provider: Any = None,
    transaction_store: OAuthTransactionStore | None = None,
    clock: Callable[[], Any] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> APIRouter:
    router = APIRouter()
    new_token = token_factory or _new_token
    store = transaction_store or OAuthTransactionStore(clock=clock)
    now = clock or store.clock

    def rejection() -> HTMLResponse:
        return HTMLResponse(_REJECTION_HTML, status_code=400)

    @router.get("/gallery/google/auth-url")
    def get_auth_url(
        request: Request,
        response: Response,
        browser_cookie: str | None = Cookie(default=None),
    ) -> dict[str, str]:
        binding = browser_cookie or new_token()
        transaction = store.create(browser_cookie=binding, state=new_token())
        response.set_cookie(
            BROWSER_COOKIE_NAME,
            binding,
            max_age=TRANSACTION_TTL_SECONDS,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="lax",
        )
        return {"auth_url": provider.get_auth_url(transaction["state"])}

    @router.get("/gallery/google/callback", response_class=HTMLResponse)
    def oauth_callback(
        code: str = "",
        state: str = "",
        browser_cookie: str | None = Cookie(default=None),
    ) -> Any:
        if not code or store.consume(state, browser_cookie, now=now()) is None:
            return rejection()
        try:
            provider.exchange_code(code)
        except Exception:
            return rejection()
        return HTMLResponse(_success_html(_frontend_origin(app_settings)))

    @router.get("/gallery/google/status")
    def get_gallery_status() -> dict[str, Any]:
        return asdict(provider.get_status())

    return router
