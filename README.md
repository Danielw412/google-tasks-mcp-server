# Google Tasks MCP server

A personal [MCP](https://modelcontextprotocol.io) server that lets ChatGPT read and manage your
Google Tasks. It includes an optional interactive task manager built on the MCP Apps UI standard.
It runs on your own machine on `127.0.0.1:8765` and reaches ChatGPT only through the OpenAI
Secure MCP Tunnel.

```
ChatGPT ──(OpenAI Secure MCP Tunnel: outbound only, profile google-tasks-test)──┐
                                                                                ▼
                         google-tasks-mcp on 127.0.0.1:8765
                           /mcp                     MCP Streamable HTTP (stateless JSON)
                           /healthz, /readyz        local health checks
                           /oauth/google/callback   Google sign-in redirect (browser only)
                                                                                │
                              server-side Google OAuth (tasks scope only)       ▼
                                                                       Google Tasks API
```

- **Two separate trust paths.** ChatGPT → MCP is authorized by the tunnel; this deployment
  implements no MCP-level OAuth. MCP server → Google uses ordinary server-side Google OAuth.
  Google tokens never leave the server, and they are never used to authorize ChatGPT.
- **Loopback only.** The server refuses to bind anywhere but loopback. It rejects requests
  whose `Host` header isn't loopback (DNS-rebinding guard), and `/mcp` also rejects foreign
  `Origin` headers.
- **Works without the GUI.** "Add 'finish physics homework' to my Google Tasks tomorrow" is a
  single `create_task` call. The app UI is optional.

## Contents

1. [Quick start](#quick-start)
2. [Google Cloud setup](#google-cloud-setup)
3. [Install and run the services](#install-and-run-the-services)
4. [Connect your Google account](#connect-your-google-account)
5. [Add it to ChatGPT](#add-it-to-chatgpt)
6. [Using it](#using-it) (tools and the app UI)
7. [Customize task creation](#customize-task-creation)
8. [Configuration reference](#configuration-reference)
9. [Operations and troubleshooting](#operations-and-troubleshooting)
10. [Security notes](#security-notes)
11. [Development](#development)
12. [Limitations](#limitations)

## Quick start

1. Create a Google OAuth client and save its JSON as
   `~/.config/google-tasks-mcp/client_secret.json` ([details](#google-cloud-setup)).
2. Install and start both services; enter the tunnel key when prompted:
   ```bash
   scripts/setup-services.sh
   ```
3. Connect Google (prints a link; see [the options](#connect-your-google-account)):
   ```bash
   .venv/bin/google-tasks-mcp auth
   ```
4. Check everything:
   ```bash
   .venv/bin/google-tasks-mcp doctor
   ```
5. In ChatGPT, add a custom MCP server that uses the tunnel ([details](#add-it-to-chatgpt)).

## Google Cloud setup

You need your own OAuth client. It is free, and personal use needs no Google verification.

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create (or pick) a project.
2. **Enable the API:** go to *APIs & Services → Library*, find **Google Tasks API**, and click **Enable**.
3. **Consent screen:** open *Google Auth Platform* (formerly *OAuth consent screen*).
   - Choose **External** user type (or **Internal** if you use Google Workspace).
   - Fill in the app name and your email.
   - Under *Data access* (scopes) you may add `https://www.googleapis.com/auth/tasks`. The server
     requests only this scope either way.
   - While the app is in **Testing**, add your Google account under *Audience → Test users*.
   - **Refresh tokens expire after 7 days in Testing mode.** For a connection that lasts, set
     *Publishing status* to **In production**. Google then shows an "unverified app" warning at
     sign-in. That is expected for a personal app: click *Advanced → Go to … (unsafe)*.
     `doctor` warns you when your authorization has an expiry date.
4. **Create the client:** go to *Clients → Create client*.
   - Application type: **Desktop app**, the recommended type. Desktop clients accept the
     loopback redirect `http://127.0.0.1:8765/oauth/google/callback` without any redirect-URI
     configuration.
   - Use a **Web application** client only if you set up a public HTTPS callback
     (`GTASKS_PUBLIC_BASE_URL`). In that case add `<that URL>/oauth/google/callback` as an
     authorized redirect URI.
5. Download the JSON and install it with owner-only permissions:
   ```bash
   install -m 600 ~/Downloads/client_secret_*.json ~/.config/google-tasks-mcp/client_secret.json
   ```
   Alternatively, set `GTASKS_GOOGLE_CLIENT_ID` and `GTASKS_GOOGLE_CLIENT_SECRET` in
   `~/.config/google-tasks-mcp/server.env`.

## Install and run the services

`scripts/setup-services.sh` is idempotent: run it after every `git pull`. It:

- checks for Python 3.11+, the systemd user manager, `/usr/local/bin/tunnel-client`, and the
  existing `google-tasks-test` profile (it never creates or edits tunnel profiles);
- creates `.venv` if needed and runs `pip install -e .`;
- creates `~/.config/google-tasks-mcp/` (0700) with `server.env` from
  `deploy/server.env.example`, and `~/.local/state/google-tasks-mcp/` (0700);
- writes `~/.config/google-tasks-mcp/tunnel.env` (0600) with `CONTROL_PLANE_API_KEY`, taken from
  the environment or prompted for with hidden input, if the file doesn't hold it yet;
- installs `google-tasks-mcp.service` and `google-tasks-tunnel.service` into
  `~/.config/systemd/user/`, then runs `daemon-reload` and `enable`;
- enables lingering (`loginctl enable-linger $USER`) so both services start at boot without a
  login. If polkit refuses, it prints the `sudo` command to run;
- refuses to start while a copy started by hand in a terminal holds port 8765 or the tunnel
  profile. Stop those first, or pass `--stop-manual`;
- restarts both services, waits for `/readyz` on the server and the tunnel, and runs `doctor`.

| Unit | Runs | Notes |
|---|---|---|
| `google-tasks-mcp.service` | `.venv/bin/google-tasks-mcp serve` | `Restart=on-failure`. Reports started only once `/readyz` answers (`ExecStartPost=… wait-ready`). Sandboxed: `ProtectSystem=strict`, can write only its state directory. |
| `google-tasks-tunnel.service` | `/usr/local/bin/tunnel-client run --profile google-tasks-test` | `Wants=` and `After=` the MCP service, `Restart=always`, key from `EnvironmentFile=…/tunnel.env`. |

No secret appears in a unit file. Useful commands:

```bash
systemctl --user status google-tasks-mcp google-tasks-tunnel
```

```bash
journalctl --user -u google-tasks-mcp -u google-tasks-tunnel -f
```

```bash
systemctl --user restart google-tasks-mcp
```

The tunnel profile `~/.config/tunnel-client/google-tasks-test.yaml` is left unchanged. It points
`mcp.server_urls[main]` at `http://127.0.0.1:8765/mcp`, reads its key as
`env:CONTROL_PLANE_API_KEY`, and serves its own health UI on `127.0.0.1:8080`.

## Connect your Google account

Google sends the browser back to `http://127.0.0.1:8765/oauth/google/callback` (the *redirect
URI*). Only a browser that can reach this server's loopback port can follow that redirect. The
Secure MCP Tunnel carries MCP traffic only, not browser traffic. Pick whichever option fits:

| Where your browser is | What to do |
|---|---|
| On the server itself | Run `google-tasks-mcp auth`, open the link, and approve. Done. |
| On another computer, with SSH access | Run `ssh -L 8765:127.0.0.1:8765 <server>` first. Then use `google-tasks-mcp auth` on the server, or **Connect** in ChatGPT, and open the link on that computer. The redirect travels through the SSH tunnel. |
| Anywhere, with no tunnel (headless fallback) | Open the link and approve. The browser then shows a connection error on a `127.0.0.1:8765/oauth/google/callback?…` page. Copy that **entire URL** and paste it into the waiting `google-tasks-mcp auth`, or run `google-tasks-mcp auth --redirect-url '<URL>'`. This also completes sign-ins started from ChatGPT. |
| From ChatGPT | Click **Connect** in the app UI or ask "connect my Google Tasks"; the `auth_connect` tool returns the link. Finish with one of the rows above. The app UI notices the connection within a few seconds. |

Details:

- **The link:** it expires after 10 minutes and works once. Its `state` is random, single-use,
  and checked on return. PKCE (S256) binds the code to this server. Only the `tasks` scope is
  requested.
- **Reconnecting:** run `auth` again, or click *Reconnect*. This replaces the stored
  authorization, for example to switch accounts or recover from "reconnect needed".
- **Disconnecting:** `google-tasks-mcp disconnect`, or *Disconnect* in the app, revokes the
  authorization at Google and deletes the local credentials. If Google can't be reached, the
  local credentials are still deleted, and you can remove access at
  <https://myaccount.google.com/connections>.
- **Public HTTPS callback (optional, advanced):** if you publish **only** the path
  `/oauth/google/callback` over HTTPS, set
  `GTASKS_PUBLIC_BASE_URL=https://host.example` and use a Web application client. One way is
  `tailscale serve`, path-limited, so it is reachable only inside your tailnet. Never publish
  `/mcp`. The server also refuses `/mcp` and every other path when the request carries that
  public host name.

## Add it to ChatGPT

The tunnel behind profile `google-tasks-test` must be linked to the ChatGPT workspace you use. Manage it at
<https://platform.openai.com/settings/organization/tunnels>; your key needs the *Tunnels
Read + Use* permissions.

1. Start both services and confirm that `google-tasks-mcp doctor` shows `tunnel_health` as
   ready.
2. In ChatGPT on the web, open the apps/plugins settings and enable developer mode if your
   account requires it. Choose **Add custom MCP server**, set the connection type to
   **Tunnel**, and pick the tunnel (or paste its id). Set authentication to **No
   authentication**: the tunnel is the access control. OpenAI renames these screens from time
   to time; the Secure MCP Tunnel guide at
   <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels> has the current steps.
3. Create or install it, then mention it in a chat (for example with `@`) or let ChatGPT pick it.
4. After changing tools or tool descriptions (including
   [`task_behavior.py`](#customize-task-creation)), restart the service and use **Refresh** on
   the plugin so ChatGPT reloads them.

## Using it

### Tools

| Tool | Kind | Purpose |
|---|---|---|
| `create_task` | write | Create a task: title, optional notes, a `due` date or phrase ("tomorrow", "friday", "oct 12"), list, and parent. |
| `list_tasks` | read | Open tasks by default. Options: `include_completed`, `query`, `due_before`, `due_after`, `all_lists`. |
| `get_task` | read | One task by id; finds its list automatically. |
| `update_task` | write | Change title, notes, or due date (`"none"` clears it). |
| `complete_task` | write | Complete, or reopen with `completed=false`. |
| `move_task` | write | Reorder (`after_task_id`), nest (`parent_task_id`), or move to another list. |
| `delete_task` | **destructive** | Permanently delete a task and its subtasks. |
| `list_task_lists`, `create_task_list`, `rename_task_list` | read / write | Manage lists. |
| `delete_task_list` | **destructive** | Delete a list and all its tasks; the default list is refused. |
| `auth_status` | read | Connection state and next step. Never returns credentials. |
| `auth_connect` | read | Returns a Google sign-in link (connect or reconnect). |
| `auth_disconnect` | **destructive** | Revoke access and delete the stored credentials. |
| `open_tasks_app` | read, UI | Opens the interactive app. |
| `tasks_app_state` | read, app-only | The app's data source; hidden from the model. |
| `ping` | read | Connectivity check; doesn't contact Google. |

Annotation details:

- Every tool sets `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`
  (false: everything stays inside your own account).
- ChatGPT asks you to confirm write tools, and asks for explicit approval of destructive ones.
- `auth_connect` is marked read-only: it only produces a link, and nothing changes until you
  approve in the browser.

Errors come back as `isError` results with structured content of the form
`{"error": {"code", "message", "action"}}`. The codes are `google_not_configured`,
`google_not_connected`, `google_reauth_required`, `not_found`, `invalid_argument`,
`google_unavailable`, `tasks_api_disabled`, and `google_api_error`. When Google is disconnected,
ChatGPT is told to call `auth_connect` and hand you the link.

Things to try:

- "Add 'finish physics homework' to my Google Tasks tomorrow."
- "What's due this week across all my lists?"
- "Mark the physics homework done."
- "Move 'read chapter 4' to my School list."
- "Show my Google Tasks." (opens the app UI)

### App UI

`open_tasks_app` renders a sandboxed HTML view: the `ui://google-tasks/app-<hash>.html`
resource, served as `text/html;profile=mcp-app`. It shows:

- the connection state, with **Connect**, **Reconnect**, and **Disconnect** (with confirmation)
  under *Account…*;
- a task-list selector and a *Show completed* toggle;
- an add form with a date picker and optional notes;
- checkboxes to complete or reopen tasks, inline **Edit** (title, due date, notes), and
  **Delete** with an inline confirmation;
- **Refresh** (↻) and full-screen (⤢) buttons.

How it is built:

- **Bridge:** it uses the standard MCP Apps postMessage bridge (`ui/initialize` with protocol
  `2026-01-26`, `tools/call`, `ui/open-link`, `ui/request-display-mode`, size notifications).
- **ChatGPT-specific fallbacks:** `window.openai.openExternal`, used only if `ui/open-link`
  fails, and `window.openai.callTool`, used only if the standard handshake is unavailable.
- **No direct network access:** the view never contacts Google or any server itself. Every
  action is an MCP tool call through the host. The HTML is fully self-contained (inline
  CSS/JS), so its CSP declares no `connectDomains`, `resourceDomains`, or `frameDomains`. The
  view stores nothing in the browser.
- **Cache-busting:** the resource URI includes a hash of the HTML, so hosts that cache UI
  templates pick up changes.

## Customize task creation

**Edit [`src/google_tasks_mcp/task_behavior.py`](src/google_tasks_mcp/task_behavior.py).** It is
the only file that decides how a request becomes a task, and it doesn't touch MCP, OAuth, or
the Google API.

| Setting | Effect |
|---|---|
| `CREATION_GUIDANCE` | Instructions ChatGPT follows when creating tasks: title style, when to set dates, notes, several tasks per request. Added to the server instructions and the `create_task` description. |
| `DEFAULT_TASK_LIST` | List for new tasks when none is named (`None` = your default list). |
| `DEFAULT_DUE` | Due date when none is given (`None`, or a phrase such as `"today"`). |
| `TIMEZONE` | Timezone for "today", "tomorrow", and so on (`GTASKS_TIMEZONE` overrides it). |
| `WARN_ON_DUPLICATE_TITLE` | Warn when an open task with the same title exists. |
| `CAPITALIZE_FIRST_LETTER`, `STRIP_TRAILING_PERIOD`, `MAX_TITLE_LENGTH` | Title clean-up. |
| `SAME_WEEKDAY_MEANS_TODAY`, `NEXT_WEEKDAY_SKIPS_A_WEEK`, `NEXT_WEEK_WEEKDAY`, `END_OF_WEEK_WEEKDAY` | How date phrases resolve. |
| `prepare_new_task(task)` | Hook run on every new task, from ChatGPT or the app, just before it is sent to Google. Change the title, notes, due date, or list, or raise `ValueError("why")` to reject the task. |

Then run `pytest`, restart the service with
`systemctl --user restart google-tasks-mcp`, and refresh the plugin in ChatGPT. The parser for
date phrases lives in `dates.py`; its rules come from the settings above.

## Configuration reference

Settings come from the environment first, then `~/.config/google-tasks-mcp/server.env`, then
the defaults. The service, the CLI, and `doctor` all read the same file. See
[`deploy/server.env.example`](deploy/server.env.example).

| Variable | Default | |
|---|---|---|
| `GTASKS_HOST` / `GTASKS_PORT` | `127.0.0.1` / `8765` | Host must be loopback. |
| `GTASKS_GOOGLE_CLIENT_FILE` | `~/.config/google-tasks-mcp/client_secret.json` | Downloaded OAuth client JSON. |
| `GTASKS_GOOGLE_CLIENT_ID` / `_SECRET` | – | Alternative to the JSON file. |
| `GTASKS_PUBLIC_BASE_URL` | `http://127.0.0.1:8765` | Base URL of the browser-facing OAuth callback. |
| `GTASKS_TIMEZONE` | system timezone | IANA name, e.g. `America/New_York`. |
| `GTASKS_LOG_LEVEL` | `INFO` | |
| `GTASKS_STATE_DIR` | `~/.local/state/google-tasks-mcp` | Credentials. The systemd sandbox only allows the default. |
| `GTASKS_TUNNEL_PROFILE` / `GTASKS_TUNNEL_CLIENT` | `google-tasks-test` / `/usr/local/bin/tunnel-client` | Used by `doctor`. |

| File | Contents | Mode |
|---|---|---|
| `~/.config/google-tasks-mcp/server.env` | Optional settings | 0600 |
| `~/.config/google-tasks-mcp/client_secret.json` | Google OAuth client | 0600 |
| `~/.config/google-tasks-mcp/tunnel.env` | `CONTROL_PLANE_API_KEY` for the tunnel service | 0600 |
| `~/.local/state/google-tasks-mcp/google-token.json` | Google refresh and access tokens | 0600 (dir 0700) |
| `~/.local/state/google-tasks-mcp/pending-auth/` | Sign-ins in progress (hashed state + PKCE verifier, 10 min) | 0600 |

## Operations and troubleshooting

```
google-tasks-mcp [serve]          run the server (what the service does)
google-tasks-mcp auth             connect or reconnect Google (--redirect-url URL for the paste fallback)
google-tasks-mcp auth-status      connection state, no secrets (--verify makes a read-only API call; --json)
google-tasks-mcp disconnect       revoke and delete stored credentials (--yes)
google-tasks-mcp doctor           check everything below (--offline skips the Google call; --json)
google-tasks-mcp wait-ready       wait for /readyz (used by systemd)
```

`doctor` checks, without printing secrets:

- **Configuration:** bind address; env-file, client-file, and token-file permissions.
- **Google:** OAuth client type and redirect-URI fit; connection state and scopes; Testing-mode
  expiry; a read-only Tasks API call.
- **Local server:** `/healthz` and `/readyz`; an MCP `initialize`, `tools/list`, and `ping`; the
  UI resource and its MIME type.
- **Tunnel:** the `tunnel-client` binary, the profile and its target URL, that the API key is
  set (never its value), tunnel `/healthz` and `/readyz`, and `tunnel-client doctor`.
- **systemd:** both units enabled and active; lingering.

Health endpoints:

- `GET http://127.0.0.1:8765/healthz` returns `{"status":"ok"}`.
- `GET http://127.0.0.1:8765/readyz` returns server info and the Google state (no secrets).
- The tunnel's own endpoints are at `http://127.0.0.1:8080/healthz`, `/readyz`, and `/ui`.

| Symptom | Fix |
|---|---|
| `google_not_configured` | Install the client JSON ([Google Cloud setup](#google-cloud-setup)); `doctor` shows the expected path. |
| `google_not_connected` | `google-tasks-mcp auth`, or **Connect** in the app. |
| `google_reauth_required` | The authorization was revoked or expired (Testing mode: 7 days). Reconnect, and publish the consent screen. |
| `tasks_api_disabled` | Enable the Google Tasks API in the client's Cloud project. |
| Browser shows "can't connect" after approving | Expected without an SSH tunnel: paste that URL into `google-tasks-mcp auth`. |
| "This sign-in link is unknown or was already used" / "expired" | Start again; links are single-use and last 10 minutes. |
| `Error 400: redirect_uri_mismatch` from Google | Use a Desktop app client, or register the exact redirect URI (`doctor` prints it) on the Web client. |
| `tunnel_api_key` FAIL | Add `CONTROL_PLANE_API_KEY=…` to `~/.config/google-tasks-mcp/tunnel.env` (0600), or re-run `scripts/setup-services.sh`. |
| Tunnel `readyz` not ready, or ChatGPT can't reach tools | `journalctl --user -u google-tasks-tunnel`; `tunnel-client doctor --profile google-tasks-test --explain` (export the key first, e.g. `set -a; . ~/.config/google-tasks-mcp/tunnel.env; set +a`). |
| Setup says a manual copy is running | Stop the `google-tasks-mcp` or `tunnel-client run` you started in a terminal, or use `--stop-manual`. |
| Services don't start after reboot | `loginctl show-user $USER -p Linger` must be `yes`; run `sudo loginctl enable-linger $USER`. |
| ChatGPT shows old tool descriptions or an old UI | Restart the service and **Refresh** the plugin in ChatGPT. |

## Security notes

- **Network exposure:** the bind address must be loopback (enforced). Requests with a foreign
  `Host` get 421 (DNS-rebinding guard), and `/mcp` also returns 403 for foreign `Origin`s.
  Only the OAuth callback accepts the optional `GTASKS_PUBLIC_BASE_URL` host.
- **Google permissions:** only `https://www.googleapis.com/auth/tasks` is requested; a grant
  without it is refused and revoked. No identity scopes are requested, so the UI shows
  "connected" rather than an email address.
- **OAuth flow:**
  - authorization code with PKCE S256, and `prompt=consent`, so Google always issues a refresh
    token;
  - 256-bit random `state`, stored only as a SHA-256 hash, single-use (claimed by an atomic
    rename), expiring after 10 minutes;
  - every token request uses Google's fixed HTTPS endpoints, with TLS verification on (system
    trust store).
- **Credential storage:** credentials live in one 0600 file in a 0700 directory, outside the
  repository, written atomically. Refresh happens automatically (2 minutes before expiry, and
  once more on a 401). A refresh token Google rejects marks the connection "reconnect needed".
- **Model-visible and log output:**
  - Tool results, the UI, `/readyz`, `auth-status`, and `doctor` never include tokens, the
    client secret, or the tunnel key.
  - The access log is off, because callback URLs carry codes.
  - A redacting log formatter scrubs token- and secret-shaped strings from every log line,
    including tracebacks.
  - Tool failures are logged by error code only.
- **Callback page:** `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, a
  `default-src 'none'` CSP, and `X-Frame-Options: DENY`.
- **Git:** `.gitignore` blocks env files, client JSONs, and token files. Nothing secret is
  needed inside the repository.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/pytest
```

```bash
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
```

```bash
.venv/bin/google-tasks-mcp serve
```

On Windows use `py -3 -m venv .venv` and `.venv\Scripts\...`. The server, CLI, and tests are
cross-platform; the config lives in `%APPDATA%\google-tasks-mcp` and the state in
`%LOCALAPPDATA%\google-tasks-mcp`. `scripts/setup-services.sh` and the systemd units are
Linux-only.

```
src/google_tasks_mcp/
  task_behavior.py  ★ editable task-creation behavior
  tools.py          MCP tools: schemas, annotations, structured errors, server instructions
  app_ui.py         MCP Apps resource + UI tools;  ui/tasks_app.html is the view
  service.py        task logic shared by tools and UI (list resolution, conversions)
  google_tasks.py   Google Tasks REST client (retries, error mapping)
  oauth.py          Google OAuth: PKCE flow, token storage/refresh/revoke
  dates.py          due-date phrases → dates
  http_routes.py    /healthz, /readyz, OAuth callback, Host guard
  server.py         assembly + uvicorn;  cli.py commands;  diagnostics.py doctor
  config.py  models.py  errors.py  logs.py
deploy/             systemd unit templates, server.env.example
scripts/            setup-services.sh
tests/              pytest suite; fake_google.py emulates Google OAuth + Tasks over httpx2.MockTransport
```

Design choices:

- **Dependencies:** none beyond the MCP SDK's own stack. Google OAuth and the Tasks REST API
  are called directly with `httpx2`, which the SDK already ships, rather than adding
  `google-api-python-client` and `google-auth`. It is async-native, has a small surface, and is
  easy to mock.
- **Transport:** stateless Streamable HTTP with JSON responses, which suits the tunnel and
  keeps nothing per session.
- **Auth state on disk:** pending sign-ins are files, so the CLI and the server can each finish
  a sign-in the other started.

The tests use `fake_google.py`, an in-memory emulation of Google OAuth and Tasks, and cover:

- the OAuth flow: PKCE, state single-use and expiry, denied or missing scope, refresh,
  `invalid_grant`, revoke, and testing-mode expiry;
- unauthenticated and not-configured results, and every task and list operation, including
  moves, filters, retries, and a 401-then-refresh;
- the absence of secrets in tool output;
- tool annotations, and UI resource registration and metadata;
- the HTTP routes: health, the Host guard, and the callback, including replay and HTML
  escaping;
- real Streamable HTTP over a socket;
- date phrases, the behavior hooks, config loading, and log redaction.

## Limitations

- **Google Tasks dates:** due dates have no time of day (Google discards it). Recurring tasks
  can't be created through the API, and can't be moved between lists.
- **One account:** one Google account per server. Switch by reconnecting.
- **Sign-in path:** the browser must reach the callback, either on this machine, through an SSH
  tunnel, by pasting the URL, or through an optional public HTTPS callback. The Secure MCP
  Tunnel doesn't carry browser traffic.
- **Duplicate check:** it inspects the first 100 open tasks of the target list.
- **Extension advertisement:** the MCP Apps capability is advertised (`capabilities.extensions`)
  only to clients that use the 2026-07-28 protocol. Older clients still get the UI metadata on
  the tools, and those without Apps support ignore it.
- **Consent screen in Testing mode:** reconnects are needed every 7 days until the screen is
  published.
