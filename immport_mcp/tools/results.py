"""
Assay Result tool — authenticated endpoints.
All paths follow the pattern: GET /result/{assay_type}

A single tool covers all 11 assay result endpoints via a Literal `assay_type` parameter.
All assay types share the same filter parameters; only the response fields differ.

The upstream ImmPort API has no server-side pagination, aggregation, or field
projection — a single call can return tens of thousands of rows (e.g. flow
cytometry gates one row per subject per population). To keep calls fast and
inside context limits, this wrapper adds client-side post-processing on top
of the raw API response:
  - `fields`: project down to just the columns you need
  - `name_contains`: substring-filter on the assay's "name" field
    (population, analyte, virus strain, gene, locus...) before returning
  - `summarize`: collapse rows into per-group n/mean/median/min/max stats
  - `limit` / `offset`: page through the (already-fetched) result set
These only shrink what comes back to the caller — they do not reduce load on
the upstream API, so narrowing with real filters (planned_visit_accession,
arm_accession, biosample_type, etc.) is still the best way to avoid timeouts.
"""
from __future__ import annotations

import statistics
from typing import Literal

import httpx
from fastmcp import FastMCP

from immport_mcp.services.immport_client import api_get
from immport_mcp.telemetry.tool_metrics import record_tool_metric

# Registry: assay_type slug → (endpoint_path, response_fields_description, name_field, value_field)
# name_field: the primary "response field only — not filterable" column callers most
#             often want to slice on client-side (population, analyte, virus strain...).
#             A tuple means: check ALL of these fields for a name_contains match (used
#             when the single most-obvious field is too generic/coarse to search on —
#             e.g. fcs_analyzed's populationNamePreferred is just "cell"/"lymphocyte"/
#             "CD4+ T cell"; the actual marker-level detail lives in the *Reported/
#             *DefnitionPreferred columns instead).
# value_field: the primary numeric column used as the default for summarize=True.
AssayNameField = str | tuple[str, ...] | None
_ASSAYS: dict[str, tuple[str, str, AssayNameField, str | None]] = {
    "elisa": (
        "/result/elisa",
        "analytePreferred, analyteReported, valuePreferred, valueReported, unitPreferred, unitReported, subjectAccession, armAccession, biosampleAccession, studyTimeCollected. "
        "NOTE: analyte/unit are response fields only — not filterable. "
        "Example studies: SDY406, SDY208, SDY268.",
        "analytePreferred",
        "valuePreferred",
    ),
    "elispot": (
        "/result/elispot",
        "analytePreferred, analyteReported, spotNumberPreferred, spotNumberReported, unitPreferred, unitReported, subjectAccession, armAccession, biosampleAccession, studyTimeCollected.",
        "analytePreferred",
        "spotNumberPreferred",
    ),
    "fcs_analyzed": (
        "/result/fcsAnalyzed",
        "populationDefnPreferred, populationNamePreferred, populationStatisticPreferred (count or %), workspaceFileInfoId. "
        "Flow cytometry / CyTOF gated cell population data.",
        ("populationNameReported", "populationDefnitionReported", "populationDefnitionPreferred"),
        "populationStatisticPreferred",
    ),
    "file_paths": (
        "/result/filePath",
        "filePathId, fileInfoId, fileDetail, filesizeBytes, fileName, originalFileName, sourceType, sourceAccession, experimentAccession, measurementTechnique, filePath. "
        "Use with measurement_technique filter for gene expression files (RNA sequencing, Microarray, Nanostring). "
        "⚠️ Does NOT return fileUUID — you cannot use these results with get_study_files(action='download'/'stream'). "
        "To download files, use get_study_files(action='manifest', file_type='result_file') which returns fileUUID.",
        "fileName",
        None,
    ),
    "hai": (
        "/result/hai",
        "virusStrainPreferred, virusStrainReported, valuePreferred, valueReported, unitPreferred, unitReported, subjectAccession, studyTimeCollected. "
        "Hemagglutination inhibition — influenza antibody titers post-vaccination. "
        "NOTE: virusStrain is a response field only — not filterable.",
        "virusStrainPreferred",
        "valuePreferred",
    ),
    "hla_typing": (
        "/result/hlaTyping",
        "allele1, allele2, locusName (HLA-A/B/C/DR/DQ/DP), subjectAccession, biosampleAccession, experimentAccession. "
        "HLA typing (Class I and II). NOTE: locusName/allele are response fields only — not filterable.",
        "locusName",
        None,
    ),
    "kir_typing": (
        "/result/kirTyping",
        "locusName, alleleGroup, subjectAccession, biosampleAccession, comments. "
        "KIR (killer-cell immunoglobulin-like receptor) gene presence/absence. NK cell biology / transplant.",
        "locusName",
        None,
    ),
    "mass_spectrometry": (
        "/result/massSpectrometryResult",
        "metaboliteNamePreferred, metaboliteNameReported, proteinNamePreferred, proteinNameReported, "
        "databaseIdPreferred, intensity, mzratio, zcharge, retentionTime, retentionTimeUnit, massSpectrometryType. "
        "Proteomics / metabolomics.",
        ("metaboliteNamePreferred", "metaboliteNameReported", "proteinNamePreferred", "proteinNameReported"),
        "intensity",
    ),
    "mbaa": (
        "/result/mbaa",
        "analyteAccession, analytePreferred, analyteReported, valuePreferred, valueReported, unitPreferred, unitReported, sourceAccession, sourceType, repositoryAccession. "
        "Multiplex bead array (Luminex, Meso Scale Discovery) — cytokine/chemokine panels, antibody profiling. "
        "NOTE: analyte/unit are response fields only — not filterable.",
        "analytePreferred",
        "valuePreferred",
    ),
    "neut_ab_titer": (
        "/result/neutAbTiter",
        "virusStrainPreferred, virusStrainReported, valuePreferred, valueReported, unitPreferred, unitReported, subjectAccession, studyTimeCollected. "
        "Neutralizing antibody titers (plaque reduction, microneutralization). Vaccine efficacy studies.",
        "virusStrainPreferred",
        "valuePreferred",
    ),
    "pcr": (
        "/result/pcr",
        "geneId, geneName, valuePreferred, valueReported, unitPreferred, unitReported, subjectAccession, studyTimeCollected, biosampleAccession, expsampleAccession. "
        "RT-PCR / Q-PCR gene expression.",
        "geneName",
        "valuePreferred",
    ),
}

