"""
Shared async HTTP client with:
- Connection pooling (single httpx.AsyncClient instance)
- TTL response cache (cachetools)
- Automatic retry with exponential backoff (tenacity)
- Clean param serialization: list values become repeated query params
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx
from cachetools import TTLCache
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from immport_mcp.auth.token_manager import get_auth_header, get_current_username
from immport_mcp.config.settings import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response cache — keyed by (method, path, sorted-params)
# ---------------------------------------------------------------------------
_cache: TTLCache = TTLCache(
    maxsize=settings.response_cache_maxsize,
    ttl=settings.response_cache_ttl,
)

# Shared client — created lazily
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.immport_base_url,
            timeout=60.0,
            follow_redirects=True,
        )
    return _client


def _cache_key(path: str, params: dict) -> str:
    raw = json.dumps({"path": path, "params": params}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _build_params(params: dict) -> list[tuple[str, str]]:
    """
    Convert dict to list of (key, value) pairs so that list values
    become repeated query parameters: studyAccession=SDY1&studyAccession=SDY2
    """
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        if v is None or v == "" or v == [] or v == 0 and k not in ("page", "pageSize", "fromRecord", "pageSize"):
            continue
        if isinstance(v, list):
            for item in v:
                if item not in (None, ""):
                    out.append((k, str(item)))
        else:
            out.append((k, str(v)))
    return out


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

# Facets that just re-list the accession of every matched hit — never useful
# as a "distribution" facet, so always excluded even if explicitly requested.
_ACCESSION_FACET_KEYS = {"study_accession", "subject_accession"}
_VALID_FACET_NAMES = {
    "gender", "race", "ethnicity", "species", "clinical_trial", "condition_or_disease",
    "research_focus", "assay_method", "program_name", "biosample_type", "has_lab_test",
    "has_assessment", "age",
}
_DEFAULT_FACET_LIMIT = 15


def _trim_buckets(agg: dict, limit: int) -> dict:
    """Drop zero-count buckets, cap at `limit`, fold the rest into 'other'.

    Honors OpenSearch's own sum_other_doc_count (set when the backend already capped
    buckets server-side, e.g. via compactFacets) in addition to any further trimming
    done here, so a backend-side remainder is never silently dropped.
    """
    buckets = agg.get("buckets")
    if isinstance(buckets, dict):  # keyed range agg (e.g. "age")
        pairs = [(k, v.get("doc_count", 0)) for k, v in buckets.items()]
    elif isinstance(buckets, list):
        pairs = [(b.get("key"), b.get("doc_count", 0)) for b in buckets]
    else:
        return {}
    pairs = [(k, c) for k, c in pairs if c > 0]
    top, rest = pairs[:limit], pairs[limit:]
    out = dict(top)
    remainder = sum(c for _, c in rest) + agg.get("sum_other_doc_count", 0)
    if remainder:
        out["other"] = remainder
    return out


def shape_search_response(
    data: Any,
    item_key: str,
    facets: list[str] | None = None,
    facet_limit: int = _DEFAULT_FACET_LIMIT,
    include_score: bool = False,
    include_highlights: bool = False,
) -> Any:
    """Flatten a native OpenSearch _search response into a compact MCP-friendly shape.

    Replaces the raw OpenSearch envelope (_index/_score/_shards/highlight/etc.) with
    `{total, returned, <item_key>: [...]}`. Aggregations are only computed and returned
    when `facets` is given, and even then are trimmed to zero-count-removed, capped at
    `facet_limit`, "other"-folded buckets — see `_trim_buckets`. Accession facets are
    always dropped since they duplicate the hits. Item order always matches the
    backend's relevance ranking even when `_score` itself isn't included.
    """
    if not isinstance(data, dict):
        return data
    hits_block = data.get("hits", {})
    total = hits_block.get("total", {})
    total_value = total.get("value", total) if isinstance(total, dict) else total
    raw_hits = hits_block.get("hits", [])
    if include_score or include_highlights:
        items = []
        for h in raw_hits:
            item = dict(h.get("_source", h))
            if include_score and h.get("_score") is not None:
                item["relevance_score"] = h["_score"]
            if include_highlights and h.get("highlight"):
                item["highlights"] = h["highlight"]
            items.append(item)
    else:
        items = [h.get("_source", h) for h in raw_hits]

    shaped: dict = {"total": total_value, "returned": len(items), item_key: items}

    if facets:
        unknown = [f for f in facets if f not in _VALID_FACET_NAMES and f not in _ACCESSION_FACET_KEYS]
        if unknown:
            raise ValueError(
                f"Unknown facet name(s): {unknown}. Valid facet names are: "
                f"{sorted(_VALID_FACET_NAMES)}. See the immport://search-fields resource "
                "for the full field reference."
            )
        aggs = data.get("aggregations", {})
        out_facets = {}
        for name in facets:
            if name in _ACCESSION_FACET_KEYS:
                continue
            agg = aggs.get(name)
            buckets = agg.get("buckets") if isinstance(agg, dict) else None
            if isinstance(buckets, (list, dict)):
                out_facets[name] = _trim_buckets(agg, facet_limit)
        if out_facets:
            shaped["facets"] = out_facets

    return shaped


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.ConnectError, httpx.TimeoutException))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def api_get(
    path: str,
    params: dict | None = None,
    use_cache: bool = True,
    requires_auth: bool = True,
) -> Any:
    """
    Perform an authenticated (or public) GET against the ImmPort API.

    Args:
        path: Path relative to the base URL, e.g. "/api/search/study"
        params: Query parameters dict; list values are expanded to repeated params.
        use_cache: Whether to use/populate the TTL response cache.
        requires_auth: Set False for public endpoints (search, lookup).
    """
    clean_params = params or {}
    param_list = _build_params(clean_params)

    if use_cache and settings.response_cache_ttl > 0:
        key = _cache_key(path, dict(param_list))
        if key in _cache:
            return _cache[key]

    headers = get_auth_header() if requires_auth else {}
    client = await _get_client()

    log.info("GET %s%s  user=%s", settings.immport_base_url.rstrip("/"), path, get_current_username()) if "/api/lookup/" not in path else log.debug("GET %s%s  user=%s", settings.immport_base_url.rstrip("/"), path, get_current_username())
    try:
        resp = await client.get(path, params=param_list, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.warning(
            "GET %s → HTTP %s  %s",
            path,
            exc.response.status_code,
            exc.response.text[:400],
        )
        raise
    except Exception:
        log.warning("GET %s failed", path, exc_info=True)
        raise

    data = resp.json()

    if use_cache and settings.response_cache_ttl > 0:
        _cache[key] = data  # type: ignore[possibly-undefined]

    return data
