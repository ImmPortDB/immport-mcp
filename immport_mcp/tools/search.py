"""
Study Search tools — public endpoints, no auth required.
Base paths:
  GET /api/search/study
  GET /api/search/subject

Both use OpenSearch under the hood. The raw OpenSearch _search response is reshaped
(see shape_search_response in immport_client.py) into {total, returned, studies|subjects,
facets}: no _index/_score/_shards/highlight noise, and no aggregations unless the caller
passes `facets`, in which case only the requested (non-accession) facets are included,
zero-count-trimmed and capped per facet.

Full field reference (source_fields, nested objects, facet names, sort_field) lives in
the immport://search-fields MCP resource, not duplicated in these tool descriptions.

Spec-confirmed parameters (from /v3/api-docs):
  term, fromRecord, pageSize, preTag, postTag, format, sourceFields
  sortField, sortFieldDirection, searchFields
  studyAccession, subjectAccession, programName
  conditionOrDisease, researchFocus, clinicalTrial
  sex, race, ethnicity, species, minAge, ageRange
  assayMethod, biosampleType, hasAssessment, hasLabTest
"""
from __future__ import annotations

from fastmcp import FastMCP

from immport_mcp.services.immport_client import api_get, shape_search_response
from immport_mcp.telemetry.tool_metrics import record_tool_metric


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        description=(
            "Full-text and faceted search across all ImmPort studies. "
            "Returns a compact shape: {total, returned, studies, facets}. "
            "'studies' is a flat list of study records (no OpenSearch envelope/score/highlight noise). "
            "'facets' is omitted unless you pass the facets parameter. "
            "Use this as the entry point for any study discovery workflow. "
            "No authentication required.\n\n"
            "Key parameters:\n"
            "- term: partial matching by default ('flu' finds 'influenza'); "
            "  use quotes for exact phrase (e.g. '\"influenza vaccine\"')\n"
            "- research_focus: e.g. 'Vaccine Response', 'Allergy', 'Autoimmune' "
            "  (use lookup_research_focus() for valid values)\n"
            "- condition_or_disease: disease/condition name, e.g. 'Influenza', 'Atopic Dermatitis' "
            "  (use lookup_disease() for valid values)\n"
            "- program_name: NIH program name, e.g. 'HIPC', 'ITN', 'SeroNet'\n"
            "- clinical_trial: 'Y' or 'N' to filter to clinical trials only\n"
            "- species: e.g. 'Homo sapiens', 'Mus musculus' (use lookup_controlled_vocabulary('lkSpecies'))\n"
            "- sex: 'Female', 'Male', 'Unknown' (use lookup_controlled_vocabulary('lkSex'))\n"
            "- race: e.g. 'White', 'Black or African American' (use lookup_controlled_vocabulary('lkRace'))\n"
            "- ethnicity: e.g. 'Hispanic or Latino' (use lookup_controlled_vocabulary('lkEthnicity'))\n"
            "- min_age: minimum subject age filter (numeric)\n"
            "- age_range: age range string filter\n"
            "- assay_method: measurement technique, e.g. 'Flow Cytometry', 'ELISA', 'RNA sequencing' "
            "  (use lookup_controlled_vocabulary('lkExpMeasurementTech'))\n"
            "- biosample_type: e.g. 'PBMC', 'Whole Blood', 'Serum' (use lookup_controlled_vocabulary('lkSampleType'))\n"
            "- has_assessment: 'Y' or 'N'\n"
            "- has_lab_test: 'Y' or 'N'\n"
            "- study_accession: filter by specific study IDs, e.g. ['SDY1', 'SDY2']\n"
            "- subject_accession: filter by specific subject IDs, e.g. ['SUB00240']\n"
            "- facets: list of facet names to compute, e.g. ['assay_method', 'condition_or_disease']. "
            "  Omit this (default) for discovery/lookup queries — no aggregations are computed, "
            "  which keeps responses small and fast. Only request facets when the question is about "
            "  distribution/breakdown (e.g. 'what assay types exist across these studies?'), not when "
            "  you just need the matching studies themselves. Valid names: gender, race, ethnicity, "
            "  species, clinical_trial, condition_or_disease, research_focus, assay_method, "
            "  program_name, biosample_type, has_lab_test, has_assessment, age. Each returned facet is "
            "  {value: count} for up to facet_limit non-zero values, with any remainder folded into "
            "  'other' (note: 'other' is a doc count, not a list of hidden values — if it's non-trivial "
            "  and you need the actual long-tail categories, raise facet_limit rather than guessing). "
            "  study_accession/subject_accession are never returned as facets since they'd just "
            "  duplicate the 'studies' list.\n"
            "- facet_limit: max buckets returned per facet (default 15). Raise this (e.g. 50, 100) when "
            "  the 'other' bucket is large and you actually need the long-tail breakdown rather than "
            "  just the top values — e.g. 'list every condition_or_disease in these results', not just "
            "  the most common ones. Values ≤ 15 use the backend's cheapest pre-capped response; higher "
            "  values cost more (the backend returns its full, still zero-count-trimmed, aggregation "
            "  and the cap is applied here instead).\n"
            "- sort_field: field to sort by. Valid values:\n"
            "    study_accession_number (numeric — use desc for most recently added studies),\n"
            "    study_accession (string sort only — do NOT use for recency),\n"
            "    latest_data_release_date (date — use desc for most recently updated studies),\n"
            "    latest_data_release_version_number (numeric release version, use desc for latest),\n"
            "    initial_data_release_date (date — use desc for newest additions),\n"
            "    initial_data_release_version_number (numeric),\n"
            "    brief_title, min_age, max_age, condition_or_disease, research_focus,\n"
            "    species, study_pi, actual_enrollment, planned_visit_total_count, shared_subject_count\n"
            "- sort_field_direction: 'asc' (default) or 'desc'\n"
            "- search_fields: limit text search to specific fields (comma-separated)\n"
            "- source_fields: list of fields to return (e.g. ['study_accession', 'brief_title']). "
            "  Common fields: study_accession, brief_title, brief_description, condition_or_disease, "
            "  research_focus, species, study_pi, program_name, clinical_trial, assay_method, "
            "  biosample_type, doi, pubmed_id. "
            "  For the FULL field list \u2014 including nested objects like pubmed (journal, year, doi), "
            "  reagent, experiment, personnel, cde \u2014 dot-path projection syntax (e.g. "
            "  'pubmed.journal'), fields that exist but can't be retrieved, and known data-quality "
            "  caveats, see the immport://search-fields resource. "
            "  '*' does NOT work as a wildcard for 'all fields' \u2014 it returns an empty object. "
            "  Restrict to only fields you need to reduce response size.\n"
            "- from_record: zero-based pagination offset (default 0)\n"
            "- page_size: max records per page (default 10, hard cap 10000)\n"
            "- include_score: adds a 'relevance_score' field to each study (default False). "
            "  Results are always returned in relevance order regardless of this flag — only use "
            "  this when you need the actual numeric score (e.g. to judge whether low-ranked hits "
            "  are only tangentially related).\n"
            "- include_highlights: adds a 'highlights' field to each study showing which text "
            "  snippets matched `term`, e.g. distinguishing '<b>adjuvant</b> vaccine' from "
            "  '<b>adjuvant</b> chemotherapy' (default False). Only meaningful when `term` is set; "
            "  ignored otherwise. Not available on search_subjects (backend limitation).\n\n"
            "EFFICIENCY TIPS:\n"
            "- Cross-study questions (e.g. 'top journals', 'which studies used X'): use ONE call "
            "with page_size covering the full result set and narrow source_fields \u2014 do not loop "
            "get_study_data per study. See immport://query-tips for a worked example.\n"
            "- source_fields: Always specify only the fields you need — omitting this returns "
            "full OpenSearch documents and produces very large responses.\n"
            "- Sizing page_size: each row costs ~2,500 chars with full documents (no source_fields) "
            "vs. ~150 chars when source_fields is restricted to 2-3 fields. To fetch many results in "
            "one call instead of paginating, restrict source_fields first, then raise page_size "
            "(e.g. page_size=1000 with 2-3 source_fields is ~125KB; the same page_size with full "
            "documents would be ~2.5MB and likely exceed the response size limit). Don't raise "
            "page_size much past 50 unless source_fields is restricted.\n"
            "- Pagination: Always check the top-level 'total' field. If total > page_size, "
            "issue additional calls incrementing from_record by page_size until all results are fetched.\n"
            "- Parallel lookups: lookup_controlled_vocabulary is served from in-memory cache with no "
            "API cost — issue lookup calls in parallel with this search, not before it.\n"
            "- Recency sort: use study_accession_number desc for most recently added; "
            "latest_data_release_date desc for most recently updated. "
            "Do NOT use study_accession for recency — it is a string (SDY10 sorts before SDY9)."
        )
    )
    @record_tool_metric(
        "search_studies",
        sample_rate=1.0,
        param_extractor=lambda *a, **k: {
            "term": k.get("term") or "",
            # Active filters — only included when non-empty
            **({} if not k.get("study_accession") else {"studyAccessions": k["study_accession"]}),
            **({} if not k.get("research_focus") else {"researchFocus": k["research_focus"]}),
            **({} if not k.get("condition_or_disease") else {"conditionOrDisease": k["condition_or_disease"]}),
            **({} if not k.get("program_name") else {"programName": k["program_name"]}),
            **({} if not k.get("assay_method") else {"assayMethod": k["assay_method"]}),
        },
    )
    async def search_studies(
        term: str = "",
        research_focus: list[str] | None = None,
        condition_or_disease: list[str] | None = None,
        program_name: list[str] | None = None,
        clinical_trial: list[str] | None = None,
        species: list[str] | None = None,
        sex: list[str] | None = None,
        race: list[str] | None = None,
        ethnicity: list[str] | None = None,
        min_age: float | None = None,
        age_range: str | None = None,
        assay_method: list[str] | None = None,
        biosample_type: list[str] | None = None,
        has_assessment: list[str] | None = None,
        has_lab_test: list[str] | None = None,
        study_accession: list[str] | None = None,
        subject_accession: list[str] | None = None,
        sort_field: str = "",
        sort_field_direction: str = "asc",
        search_fields: list[str] | None = None,
        from_record: int = 0,
        page_size: int = 10,
        source_fields: list[str] | None = None,
        facets: list[str] | None = None,
        facet_limit: int = 15,
        include_score: bool = False,
        include_highlights: bool = False,
    ) -> dict:
        """
        Search ImmPort studies by free text and/or facets.
        - Multiple values for any facet filter are OR'd together
        - All different facets are AND'd
        - page_size hard cap = 10000 (see tool description for sizing guidance)
        """
        # Highlights only exist in the backend's aggs-including template, so requesting
        # them forces includeFacets=true even if the caller didn't ask for facets
        # themselves (shape_search_response still omits the facets key unless requested).
        want_aggs = bool(facets) or include_highlights
        use_compact = facet_limit <= 15
        params: dict = {
            "term": term,
            "researchFocus": research_focus or [],
            "conditionOrDisease": condition_or_disease or [],
            "programName": program_name or [],
            "clinicalTrial": clinical_trial or [],
            "species": species or [],
            "sex": sex or [],
            "race": race or [],
            "ethnicity": ethnicity or [],
            "assayMethod": assay_method or [],
            "biosampleType": biosample_type or [],
            "hasAssessment": has_assessment or [],
            "hasLabTest": has_lab_test or [],
            "studyAccession": study_accession or [],
            "subjectAccession": subject_accession or [],
            "sortFieldDirection": sort_field_direction,
            "fromRecord": from_record,
            "pageSize": page_size,
            "includeFacets": want_aggs,
            "compactFacets": want_aggs and use_compact,
        }
        if min_age is not None:
            params["minAge"] = min_age
        if age_range:
            params["ageRange"] = age_range
        if sort_field:
            params["sortField"] = sort_field
        if search_fields:
            params["searchFields"] = search_fields
        if source_fields:
            params["sourceFields"] = source_fields
        result = await api_get("/api/search/study", params=params, requires_auth=False)
        return shape_search_response(
            result,
            item_key="studies",
            facets=facets,
            facet_limit=facet_limit,
            include_score=include_score,
            include_highlights=include_highlights,
        )

    @mcp.tool(
        description=(
            "Full-text and faceted search across all ImmPort subjects/participants. "
            "Returns a compact shape: {total, returned, subjects, facets}. "
            "'subjects' is a flat list of subject records (no OpenSearch envelope/score/highlight noise). "
            "'facets' is omitted unless you pass the facets parameter. "
            "Useful for cross-study cohort discovery (e.g. find all female subjects in influenza studies). "
            "No authentication required.\n\n"
            "Key parameters:\n"
            "- term: partial matching by default; use quotes for exact phrase\n"
            "- study_accession: filter by specific study IDs, e.g. ['SDY1', 'SDY2']\n"
            "- subject_accession: filter by specific subject IDs, e.g. ['SUB00240']\n"
            "- research_focus: e.g. 'Vaccine Response', 'Allergy'\n"
            "- condition_or_disease: disease/condition name (use lookup_controlled_vocabulary('lkDisease'))\n"
            "- program_name: NIH program name, e.g. 'HIPC', 'SeroNet'\n"
            "- clinical_trial: 'Y' or 'N'\n"
            "- sex: 'Female', 'Male', 'Unknown' (use lookup_controlled_vocabulary('lkSex'))\n"
            "- race: e.g. 'White', 'Black or African American' (use lookup_controlled_vocabulary('lkRace'))\n"
            "- ethnicity: e.g. 'Hispanic or Latino' (use lookup_controlled_vocabulary('lkEthnicity'))\n"
            "- species: e.g. 'Homo sapiens', 'Mus musculus' (use lookup_controlled_vocabulary('lkSpecies'))\n"
            "- min_age: minimum subject age (numeric)\n"
            "- age_range: age range string filter\n"
            "- assay_method: measurement technique, e.g. 'Flow Cytometry', 'ELISA' "
            "  (use lookup_controlled_vocabulary('lkExpMeasurementTech'))\n"
            "- biosample_type: e.g. 'PBMC', 'Whole Blood' (use lookup_controlled_vocabulary('lkSampleType'))\n"
            "- has_assessment: 'Y' or 'N'\n"
            "- has_lab_test: 'Y' or 'N'\n"
            "- facets: list of facet names to compute, e.g. ['assay_method', 'condition_or_disease']. "
            "  Omit this (default) for discovery/lookup queries — no aggregations are computed, "
            "  which keeps responses small and fast. Only request facets when the question is about "
            "  distribution/breakdown, not when you just need the matching subjects themselves. "
            "  Valid names: gender, race, ethnicity, species, clinical_trial, condition_or_disease, "
            "  research_focus, assay_method, program_name, biosample_type, has_lab_test, "
            "  has_assessment, age. Each returned facet is {value: count} for up to facet_limit "
            "  non-zero values, with any remainder folded into 'other' (a doc count, not a list of "
            "  hidden values — raise facet_limit if you need the actual long-tail categories). "
            "  study_accession/subject_accession are never returned as facets since they'd just "
            "  duplicate the 'subjects' list.\n"
            "- facet_limit: max buckets returned per facet (default 15). Raise this (e.g. 50, 100) when "
            "  the 'other' bucket is large and you need the long-tail breakdown, not just top values.\n"
            "- sort_field: field to sort by. Valid values:\n"
            "    subject_accession (string sort), study_accession (string sort),\n"
            "    assay_method, biosample_type, clinical_trial,\n"
            "    condition_or_disease, ethnicity, sex, has_assessment, has_lab_test,\n"
            "    min_age, race, research_focus, species\n"
            "- sort_field_direction: 'asc' (default) or 'desc'\n"
            "- search_fields: limit text search to specific fields\n"
            "- source_fields: list of fields to return (e.g. ['study_accession', 'subject_accession']). "
            "  Common fields: study_accession, subject_accession, condition_or_disease, research_focus, "
            "  species, sex, race, ethnicity, min_age, max_age, assay_method, biosample_type, "
            "  clinical_trial. "
            "  For the FULL field list — including nested objects like pubmed (journal, year, doi), "
            "  reagent, experiment, cde — dot-path projection syntax (e.g. 'pubmed.journal'), and "
            "  known data-quality caveats, see the immport://search-fields resource. "
            "  '*' does NOT work as a wildcard for 'all fields' — it returns an empty object.\n"
            "  Restrict to only the fields you need to reduce response size.\n"
            "- from_record: zero-based pagination offset (default 0)\n"
            "- page_size: max records per page (default 10, hard cap 10000)\n"
            "- include_score: adds a 'relevance_score' field to each subject (default False). "
            "  Results are always returned in relevance order regardless of this flag.\n\n"
            "EFFICIENCY TIPS:\n"
            "- Cross-study questions (e.g. 'which subjects had assay X'): use ONE call with "
            "page_size covering the full result set and narrow source_fields — do not loop "
            "get_study_data per study. See immport://query-tips for a worked example.\n"
            "- source_fields: Always specify only the fields you need — omitting this returns "
            "full OpenSearch documents and produces very large responses.\n"
            "- Sizing page_size: each row costs ~2,500 chars with full documents (no source_fields) "
            "vs. ~150 chars when source_fields is restricted to 2-3 fields. To fetch many results in "
            "one call instead of paginating, restrict source_fields first, then raise page_size "
            "(e.g. page_size=1000 with 2-3 source_fields is ~125KB; the same page_size with full "
            "documents would be ~2.5MB and likely exceed the response size limit). Don't raise "
            "page_size much past 50 unless source_fields is restricted.\n"
            "- Pagination: Always check the top-level 'total' field. If total > page_size, "
            "issue additional calls incrementing from_record by page_size until all results are fetched.\n"
            "- Parallel lookups: lookup_controlled_vocabulary is served from in-memory cache with no "
            "API cost — issue lookup calls in parallel with this search, not before it."
        )
    )
    @record_tool_metric(
        "search_subjects",
        sample_rate=1.0,
        param_extractor=lambda *a, **k: {
            "term": k.get("term") or "",
            **({} if not k.get("study_accession") else {"studyAccessions": k["study_accession"]}),
            **({} if not k.get("research_focus") else {"researchFocus": k["research_focus"]}),
            **({} if not k.get("condition_or_disease") else {"conditionOrDisease": k["condition_or_disease"]}),
            **({} if not k.get("program_name") else {"programName": k["program_name"]}),
            **({} if not k.get("sex") else {"sex": k["sex"]}),
            **({} if not k.get("race") else {"race": k["race"]}),
            **({} if not k.get("species") else {"species": k["species"]}),
        },
    )
    async def search_subjects(
        term: str = "",
        study_accession: list[str] | None = None,
        subject_accession: list[str] | None = None,
        research_focus: list[str] | None = None,
        condition_or_disease: list[str] | None = None,
        program_name: list[str] | None = None,
        clinical_trial: list[str] | None = None,
        sex: list[str] | None = None,
        race: list[str] | None = None,
        ethnicity: list[str] | None = None,
        species: list[str] | None = None,
        min_age: float | None = None,
        age_range: str | None = None,
        assay_method: list[str] | None = None,
        biosample_type: list[str] | None = None,
        has_assessment: list[str] | None = None,
        has_lab_test: list[str] | None = None,
        sort_field: str = "",
        sort_field_direction: str = "asc",
        search_fields: list[str] | None = None,
        from_record: int = 0,
        page_size: int = 10,
        source_fields: list[str] | None = None,
        facets: list[str] | None = None,
        facet_limit: int = 15,
        include_score: bool = False,
    ) -> dict:
        """
        Search ImmPort subjects by free text and/or facets.
        Returns subject-level data joined to study metadata.
        page_size hard cap = 10000 (see tool description for sizing guidance).
        """
        use_compact = bool(facets) and facet_limit <= 15
        params: dict = {
            "term": term,
            "studyAccession": study_accession or [],
            "subjectAccession": subject_accession or [],
            "researchFocus": research_focus or [],
            "conditionOrDisease": condition_or_disease or [],
            "programName": program_name or [],
            "clinicalTrial": clinical_trial or [],
            "sex": sex or [],
            "race": race or [],
            "ethnicity": ethnicity or [],
            "species": species or [],
            "assayMethod": assay_method or [],
            "biosampleType": biosample_type or [],
            "hasAssessment": has_assessment or [],
            "hasLabTest": has_lab_test or [],
            "sortFieldDirection": sort_field_direction,
            "fromRecord": from_record,
            "pageSize": page_size,
            "includeFacets": bool(facets),
            "compactFacets": use_compact,
        }
        if min_age is not None:
            params["minAge"] = min_age
        if age_range:
            params["ageRange"] = age_range
        if sort_field:
            params["sortField"] = sort_field
        if search_fields:
            params["searchFields"] = search_fields
        if source_fields:
            params["sourceFields"] = source_fields
        result = await api_get("/api/search/subject", params=params, requires_auth=False)
        return shape_search_response(
            result, item_key="subjects", facets=facets, facet_limit=facet_limit, include_score=include_score
        )