AssayType = Literal[tuple(_ASSAYS.keys())]  # type: ignore[valid-type]

_ASSAY_TOC = "\n".join(
    f"  {name}: {resp}"
    for name, (_, resp, _n, _v) in _ASSAYS.items()
)

_FILTER_HELP = """
Common filter parameters (all optional except study_accession):
- study_accession: required list of SDY accessions, e.g. ['SDY406', 'SDY208']
- arm_accession / arm_name: filter by study arm
- subject_accession / subject_phenotype: filter by specific subjects
- sex: 'Female', 'Male' (use lookup_controlled_vocabulary('lkSex') for valid values)
- race: e.g. 'White' (use lookup_controlled_vocabulary('lkRace'))
- ethnicity: e.g. 'Hispanic or Latino' (use lookup_controlled_vocabulary('lkEthnicity'))
- ancestral_population (use lookup_controlled_vocabulary('lkAncestralPopulation'))
- age_event: e.g. 'Age at enrollment' (use lookup_controlled_vocabulary('lkAgeEvent'))
- age_unit: 'Years', 'Months' etc
- min_subject_age_gte/lte/gt/lt, max_subject_age_gte/lte/gt/lt: age range filters
- biosample_accession / biosample_type / biosample_subtype (use lookup_controlled_vocabulary('lkSampleType'))
- measurement_technique (use lookup_controlled_vocabulary('lkExpMeasurementTech'))
- experiment_accession / expsample_accession / planned_visit_accession / treatment_accession
- species / strain (use lookup_controlled_vocabulary('lkSpecies'))
- clinical: 'Y' = clinical result, 'N' = non-clinical
- study_time_collected / _gte / _lte / _gt / _lt: time relative to the study_time_t0_event,
  in whatever unit that study's data uses (check the response's studyTimeCollectedUnit field —
  commonly 'Days'; do not assume). _gte/_lte are inclusive, _gt/_lt are exclusive.
- study_time_collected_unit: 'Days', 'Hours', 'Weeks' — filters to rows recorded in this unit;
  does NOT convert study_time_collected's value into this unit.
- study_time_t0_event / study_time_t0_event_specify (use lookup_controlled_vocabulary('lkT0Event'))
"""

