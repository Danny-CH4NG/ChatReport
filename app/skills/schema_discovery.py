"""
Session startup: fetch enriched schema from the MCP server.

Called once when a Chainlit session starts. Merges live DB metadata
(columns, FK relationships) with business semantics from schema_context.yaml
(already injected server-side via describe_table).
"""
import json
import logging
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080")


def _mcp_text(mcp_result) -> str:
    content = mcp_result.content
    if content and hasattr(content[0], "text"):
        return content[0].text
    return "{}"


def _mcp_list(mcp_result) -> list[str]:
    """
    MCP SDK ≥1.9 may return each list item as a separate TextContent instead
    of a single JSON-encoded array.  Handle both formats.
    """
    content = mcp_result.content
    if not content:
        return []
    # Try single-item JSON array first (older behavior)
    text0 = content[0].text if hasattr(content[0], "text") else ""
    try:
        parsed = json.loads(text0)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    # Newer behavior: one TextContent per item
    return [c.text for c in content if hasattr(c, "text")]


async def build_schema_cache() -> dict:
    """
    Return enriched schema dict keyed by table name.

    Each value is the dict returned by MCP's describe_table, which already
    includes columns, foreign_keys, alias, description, join_pattern, etc.
    Returns {} on connection failure so the agent can still start.
    """
    cache: dict = {}

    try:
        async with sse_client(f"{MCP_SERVER_URL}/sse") as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()

                # Discover tables
                tables_result = await session.call_tool("list_tables", {})
                tables: list[str] = _mcp_list(tables_result)
                logger.info("Schema discovery: %d tables found", len(tables))

                # Describe each table
                for table_name in tables:
                    try:
                        desc_result = await session.call_tool(
                            "describe_table", {"table_name": table_name}
                        )
                        info = json.loads(_mcp_text(desc_result))
                        if "error" not in info:
                            cache[table_name] = info
                    except Exception as exc:
                        logger.warning("Could not describe %s: %s", table_name, exc)

    except Exception as exc:
        logger.error("Schema discovery failed (MCP unreachable?): %s", exc)

    logger.info("Schema cache built: %d tables", len(cache))
    return cache
