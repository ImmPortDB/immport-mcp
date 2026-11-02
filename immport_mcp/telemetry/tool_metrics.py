"""Tool-level telemetry decorator for immport-mcp-server.

Provides a simple decorator to record TOOL metrics for FastMCP tools.
It builds a compact payload and calls `metric_client.fire_and_forget`.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, Optional

from immport_mcp.services.metric_client import fire_and_forget
from immport_mcp.config.settings import settings
from immport_mcp.auth.token_manager import _request_api_key, get_current_username

log = logging.getLogger(__name__)

# US/Eastern timezone for metric timestamps (matches the Java metric service convention).
# Falls back to a fixed UTC-5 offset if tzdata is not installed — timestamps will still
# be recorded, just without DST adjustment.
try:
    from zoneinfo import ZoneInfo
    US_EAST = ZoneInfo("US/Eastern")
except Exception:
    import datetime as _dt
    US_EAST = _dt.timezone(_dt.timedelta(hours=-5))  # type: ignore[assignment]


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(US_EAST).strftime("%Y/%m/%d %H:%M:%S")


def _get_api_key() -> str:
    """Resolve the API key to use for metric auth.

    Mirrors token_manager.get_auth_header() but never raises — returns ""
    if no key is available so the metric post is simply skipped.
    """
    return _request_api_key.get() or settings.immport_api_key


def _get_session_id() -> str | None:
    """Read the MCP session ID from the current HTTP request headers.

    The streamable-HTTP transport sends ``mcp-session-id`` on every request
    after the initial ``initialize``.  Returns None in stdio mode or when
    the header is absent (e.g. on the first initialize call itself).
    """
    try:
        from fastmcp.server.http import _current_http_request
        req = _current_http_request.get()
        if req is not None:
            return req.headers.get("mcp-session-id") or None
    except Exception:
        log.debug("Could not read mcp-session-id", exc_info=True)
    return None


def _extract_username_from_request() -> str:
    """Delegate to token_manager.get_current_username() — shared JWT decode logic."""
    return get_current_username()


def default_param_extractor(*args, **kwargs) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if "term" in kwargs:
        params["term"] = kwargs.get("term") or ""
    if "file_uuid" in kwargs:
        params["fileUUID"] = kwargs.get("file_uuid")
    if "action" in kwargs:
        params["action"] = kwargs.get("action")
    return params


def record_tool_metric(tool_name: str, sample_rate: float = 1.0, param_extractor: Optional[Callable] = None):
    """Decorator to record a TOOL metric for a FastMCP tool.

    - tool_name: logical name for the tool (e.g. 'search_studies')
    - sample_rate: fraction [0,1] of calls to record
    - param_extractor: optional callable(*args, **kwargs) -> dict for compact params
    """
    if param_extractor is None:
        param_extractor = default_param_extractor

    def decorator(fn: Callable):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            start = datetime.utcnow()
            try:
                result = await fn(*args, **kwargs)
            except Exception:
                # still record metric about failure
                result = None
                raise
            finally:
                try:
                    if random.random() <= sample_rate:
                        params = param_extractor(*args, **kwargs) or {}

                        # Always include sessionId (None in stdio mode — omitted from JSON).
                        session_id = _get_session_id()
                        if session_id:
                            params["sessionId"] = session_id

                        payload = {
                            "applicationName": "immport-mcp-server",
                            "metricType": "TOOL",
                            "endPoint": f"/mcp/{tool_name}",
                            "parameters": json.dumps(params),
                            "startTime": _format_ts(start),
                            "username": _extract_username_from_request(),
                            "remoteIpAddress": None,
                        }
                        # fire-and-forget post — forward the user's own API key
                        fire_and_forget(payload, api_key=_get_api_key())
                except Exception:
                    log.exception("Failed to build/send tool metric")
            return result

        return wrapper

    return decorator
