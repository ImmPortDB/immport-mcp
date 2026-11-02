"""
ImmPort MCP Server — main entrypoint.

Registers all tools, resources, and prompts then starts the server.
Transport is configured via MCP_TRANSPORT env var: stdio (default) or streamable-http.

Multi-user HTTP mode:
  Each user passes their own ImmPort API key as the Authorization: Bearer <key> header
  when configuring the claude.ai connector (or any MCP client).
  The middleware below extracts it per-request so get_auth_header() uses it automatically.
  No server-level IMMPORT_API_KEY is required in this mode.

Single-user / local mode (stdio):
  Set IMMPORT_API_KEY in .env — it is used for all requests.
"""
from __future__ import annotations

import logging

from fastmcp import FastMCP

from immport_mcp.auth.token_manager import warn_if_missing, set_request_api_key
from immport_mcp.config.settings import settings
from immport_mcp.resources import data_model
from immport_mcp.prompts import templates
from immport_mcp.tools import search, study_data, results, lookups, files
from immport_mcp.services.lookup_cache import lookup_cache


warn_if_missing()

# Configure logging for all immport_mcp.* loggers from settings (MCP_LOG_LEVEL env var).
# INFO  → metric payloads + POST results; WARNING → failures only (production default).
# We attach a handler directly to the "immport_mcp" logger so that Python's lastResort handler
# (hardcoded to WARNING, used when no handlers exist anywhere in the hierarchy) never
# silently drops messages at levels below WARNING.  propagate=False avoids duplicate
# output if FastMCP or another library later installs a root-level handler.
_log_level: int = getattr(logging, settings.mcp_log_level.upper(), logging.WARNING)
_src_logger = logging.getLogger("immport_mcp")
_src_logger.setLevel(_log_level)
if not _src_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(_log_level)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    )
    _src_logger.addHandler(_handler)
    _src_logger.propagate = False

mcp = FastMCP(
    settings.mcp_server_name,
    instructions=(
        "This server provides access to the ImmPort immunology data repository. "
        "Use search_studies or search_subjects to discover data, then drill into "
        "specific studies with get_study_data, retrieve assay results with "
        "get_assay_results, and access files with get_study_files. "
        "Use lookup_controlled_vocabulary to get valid filter values for any parameter. "
        "Refer to the immport://data-model and immport://query-tips resources for "
        "guidance on the data model and efficient query patterns, immport://search-fields "
        "for the full list of fields searchable/retrievable via search_studies/search_subjects "
        "(including nested objects like publications, reagents, and experiments — check this "
        "before looping get_study_data over many studies for any cross-study question), and "
        "immport://assay-results-reference for the full per-assay-type field list and filter "
        "parameters for get_assay_results.\n\n"
        "AUTHENTICATION — two layers:\n"
        "  • search_studies, search_subjects, lookup_controlled_vocabulary: PUBLIC — no auth needed.\n"
        "  • get_study_data, get_assay_results, get_study_files: REQUIRE an ImmPort API key.\n"
        "    When connected via mcp.immport.org (claude.ai / AgentCore): the OAuth2 login you\n"
        "    completed when adding this server IS your ImmPort API key — it is forwarded\n"
        "    automatically on every authenticated tool call. No extra configuration needed.\n"
        "    When running locally (stdio): set IMMPORT_API_KEY in .env.\n\n"
        "EFFICIENCY RULES — follow these before calling any tool:\n"
        "1. DATA AVAILABILITY FIRST: Before calling get_study_data or get_assay_results, "
        "call get_study_data_availability(study_accession) to see record counts for every ImmPort data category. "
        "This single call (reads a ~5 KB file) tells you exactly which assay types, demographics, "
        "and clinical data categories have been parsed into the ImmPort database for this study. "
        "For several studies pass them comma-separated (up to 20) in one call.\n"
        "   Examples: elisa_result=0 → skip ELISA; hai_result=240 → fetch HAI titers; "
        "assessment_component>10000 → use Tab.zip not the API.\n"
        "   ⚠️ Counts reflect PARSED data only. Zero does not mean the data doesn't exist — "
        "it may be in the study files but not yet extracted into the database. "
        "Always check get_study_files when counts are unexpectedly low.\n"
        "2. PARALLEL LOOKUPS: lookup_controlled_vocabulary is zero-cost (in-memory cache). "
        "Issue lookup calls in parallel with search calls — never wait for a lookup before searching.\n"
        "3. TAB.ZIP OVER MULTIPLE API CALLS: If you need more than 2 resources for one study, "
        "get a signed URL for the Tab.zip (get_study_files action='manifest', file_type='release_file' "
        "→ find _Tab.zip → get_study_files action='download') instead of multiple get_study_data calls.\n"
        "4. PARALLEL STUDY FETCHES: When fetching data for multiple studies, "
        "issue all get_study_data calls in parallel — they are independent.\n"
        "5. SOURCE FIELDS: Always set source_fields on search_studies/search_subjects. "
        "The default returns full OpenSearch documents (very large). "
        "Specify only the fields you need.\n"
        "6. PAGINATION: Always check hits.total.value in search results. "
        "If total > page_size, issue additional calls with from_record incremented by page_size.\n"
        "7. STREAM VS DOWNLOAD: get_study_files action='stream' is for individual TSV/text files only. "
        "Use action='download' for any zip file (Tab.zip, ALL_DATA.zip) — it returns a signed URL. "
        "⛔ NEVER attempt to fetch or open a signed CloudFront/S3 URL yourself — you are in a sandbox "
        "with no network access to those hosts and it will always fail. "
        "Present the URL to the user and let them download it.\n"
        "8. SPARSE STUDY DATA: Many ImmPort studies — especially older studies and recently released "
        "data — have minimal parsed data in the database. When get_study_data returns very few "
        "subjects, missing demographics, or limited assay data, do NOT stop there. Always check "
        "the study files for the full picture:\n"
        "   a. Call get_study_files(action='manifest', study_accession=...) with NO file_type "
        "filter to see everything available.\n"
        "   b. Stream individual study or protocol documents (file_type='study_file' or "
        "'protocol_file') using action='stream' — most are plain text or TSV and can be read "
        "inline for AI analysis.\n"
        "   c. For all structured metadata tables, get a signed URL for the Tab.zip "
        "(file_type='release_file' → find fileName ending '_Tab.zip' → action='download') and "
        "present that URL to the user — the zip contains every study table as a TSV file.\n"
        "   d. For the complete dataset including raw result files, check file_type='zip_file' "
        "(SDY##_DR##_ALL_DATA.zip) — but warn the user it can be very large (some studies >100 GB)."
    ),
)

