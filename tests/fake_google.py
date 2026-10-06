"""An in-memory stand-in for Google's OAuth and Tasks endpoints, served via httpx2.MockTransport."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

CLIENT_ID = "1234567890-testclient.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret-value"
# A second OAuth client, standing in for another app (Canvas Task Sync) whose token is the fallback.
OTHER_CLIENT_ID = "1234567890-otherapp.apps.googleusercontent.com"
OTHER_CLIENT_SECRET = "GOCSPX-other-secret-value"
CLIENTS = {CLIENT_ID: CLIENT_SECRET, OTHER_CLIENT_ID: OTHER_CLIENT_SECRET}
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"


@dataclass
class FakeGoogle:
    access_tokens: set[str] = field(default_factory=set)
    refresh_tokens: set[str] = field(default_factory=set)
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    revoked: list[str] = field(default_factory=list)
    lists: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)
    grant_scope: str = TASKS_SCOPE
    refresh_token_expires_in: int | None = None
    fail_next_api_with: int | None = None
    fail_reason: str | None = None
    fail_after_applying: tuple[str, int] | None = None  # (HTTP method, status)
    default_id: str = "default-list"
    revoke_unreachable: bool = False
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        self.add_list("My Tasks", list_id="default-list")

    # Helpers for tests ---------------------------------------------------------

    def add_list(self, title: str, list_id: str | None = None) -> str:
        list_id = list_id or f"list-{next(self._ids)}"
        self.lists[list_id] = {
            "kind": "tasks#taskList",
            "id": list_id,
            "title": title,
            "updated": "2026-10-01T00:00:00Z",
        }
        self.tasks[list_id] = []
        return list_id

    def add_task(self, list_id: str, title: str, **extra: Any) -> dict[str, Any]:
        task = {
            "kind": "tasks#task",
            "id": f"task-{next(self._ids)}",
            "title": title,
            "status": "needsAction",
            "updated": "2026-10-01T00:00:00Z",
            **extra,
        }
        self.tasks[list_id].insert(0, task)
        self._renumber(list_id)
        return task

    def consent(self, authorization_url: str, *, scope: str | None = None) -> str:
        """Simulate the user approving access; returns the redirect URL Google would send the browser to."""
        params = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert params["client_id"] == CLIENT_ID
        assert params["code_challenge_method"] == "S256"
        code = "4/" + secrets.token_urlsafe(16)
        self.codes[code] = {
            "challenge": params["code_challenge"],
            "redirect_uri": params["redirect_uri"],
            "scope": scope if scope is not None else self.grant_scope,
        }
        answer = urlencode({"state": params["state"], "code": code, "scope": self.grant_scope})
        return f"{params['redirect_uri']}?{answer}"

    def issue_tokens(self) -> tuple[str, str]:
        """An (access, refresh) pair granted outside this server, as another app would hold."""
        access, refresh = "ya29." + secrets.token_urlsafe(24), "1//" + secrets.token_urlsafe(24)
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return access, refresh

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    def revoke(self, refresh_token: str) -> None:
        self.refresh_tokens.discard(refresh_token)

    def revoke_all(self) -> None:
        self.access_tokens.clear()
        self.refresh_tokens.clear()

    # Transport -------------------------------------------------------------------

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            return self._token(dict(parse_qs(request.content.decode())))
        if url.host == "oauth2.googleapis.com" and url.path == "/revoke":
            if self.revoke_unreachable:
                raise httpx2.ConnectError("unreachable", request=request)
            token = parse_qs(request.content.decode())["token"][0]
            self.revoked.append(token)
            self.refresh_tokens.discard(token)
            return httpx2.Response(200, json={})
        if url.host == "tasks.googleapis.com":
            return self._api(request)
        return httpx2.Response(404, json={"error": "unexpected host"})

    def _token(self, form: dict[str, list[str]]) -> httpx2.Response:
        f = {k: v[0] for k, v in form.items()}
        if CLIENTS.get(f.get("client_id", "")) != f.get("client_secret"):
            return httpx2.Response(401, json={"error": "invalid_client"})
        if f["grant_type"] == "authorization_code":
            grant = self.codes.pop(f["code"], None)
            if grant is None or grant["redirect_uri"] != f.get("redirect_uri"):
                return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})
            digest = hashlib.sha256(f["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant["challenge"]:
                return httpx2.Response(
                    400, json={"error": "invalid_grant", "error_description": "PKCE mismatch"}
                )
            access, refresh = "ya29." + secrets.token_urlsafe(24), "1//" + secrets.token_urlsafe(24)
            self.access_tokens.add(access)
            self.refresh_tokens.add(refresh)
            body: dict[str, Any] = {
                "access_token": access,
                "refresh_token": refresh,
                "expires_in": 3599,
                "scope": grant["scope"],
                "token_type": "Bearer",
            }
            if self.refresh_token_expires_in:
                body["refresh_token_expires_in"] = self.refresh_token_expires_in
            return httpx2.Response(200, json=body)
        if f["grant_type"] == "refresh_token":
            if f["refresh_token"] not in self.refresh_tokens:
                return httpx2.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "error_description": "Token has been expired or revoked.",
                    },
                )
            access = "ya29." + secrets.token_urlsafe(24)
            self.access_tokens.add(access)
            return httpx2.Response(
                200,
                json={
                    "access_token": access,
                    "expires_in": 3599,
                    "scope": self.grant_scope,
                    "token_type": "Bearer",
                },
            )
        return httpx2.Response(400, json={"error": "unsupported_grant_type"})

    # Tasks API -----------------------------------------------------------------------

    def _renumber(self, list_id: str) -> None:
        top = [t for t in self.tasks[list_id] if not t.get("parent")]
        for i, task in enumerate(top):
            task["position"] = f"{i:020d}"
        for task in self.tasks[list_id]:
            if task.get("parent"):
                siblings = [t for t in self.tasks[list_id] if t.get("parent") == task["parent"]]
                task["position"] = f"{siblings.index(task):020d}"

    def _find(self, list_id: str, task_id: str) -> dict[str, Any] | None:
        return next((t for t in self.tasks.get(list_id, []) if t["id"] == task_id), None)

    def _api(self, request: httpx2.Request) -> httpx2.Response:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.access_tokens:
            return httpx2.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        if self.fail_next_api_with:
            status, self.fail_next_api_with = self.fail_next_api_with, None
            errors = [{"reason": self.fail_reason}] if self.fail_reason else []
            return httpx2.Response(
                status, json={"error": {"code": status, "message": "Injected failure", "errors": errors}}
            )
        if self.fail_after_applying and self.fail_after_applying[0] == request.method:
            status, self.fail_after_applying = self.fail_after_applying[1], None
            self._api_ok(request)
            return httpx2.Response(status, json={"error": {"code": status, "message": "Injected failure"}})
        return self._api_ok(request)

    def _api_ok(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/tasks/v1")
        q = request.url.params
        method = request.method
        body = json.loads(request.content) if request.content else {}
        parts = [p for p in path.split("/") if p]

        if parts[:2] == ["users", "@me"]:
            if len(parts) == 3 and method == "GET":
                return httpx2.Response(
                    200, json={"kind": "tasks#taskLists", "items": list(self.lists.values())}
                )
            if len(parts) == 3 and method == "POST":
                new_id = self.add_list(body["title"])
                return httpx2.Response(200, json=self.lists[new_id])
            list_id = self.default_id if parts[3] == "@default" else parts[3]
            if list_id not in self.lists:
                return httpx2.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            if method == "GET":
                return httpx2.Response(200, json=self.lists[list_id])
            if method == "PATCH":
                self.lists[list_id]["title"] = body["title"]
                return httpx2.Response(200, json=self.lists[list_id])
            if method == "DELETE":
                if list_id == self.default_id:
                    return httpx2.Response(400, json={"error": {"code": 400, "message": "Invalid Value"}})
                del self.lists[list_id]
                del self.tasks[list_id]
                return httpx2.Response(204)

        if parts[0] == "lists":
            list_id = parts[1]
            if list_id not in self.lists:
                return httpx2.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            tasks = self.tasks[list_id]
            if len(parts) == 3 and method == "GET":
                show_completed = q.get("showCompleted", "true") == "true"
                items = [t for t in tasks if show_completed or t["status"] != "completed"]
                start = int(q.get("pageToken", "0"))
                end = start + int(q.get("maxResults", "20"))
                page: dict[str, Any] = {"kind": "tasks#tasks", "items": items[start:end]}
                if end < len(items):
                    page["nextPageToken"] = str(end)
                return httpx2.Response(200, json=page)
            if len(parts) == 3 and method == "POST":
                task = {
                    "kind": "tasks#task",
                    "id": f"task-{next(self._ids)}",
                    "status": "needsAction",
                    "updated": "2026-10-06T12:00:00Z",
                    **body,
                }
                if q.get("parent"):
                    if not self._find(list_id, q["parent"]):
                        return httpx2.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
                    task["parent"] = q["parent"]
                tasks.insert(0, task)
                self._renumber(list_id)
                return httpx2.Response(200, json=task)
            task = self._find(list_id, parts[3])
            if task is None:
                return httpx2.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            if len(parts) == 5 and parts[4] == "move" and method == "POST":
                dest = q.get("destinationTasklist") or list_id
                tasks.remove(task)
                task.pop("parent", None)
                if q.get("parent"):
                    task["parent"] = q["parent"]
                target = self.tasks[dest]
                index = 0
                if q.get("previous"):
                    prev = self._find(dest, q["previous"])
                    index = target.index(prev) + 1 if prev else 0
                target.insert(index, task)
                self._renumber(list_id)
                self._renumber(dest)
                return httpx2.Response(200, json=task)
            if method == "GET":
                return httpx2.Response(200, json=task)
            if method == "PATCH":
                for key, value in body.items():
                    if value is None:
                        task.pop(key, None)
                    else:
                        task[key] = value
                if body.get("status") == "completed":
                    task["completed"] = "2026-10-06T12:00:00.000Z"
                return httpx2.Response(200, json=task)
            if method == "DELETE":
                self.tasks[list_id] = [
                    t for t in tasks if t["id"] != task["id"] and t.get("parent") != task["id"]
                ]
                return httpx2.Response(204)
        return httpx2.Response(400, json={"error": {"code": 400, "message": f"Unhandled {method} {path}"}})
