"""
Study Data tool — authenticated endpoints.
All paths follow the pattern: GET /api/study/{resource}/{studyAccession}, where
studyAccession may be a comma-separated list of up to 20 studies.

A single tool covers all 24 study sub-resources via a Literal `resource` parameter.
get_release_notes covers the data release endpoints (/api/release, /api/study/releaseNotes).
"""
from __future__ import annotations

import asyncio
import re
from typing import Literal

import httpx
from fastmcp import FastMCP

from immport_mcp.auth.token_manager import get_auth_header
from immport_mcp.services.immport_client import api_get, _get_client
from immport_mcp.telemetry.tool_metrics import record_tool_metric

# Registry: resource slug → (endpoint_suffix, description, large_warning)
_RESOURCES: dict[str, tuple[str, str, bool]] = {
    "summary":           ("summary",            "▶ START HERE for any study workflow. Rich summary: title, PI, description, objectives, endpoints, hypothesis, subjects count, interventions, data completeness, publications, personnel, links", False),
    "basic_info":        ("",                   "Basic study fields: title, briefDescription, doi, clinicalTrial, enrollment, age range, start/completion dates, sponsoring org", False),
    "arm":               ("arm",                "Study arms (cohorts/treatment groups): armAccession, name, description, typeReported, typePreferred (e.g. Placebo Comparator, Experimental)", False),
    "planned_visits":    ("plannedVisit",        "Planned visits (time points): plannedVisitAccession, name, orderNumber, minStartDay, maxStartDay, startRule, endRule", False),
    "conditions":        ("condition",           "Disease/conditions studied: conditionPreferred, conditionReported", False),
    "personnel":         ("personnel",           "Study personnel: personAccession, firstName, lastName, roleInStudy, titleInStudy, organization, email, orcid", False),
    "protocols":         ("protocol",            "Protocol documents: protocolAccession, type, name, description, fileName", False),
    "contract_program":  ("contract",            "Contract/funding: contractGrantId, contractGrantName, contractGrantCategory, programId, programCategory, programDescription", False),
    "inclusion_exclusion":("inclusionExclusion", "Inclusion/exclusion eligibility criteria: criterion text and category (Inclusion or Exclusion)", False),
    "pubmed":            ("pubmed",              "PubMed publications: pubmedId, doi, authors, title, journal, year, issue. For cross-study publication analysis (e.g. 'top journals'), use search_studies(source_fields=['pubmed.journal']) in bulk instead of looping this per study — see immport://search-fields", False),
    "file":              ("file",                "Study document files (case report forms, data dictionaries, protocols): studyFileAccession, studyFileType, description, fileName. NOT for downloadable result files — use files tool for those", False),
    "json_ld":           ("json-ld",             "JSON-LD (schema.org Dataset) structured metadata: title, abstract, DOI, creators, funders, health conditions, measurement techniques, citations, data access conditions", False),
    "links":             ("link",                "External links: studyLinkId, name, type, value (URL)", False),
    "experiments":       ("experiment",          "Experiments (assay runs): experimentAccession, name, description, measurementTechnique", False),
    "adverse_events":    ("adverseEvent",        "Adverse events: adverseEventAccession, subjectAccession, namePreferred, severityPreferred, outcomePreferred, causality, organOrBodySystemPreferred, startStudyDay, endStudyDay", False),
    # Large endpoints — may return many rows
    "demographic":       ("demographic",         "⚠️ LARGE — Subject demographics (one row per subject): subjectAccession, armAccession, armName, ageEvent, ageUnit, minSubjectAge, maxSubjectAge, gender (=sex field), race, ethnicity, ancestral_population, species, strain, subjectLocation. For deep analysis use Tab.zip (subject.txt) instead.", True),
    "biosample":         ("biosample",           "⚠️ LARGE — Subject→biosample: subjectAccession, biosampleAccession, biosampleName, type, subtype, plannedVisitAccession. For deep analysis use Tab.zip (biosample.txt) instead.", True),
    "expsample_mapping": ("expsample",           "⚠️ LARGE — Full Subject→BioSample→Experiment→ExpSample hierarchy. Prefer get_assay_results for assay data, or Tab.zip (expsample.txt) for the full hierarchy.", True),
    "interventions":     ("intervention",        "⚠️ LARGE — Subject-level interventions: compoundNameReported, compoundRole, dose, doseUnits, routeOfAdminPreferred, startDay, endDay, status. For deep analysis use Tab.zip (treatment.txt) instead.", True),
    "lab_tests":         ("labtest",             "⚠️ LARGE — Clinical lab test results (CBC, BMP etc): labTestPanelNamePreferred, labTestNamePreferred, resultValuePreferred, resultUnitPreferred, plannedVisitAccession. For deep analysis use Tab.zip or get_assay_results(assay_type='lab_test') instead.", True),
    "assessment":        ("assessment",          "⚠️ LARGE — Questionnaires/clinical assessments: assessmentComponentNamePreferred, resultValuePreferred, resultValueCategory, verbatimQuestion, studyDay. For deep analysis use Tab.zip (assessment_component.txt) instead.", True),
    "immune_exposure":   ("immuneExposure",      "⚠️ LARGE — Vaccinations/infections/challenges: exposureMaterialPreferred, exposureProcessPreferred, diseasePreferred, diseaseStagePreferred. For deep analysis use Tab.zip (immune_exposure.txt) instead.", True),
}

