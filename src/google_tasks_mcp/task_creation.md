# Task creation guide

Follow this guide without checking existing tasks for their style. Explicit
user instructions take precedence. Examples show wording, not tasks to recreate.

## Dates

The server enforces this TOML block when no date is supplied. List names ignore case.

```toml
default_due = "today"

[lists.Jira]
default_due = "none"

[lists.SCC]
default_due = "none"
```

Use the supplied deadline, assessment date, or event date. Otherwise omit `due`:
today for every list except Jira/SCC, which remain undated. This includes undated
homework/tests; do not invent a next-class deadline. An explicit date applies to
Jira/SCC too. For an explicitly undated task, pass `due="none"`. Use a supported
phrase or YYYY-MM-DD in the returned user timezone. Put supplied times in notes.
Ask about conflicting dates; a missing date alone needs no question.

## Lists

Use an explicitly requested existing list; otherwise:

| List | Route here | Title style |
| --- | --- | --- |
| School | Coursework, reading, projects, studying, test prep/corrections; school/club obligations, dues, forms, college applications, debate prep/deadlines | Coursework: `[COURSE] Short title`. Other obligations: concise, usually lowercase, without a course tag. |
| Tests | Actual quizzes, tests, exams, midterms, finals | `[COURSE] Topic/section Quiz`, `Test`, or `Exam`, using the source's assessment word. |
| Events/Misc | Events, tournaments, holidays, school-calendar milestones, personal errands, other miscellaneous work | Short event name or specific action. Default when no other category fits. |
| Jira | Software/website changes, bugs, project improvements, including SCC code work | `Project: brief action`, when the project is known. |
| SCC | Steel City Codes outreach, enrollment, communications, volunteering, operations | Short operational action; no `SCC:` prefix. |

Debate dues/essay deadlines and preparation belong in School; tournaments belong
in Events/Misc. SCC website/code changes belong in Jira with `SCC:`; outreach and
operations belong in SCC. A course project belongs in School, not Jira. Use only
these lists unless the user adds another. Do not recreate deleted lists.

## Concise, slightly more specific titles

Usually aim for 3–7 words, excluding a course/project prefix. Add one useful
identifier from the request or established context: recipient, purpose, topic,
project, assignment number, or deliverable. Prefer an action plus its object
for work; events and assessments can remain noun phrases. Preserve proper names,
acronyms, and essential ranges. Do not pad an already clear title or force a word
count. No quotes, trailing period, priority markers, or redundant dates.

For example, when the added details are supplied:

- `email school` → `email school about SCC registration`
- `more outreach` → `contact schools about SCC classes`
- `send sign up form` → `send SCC registration form to volunteers`
- `IDE: fix url` → `IDE: fix Cloudflare preview URL`
- `debate dues due` can stay as written; add an amount only if supplied.

Do not invent a school name, audience, purpose, amount, or requirement. If the
request lacks extra context, keep the task short; do not ask just to embellish it.

## Coursework and assessments

Tags: English/literature = `ENGLISH`; physics = `PHYSICS`; Linear Algebra =
`LINALG`; chemistry = `CHEM`; Computer Programming A = `CPP`; psychology =
`PSYCH`. Tags are uppercase; title text uses sentence case, preserving names
and acronyms. Use known course context; not every programming task is CPP.
For an unfamiliar course, use the user's tag or clarify when needed.

Retain assignment names and unit/module/problem numbers. Homework titles can
be actions or noun phrases: `[ENGLISH] Submit prose essay`,
`[CHEM] Unit 4 thermochemistry packet`, `[LINALG] Pearson assignment`.
Actual assessments go in Tests: `[PHYSICS] Unit 2 MCQ Exam`.
Studying/preparation/corrections stay in School: `[PSYCH] Review research study guide`.
Create separate tasks for distinct dated assessment sections; do not add study
or preparation tasks unless requested.

## Descriptions

Tell me what to do and briefly how: use a template, follow a submission method,
or complete a necessary step. Usually 1–2 short sentences. Include extra context
ONLY when it helps complete the task; leave notes empty if the title is enough.
Useful links are welcome. Keep only supplied facts; do not invent requirements.
Omit source attribution, email subjects/dates, quotations, background summaries,
sync metadata, and explanations of where the information came from. Do not
repeat the deadline already stored on the task.

Example: `Submit PJAS research plan` — "Complete the research plan using the
template in the slides, then submit it." Add the slides link, without describing
the source email or meeting history.

When extracting supplied school material, keep one task per distinct assigned
action. A dated agenda row can date an assessment. Combine repeated mentions
of the same assignment. Skip learning targets, teacher narration, ordinary
in-class practice without a deadline, and general make-up policies. Do not
import an entire agenda when asked for one task.

## Tool use and guide edits

Before each `create_task`, load `get_task_creation_instructions`, follow its
live content, and pass `revision` as `instructions_revision`. Use the returned
`today`/`timezone`. Call once per distinct task with the chosen list; create
subtasks only when requested or clearly intended. Confirm the returned title,
list/date, and warnings briefly. Use `list_task_lists` only to resolve unfamiliar
or missing lists; do not inspect other tasks to rediscover formatting.

The read tool returns the active editable Markdown path. Direct user edits
apply on the next creation without a restart. For agent edits, read the guide,
preview complete replacement Markdown with `propose_task_creation_instructions`
and its current revision, show the diff, and obtain explicit user approval.
Only then call `update_task_creation_instructions` with that proposal ID and
`user_approved=true`. A stale proposal needs a fresh approved diff. Creating
tasks/lists or observing preferences does not authorize changing this guide.

For a future list, add a routing/style row and any necessary examples here.
It defaults to today; to override, add `[lists."List name"]` and
`default_due = "none"` (or a supported date phrase) to the TOML block. Do not
create/rename lists or update these rules without the user's request/approval.
