"""Domain errors. Each carries a stable ``code`` and an actionable hint.

Tools turn these into structured MCP error results; nothing here ever includes
credentials in its message.
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    code = "error"
    default_action: str | None = None

    def __init__(self, message: str, *, code: str | None = None, action: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.action = action if action is not None else self.default_action

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.action:
            data["action"] = self.action
        return data


class NotConfigured(ServiceError):
    code = "google_not_configured"
    default_action = (
        "The server owner must add a Google OAuth client (see README, 'Google Cloud setup'), "
        "then run `google-tasks-mcp doctor`."
    )


class NotConnected(ServiceError):
    code = "google_not_connected"
    default_action = (
        "Call auth_connect and give the user the sign-in link, or run `google-tasks-mcp auth` on the server."
    )


class ReauthRequired(ServiceError):
    code = "google_reauth_required"
    default_action = "Reconnect Google: call auth_connect (or run `google-tasks-mcp auth` on the server)."


class AuthFlowError(ServiceError):
    code = "oauth_flow_failed"
    default_action = "Start again with auth_connect (or `google-tasks-mcp auth`)."


class NotFound(ServiceError):
    code = "not_found"


class InvalidArgument(ServiceError):
    code = "invalid_argument"


class GoogleUnavailable(ServiceError):
    code = "google_unavailable"
    default_action = "Google did not respond; try again shortly."


class GoogleApiError(ServiceError):
    code = "google_api_error"