ResourceName = Literal[tuple(_RESOURCES.keys())]  # type: ignore[valid-type]

_MAX_STUDIES = 20

_TOC = "\n".join(
    f"  {name}: {desc}" + (" (⚠️ may be large)" if large else "")
    for name, (_, desc, large) in _RESOURCES.items()
)


async def _read_table_count(file_uuid: str) -> dict[str, int]:
    """Stream a _table_count.txt release file and parse its "table_name count" lines."""
    auth = get_auth_header()
    client = await _get_client()
    resp = await client.get(f"/drs/download/stream/{file_uuid}", headers=auth)
    if resp.status_code == 429:
        raise RuntimeError("Download limit reached fetching table counts")
    resp.raise_for_status()

    # Follow JSON envelope redirect if present (same pattern as files.py)
    if "application/json" in resp.headers.get("content-type", ""):
        try:
            envelope = resp.json()
            if isinstance(envelope, dict) and "url" in envelope:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as tmp:
                    resp = await tmp.get(envelope["url"])
                resp.raise_for_status()
        except (ValueError, KeyError):
            pass

    text = resp.content.decode("utf-8", errors="replace")
    tables: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            try:
                tables[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return tables


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        description=(
            "Get any sub-resource for one or more ImmPort studies by specifying the `resource` parameter. "
            "All endpoints require authentication. Use study_accession format SDY#### (e.g. SDY1, SDY305).\n\n"
            "MULTIPLE STUDIES: study_accession may list up to 20 studies separated by commas "
            "(e.g. 'SDY1,SDY269,SDY305') to get one combined result — every row carries studyAccession, "
            "so rows can be grouped per study. Studies without data for that resource are simply absent. "
            "Prefer one multi-study call over one call per study when comparing studies. "
            "Every resource returns a JSON array; 'summary' and 'json_ld' return one entry per study.\n\n"
            "⚡ EFFICIENCY RULE: Issue multiple resource calls IN PARALLEL — they are independent. "
            "This tool returns structured JSON directly and is the preferred approach for AI agents. "
            "Only use Tab.zip (get_study_files action='manifest' → action='download') when you need join tables "
            "with no API equivalent: reagent specs, expsample-to-reagent mapping, "
            "fcs_header_marker, or expsample-to-treatment joins.\n\n"
            "Available resources:\n" + _TOC
        )
    )
    @record_tool_metric(
        "get_study_data",
        sample_rate=1.0,
        param_extractor=lambda *a, **k: {
            "studyAccession": k.get("study_accession", ""),
            "resource": k.get("resource", ""),
        },
    )
    async def get_study_data(
        study_accession: str,
        resource: ResourceName,  # type: ignore[valid-type]
    ) -> dict | list:
        """
        Fetches the requested sub-resource for the given study accession(s).
        Resources marked ⚠️ LARGE may return thousands of rows for large studies;
        with several studies the rows add up.
        """
        accessions = [a.strip() for a in study_accession.split(",") if a.strip()]
        if len(accessions) > _MAX_STUDIES:
            raise ValueError(
                f"At most {_MAX_STUDIES} studies per call (got {len(accessions)}); split into several calls"
            )
        suffix, _, _ = _RESOURCES[resource]
        path = f"/api/study/{suffix}/{study_accession}" if suffix else f"/api/study/{study_accession}"
        return await api_get(path, params={"format": "json"})

    # ── data availability ───────────────────────────────────────────────────

    @mcp.tool(
        description=(
            "Get a record-count inventory for every ImmPort data category in a study — "
            "how many rows have been parsed out of the raw data and stored in the ImmPort database.\n\n"
            "⚡ CALL THIS FIRST when assessing what data a study contains. It reads the study's "
            "~5 KB release file listing every ImmPort data category alongside its record count "
            "for this specific study.\n\n"
            "MULTIPLE STUDIES: study_accession may list up to 20 studies separated by commas "
            "(e.g. 'SDY180,SDY212') — one call instead of one per study; the result is then a list "
            "with one entry per study, in the order given.\n\n"
            "USE THIS TO AVOID WASTED API CALLS:\n"
            "  • elisa_result=0 → skip get_assay_results(assay_type='elisa')\n"
            "  • hai_result=240 → study has HAI titer data worth fetching\n"
            "  • lab_test=0 → no clinical lab data, don't call get_study_data(resource='lab_tests')\n"
            "  • assessment_component=98893 → very large dataset, use Tab.zip instead of the API\n"
            "  • immune_exposure=0 → no vaccination/challenge records\n"
            "  • fcs_analyzed_result=0 but expsample=500 → raw FCS files exist but no gated data\n\n"
            "⚠️ IMPORTANT LIMITATION — PARSED DATA ONLY:\n"
            "These counts reflect only data that ImmPort has parsed and loaded into its relational "
            "database. Many studies — especially older studies, recently submitted data, and studies "
            "with non-standard formats — contain data exclusively inside their study files that has "
            "not been parsed into the database. A count of 0 does NOT mean the data doesn't exist; "
            "it means it hasn't been extracted into the ImmPort data model yet.\n\n"
            "When counts are low or zero for a study you expect to have data:\n"
            "  1. Call get_study_files(action='manifest', study_accession=...) with no file_type "
            "filter to see all available files.\n"
            "  2. Stream individual study_file or protocol_file documents "
            "(action='stream') — protocols, case report forms, and data dictionaries often describe "
            "data that was submitted but not parsed.\n"
            "  3. Get a signed URL for the Tab.zip (file_type='release_file' → find '_Tab.zip' → "
            "action='download') — it contains the complete set of parsed TSV files and is often "
            "more complete than the API responses.\n\n"
            "Returns (one study): {"
            "'studyAccession': 'SDY####', "
            "'dataRelease': 'DR##', "
            "'tables': {category_name: record_count, ...}"
            "}; a study without the release file gets {'studyAccession', 'error'} instead. "
            "Several studies: a list of these.\n\n"
            "Zero-count categories ARE included — 0 means not parsed into the database, "
            "not necessarily absent from the study.\n"
            "Requires authentication."
        )
    )
    @record_tool_metric(
        "get_study_data_availability",
        sample_rate=1.0,
        param_extractor=lambda *a, **k: {"studyAccession": k.get("study_accession", "")},
    )
    async def get_study_data_availability(study_accession: str) -> dict | list:
        """
        Fetches and parses the _table_count.txt release file of each study: one manifest
        call for all studies, then the files are read in parallel.
        """
        accessions = list(dict.fromkeys(
            a.strip().upper() for a in study_accession.split(",") if a.strip()
        ))
        if not accessions:
            raise ValueError("study_accession is required")
        if len(accessions) > _MAX_STUDIES:
            raise ValueError(
                f"At most {_MAX_STUDIES} studies per call (got {len(accessions)}); split into several calls"
            )

        # Step 1 — manifest restricted to release_file type (small subset), all studies at once
        manifest = await api_get(
            f"/api/study/manifest/{','.join(accessions)}",
            params={"format": "json", "fileType": "release_file"},
        )

        # Step 2 — locate each study's _table_count.txt (the newest release if there are several)
        found: dict[str, tuple[float, str, str]] = {}
        entries = manifest if isinstance(manifest, list) else manifest.get("data", [])
        for entry in entries:
            name = entry.get("fileName", "")
            file_uuid = entry.get("fileUUID") or entry.get("fileId")
            if not name.endswith("_table_count.txt") or not file_uuid:
                continue
            accession = (entry.get("studyAccession") or name.split("-")[0]).upper()
            m = re.search(r"DR(\d+(?:\.\d+)?)", name)
            version = float(m.group(1)) if m else 0.0
            if accession not in found or version > found[accession][0]:
                found[accession] = (version, f"DR{m.group(1)}" if m else "", file_uuid)

        # Step 3 — stream and parse the files in parallel
        async def availability(accession: str) -> dict:
            if accession not in found:
                return {
                    "studyAccession": accession,
                    "error": (
                        "_table_count.txt not found in release files. "
                        "The study may not have a published data release yet."
                    ),
                }
            _, data_release, file_uuid = found[accession]
            return {
                "studyAccession": accession,
                "dataRelease": data_release,
                "tables": await _read_table_count(file_uuid),
            }

        results = await asyncio.gather(*(availability(a) for a in accessions), return_exceptions=True)
        if len(results) == 1:
            if isinstance(results[0], BaseException):
                raise results[0]
            return results[0]
        # one unreadable file must not fail the other studies
        return [
            {"studyAccession": a, "error": f"Could not read the table count file: {r}"}
            if isinstance(r, BaseException) else r
            for a, r in zip(accessions, results)
        ]

    # ── release notes ───────────────────────────────────────────────────────

    @mcp.tool(
        description=(
            "ImmPort data releases and the release history of studies. Data releases are named "
            "DR<number> (e.g. DR69, DR58, point releases like DR52.1); version also accepts 58 or dr58. "
            "Requires authentication.\n\n"
            "Modes (chosen by which arguments are given):\n"
            "  • study_accession only → each study's release history, newest first: "
            "[{studyAccession, briefTitle, releases: [{version, releaseDate, status, releaseNotes}]}]. "
            "status is 'Initial' (first released) or 'Updated'. releaseNotes is often null for older releases. "
            "study_accession may list up to 20 studies separated by commas.\n"
            "  • study_accession + version='DR58' → only that release's entry for each study "
            "(404 if the study was not in that release).\n"
            "  • study_accession + version='latest' → each study's OWN most recent release "
            "(e.g. SDY1 → DR58, even if the current release is DR69).\n"
            "  • version only (e.g. 'DR69', or 'latest' for the newest release) → the release notes document "
            "the data release process published for that release, as is: release_version, release_date, "
            "study.studies[] (study_accession, brief_title, description, status, subjects, personnel, "
            "publications, assays, programs, release_notes on recent releases) and dataSummary "
            "(totals at that release). ⚠️ Large: 100–700 KB. 404 if its notes are not published yet.\n"
            "  • neither → list of all data releases, newest first: "
            "[{version, releaseDate, initialStudyCount, updatedStudyCount}].\n\n"
            "Examples:\n"
            "  get_release_notes()                                        # which releases exist\n"
            "  get_release_notes(version='latest')                        # what's in the newest release\n"
            "  get_release_notes(study_accession='SDY1')                  # when was SDY1 released/updated, and why\n"
            "  get_release_notes(study_accession='SDY1,SDY269', version='latest')"
        )
    )
    @record_tool_metric(
        "get_release_notes",
        sample_rate=1.0,
        param_extractor=lambda *a, **k: {
            "studyAccession": k.get("study_accession", ""),
            "version": k.get("version", ""),
        },
    )
    async def get_release_notes(study_accession: str = "", version: str = "") -> dict | list:
        """
        Release history of studies (/api/study/releaseNotes), the published notes of one
        data release (/api/release/{version}), or the list of releases (/api/release).
        """
        study_accession = study_accession.strip()
        version = version.strip()
        if study_accession:
            path, params = f"/api/study/releaseNotes/{study_accession}", ({"version": version} if version else None)
        elif version:
            path, params = f"/api/release/{version}", None
        else:
            path, params = "/api/release", None
        try:
            return await api_get(path, params=params)
        except httpx.HTTPStatusError as exc:
            # 400/404 here carry a useful message, e.g. "Study 'SDY1' was not part of data
            # release 'DR59'" or "Release notes for 'DR68.1' are not published yet"
            if exc.response.status_code in (400, 404):
                try:
                    message = exc.response.json().get("message")
                except ValueError:
                    message = None
                if message:
                    raise ValueError(message) from exc
            raise
