"""Async client for the BookOrbit REST API (requests, metadata search, books, email)."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

PROVIDER_STATUS_EVENT = "provider-status"


class BookOrbitError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def sse_events(lines: Iterable[str]) -> Iterable[tuple[str | None, str]]:
    """Yield (event_name, data) per SSE message from an iterable of lines."""
    event, data = None, []
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line.strip():
            if data:
                yield event, "\n".join(data)
            event, data = None, []
    if data:
        yield event, "\n".join(data)


def pick_best(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Closest title match to the query; ISBN-bearing candidates win ties."""
    q = query.lower()

    def score(c: dict[str, Any]) -> tuple[float, bool]:
        title = (c.get("title") or c.get("displayTitle") or "").lower()
        authors = " ".join(c.get("authors") or []).lower()
        return (
            max(SequenceMatcher(None, q, title).ratio(), SequenceMatcher(None, q, f"{title} {authors}").ratio()),
            bool(c.get("isbn13")),
        )

    return max(candidates, key=score)


class BookOrbit:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._c = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._username, self._password = username, password
        self._refresh_token: str | None = None

    async def close(self) -> None:
        await self._c.aclose()

    # -- auth ----------------------------------------------------------------

    def _use(self, creds: dict[str, Any]) -> None:
        self._c.headers["Authorization"] = f"Bearer {creds['accessToken']}"
        self._refresh_token = creds.get("refreshToken")

    async def _login(self) -> None:
        r = await self._c.post(
            "/auth/login",
            json={"username": self._username, "password": self._password, "clientKind": "native", "deviceLabel": "telegram-bot"},
        )
        if r.status_code >= 400:
            raise BookOrbitError(f"BookOrbit login failed: {_error_message(r)}", r.status_code)
        self._use(r.json())
        logger.info("Logged in to BookOrbit as %s", self._username)

    async def _reauth(self) -> None:
        if self._refresh_token:
            r = await self._c.post("/auth/refresh", json={"refreshToken": self._refresh_token})
            if r.status_code < 400:
                self._use(r.json())
                return
        await self._login()

    async def _ensure_auth(self) -> None:
        if "Authorization" not in self._c.headers:
            await self._login()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        await self._ensure_auth()
        r = await self._c.request(method, path, **kw)
        if r.status_code == 401:
            await self._reauth()
            r = await self._c.request(method, path, **kw)
        if r.status_code >= 400:
            raise BookOrbitError(_error_message(r), r.status_code)
        return r.json() if r.content else None

    @asynccontextmanager
    async def _stream(self, path: str, **kw: Any):
        """Open a streaming GET, re-authenticating once on 401."""
        await self._ensure_auth()
        for attempt in range(2):
            async with self._c.stream("GET", path, **kw) as r:
                if r.status_code == 401 and attempt == 0:
                    await self._reauth()
                    continue
                if r.status_code >= 400:
                    await r.aread()
                    raise BookOrbitError(_error_message(r), r.status_code)
                yield r
                return

    # -- metadata search -----------------------------------------------------

    async def search(self, title: str, media_kind: str = "ebook") -> list[dict[str, Any]]:
        """Stream metadata candidates for a title from all enabled providers."""
        async with self._stream(
            "/metadata-fetch/stream", params={"title": title, "mediaKind": media_kind}, timeout=httpx.Timeout(120.0, connect=10.0)
        ) as r:
            body = (await r.aread()).decode()
        return [json.loads(data) for event, data in sse_events(body.splitlines()) if event != PROVIDER_STATUS_EVENT]

    # -- requests ------------------------------------------------------------

    async def create_request(self, candidate: dict[str, Any], media_kind: str = "ebook") -> dict[str, Any]:
        """POST a book request built from a metadata candidate. Returns {request, subscribed}."""
        cover = candidate.get("coverUrl") or ""
        body = {
            "title": candidate.get("title") or candidate.get("displayTitle"),
            "mediaKind": media_kind,
            "subtitle": candidate.get("subtitle"),
            "authors": candidate.get("authors"),
            "seriesName": candidate.get("seriesName"),
            "isbn10": candidate.get("isbn10"),
            "isbn13": candidate.get("isbn13"),
            "publishedYear": candidate.get("publishedYear"),
            "coverUrl": cover if cover.startswith(("http://", "https://")) else None,
            "providerKey": candidate.get("provider"),
            "providerId": candidate.get("providerId"),
        }
        return await self._request("POST", "/book-requests", json={k: v for k, v in body.items() if v is not None})

    async def owned_book_id(self, candidate: dict[str, Any], media_kind: str = "ebook") -> int | None:
        """Id of a library book matching the candidate (ISBN13 first, then title + author), or None.
        Unlike request creation this check is not gated by the user's 'see own requested books' flag."""
        query = {
            "title": candidate.get("title") or candidate.get("displayTitle"),
            "mediaKind": media_kind,
            "author": (candidate.get("authors") or [None])[0],
            "isbn13": candidate.get("isbn13"),
            "providerKey": candidate.get("provider"),
            "providerId": candidate.get("providerId"),
        }
        rows = await self._request("POST", "/book-requests/availability", json={"items": [{k: v for k, v in query.items() if v}]})
        return (rows or [{}])[0].get("ownedBookId")

    async def get_request(self, request_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/book-requests/{request_id}")

    # -- books ---------------------------------------------------------------

    async def book(self, book_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/books/{book_id}")

    async def search_library(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        """Substring search over title, subtitle, series and author name across the user's libraries."""
        return await self._request("GET", "/books/search", params={"q": q[:500], "limit": limit})

    async def download_file(self, file_id: int, dest: str) -> None:
        """Stream a library file to `dest` on disk (needs the library_download permission)."""
        async with self._stream(f"/books/files/{file_id}/download", timeout=httpx.Timeout(300.0, connect=10.0)) as r:
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)

    # -- email ---------------------------------------------------------------

    async def recipients(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/email/recipients")

    async def create_recipient(self, **fields: Any) -> dict[str, Any]:
        """fields: name, email, deviceType (kindle|kobo|other). Recipients belong to the bot's BookOrbit user."""
        return await self._request("POST", "/email/recipients", json=fields)

    async def update_recipient(self, recipient_id: int, **fields: Any) -> dict[str, Any]:
        return await self._request("PUT", f"/email/recipients/{recipient_id}", json=fields)

    async def providers(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/email/providers")

    async def send_email(self, book_id: int, recipient_id: int, provider_id: int) -> dict[str, Any]:
        # providerId is explicit: BookOrbit only falls back to the user's preference, not the provider's default flag
        return await self._request(
            "POST", "/email/send", json={"bookIds": [book_id], "recipientIds": [recipient_id], "providerId": provider_id}
        )


def _error_message(r: httpx.Response) -> str:
    try:
        msg = r.json().get("message", r.text)
    except Exception:
        msg = r.text
    return "; ".join(msg) if isinstance(msg, list) else str(msg)
