"""OpenOutreach MCP server — code-mode wrapper over the REST API.

Each MCP tool is a thin, strictly-typed shim over a single REST endpoint
defined in ``linkedin/api_views.py``. Tools take structured arguments
(IDs, enums, literal field values) and return structured data — no
natural-language instruction fields. This is the "code mode" pattern:
the LLM client composes tool calls programmatically, not by describing
intent in prose.

Run as ``python -m mcp_server`` (stdio transport, suitable for Claude
Desktop / Claude Code) or ``python -m mcp_server --transport http`` for
Streamable HTTP.

Configuration via environment variables:
- ``OPENOUTREACH_BASE_URL`` — base URL of the REST API (default: http://localhost:8000)
- ``OPENOUTREACH_API_KEY``  — Bearer token, must match the server's ``API_KEY``
- ``OPENOUTREACH_TIMEOUT``  — HTTP timeout in seconds (default: 30)
"""
