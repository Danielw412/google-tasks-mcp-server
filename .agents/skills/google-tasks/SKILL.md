---
name: google-tasks
description: Create and manage tasks with this Google Tasks MCP server, or propose approved edits to its task-creation guide. Applies to requests to add Google Tasks, reminders, homework, assessments, and list-specific task rules.
---

Use `get_task_creation_instructions` immediately before every `create_task`
call. Its live Markdown is the maintained source for list routing, course
tags, titles, notes, and date defaults; do not inspect other tasks to relearn
these conventions. Pass the returned revision as `instructions_revision`.
If creation reports stale/missing instructions, read the guide again and
rebuild the task before retrying. Use returned `today` and `timezone`.

The bundled starting guide is
[task_creation.md](../../../src/google_tasks_mcp/task_creation.md).
The read tool reports the active file and writable path. User-approved edits
are saved in that separate Markdown file and apply without a server restart.

For instruction changes, read the current guide and prepare the complete
replacement Markdown. Call `propose_task_creation_instructions` with its
revision, show the returned diff, and ask the user to approve the exact
changes. Only after explicit approval call `update_task_creation_instructions`
with that proposal ID and `user_approved=true`. If the guide changed, obtain
approval for a fresh proposal. Creating a task/list does not authorize guide
edits; never infer new rules from observed tasks or silently maintain this file.

Use ordinary lookup tools when locating an existing task for updates, moves,
completion, or deletion. The creation guide replaces repeated formatting
research, not the lookup needed to identify an existing task.
