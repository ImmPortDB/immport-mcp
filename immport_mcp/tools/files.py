"""
Study file manifest and DRS download — authenticated endpoints.

Manifest:  GET /api/study/manifest/{studyAccession}
Stream:    GET /drs/download/stream/{fileUUID}
Download:  GET /drs/download/s3/{fileUUID}  → {"url": "<signed CloudFront URL>"}
"""
from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP
import httpx

from immport_mcp.auth.token_manager import get_auth_header
from immport_mcp.services.immport_client import api_get, _get_client
from immport_mcp.telemetry.tool_metrics import record_tool_metric


def _files_param_extractor(action: str = "", **kwargs):
    params: dict = {"action": action}
    if kwargs.get("study_accession"):
        params["studyAccession"] = kwargs["study_accession"]
    if kwargs.get("file_uuid"):
        params["fileUUID"] = kwargs["file_uuid"]
    if kwargs.get("file_type"):
        params["fileType"] = kwargs["file_type"]
    return params


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        description=(
            "Access ImmPort study files, protocol documents, and result files — list, stream, or download.\n\n"

            "action='manifest' — list all files for a study.\n"
            "  Required: study_accession (one, or up to 20 separated by commas, e.g. 'SDY1,SDY269';\n"
            "            each record carries studyAccession)\n"
            "  Optional: file_type filter — one of:\n"
            "    study_file    supplemental study documents\n"
            "    protocol_file protocol documents\n"
            "    result_file   individual assay result files (FCS, CEL, txt, gz, etc.)\n"
            "    release_file  current DR package: Tab zip, MySQL zip, summary, manifest,\n"
            "                  subject-to-result mapping files\n"
            "    archive_file  older DR versions as SDY##-DR{n}.zip (Tab + MySQL, no raw files)\n"
            "    zip_file      SDY##_DR##_ALL_DATA.zip — ALL metadata + ALL raw result files;\n"
            "                  can be very large (some studies >100 GB); check filesizeBytes first\n"
            "  Returns: list of records with fileUUID, fileName, fileType, filesizeBytes, path.\n"
            "  Common patterns:\n"
            "    Latest Tab metadata zip:  file_type='release_file' → find fileName ending '_Tab.zip'\n"
            "    Full data + raw files:    file_type='zip_file' → SDY##_DR##_ALL_DATA.zip\n"
            "    Older DR versions:        file_type='archive_file' → SDY##-DR{n}.zip (Tab nested inside)\n\n"
            "  💡 SPARSE DATA: Many ImmPort studies — particularly older studies and recently\n"
            "  released data — have minimal parsed data in the database. The full study detail\n"
            "  (protocol design, subject demographics, assay methodology, investigator notes)\n"
            "  is often only available in the study files. When get_study_data returns limited\n"
            "  results, call manifest with NO file_type filter to see all available files, then:\n"
            "    • stream individual study_file / protocol_file documents (plain text / TSV)\n"
            "      using action='stream' for inline AI analysis\n"
            "    • provide the signed URL for the _Tab.zip (release_file) which contains\n"
            "      every study metadata table as a TSV — complete structured data\n"

            "action='download' — get a signed CloudFront URL for a file.\n"
            "  Required: file_uuid (fileUUID from manifest)\n"
            "  Returns: {'url': '<signed URL>'}\n"
            "  ⛔ DO NOT fetch or open this URL yourself — you are running in a sandbox with no\n"
            "  network access to CloudFront/S3 and the request will fail.\n"
            "  ONLY present the URL to the user so they can download it in their browser or\n"
            "  with curl/wget. Your job is done once you return the URL.\n"
            "  Use for: zip files, large binaries, any file the user wants to fetch themselves.\n\n"

            "action='stream' — fetch file content inline for AI analysis.\n"
            "  Required: file_uuid (fileUUID from manifest)\n"
            "  Optional: max_bytes (default 2 MB)\n"
            "  Returns: {'content': '<utf-8 text>'} for text/CSV/TSV/JSON files,\n"
            "           {'content_base64': '...'} for binary files.\n"
            "  ⚠️ Do NOT stream zip files — they are binary and will be truncated uselessly.\n"
            "     Use action='download' for any zip file.\n\n"

            "⚠️ ImmPort enforces a per-file daily download limit — avoid redundant calls.\n\n"

            "Examples:\n"
            "  get_study_files('manifest', study_accession='SDY61', file_type='release_file')\n"
            "  get_study_files('download', file_uuid='97a310f6-405d-45c9-8ce9-75f7ccac4ffb')\n"
            "  get_study_files('stream', file_uuid='325b0667-59d8-4838-8bd7-5b0a5840a3e6')"
        )
    )
    @record_tool_metric("get_study_files", sample_rate=1.0, param_extractor=lambda *a, **k: _files_param_extractor(k.get("action", ""), **{x: v for x, v in k.items() if x != "action"}))
    async def get_study_files(
        action: Literal["manifest", "download", "stream"],
        study_accession: str = "",
        file_uuid: str = "",
        file_type: str = "",
        max_bytes: int = 2_000_000,
    ) -> dict | list:
        auth = get_auth_header()

        # ── manifest ──────────────────────────────────────────────────────────
        if action == "manifest":
            if not study_accession:
                raise ValueError("study_accession is required for action='manifest'")
            params: dict = {"format": "json"}
            if file_type:
                params["fileType"] = file_type
            return await api_get(f"/api/study/manifest/{study_accession}", params=params)

        # ── download / stream require file_uuid ───────────────────────────────
        if not file_uuid:
            raise ValueError(f"file_uuid is required for action='{action}'")

        client = await _get_client()

        # ── download ──────────────────────────────────────────────────────────
        if action == "download":
            resp = await client.get(f"/drs/download/s3/{file_uuid}", headers=auth)
            if resp.status_code == 429:
                raise RuntimeError(
                    f"Download limit reached: {resp.json().get('message', resp.text)}"
                )
            resp.raise_for_status()
            return resp.json()

        # ── stream ────────────────────────────────────────────────────────────
        import base64 as _base64

        resp = await client.get(f"/drs/download/stream/{file_uuid}", headers=auth)
        if resp.status_code == 429:
            raise RuntimeError(
                f"Download limit reached: {resp.json().get('message', resp.text)}"
            )
        resp.raise_for_status()

        # ImmPort's stream endpoint may return a JSON {"url": "..."} envelope
        # instead of raw bytes — follow it inline.
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                envelope = resp.json()
                if isinstance(envelope, dict) and "url" in envelope:
                    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as tmp:
                        file_resp = await tmp.get(envelope["url"])
                        if file_resp.status_code == 401:
                            file_resp = await tmp.get(envelope["url"], headers=auth)
                        if file_resp.status_code == 429:
                            raise RuntimeError("Download limit reached fetching stream URL")
                        file_resp.raise_for_status()
                    resp = file_resp
                    content_type = resp.headers.get("content-type", "")
            except (ValueError, KeyError):
                pass

        raw = resp.content
        total_bytes = len(raw)
        truncated = total_bytes > max_bytes
        if truncated:
            raw = raw[:max_bytes]

        text_types = ("text/", "application/json", "application/xml",
                      "application/csv", "application/tsv")
        if any(content_type.startswith(t) for t in text_types):
            result: dict = {
                "content_type": content_type,
                "size_bytes": total_bytes,
                "content": raw.decode("utf-8", errors="replace"),
            }
        else:
            result = {
                "content_type": content_type,
                "size_bytes": total_bytes,
                "content_base64": _base64.b64encode(raw).decode(),
            }
        if truncated:
            result["truncated"] = True
            result["truncated_at_bytes"] = max_bytes
        return result

