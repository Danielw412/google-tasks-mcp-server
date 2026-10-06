"""Thin async client for the Google Tasks REST API (v1).

Maps HTTP failures to domain errors, retries one 401 after forcing a token
refresh, and retries transient 429/5xx responses briefly. Returns Google's
JSON resources unchanged; ``service`` converts them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx2

from .errors import GoogleApiError, GoogleUnavailable, InvalidArgument, NotFound, ReauthRequired
from .oauth import GoogleAuth

logger = logging.getLogger(__name__)

API_BASE = "https://tasks.googleapis.com/tasks/v1"
PAGE_SIZE = 100
MAX_TASKS_PER_LIST = 1000
RETRY_DELAYS = (0.5, 1.5)


def _google_error(response: httpx2.Response) -> tuple[str, str]:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return "", ""
    if not isinstance(error, dict):
        return str(error), ""
    reasons = [d.get("reason", "") for d in error.get("errors", []) if isinstance(d, dict)]
    reasons += [d.get("reason", "") for d in error.get("details", []) if isinstance(d, dict)]
    return " ".join(r for r in reasons if r), str(error.get("message", ""))[:300]


def _seg(value: str) -> str:
    return quote(value, safe="@")


class GoogleTasksClient:
    def __init__(self, auth: GoogleAuth, http: httpx2.AsyncClient) -> None:
        self.auth = auth
        self._http = http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        token = await self.auth.access_token()
        refreshed = False
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    method,
                    API_BASE + path,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx2.HTTPError as exc:
                if attempt < len(RETRY_DELAYS) and method in ("GET", "PATCH", "DELETE"):
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raise GoogleUnavailable(f"Could not reach Google Tasks ({type(exc).__name__}).") from None

            status = response.status_code
            if status in (200, 201):
                return response.json()
            if status == 204:
                return None
            reason, message = _google_error(response)
            reasons = reason.lower()
            if status == 404 and method == "DELETE" and attempt > 0:
                return None  # An earlier attempt went through before the retry.
            if status == 401:
                if not refreshed:
                    token = await self.auth.access_token(force_refresh=True)
                    refreshed = True
                    continue
                raise ReauthRequired("Google no longer accepts the saved authorization.")
            rate_limited = status == 429 or (
                status == 403 and ("ratelimitexceeded" in reasons or "rate_limit_exceeded" in reasons)
            )
            if rate_limited or status in (500, 502, 503, 504):
                # A POST that failed server-side may still have been applied; don't risk duplicates.
                if attempt < len(RETRY_DELAYS) and (rate_limited or method != "POST"):
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raise GoogleUnavailable(f"Google Tasks is busy or unavailable (HTTP {status}).")
            if status == 403:
                if "scope_insufficient" in reasons or "insufficientpermissions" in reasons:
                    raise ReauthRequired("The saved authorization does not allow Google Tasks access.")
                if "service_disabled" in reasons or "accessnotconfigured" in reasons:
                    raise GoogleApiError(
                        "The Google Tasks API is not enabled for this OAuth client's Cloud project.",
                        code="tasks_api_disabled",
                        action="Enable the Google Tasks API in the Google Cloud console, then retry.",
                    )
                raise GoogleApiError(f"Google denied the request: {message or reason or 'forbidden'}")
            if status == 404:
                raise NotFound("Google Tasks could not find that task or list.")
            if status == 400:
                raise InvalidArgument(f"Google rejected the request: {message or reason or 'bad request'}")
            raise GoogleApiError(f"Google Tasks returned HTTP {status}: {message or reason}".rstrip(": "))

    async def _paginate(self, path: str, params: dict[str, Any], limit: int) -> tuple[list[dict], bool]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            query = {**params, "maxResults": PAGE_SIZE}
            if page_token:
                query["pageToken"] = page_token
            data = await self._request("GET", path, params=query) or {}
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return items, False
            if len(items) >= limit:
                return items, True

    # Task lists ---------------------------------------------------------------

    async def list_tasklists(self) -> list[dict[str, Any]]:
        items, _ = await self._paginate("/users/@me/lists", {}, limit=1000)
        return items

    async def get_tasklist(self, tasklist_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/users/@me/lists/{_seg(tasklist_id)}") or {}

    async def insert_tasklist(self, title: str) -> dict[str, Any]:
        return await self._request("POST", "/users/@me/lists", json={"title": title}) or {}

    async def patch_tasklist(self, tasklist_id: str, title: str) -> dict[str, Any]:
        return (
            await self._request("PATCH", f"/users/@me/lists/{_seg(tasklist_id)}", json={"title": title}) or {}
        )

    async def delete_tasklist(self, tasklist_id: str) -> None:
        await self._request("DELETE", f"/users/@me/lists/{_seg(tasklist_id)}")

    # Tasks --------------------------------------------------------------------

    async def list_tasks(
        self, tasklist_id: str, *, show_completed: bool, limit: int | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        params: dict[str, Any] = {"showCompleted": str(show_completed).lower()}
        if show_completed:
            # Tasks completed in Google's own apps are also marked hidden.
            params["showHidden"] = "true"
        return await self._paginate(f"/lists/{_seg(tasklist_id)}/tasks", params, limit or MAX_TASKS_PER_LIST)

    async def get_task(self, tasklist_id: str, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/lists/{_seg(tasklist_id)}/tasks/{_seg(task_id)}") or {}

    async def insert_task(
        self, tasklist_id: str, body: dict[str, Any], *, parent: str | None = None
    ) -> dict[str, Any]:
        params = {"parent": parent} if parent else None
        return (
            await self._request("POST", f"/lists/{_seg(tasklist_id)}/tasks", params=params, json=body) or {}
        )

    async def patch_task(self, tasklist_id: str, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        path = f"/lists/{_seg(tasklist_id)}/tasks/{_seg(task_id)}"
        return await self._request("PATCH", path, json=body) or {}

    async def delete_task(self, tasklist_id: str, task_id: str) -> None:
        await self._request("DELETE", f"/lists/{_seg(tasklist_id)}/tasks/{_seg(task_id)}")

    async def move_task(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        parent: str | None = None,
        previous: str | None = None,
        destination_tasklist: str | None = None,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in (
                ("parent", parent),
                ("previous", previous),
                ("destinationTasklist", destination_tasklist),
            )
            if value
        }
        path = f"/lists/{_seg(tasklist_id)}/tasks/{_seg(task_id)}/move"
        return await self._request("POST", path, params=params or None) or {}
