"""
Lookup cache — pre-fetches all ImmPort controlled vocabulary tables at startup
and refreshes them on a configurable background interval.

All lookup endpoints are public (no auth required) and the data changes only
on ImmPort data releases (a few times per year), so serving from memory is safe.

Usage:
    from immport_mcp.services.lookup_cache import lookup_cache

    await lookup_cache.warm()                        # call once at startup
    lookup_cache.start_background_refresh(hours=24)  # start periodic refresh
    data = lookup_cache.get("lkRace")                # instant in-memory lookup
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from immport_mcp.services.immport_client import api_get

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry: API table name → human-readable description
# ---------------------------------------------------------------------------
LOOKUP_TABLES: dict[str, str] = {
    # Subject / demographic
    "lkRace":                 "Race values used in subject demographic data",
    "lkEthnicity":            "Ethnicity values used in subject demographic data",
    "lkSpecies":              "Species values (e.g. Homo sapiens, Mus musculus)",
    "lkSubjectLocation":      "Subject location values used in studies",
    "lkAncestralPopulation":  "Ancestral population values used in subject data",
    "lkSex":                  "Sex values (e.g. Female, Male, Not Specified)",
    "lkAgeEvent":             "Age event values (e.g. Age at enrollment)",
    # Disease / condition
    "lkDisease":              "Disease/condition names with disease ontology IDs",
    "lkDiseaseStage":         "Disease stage values",
    "lkResearchFocus":        "Research focus areas (e.g. Vaccine Response, Allergy)",
    # Sample / biosample
    "lkSampleType":           "Biosample types (e.g. PBMC, Whole Blood, Serum)",
    "lkSourceType":           "Source types (e.g. File, Public Repository)",
    # Experiment / assay
    "lkExpMeasurementTech":   "Measurement techniques (e.g. Flow Cytometry, ELISA, RNA sequencing)",
    "lkExpsampleResultSchema": "Experiment sample result schema values (e.g. PUBLIC REPOSITORY)",
    "lkReagentType":          "Reagent types used in experiment metadata",
    "lkPlateType":            "Plate types used in experiment/assay metadata",
    # Analyte
    "lkAnalyte":              "Analyte reference: gene symbols, gene IDs, UniProt entries, protein ontology IDs, immunology symbols",
    "lkProteinName":          "Protein names with UniProt IDs",
    "lkHmdb":                 "HMDB metabolite entries used in metabolomics data",
    # Immunogenetics
    "lkLocusName":            "HLA locus names (e.g. HLA-A, HLA-B, HLA-DRB1)",
    "lkCellPopulation":       "Cell populations that appear in flow cytometry (FCS) response data",
    "lkCellPopulationMarker": "Cell population marker values used in flow cytometry panel metadata",
    # Study / program
    "lkStudyPanel":           "Study panel values used in study categorization",
    "lkStudyFileType":        "Study file types (e.g. study_file, protocol, result)",
    "lkPublicRepository":     "Public repository names (e.g. GEO, SRA, ArrayExpress)",
    "lkDataCompleteness":     "Data completeness level descriptions",
    "lkVisibilityCategory":   "Visibility category values",
    # Personnel / organization
    "lkPersonnelRole":        "Personnel roles (e.g. Principal Investigator, Co-Investigator)",
    "lkUserRoleType":         "User role types used in system access control",
    "lkOrganization":         "Organization names used in study personnel data",
    # Clinical / miscellaneous
    "lkTimeUnit":             "Time units (e.g. Days, Hours, Weeks)",
    "lkT0Event":              "T0 event values (study time zero reference events)",
    "lkUnitOfMeasure":        "Units of measure used across all result types",
    "lkAdverseEventSeverity": "Adverse event severity values (e.g. Grade 1 Mild, Grade 2 Moderate)",
    "lkCompoundRole":         "Compound/intervention roles (e.g. Vaccine, Adjuvant)",
    "lkCriterionCategory":    "Criterion categories used in study eligibility criteria",
    "lkExposureProcess":      "Exposure process values used in immunization/exposure data",
    "lkExposureMaterial":     "Exposure materials (e.g. vaccine names, pathogens)",
    "lkVirusStrain":          "Virus strains with taxonomy IDs used in HAI/neutralization assays",
    "lkTranscriptType":       "Transcript types (e.g. mRNA, microRNA)",
    "lkLabTestPanelName":     "Lab test panel names (e.g. CBC, BMP) — response field only, not filterable",
    "lkLabTestName":          "Lab test names with CDISC codes — response field only, not filterable",
    "lkMassSpectrometryType": "Mass spectrometry types used in proteomics/metabolomics metadata",
    "lkFileDetail":           "File detail types used in study file metadata",
    "lkProtocolType":         "Protocol types (e.g. Informed Consent, Lab Test)",
}


class LookupCache:
    """In-memory cache for ImmPort controlled vocabulary tables."""

    def __init__(self) -> None:
        self._data: dict[str, list[Any]] = {}
        self._last_refresh: datetime | None = None
        self._refresh_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, table: str) -> list[Any]:
        """Return cached data for a table, or [] if not yet loaded."""
        return self._data.get(table, [])

    def all_tables(self) -> dict[str, str]:
        """Return the full registry of table name → description."""
        return dict(LOOKUP_TABLES)

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    @property
    def loaded_count(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------------
    # Warm-up
    # ------------------------------------------------------------------

    async def warm(self) -> None:
        """
        Fetch all lookup tables concurrently and store in memory.
        Safe to call multiple times — subsequent calls refresh the cache.
        """
        log.info("LookupCache: warming %d tables...", len(LOOKUP_TABLES))
        results = await asyncio.gather(
            *[self._fetch(table) for table in LOOKUP_TABLES],
            return_exceptions=True,
        )
        loaded = 0
        for table, result in zip(LOOKUP_TABLES, results):
            if isinstance(result, BaseException):
                log.warning("LookupCache: failed to load %s: %s", table, result)
            else:
                self._data[table] = result
                loaded += 1

        self._last_refresh = datetime.now(timezone.utc)
        log.info("LookupCache: warmed %d/%d tables at %s", loaded, len(LOOKUP_TABLES), self._last_refresh.isoformat())

    # ------------------------------------------------------------------
    # Background refresh
    # ------------------------------------------------------------------

    def start_background_refresh(self, hours: float = 24.0) -> None:
        """
        Start a background asyncio task that re-warms the cache every `hours` hours.
        Should be called once after the event loop is running (e.g. after server start).
        """
        if self._refresh_task and not self._refresh_task.done():
            log.debug("LookupCache: background refresh already running")
            return

        interval_secs = hours * 3600

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval_secs)
                log.info("LookupCache: background refresh triggered (interval=%.1fh)", hours)
                try:
                    await self.warm()
                except Exception as exc:  # pragma: no cover
                    log.error("LookupCache: background refresh failed: %s", exc)

        self._refresh_task = asyncio.create_task(_loop())
        log.info("LookupCache: background refresh scheduled every %.1f hours", hours)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch(self, table: str) -> list[Any]:
        return await api_get(f"/api/lookup/{table}", params={"format": "json"}, requires_auth=False)


# Module-level singleton
lookup_cache = LookupCache()
