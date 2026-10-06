"""Task operations shared by the MCP tools and the app UI.

Resolves task lists by title or id, applies ``task_behavior`` to new tasks, and
converts Google resources into the result models.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from . import task_behavior as behavior
from .config import Settings
from .dates import from_google_due, resolve_due, resolve_timezone, to_google_due, today_in
from .errors import InvalidArgument, NotFound, ServiceError
from .google_tasks import GoogleTasksClient
from .models import (
    AppState,
    DeleteResult,
    ErrorInfo,
    Task,
    TaskLink,
    TaskList,
    TaskListResult,
    TaskListsResult,
    TaskResult,
    TasksResult,
)
from .oauth import GoogleAuth

LISTS_CACHE_SECONDS = 30.0


def _clean_title(title: str) -> str:
    cleaned = " ".join(title.split())
    if not cleaned:
        raise InvalidArgument("The title cannot be empty.")
    return cleaned


def _order(tasks: list[Task]) -> list[Task]:
    """Google's position order, with subtasks directly under their parent."""
    by_id = {t.id: t for t in tasks}

    def key(task: Task) -> tuple[str, int, str]:
        parent = by_id.get(task.parent) if task.parent else None
        if parent is None:
            return (task.position or "", 0, "")
        return (parent.position or "", 1, task.position or "")

    return sorted(tasks, key=key)