# Register all modules
search.register(mcp)
study_data.register(mcp)
results.register(mcp)
lookups.register(mcp)
files.register(mcp)
data_model.register(mcp)
templates.register(mcp)


def main() -> None:
    if settings.mcp_transport in ("streamable-http", "sse", "http"):
        # Add ASGI middleware to extract the per-user Bearer token from each request
        # and make it available to get_auth_header() via a ContextVar.
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request

        class ImmPortKeyMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                auth = request.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    set_request_api_key(auth[7:].strip())
                else:
                    set_request_api_key("")
                return await call_next(request)

        # stateless_http=True: no Mcp-Session-Id is ever issued, so AgentCore has no
        # session to route by and can serve any request from any (possibly fresh) microVM.
        # Without this, a container recycled by AgentCore's idle/max-lifetime limits can't
        # resume a client's cached session, and AgentCore's gateway reports that as a 200
        # response wrapping a JSON-RPC error rather than a raw 404 the client could act on.
        app = mcp.http_app(stateless_http=True)
        app.add_middleware(ImmPortKeyMiddleware)

        import uvicorn

        async def _serve() -> None:
            # Warm the lookup cache in the background so a cold start can accept
            # requests immediately. lookup_cache.get() returns [] until warm()
            # finishes (a ~1-2s window), rather than blocking server startup.
            asyncio.create_task(lookup_cache.warm())
            # Start background refresh task (no-op if lookup_refresh_hours == 0)
            if settings.lookup_refresh_hours > 0:
                lookup_cache.start_background_refresh(hours=settings.lookup_refresh_hours)
            config = uvicorn.Config(
                app,
                host=settings.mcp_host,
                port=settings.mcp_port,
                access_log=False,   # suppress per-request 200 OK flood (AgentCore keepalives)
            )
            server = uvicorn.Server(config)
            await server.serve()

        import asyncio
        asyncio.run(_serve())
    else:
        mcp.run(transport=settings.mcp_transport)


if __name__ == "__main__":
    main()
