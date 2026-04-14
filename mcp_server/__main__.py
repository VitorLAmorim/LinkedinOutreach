"""Entry point: ``python -m mcp_server``.

Defaults to stdio transport (suitable for Claude Desktop / Claude Code).
Use ``--transport http`` to run as a Streamable HTTP server (suitable for
Docker, multi-client, or remote access).
"""
from __future__ import annotations

import argparse

from mcp_server.server import mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_server", description="OpenOutreach MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
    elif args.transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
