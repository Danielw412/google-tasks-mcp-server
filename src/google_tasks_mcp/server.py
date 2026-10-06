"""Serve a single ping tool over Streamable HTTP on localhost."""

from mcp.server import MCPServer

mcp = MCPServer("google-tasks-mcp-test")


@mcp.tool()
def ping(message: str = "hello") -> dict[str, bool | str]:
    """Echo a message to verify the MCP connection."""
    return {
        "success": True,
        "message": message,
        "server": "google-tasks-mcp-test",
    }


def main() -> None:
    try:
        mcp.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=8765,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
        )
    except KeyboardInterrupt:
        pass
