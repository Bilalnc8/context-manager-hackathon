"""MCP server so any Claude session can load a company's requirements file from Drive.

Register it once (user scope, so every new Claude Code session has it):

    claude mcp add emailctx --scope user -e EMAILCTX_CONFIG=<path>/config.yaml -- <path>/.venv/Scripts/emailctx.exe mcp

Then in any session:
- ask about a company ("what are Acme's requirements?") and Claude calls
  get_company_context, or
- type /mcp__emailctx__company Acme to load it at the start of a session.

Read-only: it lists and reads the .md files the app created in Drive, nothing else.
It speaks MCP over stdio, so it never prints anything itself.
"""

from __future__ import annotations

from collections.abc import Callable

from emailctx.context import CompanyNotFound, fetch_context, list_company_files
from emailctx.drive import DriveStore

INSTRUCTIONS = (
    "Requirements files for client companies, kept in Google Drive by emailctx and "
    "built from the company's emails and meeting summaries. When the user names a client "
    "company or asks about its requirements, fetch its file with get_company_context and "
    "treat it as the current, cited source of truth for that company."
)


def build_server(store_factory: Callable[[], DriveStore]):
    """store_factory signs in lazily, so the server starts even before `emailctx auth`."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("emailctx", instructions=INSTRUCTIONS)
    cached: dict[str, DriveStore] = {}

    def store() -> DriveStore:
        if "s" not in cached:
            cached["s"] = store_factory()
        return cached["s"]

    @server.tool()
    def list_companies() -> str:
        """List the companies that have a requirements file in Google Drive."""
        try:
            files = list_company_files(store())
        except Exception as e:  # auth or Drive problems: tell the user what to do
            return f"Could not reach Google Drive: {e}"
        if not files:
            return "No company files yet. Run `emailctx sync` to build them."
        return "\n".join(
            f"- {c.name}" + (f" (updated {c.modified[:10]})" if c.modified else "") for c in files
        )

    @server.tool()
    def get_company_context(company: str) -> str:
        """Fetch a company's requirements file (<Company>.md) from Google Drive.

        `company` is the company's name or id; case doesn't matter and a partial name
        works when it matches exactly one company. Returns the file's markdown: the
        requirements by category, items needing confirmation, the changelog, and
        footnotes citing the emails and meeting summaries each requirement came from.
        """
        try:
            found, text = fetch_context(store(), company)
        except CompanyNotFound as e:
            return str(e)
        except Exception as e:
            return f"Could not reach Google Drive: {e}"
        header = f"<!-- {found.name}.md from Google Drive"
        header += f", last updated {found.modified}" if found.modified else ""
        header += f", {found.link}" if found.link else ""
        return header + " -->\n" + text

    @server.prompt()
    def company(name: str) -> str:
        """Load a company's requirements file into this session."""
        return (
            f"Fetch the requirements file for the company {name!r} with the "
            "get_company_context tool and keep it as background for this session. "
            "Then give me a short overview: how many requirements per category, anything "
            "under Needs confirmation, and the most recent changes with their dates. "
            "After that, wait for my question."
        )

    return server


def run(store_factory: Callable[[], DriveStore]) -> None:
    build_server(store_factory).run("stdio")
