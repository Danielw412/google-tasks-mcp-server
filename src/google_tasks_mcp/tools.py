"""MCP tools: schemas, annotations, and structured errors.

Tools stay thin; the work happens in ``service`` (tasks) and ``oauth`` (Google
sign-in). Every expected failure becomes an ``isError`` result whose structured
content is ``{"error": {"code", "message", "action"}}``, so ChatGPT and the app
UI can react without parsing prose.
"""

# No `from __future__ import annotations`: the MCP SDK reads these signatures at runtime.

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from . import task_behavior as behavior
from .config import SERVER_NAME
from .errors import ServiceError
from .models import (
    AuthStatus,
    ConnectInfo,
    DeleteResult,
    DisconnectResult,
    GoogleResult,
    PingResult,
    TaskListResult,
    TaskListsResult,
    TaskResult,
    TasksResult,
)
from .oauth import iso
from .service import TasksService

T = TypeVar("T")
logger = logging.getLogger(__name__)

# Annotation presets. ChatGPT asks the user to confirm any tool that is not read-only,
# and asks for explicit approval when destructiveHint is true.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
WRITE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


def ui_meta(invoking: str, invoked: str) -> dict[str, Any]:
    """Tool _meta: callable by both the model and the app UI, plus ChatGPT status text."""
    return {
        "ui": {"visibility": ["model", "app"]},
        "openai/widgetAccessible": True,
        "openai/toolInvocation/invoking": invoking,
        "openai/toolInvocation/invoked": invoked,
    }