class TasksService:
    def __init__(self, settings: Settings, auth: GoogleAuth, client: GoogleTasksClient) -> None:
        self.settings = settings
        self.auth = auth
        self.client = client
        self.tz, self.tz_name = resolve_timezone(settings.timezone)
        self._lists: tuple[float, list[TaskList]] | None = None
        self._default_list_id: str | None = None
        self._account: str | None = None

    def today(self) -> date:
        return today_in(self.tz)

    def _due(self, text: str | None) -> date | None:
        return resolve_due(text, self.today(), self.tz)

    # Task lists ----------------------------------------------------------------

    async def task_lists(self, *, fresh: bool = False) -> list[TaskList]:
        account = self.auth.account_marker()
        if account != self._account:
            # Reconnected (possibly to another Google account, possibly from the CLI): drop caches.
            self._account, self._lists, self._default_list_id = account, None, None
        if not fresh and self._lists and time.monotonic() - self._lists[0] < LISTS_CACHE_SECONDS:
            return self._lists[1]
        raw = await self.client.list_tasklists()
        if self._default_list_id is None:
            self._default_list_id = (await self.client.get_tasklist("@default")).get("id")
        lists = [
            TaskList(
                id=item["id"],
                title=item.get("title", ""),
                updated=item.get("updated"),
                is_default=item["id"] == self._default_list_id,
            )
            for item in raw
            if item.get("id")
        ]
        lists.sort(key=lambda lst: not lst.is_default)  # Stable: default first, then Google's order.
        self._lists = (time.monotonic(), lists)
        return lists

    def _forget_lists(self) -> None:
        self._lists = None

    async def resolve_list(self, ref: str | None) -> TaskList:
        """Find a list by id or (case-insensitive) title; None means the default list."""
        ref = (ref or "").strip() or behavior.DEFAULT_TASK_LIST
        for fresh in (False, True):
            lists = await self.task_lists(fresh=fresh)
            if not lists:
                raise NotFound("This Google account has no task lists.")
            if ref is None or ref.lower() in ("@default", "default"):
                return next((lst for lst in lists if lst.is_default), lists[0])
            for lst in lists:
                if lst.id == ref:
                    return lst
            matches = [lst for lst in lists if lst.title.casefold() == ref.casefold()]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise InvalidArgument(f"Several lists are named {ref!r}; pass the list id instead.")
        names = ", ".join(repr(lst.title) for lst in lists)
        raise NotFound(
            f"No task list named {ref!r}. Available lists: {names}.",
            action="Use one of the available list titles, or create_task_list to make a new one.",
        )

    async def list_task_lists(self) -> TaskListsResult:
        lists = await self.task_lists(fresh=True)
        default = next((lst.id for lst in lists if lst.is_default), None)
        return TaskListsResult(task_lists=lists, default_task_list_id=default)

    async def create_task_list(self, title: str) -> TaskListResult:
        raw = await self.client.insert_tasklist(_clean_title(title))
        self._forget_lists()
        lst = TaskList(id=raw["id"], title=raw.get("title", ""), updated=raw.get("updated"))
        return TaskListResult(task_list=lst, message=f"Created the list “{lst.title}”.")

    async def rename_task_list(self, task_list: str, new_title: str) -> TaskListResult:
        lst = await self.resolve_list(task_list)
        raw = await self.client.patch_tasklist(lst.id, _clean_title(new_title))
        self._forget_lists()
        renamed = lst.model_copy(update={"title": raw.get("title", new_title), "updated": raw.get("updated")})
        return TaskListResult(task_list=renamed, message=f"Renamed “{lst.title}” to “{renamed.title}”.")

    async def delete_task_list(self, task_list: str) -> DeleteResult:
        lst = await self.resolve_list(task_list)
        if lst.is_default:
            raise InvalidArgument("The default task list cannot be deleted.")
        await self.client.delete_tasklist(lst.id)
        self._forget_lists()
        return DeleteResult(deleted=True, id=lst.id, message=f"Deleted the list “{lst.title}” and its tasks.")

    # Tasks ------------------------------------------------------------------------

    @staticmethod
    def to_task(raw: dict[str, Any], lst: TaskList) -> Task:
        status = raw.get("status") if raw.get("status") in ("needsAction", "completed") else "needsAction"
        return Task(
            id=raw["id"],
            title=raw.get("title", ""),
            notes=raw.get("notes") or None,
            status=status,
            completed=status == "completed",
            due=from_google_due(raw.get("due")),
            completed_at=raw.get("completed"),
            updated=raw.get("updated"),
            parent=raw.get("parent"),
            position=raw.get("position"),
            task_list_id=lst.id,
            task_list_title=lst.title,
            web_link=raw.get("webViewLink"),
            links=[TaskLink(**link) for link in raw.get("links", []) if link.get("link")],
        )

    async def list_tasks(
        self,
        task_list: str | None = None,
        *,
        all_lists: bool = False,
        include_completed: bool = False,
        query: str | None = None,
        due_before: str | None = None,
        due_after: str | None = None,
        max_results: int = 100,
    ) -> TasksResult:
        before = self._due(due_before) if due_before else None
        after = self._due(due_after) if due_after else None
        needle = (query or "").strip().casefold()
        lists = await self.task_lists() if all_lists else [await self.resolve_list(task_list)]
        tasks: list[Task] = []
        truncated = False
        for lst in lists:
            raw, more = await self.client.list_tasks(lst.id, show_completed=include_completed)
            truncated |= more
            converted = [
                self.to_task(item, lst) for item in raw if item.get("id") and not item.get("deleted")
            ]
            tasks.extend(_order(converted))
        if needle:
            tasks = [t for t in tasks if needle in t.title.casefold() or needle in (t.notes or "").casefold()]
        if before:
            tasks = [t for t in tasks if t.due and t.due <= before.isoformat()]
        if after:
            tasks = [t for t in tasks if t.due and t.due >= after.isoformat()]
        truncated = truncated or len(tasks) > max_results
        tasks = tasks[:max_results]
        return TasksResult(
            task_list=None if all_lists else lists[0],
            tasks=tasks,
            count=len(tasks),
            truncated=truncated,
            today=self.today().isoformat(),
        )

    async def locate_task(self, task_id: str, task_list: str | None) -> tuple[TaskList, dict[str, Any]]:
        """Find a task by id, in ``task_list`` if given, otherwise in any list."""
        task_id = task_id.strip()
        if not task_id:
            raise InvalidArgument("task_id is required.")
        if task_list:
            candidates = [await self.resolve_list(task_list)]
        else:
            lists = await self.task_lists()
            candidates = sorted(lists, key=lambda lst: not lst.is_default)
        for lst in candidates:
            try:
                raw = await self.client.get_task(lst.id, task_id)
            except NotFound:
                continue
            if raw.get("deleted"):
                break
            return lst, raw
        where = f"in “{candidates[0].title}”" if task_list else "in any list"
        raise NotFound(
            f"No task with id {task_id!r} {where}.",
            action="Call list_tasks to find the task's current id.",
        )

    async def get_task(self, task_id: str, task_list: str | None = None) -> TaskResult:
        lst, raw = await self.locate_task(task_id, task_list)
        task = self.to_task(raw, lst)
        return TaskResult(task=task, task_list=lst, message=f"Found “{task.title}” in {lst.title}.")

    async def create_task(
        self,
        title: str,
        *,
        notes: str | None = None,
        due: str | None = None,
        task_list: str | None = None,
        parent_task_id: str | None = None,
    ) -> TaskResult:
        due_text = due if due and due.strip() else behavior.DEFAULT_DUE
        draft = behavior.NewTask(
            title=title,
            notes=notes,
            due=self._due(due_text) if due_text else None,
            task_list=(task_list or "").strip() or behavior.DEFAULT_TASK_LIST,
        )
        try:
            draft = behavior.prepare_new_task(draft)
        except ValueError as exc:
            raise InvalidArgument(str(exc)) from None
        lst = await self.resolve_list(draft.task_list)

        warnings: list[str] = []
        if draft.due and draft.due < self.today():
            warnings.append(f"The due date {draft.due.isoformat()} is in the past.")
        if behavior.WARN_ON_DUPLICATE_TITLE:
            existing, _ = await self.client.list_tasks(lst.id, show_completed=False, limit=100)
            same = [t for t in existing if t.get("title", "").casefold() == draft.title.casefold()]
            if same:
                warnings.append(
                    f"An open task with the same title already exists in {lst.title} (id {same[0]['id']})."
                )

        body: dict[str, Any] = {"title": draft.title}
        if draft.notes:
            body["notes"] = draft.notes
        if draft.due:
            body["due"] = to_google_due(draft.due)
        raw = await self.client.insert_task(lst.id, body, parent=(parent_task_id or "").strip() or None)
        task = self.to_task(raw, lst)
        message = f"Created “{task.title}” in {lst.title}" + (f", due {task.due}." if task.due else ".")
        return TaskResult(task=task, task_list=lst, message=message, warnings=warnings)

    async def update_task(
        self,
        task_id: str,
        *,
        task_list: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
    ) -> TaskResult:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = _clean_title(title)
        if notes is not None:
            body["notes"] = notes.strip() or None
        if due is not None:
            day = self._due(due)
            body["due"] = to_google_due(day) if day else None
        if not body:
            raise InvalidArgument("Nothing to update: pass title, notes, and/or due.")
        lst, _ = await self.locate_task(task_id, task_list)
        raw = await self.client.patch_task(lst.id, task_id.strip(), body)
        task = self.to_task(raw, lst)
        return TaskResult(task=task, task_list=lst, message=f"Updated “{task.title}”.")

    async def set_completed(
        self, task_id: str, *, task_list: str | None = None, completed: bool = True
    ) -> TaskResult:
        lst, _ = await self.locate_task(task_id, task_list)
        body: dict[str, Any] = (
            {"status": "completed"} if completed else {"status": "needsAction", "completed": None}
        )
        raw = await self.client.patch_task(lst.id, task_id.strip(), body)
        task = self.to_task(raw, lst)
        verb = "Completed" if completed else "Reopened"
        return TaskResult(task=task, task_list=lst, message=f"{verb} “{task.title}”.")

    async def delete_task(self, task_id: str, *, task_list: str | None = None) -> DeleteResult:
        lst, raw = await self.locate_task(task_id, task_list)
        await self.client.delete_task(lst.id, task_id.strip())
        return DeleteResult(
            deleted=True, id=task_id.strip(), message=f"Deleted “{raw.get('title', '')}” from {lst.title}."
        )

    async def move_task(
        self,
        task_id: str,
        *,
        task_list: str | None = None,
        destination_task_list: str | None = None,
        parent_task_id: str | None = None,
        after_task_id: str | None = None,
    ) -> TaskResult:
        source, _ = await self.locate_task(task_id, task_list)
        target = await self.resolve_list(destination_task_list) if destination_task_list else source
        raw = await self.client.move_task(
            source.id,
            task_id.strip(),
            parent=(parent_task_id or "").strip() or None,
            previous=(after_task_id or "").strip() or None,
            destination_tasklist=target.id if target.id != source.id else None,
        )
        task = self.to_task(raw, target)
        where = f" to {target.title}" if target.id != source.id else ""
        return TaskResult(task=task, task_list=target, message=f"Moved “{task.title}”{where}.")

    # App UI ---------------------------------------------------------------------

    async def app_state(self, task_list: str | None = None, *, include_completed: bool = False) -> AppState:
        """Snapshot for the UI. Never raises: problems are reported in ``error``/``auth``."""
        state = AppState(
            auth=self.auth.status(),
            include_completed=include_completed,
            today=self.today().isoformat(),
            timezone=self.tz_name,
        )
        if not state.auth.connected:
            return state
        try:
            state.task_lists = await self.task_lists(fresh=True)
            try:
                state.selected_task_list = await self.resolve_list(task_list)
            except NotFound as exc:
                state.error = ErrorInfo(**exc.to_dict())
                state.selected_task_list = await self.resolve_list(None)
            result = await self.list_tasks(
                state.selected_task_list.id, include_completed=include_completed, max_results=500
            )
            state.tasks = result.tasks
        except ServiceError as exc:
            state.error = ErrorInfo(**exc.to_dict())
            state.auth = self.auth.status()
        return state
