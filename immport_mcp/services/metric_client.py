"""Async metric client for immport-mcp-server.

Sends MetricLog-compatible JSON to the configured metric service endpoint.
Authentication: the metric service is a Spring OAuth2 resource server that
requires SCOPE_browse.  The caller's own ImmPort API key already carries that
scope, so we forward it as ``Authorization: Bearer <key>`` — no separate
service-account credentials are needed.

Posts are fire-and-forget; failures are logged and never surface to callers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict

import httpx

from immport_mcp.config.settings import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoint configuration (overridable via .env)
# ---------------------------------------------------------------------------
_METRIC_URL = (
    settings.immport_metric_base_url.rstrip("/") + settings.immport_metric_metric_path
)
_TIMEOUT = settings.mcp_metric_timeout_seconds
_SEM = asyncio.Semaphore(settings.mcp_metric_max_concurrency)

# Log the target URL once at startup — visible at INFO+.
log.info("Metric client configured → %s (timeout=%.1fs)", _METRIC_URL, _TIMEOUT)


# ---------------------------------------------------------------------------
# Core async post
# ---------------------------------------------------------------------------

async def post_metric(payload: Dict[str, Any], api_key: str = "") -> bool:
    """POST *payload* to the metric service, authenticated with *api_key*.

    Returns True on 2xx, False otherwise.  Never raises.
    *api_key* falls back to the server-level ``IMMPORT_API_KEY`` if empty.
    """
    token = api_key or settings.immport_api_key
    if not token:
        log.debug("Metric post skipped — no API key available")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with _SEM:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(_METRIC_URL, json=payload, headers=headers)

            if 200 <= resp.status_code < 300:
                return True

            log.warning(
                "Metric POST ✗ status=%s  tool=%s\n  url: %s\n  response: %s",
                resp.status_code,
                payload.get("endPoint", "?"),
                _METRIC_URL,
                resp.text[:600],
            )
            return False
        except httpx.TimeoutException:
            log.warning("Metric POST timed out (%.1fs)  tool=%s  url=%s", _TIMEOUT, payload.get("endPoint", "?"), _METRIC_URL)
            return False
        except Exception:
            log.warning("Metric POST failed  tool=%s  url=%s", payload.get("endPoint", "?"), _METRIC_URL, exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Fire-and-forget helper
# ---------------------------------------------------------------------------

def fire_and_forget(payload: Dict[str, Any], api_key: str = "") -> None:
    """Schedule a non-blocking background metric post.

    In HTTP mode (running event loop) this creates an asyncio task.
    In stdio mode it spawns a daemon thread so the loop is never blocked.
    Does nothing when IMMPORT_METRICS_ENABLED is false.
    """
    if not settings.immport_metrics_enabled:
        return
    try:
        asyncio.get_running_loop()
        # We're inside an async context — schedule as a task.
        log.debug("Metric scheduling async task for tool=%s", payload.get("endPoint", "?"))
        asyncio.create_task(post_metric(payload, api_key))
    except RuntimeError:
        # No running loop (stdio mode) — run in a daemon thread.
        log.debug("Metric scheduling thread for tool=%s", payload.get("endPoint", "?"))

        def _run(p: Dict[str, Any], k: str) -> None:
            try:
                asyncio.run(post_metric(p, k))
            except Exception:
                log.warning("Background metric post failed", exc_info=True)

        threading.Thread(target=_run, args=(payload, api_key), daemon=True).start()
