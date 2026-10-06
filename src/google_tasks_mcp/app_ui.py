"""The optional MCP Apps UI: one ``ui://`` HTML resource and the tools that drive it.

``open_tasks_app`` renders the UI (``_meta.ui.resourceUri``). ``tasks_app_state``
is visible only to the app (``visibility: ["app"]``) and returns everything the
UI needs in one call. The UI uses only the regular task and auth tools for
changes and never talks to Google itself, so its CSP allows no network
origins at all.
"""

# No `from __future__ import annotations`: the MCP SDK reads these signatures at runtime.

import hashlib
from importlib.resources import files
from typing import Annotated

from mcp.server.apps import Apps, ResourceCsp
from pydantic import Field

from .models import AppState
from .service import TasksService
from .tools import READ_ONLY

APP_HTML = files("google_tasks_mcp").joinpath("ui/tasks_app.html").read_text(encoding="utf-8")
# The content hash in the URI makes hosts that cache UI templates pick up changes.
APP_URI = f"ui://google-tasks/app-{hashlib.sha256(APP_HTML.encode()).hexdigest()[:12]}.html"
APP_CSP = ResourceCsp(connect_domains=[], resource_domains=[], frame_domains=[])


def build_apps(svc: TasksService) -> Apps:
    apps = Apps()

    @apps.tool(
        resource_uri=APP_URI,
        visibility=["model", "app"],
        name="open_tasks_app",
        title="Open Google Tasks app",
        annotations=READ_ONLY,
        meta={
            "openai/toolInvocation/invoking": "Opening Google Tasks…",
            "openai/toolInvocation/invoked": "Opened Google Tasks",
        },
    )
    async def open_tasks_app(
        task_list: Annotated[
            str | None, Field(description="List title or id to show first. Omit for the default list.")
        ] = None,
        include_completed: Annotated[bool, Field(description="Show completed tasks too.")] = False,
    ) -> AppState:
        """Show an interactive Google Tasks manager: connect, pick a list, add, edit, complete, delete.

        Use only when the user wants to browse or manage tasks visually. For simple requests such as
        adding one task, call create_task instead.
        """
        return await svc.app_state(task_list, include_completed=include_completed)

    @apps.tool(
        resource_uri=APP_URI,
        visibility=["app"],
        name="tasks_app_state",
        title="Google Tasks app state",
        annotations=READ_ONLY,
        meta={"openai/widgetAccessible": True},
    )
    async def tasks_app_state(
        task_list: Annotated[str | None, Field(description="List title or id; omit for the default.")] = None,
        include_completed: bool = False,
    ) -> AppState:
        """Data for the Google Tasks app UI: connection state, lists, and the selected list's tasks."""
        return await svc.app_state(task_list, include_completed=include_completed)

    apps.add_html_resource(
        APP_URI,
        APP_HTML,
        name="google-tasks-app",
        title="Google Tasks",
        description="Interactive Google Tasks manager.",
        csp=APP_CSP,
        prefers_border=True,
    )
    return apps
