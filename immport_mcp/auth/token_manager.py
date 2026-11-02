"""
ImmPort uses static 30-day JWT API keys — NOT OAuth2 client credentials.

Obtain your key from: https://www.immport.org/auth/api/keys
Download the JSON file and copy the value of the "api_key" field.
Set it as IMMPORT_API_KEY in your .env file (local/single-user deployment).

For multi-user HTTP deployments: users pass their own ImmPort API key as the
Bearer token in the Authorization header. The server forwards it directly to
ImmPort. No server-level key is needed in that case.
"""
from __future__ import annotations
import base64
import json
import warnings
from contextvars import ContextVar

from immport_mcp.config.settings import settings

# Per-request API key (set by the HTTP middleware from the incoming Authorization header)
_request_api_key: ContextVar[str] = ContextVar("request_api_key", default="")


def set_request_api_key(key: str) -> None:
    """Called by HTTP middleware to inject the per-request key."""
    _request_api_key.set(key)


def warn_if_missing() -> None:
    """Emit a startup warning if no server-level IMMPORT_API_KEY is configured.
    In multi-user HTTP mode this is expected — users supply their own keys."""
    if not settings.immport_api_key and settings.mcp_transport == "stdio":
        warnings.warn(
            "IMMPORT_API_KEY is not set. Search and lookup tools will work, but "
            "study data, result, file manifest, and DRS download tools will fail. "
            "Obtain a 30-day API key from https://www.immport.org/auth/api/keys "
            "and set IMMPORT_API_KEY in your environment or .env file.",
            stacklevel=2,
        )


def get_auth_header() -> dict[str, str]:
    """Return the Authorization header for authenticated ImmPort API requests.

    Resolution order:
    1. Per-request key (from incoming Bearer token in HTTP mode)
    2. Server-level key (from IMMPORT_API_KEY env var / .env)
    """
    key = _request_api_key.get() or settings.immport_api_key
    if not key:
        raise RuntimeError(
            "No ImmPort API key available. "
            "In stdio mode: set IMMPORT_API_KEY in .env. "
            "In HTTP mode: pass your ImmPort API key as the Authorization: Bearer <key> header."
        )
    return {"Authorization": f"Bearer {key}"}


def get_current_username() -> str:
    """Decode the `sub` claim from the active API key JWT.

    Uses the same resolution order as get_auth_header() — per-request key
    first, then server-level fallback. Returns 'anonymousUser' if no token
    is available or the token cannot be decoded.
    """
    token = _request_api_key.get() or settings.immport_api_key
    if not token:
        return "anonymousUser"
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return "anonymousUser"
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        sub = claims.get("sub") or claims.get("username") or claims.get("user")
        return sub if sub else "anonymousUser"
    except Exception:
        return "anonymousUser"