_POSTPROCESS_HELP = """
Response-shrinking options (applied to the already-fetched rows, client-side —
use these instead of pulling everything and filtering it yourself afterward):
- name_contains: case-insensitive substring match against each assay's "name" field(s).
  For most assay types this is one field (analyte for elisa/mbaa/elispot, virus strain
  for hai/neut_ab_titer, gene for pcr, locus for hla_typing/kir_typing). For fcs_analyzed
  and mass_spectrometry it checks SEVERAL fields at once, because the single most
  obvious field is too generic to be useful: fcs_analyzed's populationNamePreferred is
  only ever "cell"/"lymphocyte"/"CD4+ T cell" etc — marker-level detail (e.g. "CD95")
  lives in populationNameReported/populationDefnitionReported/populationDefnitionPreferred,
  so name_contains checks all three of those instead. Similarly mass_spectrometry checks
  both metabolite and protein name fields. e.g. name_contains="CD95" on fcs_analyzed keeps
  only rows where any of those three fields contains "CD95".
- fields: list of response field names to keep — drops every other column.
- summarize=True: collapse rows into per-group stats instead of returning every row.
  Groups by `group_by` (default: armAccession + studyTimeCollected, or just armAccession
  if the latter is absent) over `value_field` (default: the assay's primary numeric field —
  valuePreferred, populationStatisticPreferred, spotNumberPreferred, or intensity, depending
  on assay_type). Each group reports BOTH `n` (row count) and `n_subjects` (distinct
  subjectAccession count) — use n_subjects, not n, when you need "how many subjects" rather
  than "how many rows". These differ a lot for long-format assays: a single flow cytometry
  visit is one row per subject x population x statistic, so n can be 100x+ larger than
  n_subjects for the same group. Falls back to a count-only summary for assay types with no
  numeric field (file_paths, hla_typing, kir_typing).
- limit / offset: page through the fetched rows (default: no limit, offset=0).
  Response always includes total_rows/returned_rows so you know if you're
  seeing everything.
⚠️ These only shrink the payload returned to you — the upstream ImmPort API
still computes the full unfiltered result set, so narrowing with real filters
(planned_visit_accession, arm_accession, biosample_type...) is still the best
way to avoid slow calls or timeouts.

Recipe — find which visits actually have data before guessing visit codes:
  get_assay_results(assay_type=..., study_accession=[...], summarize=True,
                     group_by=["plannedVisitAccession"], fields=["plannedVisitAccession"])
Note group_by/fields use the raw response field names (camelCase, e.g. plannedVisitAccession),
not the snake_case filter parameter names (planned_visit_accession) — they operate on the
already-fetched rows, not on the upstream query.

Recipe — per-subject-deduplicated mean by arm and visit (avoids inflated n from long format):
  get_assay_results(assay_type="fcs_analyzed", study_accession=[...], name_contains="CD95",
                     summarize=True, group_by=["armAccession", "plannedVisitAccession"])
  # read n_subjects per group, not n, for the actual randomized/sampled subject count.
"""


def _project_fields(rows: list[dict], fields: list[str] | None) -> list[dict]:
    if not fields:
        return rows
    return [{k: row.get(k) for k in fields if k in row} for row in rows]


def _filter_name_contains(rows: list[dict], name_field: AssayNameField, name_contains: str | None) -> list[dict]:
    if not name_contains:
        return rows
    if not name_field:
        raise ValueError("name_contains is not supported for this assay_type (no name field defined)")
    fields = (name_field,) if isinstance(name_field, str) else name_field
    needle = name_contains.lower()
    return [row for row in rows if any(needle in str(row.get(f, "")).lower() for f in fields)]