def error_result(error: ServiceError) -> CallToolResult:
    payload = {"error": error.to_dict()}
    return CallToolResult(
        content=[TextContent(text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=True,
    )


def guarded(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Turn domain errors into structured error results (the SDK skips output validation for those)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ServiceError as exc:
            # Code only: messages can quote task titles, which are user data.
            logger.info("Tool %s returned error %s", fn.__name__, exc.code)
            return error_result(exc)

    return wrapper


TaskListArg = Annotated[
    str | None,
    Field(description="Task list title (case-insensitive) or id. Omit to use the default list."),
]
TaskIdArg = Annotated[str, Field(description="The task's id, as returned by list_tasks or create_task.")]
TaskListHintArg = Annotated[
    str | None,
    Field(description="List title or id containing the task. Optional, but saves a search across lists."),
]


def connect_instructions(svc: TasksService) -> str:
    settings = svc.settings
    if settings.redirect_is_loopback:
        return (
            "Open the link and allow access to Google Tasks. Google then redirects to "
            f"{settings.redirect_uri}, which only loads in a browser on the server itself or through "
            f"an SSH tunnel (ssh -L {settings.port}:127.0.0.1:{settings.port} <server>). If the "
            "browser instead shows a connection error, copy that page's full URL and run "
            "`google-tasks-mcp auth --redirect-url '<URL>'` on the server. The link expires in 10 minutes."
        )
    return (
        "Open the link and allow access to Google Tasks. A confirmation page appears when it is "
        "done. The link expires in 10 minutes."
    )


def register_tools(mcp: MCPServer, svc: TasksService) -> None:
    auth = svc.auth

    def google_tool(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """``guarded``, plus an auth_notice on the result while the fallback authorization is in use."""

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await fn(*args, **kwargs)
            if isinstance(result, GoogleResult):
                result.auth_notice = auth.notice()
            return result

        return guarded(wrapper)

    @mcp.tool(name="ping", title="Ping", annotations=READ_ONLY)
    def ping(message: Annotated[str, Field(description="Text to echo back.")] = "hello") -> PingResult:
        """Echo a message to check that the MCP connection works. Does not contact Google."""
        return PingResult(success=True, message=message, server=SERVER_NAME, version=__version__)

    # Google account -------------------------------------------------------------

    @mcp.tool(
        name="auth_status",
        title="Google connection status",
        annotations=READ_ONLY,
        meta=ui_meta("Checking Google connection…", "Checked Google connection"),
    )
    async def auth_status() -> AuthStatus:
        """Report whether Google Tasks is connected, and what to do if not. Never returns credentials."""
        return auth.status()

    @mcp.tool(
        name="auth_connect",
        title="Connect Google Tasks",
        annotations=READ_ONLY,
        meta=ui_meta("Preparing Google sign-in…", "Google sign-in link ready"),
    )
    @guarded
    async def auth_connect() -> ConnectInfo:
        """Create a Google sign-in link that connects (or reconnects) this server to Google Tasks.

        Use when another tool reports google_not_connected or google_reauth_required, or when the
        user asks to connect or switch accounts. Show the authorization_url to the user as a link
        and relay the instructions. Nothing changes until the user approves in the browser.
        """
        request = auth.start_authorization()
        return ConnectInfo(
            authorization_url=request.url,
            expires_at=iso(request.expires_at),
            redirect_uri=request.redirect_uri,
            instructions=connect_instructions(svc),
        )

    @mcp.tool(
        name="auth_disconnect",
        title="Disconnect Google Tasks",
        annotations=DESTRUCTIVE,
        meta=ui_meta("Disconnecting Google…", "Google disconnected"),
    )
    @guarded
    async def auth_disconnect() -> DisconnectResult:
        """Revoke this server's Google access and delete the stored credentials.

        Only when the user explicitly asks to disconnect or sign out of Google Tasks.
        """
        return await auth.disconnect()

    # Task lists -------------------------------------------------------------------

    @mcp.tool(
        name="list_task_lists",
        title="List task lists",
        annotations=READ_ONLY,
        meta=ui_meta("Loading task lists…", "Loaded task lists"),
    )
    @google_tool
    async def list_task_lists() -> TaskListsResult:
        """List the user's Google Tasks lists. The default list is marked is_default."""
        return await svc.list_task_lists()

    @mcp.tool(name="create_task_list", title="Create task list", annotations=WRITE)
    @google_tool
    async def create_task_list(
        title: Annotated[str, Field(description="Name of the new list.", max_length=1024)],
    ) -> TaskListResult:
        """Create a new task list."""
        return await svc.create_task_list(title)

    @mcp.tool(name="rename_task_list", title="Rename task list", annotations=WRITE_IDEMPOTENT)
    @google_tool
    async def rename_task_list(
        task_list: Annotated[str, Field(description="Current list title or id.")],
        new_title: Annotated[str, Field(description="New name for the list.", max_length=1024)],
    ) -> TaskListResult:
        """Rename a task list."""
        return await svc.rename_task_list(task_list, new_title)

    @mcp.tool(name="delete_task_list", title="Delete task list", annotations=DESTRUCTIVE)
    @google_tool
    async def delete_task_list(
        task_list: Annotated[str, Field(description="Title or id of the list to delete.")],
    ) -> DeleteResult:
        """Permanently delete a task list and every task in it. The default list cannot be deleted.

        Only when the user explicitly asks to delete that list.
        """
        return await svc.delete_task_list(task_list)

    # Tasks ----------------------------------------------------------------------------

    @mcp.tool(
        name="list_tasks",
        title="List tasks",
        annotations=READ_ONLY,
        meta=ui_meta("Loading tasks…", "Loaded tasks"),
    )
    @google_tool
    async def list_tasks(
        task_list: TaskListArg = None,
        all_lists: Annotated[bool, Field(description="Read every list instead of one.")] = False,
        include_completed: Annotated[bool, Field(description="Also return completed tasks.")] = False,
        query: Annotated[
            str | None, Field(description="Only tasks whose title or notes contain this text.")
        ] = None,
        due_before: Annotated[
            str | None,
            Field(
                description="Only tasks due on or before this date (YYYY-MM-DD or a phrase like 'friday')."
            ),
        ] = None,
        due_after: Annotated[
            str | None, Field(description="Only tasks due on or after this date (YYYY-MM-DD or a phrase).")
        ] = None,
        max_results: Annotated[int, Field(ge=1, le=500, description="Maximum tasks to return.")] = 100,
    ) -> TasksResult:
        """List tasks, open ones only by default, in Google Tasks order (subtasks follow their parent).

        Use this to find a task's id before updating, completing, moving, or deleting it. Set
        all_lists=true with query to search everywhere. Due dates are YYYY-MM-DD; `today` in the result
        is the user's current date.
        """
        return await svc.list_tasks(
            task_list,
            all_lists=all_lists,
            include_completed=include_completed,
            query=query,
            due_before=due_before,
            due_after=due_after,
            max_results=max_results,
        )

    @mcp.tool(name="get_task", title="Get task", annotations=READ_ONLY)
    @google_tool
    async def get_task(task_id: TaskIdArg, task_list: TaskListHintArg = None) -> TaskResult:
        """Get one task by id, including its notes and links."""
        return await svc.get_task(task_id, task_list)

    @mcp.tool(
        name="create_task",
        title="Create task",
        description=(
            "Create a task in Google Tasks. Returns the created task with its resolved due date and list.\n\n"
            + behavior.CREATION_GUIDANCE
        ),
        annotations=WRITE,
        meta=ui_meta("Adding task…", "Task added"),
    )
    @google_tool
    async def create_task(
        title: Annotated[str, Field(description="Short task title, e.g. 'Finish physics homework'.")],
        notes: Annotated[str | None, Field(description="Optional details.", max_length=8192)] = None,
        due: Annotated[
            str | None,
            Field(
                description="Optional due date: YYYY-MM-DD or a phrase such as 'today', 'tomorrow', "
                "'friday', 'next week', 'in 3 days', 'oct 12'. Resolved in the user's timezone."
            ),
        ] = None,
        task_list: TaskListArg = None,
        parent_task_id: Annotated[
            str | None, Field(description="Create as a subtask of this task id (same list).")
        ] = None,
    ) -> TaskResult:
        return await svc.create_task(
            title, notes=notes, due=due, task_list=task_list, parent_task_id=parent_task_id
        )

    @mcp.tool(
        name="update_task",
        title="Update task",
        annotations=WRITE_IDEMPOTENT,
        meta=ui_meta("Updating task…", "Task updated"),
    )
    @google_tool
    async def update_task(
        task_id: TaskIdArg,
        task_list: TaskListHintArg = None,
        title: Annotated[str | None, Field(description="New title. Omit to keep.")] = None,
        notes: Annotated[
            str | None,
            Field(description="New notes; empty string clears them. Omit to keep.", max_length=8192),
        ] = None,
        due: Annotated[
            str | None,
            Field(
                description="New due date (YYYY-MM-DD or phrase); 'none' or empty clears it. Omit to keep."
            ),
        ] = None,
    ) -> TaskResult:
        """Change a task's title, notes, or due date. Only the fields you pass are changed.

        To complete or reopen a task use complete_task; to reorder or change lists use move_task.
        """
        return await svc.update_task(task_id, task_list=task_list, title=title, notes=notes, due=due)

    @mcp.tool(
        name="complete_task",
        title="Complete or reopen task",
        annotations=WRITE_IDEMPOTENT,
        meta=ui_meta("Updating task…", "Task updated"),
    )
    @google_tool
    async def complete_task(
        task_id: TaskIdArg,
        task_list: TaskListHintArg = None,
        completed: Annotated[
            bool, Field(description="true marks the task done; false reopens (uncompletes) it.")
        ] = True,
    ) -> TaskResult:
        """Mark a task as completed, or reopen it with completed=false."""
        return await svc.set_completed(task_id, task_list=task_list, completed=completed)

    @mcp.tool(
        name="delete_task",
        title="Delete task",
        annotations=DESTRUCTIVE,
        meta=ui_meta("Deleting task…", "Task deleted"),
    )
    @google_tool
    async def delete_task(task_id: TaskIdArg, task_list: TaskListHintArg = None) -> DeleteResult:
        """Permanently delete a task (and its subtasks).

        Only when the user clearly asked to delete it; to finish a task use complete_task instead.
        """
        return await svc.delete_task(task_id, task_list=task_list)

    @mcp.tool(name="move_task", title="Move or reorder task", annotations=WRITE_IDEMPOTENT)
    @google_tool
    async def move_task(
        task_id: TaskIdArg,
        task_list: TaskListHintArg = None,
        destination_task_list: Annotated[
            str | None, Field(description="Move to this list (title or id). Omit to stay in the same list.")
        ] = None,
        parent_task_id: Annotated[
            str | None, Field(description="Make it a subtask of this task id. Omit for a top-level task.")
        ] = None,
        after_task_id: Annotated[
            str | None,
            Field(description="Place it right after this sibling task id. Omit to place it first."),
        ] = None,
    ) -> TaskResult:
        """Reorder a task, nest or un-nest it, or move it to another list.

        The task ends up under parent_task_id (top level when omitted) right after after_task_id (first
        when omitted). Recurring tasks cannot move between lists.
        """
        return await svc.move_task(
            task_id,
            task_list=task_list,
            destination_task_list=destination_task_list,
            parent_task_id=parent_task_id,
            after_task_id=after_task_id,
        )


SERVER_INSTRUCTIONS = (
    """\
Manage the user's Google Tasks. All tools act on the user's real Google account.
- If a tool returns error code google_not_connected or google_reauth_required, call auth_connect and \
show the user the sign-in link with its instructions (or suggest the Connect button in the Google Tasks app).
- If a result includes auth_notice, the request still worked through a fallback authorization. Do not \
interrupt the user's request for it: finish the whole request first, then at the end of your reply tell \
the user once that the connector's own Google sign-in needs renewing, call auth_connect, and show the link.
- Find task ids with list_tasks (use all_lists=true with query to search everywhere). Pass task_list \
when you know it.
- Use complete_task (completed=false to reopen) rather than deleting finished tasks. Only delete when \
the user clearly asks.
- open_tasks_app shows an interactive task manager; use it only when the user wants to browse or manage \
tasks visually.

"""
    + behavior.CREATION_GUIDANCE
)
