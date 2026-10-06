# Google Tasks MCP connection test

A minimal Python 3.11+ server using the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
Its sole purpose is testing MCP connectivity, including an OpenAI Secure MCP Tunnel.
It exposes exactly one tool: `ping(message: str = "hello")`.

## Install and run

From this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
google-tasks-mcp
```

On Windows PowerShell, use `py -3 -m venv .venv` and
`.\.venv\Scripts\Activate.ps1` for the first two commands.

The Streamable HTTP endpoint is **http://127.0.0.1:8765/mcp**.
The server listens only on `127.0.0.1`; use that URL as the tunnel's local target
on the machine running the server. Stop it with Ctrl+C.

Calling `ping` with no arguments returns:

```json
{
  "success": true,
  "message": "hello",
  "server": "google-tasks-mcp-test"
}
```

Passing `message` echoes that string instead. There is no Google Tasks integration,
OAuth, persistence, Docker, or authentication. A successful local call verifies
the server; testing the tunnel requires calling `ping` through the connected tunnel.