def _summarize_rows(
    rows: list[dict],
    group_by: list[str] | None,
    value_field: str | None,
    default_group_by: list[str],
) -> list[dict]:
    group_fields = group_by or default_group_by
    group_fields = [f for f in group_fields if rows and f in rows[0]] or ["_all"]

    groups: dict[tuple, list[float | None]] = {}
    for row in rows:
        key = tuple(row.get(f) if f != "_all" else "all" for f in group_fields)
        groups.setdefault(key, [])
        if value_field:
            raw = row.get(value_field)
            try:
                groups[key].append(float(raw))
            except (TypeError, ValueError):
                pass  # non-numeric / missing values are excluded from stats but still counted below

    counts: dict[tuple, int] = {}
    for row in rows:
        key = tuple(row.get(f) if f != "_all" else "all" for f in group_fields)
        counts[key] = counts.get(key, 0) + 1

    subject_sets: dict[tuple, set] = {}
    for row in rows:
        key = tuple(row.get(f) if f != "_all" else "all" for f in group_fields)
        subj = row.get("subjectAccession")
        if subj is not None:
            subject_sets.setdefault(key, set()).add(subj)

    summary: list[dict] = []
    for key, values in groups.items():
        entry: dict = dict(zip(group_fields, key))
        entry["n"] = counts[key]
        if key in subject_sets:
            entry["n_subjects"] = len(subject_sets[key])
        if value_field:
            numeric = [v for v in values if v is not None]
            entry["n_numeric"] = len(numeric)
            if numeric:
                entry["mean"] = round(statistics.fmean(numeric), 4)
                entry["median"] = round(statistics.median(numeric), 4)
                entry["min"] = min(numeric)
                entry["max"] = max(numeric)
        summary.append(entry)
    return summary


def _build_params(
    study_accession: list[str],
    arm_accession: list[str] | None = None,
    arm_name: list[str] | None = None,
    subject_accession: list[str] | None = None,
    subject_phenotype: list[str] | None = None,
    study_title: list[str] | None = None,
    age_event: list[str] | None = None,
    age_event_specify: list[str] | None = None,
    age_unit: list[str] | None = None,
    sex: list[str] | None = None,
    race: list[str] | None = None,
    race_specify: list[str] | None = None,
    ethnicity: list[str] | None = None,
    ancestral_population: list[str] | None = None,
    species: list[str] | None = None,
    strain: list[str] | None = None,
    min_subject_age: float | None = None,
    min_subject_age_gte: float | None = None,
    min_subject_age_lte: float | None = None,
    min_subject_age_gt: float | None = None,
    min_subject_age_lt: float | None = None,
    max_subject_age: float | None = None,
    max_subject_age_gte: float | None = None,
    max_subject_age_lte: float | None = None,
    max_subject_age_gt: float | None = None,
    max_subject_age_lt: float | None = None,
    biosample_accession: list[str] | None = None,
    biosample_type: list[str] | None = None,
    biosample_subtype: list[str] | None = None,
    measurement_technique: list[str] | None = None,
    experiment_accession: list[str] | None = None,
    expsample_accession: list[str] | None = None,
    planned_visit_accession: list[str] | None = None,
    treatment_accession: list[str] | None = None,
    clinical: str = "",
    study_time_collected: float | None = None,
    study_time_collected_gte: float | None = None,
    study_time_collected_lte: float | None = None,
    study_time_collected_gt: float | None = None,
    study_time_collected_lt: float | None = None,
    study_time_collected_unit: list[str] | None = None,
    study_time_t0_event: list[str] | None = None,
    study_time_t0_event_specify: list[str] | None = None,
) -> dict:
    return {
        "studyAccession": study_accession,
        "armAccession": arm_accession or [],
        "armName": arm_name or [],
        "subjectAccession": subject_accession or [],
        "subjectPhenotype": subject_phenotype or [],
        "studyTitle": study_title or [],
        "ageEvent": age_event or [],
        "ageEventSpecify": age_event_specify or [],
        "ageUnit": age_unit or [],
        "sex": sex or [],
        "race": race or [],
        "raceSpecify": race_specify or [],
        "ethnicity": ethnicity or [],
        "ancestralPopulation": ancestral_population or [],
        "species": species or [],
        "strain": strain or [],
        "minSubjectAge": min_subject_age,
        "minSubjectAgeGte": min_subject_age_gte,
        "minSubjectAgeLte": min_subject_age_lte,
        "minSubjectAgeGt": min_subject_age_gt,
        "minSubjectAgeLt": min_subject_age_lt,
        "maxSubjectAge": max_subject_age,
        "maxSubjectAgeGte": max_subject_age_gte,
        "maxSubjectAgeLte": max_subject_age_lte,
        "maxSubjectAgeGt": max_subject_age_gt,
        "maxSubjectAgeLt": max_subject_age_lt,
        "biosampleAccession": biosample_accession or [],
        "biosampleType": biosample_type or [],
        "biosampleSubtype": biosample_subtype or [],
        "measurementTechnique": measurement_technique or [],
        "experimentAccession": experiment_accession or [],
        "expsampleAccession": expsample_accession or [],
        "plannedVisitAccession": planned_visit_accession or [],
        "treatmentAccession": treatment_accession or [],
        "clinical": clinical,
        "studyTimeCollected": study_time_collected,
        "studyTimeCollectedGte": study_time_collected_gte,
        "studyTimeCollectedLte": study_time_collected_lte,
        "studyTimeCollectedGt": study_time_collected_gt,
        "studyTimeCollectedLt": study_time_collected_lt,
        "studyTimeCollectedUnit": study_time_collected_unit or [],
        "studyTimeT0Event": study_time_t0_event or [],
        "studyTimeT0EventSpecify": study_time_t0_event_specify or [],
        "format": "json",
    }


