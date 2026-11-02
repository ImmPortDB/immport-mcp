"""
Lookup / Controlled Vocabulary tool — single merged tool backed by an in-process cache.

All 44 ImmPort controlled vocabulary tables are pre-fetched at server startup and
served entirely from memory. No HTTP call is made at tool-call time. The cache is
refreshed in the background every LOOKUP_REFRESH_HOURS (default 24h).

Use lookup_controlled_vocabulary(table) to retrieve valid filter values before
calling search or result endpoints.
"""
from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP

from immport_mcp.services.lookup_cache import LOOKUP_TABLES, lookup_cache
from immport_mcp.telemetry.tool_metrics import record_tool_metric

# Build the Literal type dynamically from the registry so the LLM sees all valid values
TableName = Literal[tuple(LOOKUP_TABLES.keys())]  # type: ignore[valid-type]

# Build a compact table-of-contents for the tool description
_TOC = "\n".join(f"  {name}: {desc}" for name, desc in LOOKUP_TABLES.items())


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        description=(
            "Get valid controlled vocabulary values for any ImmPort lookup table. "
            "Results are served from an in-memory cache (pre-fetched at startup, "
            "refreshed every 24 hours) — no API call is made at query time.\n\n"
            "Available tables:\n" + _TOC
        )
    )
    @record_tool_metric(
        "lookup_controlled_vocabulary",
        sample_rate=0.0,
        param_extractor=lambda *a, **k: {"table": k.get("table", "")},
    )
    async def lookup_controlled_vocabulary(
        table: TableName,  # type: ignore[valid-type]
    ) -> list:
        """
        Returns the list of valid values for the requested controlled vocabulary table.
        Use these values as filter parameters in search and result endpoints.
        """
        data = lookup_cache.get(table)
        if not data:
            # Cache miss (e.g. first call before warm() completes) — fetch live
            from immport_mcp.services.immport_client import api_get
            data = await api_get(
                f"/api/lookup/{table}",
                params={"format": "json"},
                requires_auth=False,
            )
        return data

    @mcp.tool(
        description=(
            "Get the ImmPort data model: the tables of the release database (the same tables as the "
            "Tab.zip / MySQL release files), with columns, types, descriptions, keys and joins. Public — "
            "no authentication needed.\n\n"
            "  • no table → index of all tables: [{name, type, description, columnCount}] (~110 tables)\n"
            "  • table='arm_or_cohort' → that table in full: columns [{name, type, nullable, default, "
            "description}], primaryKey, foreignKeys [{columns, referencedTable, referencedColumns}], "
            "indexes, and referencedBy (tables that point to it)\n\n"
            "Use it to understand what a column means, which columns hold controlled values (foreign keys to "
            "lk_* tables — get the values with lookup_controlled_vocabulary), and how to join tables "
            "(e.g. subject → arm_2_subject → arm_or_cohort → study) before analysing Tab.zip or MySQL files "
            "or writing SQL. For the overview of entities and API endpoints read immport://data-model."
        )
    )
    @record_tool_metric(
        "get_data_model",
        sample_rate=1.0,
        param_extractor=lambda *a, **k: {"table": k.get("table", "")},
    )
    async def get_data_model(table: str = "") -> dict | list:
        """
        The table index (/api/schema/tables) or one table's full definition
        (/api/schema/tables/{table}).
        """
        import httpx
        from immport_mcp.services.immport_client import api_get

        table = table.strip()
        path = f"/api/schema/tables/{table}" if table else "/api/schema/tables"
        try:
            return await api_get(path, requires_auth=False)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and table:
                raise ValueError(
                    f"Table '{table}' not found; call get_data_model() without a table for the list of tables"
                ) from exc
            raise
