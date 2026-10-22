"""Pydantic models for tool results (they also become the tools' output schemas)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AuthState = Literal["not_configured", "disconnected", "connected", "reauth_required"]


class AuthStatus(BaseModel):
    state: AuthState = Field(description="Overall Google connection state.")
    configured: bool = Field(description="Whether the server has a Google OAuth client configured.")
    connected: bool = Field(description="Whether Google Tasks calls can be made right now.")
    message: str
    action: str | None = Field(None, description="What to do next, when something needs fixing.")
    scopes: list[str] = Field(default_factory=list, description="Google permissions granted to this server.")
    connected_at: str | None = None
    last_refreshed_at: str | None = None
    authorization_expires_at: str | None = Field(
        None,
        description="When Google will stop honouring the saved authorization (only set for consent "
        "screens in Testing mode, which issue 7-day refresh tokens).",
    )
    redirect_uri: str | None = Field(None, description="Where Google sends the browser after sign-in.")
    client_type: str | None = None
    fallback_active: bool = Field(
        False,
        description="True when this server's own sign-in is unusable and calls use the fallback "
        "Google authorization instead. `state` then describes the server's own sign-in.",
    )


class AuthNotice(BaseModel):
    """Attached to task results while the fallback authorization is in use."""

    code: Literal["google_native_auth_expired"] = "google_native_auth_expired"
    message: str = Field(description="What to tell the user.")
    instructions: str = Field(description="When and how to tell the user.")


class GoogleResult(BaseModel):
    auth_notice: AuthNotice | None = Field(
        None,
        description="Set when this server's own Google sign-in expired and the request used a fallback "
        "authorization. Finish the user's request first, then follow its instructions.",
    )


class ConnectInfo(BaseModel):
    authorization_url: str = Field(description="Open this Google sign-in link in a browser.")
    expires_at: str = Field(description="The link stops working after this time.")
    redirect_uri: str
    instructions: str


class DisconnectResult(BaseModel):
    disconnected: bool = Field(description="True when stored Google credentials were removed.")
    revoked: bool = Field(description="True when Google confirmed the authorization was revoked.")
    message: str


class TaskList(BaseModel):
    id: str
    title: str
    updated: str | None = None
    is_default: bool = False


class TaskLink(BaseModel):
    type: str | None = None
    description: str | None = None
    link: str


class Task(BaseModel):
    id: str
    title: str
    notes: str | None = None
    status: Literal["needsAction", "completed"]
    completed: bool
    due: str | None = Field(None, description="Due date as YYYY-MM-DD (Google Tasks stores no time of day).")
    completed_at: str | None = None
    updated: str | None = None
    parent: str | None = Field(None, description="Parent task id when this is a subtask.")
    position: str | None = None
    task_list_id: str
    task_list_title: str | None = None
    web_link: str | None = None
    links: list[TaskLink] = Field(default_factory=list)


class TaskListsResult(GoogleResult):
    task_lists: list[TaskList]
    default_task_list_id: str | None = None


class TaskListResult(GoogleResult):
    task_list: TaskList
    message: str


class TasksResult(GoogleResult):
    task_list: TaskList | None = Field(None, description="The list that was read (null when all_lists=true).")
    tasks: list[Task]
    count: int
    truncated: bool = Field(description="True when more tasks matched than max_results.")
    today: str = Field(description="Today's date in the server's timezone, for interpreting due dates.")


class TaskResult(GoogleResult):
    task: Task
    task_list: TaskList
    message: str
    warnings: list[str] = Field(default_factory=list)


class DeleteResult(GoogleResult):
    deleted: bool
    id: str
    message: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    action: str | None = None


class AppState(BaseModel):
    """Everything the Tasks app UI needs to render in one call."""

    auth: AuthStatus
    task_lists: list[TaskList] = Field(default_factory=list)
    selected_task_list: TaskList | None = None
    tasks: list[Task] = Field(default_factory=list)
    include_completed: bool = False
    today: str
    timezone: str
    error: ErrorInfo | None = None


class PingResult(BaseModel):
    success: bool
    message: str
    server: str
    version: str


class TaskInstructionsResult(BaseModel):
    content: str = Field(description="Live Markdown guide to follow before creating a task.")
    revision: str = Field(description="Pass as instructions_revision to create_task.")
    source_path: str = Field(description="Active Markdown file; can be edited directly by the user.")
    writable_path: str = Field(description="Where approved MCP edits are saved.")
    today: str
    timezone: str


class TaskInstructionsProposalResult(BaseModel):
    proposal_id: str
    diff: str = Field(description="Show this exact diff to the user for approval before updating.")
    content: str
    message: str


class TaskInstructionsUpdateResult(BaseModel):
    revision: str
    source_path: str
    message: str