def register(mcp: FastMCP) -> None:

    @mcp.resource("immport://assay-results-reference")
    def assay_results_reference() -> str:
        """Full field/filter/post-processing reference for get_assay_results, by assay_type."""
        return (
            "# get_assay_results Reference\n\n"
            "Full reference for `assay_type`, response fields per assay, filter parameters, and "
            "client-side response-shrinking options. The tool description only lists the essentials "
            "and valid assay_type names — read this before constructing a non-trivial call.\n\n"
            "## Assay types and their response fields\n"
            + _ASSAY_TOC
            + "\n\n## Filter parameters\n"
            + _FILTER_HELP
            + "\n## Response-shrinking options\n"
            + _POSTPROCESS_HELP
        )

    @mcp.tool(
        description=(
            "Retrieve assay results for any ImmPort assay type. "
            "Select the assay type via the `assay_type` parameter: "
            + ", ".join(_ASSAYS.keys())
            + ".\n\n"
            "⚠️ SCOPE BEFORE CALLING: The upstream ImmPort API returns ALL matching records with no "
            "server-side pagination or aggregation. Large studies (e.g. flow cytometry, lab tests) can "
            "return tens of thousands of rows from a single visit — and combining a broad filter "
            "(arm_accession or study_time_collected alone) with a large study can time out outright. "
            "Always narrow with planned_visit_accession + arm_accession together (the most reliable "
            "combination) before calling. Use get_study_data(resource='experiments') first to confirm "
            "the assay type exists in the study.\n\n"
            "Required: study_accession (list). Common filters: arm_accession, planned_visit_accession, "
            "subject_accession, measurement_technique, study_time_collected(_gte/_lte/_gt/_lt). "
            "Response-shrinking options: fields (project columns), name_contains (substring filter), "
            "summarize (per-group stats), limit/offset (paging) — these only shrink what's returned, "
            "not the upstream query cost, so real filters are still the best way to avoid timeouts.\n\n"
            "For the full per-assay field list, every filter parameter, and response-shrinking option "
            "details, see the immport://assay-results-reference resource."
        )
    )
    @record_tool_metric(
        "get_assay_results",
        sample_rate=1.0,
        param_extractor=lambda *a, **k: {
            "assayType": k.get("assay_type", ""),
            "studyAccessions": k.get("study_accession") or [],
        },
    )
    async def get_assay_results(
        assay_type: AssayType,  # type: ignore[valid-type]
        study_accession: list[str],
        arm_accession: list[str] | None = None,
        arm_name: list[str] | None = None,
        subject_accession: list[str] | None = None,
        subject_phenotype: list[str] | None = None,
        study_title: list[str] | None = None,
        age_event: list[str] | None = None,
        age_event_specify: list[str] | None = None,
        age_unit: list[str] | None = None,
        sex: list[str] | None = None,
        race: list[str] | None = None,
        race_specify: list[str] | None = None,
        ethnicity: list[str] | None = None,
        ancestral_population: list[str] | None = None,
        species: list[str] | None = None,
        strain: list[str] | None = None,
        min_subject_age: float | None = None,
        min_subject_age_gte: float | None = None,
        min_subject_age_lte: float | None = None,
        min_subject_age_gt: float | None = None,
        min_subject_age_lt: float | None = None,
        max_subject_age: float | None = None,
        max_subject_age_gte: float | None = None,
        max_subject_age_lte: float | None = None,
        max_subject_age_gt: float | None = None,
        max_subject_age_lt: float | None = None,
        biosample_accession: list[str] | None = None,
        biosample_type: list[str] | None = None,
        biosample_subtype: list[str] | None = None,
        measurement_technique: list[str] | None = None,
        experiment_accession: list[str] | None = None,
        expsample_accession: list[str] | None = None,
        planned_visit_accession: list[str] | None = None,
        treatment_accession: list[str] | None = None,
        clinical: str = "",
        study_time_collected: float | None = None,
        study_time_collected_gte: float | None = None,
        study_time_collected_lte: float | None = None,
        study_time_collected_gt: float | None = None,
        study_time_collected_lt: float | None = None,
        study_time_collected_unit: list[str] | None = None,
        study_time_t0_event: list[str] | None = None,
        study_time_t0_event_specify: list[str] | None = None,
        name_contains: str | None = None,
        fields: list[str] | None = None,
        summarize: bool = False,
        group_by: list[str] | None = None,
        value_field: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        endpoint, _, default_name_field, default_value_field = _ASSAYS[assay_type]
        try:
            rows = await api_get(endpoint, params=_build_params(
            study_accession=study_accession,
            arm_accession=arm_accession, arm_name=arm_name,
            subject_accession=subject_accession, subject_phenotype=subject_phenotype,
            study_title=study_title,
            age_event=age_event, age_event_specify=age_event_specify, age_unit=age_unit,
            sex=sex, race=race, race_specify=race_specify,
            ethnicity=ethnicity, ancestral_population=ancestral_population,
            species=species, strain=strain,
            min_subject_age=min_subject_age,
            min_subject_age_gte=min_subject_age_gte, min_subject_age_lte=min_subject_age_lte,
            min_subject_age_gt=min_subject_age_gt, min_subject_age_lt=min_subject_age_lt,
            max_subject_age=max_subject_age,
            max_subject_age_gte=max_subject_age_gte, max_subject_age_lte=max_subject_age_lte,
            max_subject_age_gt=max_subject_age_gt, max_subject_age_lt=max_subject_age_lt,
            biosample_accession=biosample_accession,
            biosample_type=biosample_type, biosample_subtype=biosample_subtype,
            measurement_technique=measurement_technique,
            experiment_accession=experiment_accession, expsample_accession=expsample_accession,
            planned_visit_accession=planned_visit_accession, treatment_accession=treatment_accession,
            clinical=clinical,
            study_time_collected=study_time_collected,
            study_time_collected_gte=study_time_collected_gte, study_time_collected_lte=study_time_collected_lte,
            study_time_collected_gt=study_time_collected_gt, study_time_collected_lt=study_time_collected_lt,
            study_time_collected_unit=study_time_collected_unit,
            study_time_t0_event=study_time_t0_event, study_time_t0_event_specify=study_time_t0_event_specify,
        ))
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"get_assay_results(assay_type='{assay_type}') timed out — the query likely matched "
                "too many rows to assemble before the timeout. Narrow the request by combining "
                "planned_visit_accession with arm_accession (the most reliable way to shrink the "
                "result set), rather than filtering by either alone."
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (413, 500, 502, 503, 504):
                raise RuntimeError(
                    f"get_assay_results(assay_type='{assay_type}') failed with HTTP {status} — this "
                    "usually means the result set was too large to return in one call. Narrow with "
                    "planned_visit_accession + arm_accession and retry."
                ) from exc
            raise

        if not isinstance(rows, list):
            return rows  # unexpected non-list payload (e.g. an error envelope) — pass through as-is

        total_rows = len(rows)

        if name_contains:
            rows = _filter_name_contains(rows, default_name_field, name_contains)

        if summarize:
            summary = _summarize_rows(
                rows,
                group_by=group_by,
                value_field=value_field or default_value_field,
                default_group_by=["armAccession", "studyTimeCollected"],
            )
            return {
                "total_rows": total_rows,
                "matched_rows": len(rows),
                "summary": summary,
            }

        rows = _project_fields(rows, fields)

        matched_rows = len(rows)
        if limit is not None:
            rows = rows[offset:offset + limit]
        elif offset:
            rows = rows[offset:]

        return {
            "total_rows": total_rows,
            "matched_rows": matched_rows,
            "returned_rows": len(rows),
            "offset": offset,
            "truncated": (offset + len(rows)) < matched_rows,
            "results": rows,
        }
