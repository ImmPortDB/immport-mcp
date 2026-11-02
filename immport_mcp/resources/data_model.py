"""
ImmPort static resources — data model and query tips.
These are available as MCP resources and injected as context in prompts.
"""
from __future__ import annotations

from fastmcp import FastMCP

from immport_mcp.services.immport_client import api_get

# ---------------------------------------------------------------------------
# ImmPort Data Model
# ---------------------------------------------------------------------------
IMMPORT_DATA_MODEL = """
# ImmPort Data Model

## Entity Hierarchy

```
Program
 └── Study  (SDY#####)
      ├── Arm / Cohort  (ARM#####)     ← treatment or control group
      │    └── Subject  (SUB######)
      │         ├── BioSample  (BS######)
      │         │    └── Experiment  (EXP#####)
      │         │         └── ExpSample  (ES######)
      │         │              └── Assay Results (stored per assay type)
      │         ├── Lab Tests
      │         └── Adverse Events  (AE#####)
      ├── Planned Visits  (PV#####)    ← study time points
      ├── Interventions / Treatments  (TRT###)
      ├── Protocols  (SPR#####)
      └── Study Files / Documents
```

## Accession ID Formats

| Entity          | Prefix  | Example        |
|-----------------|---------|----------------|
| Study           | SDY     | SDY1, SDY305   |
| Arm / Cohort    | ARM     | ARM10848       |
| Subject         | SUB     | SUB134960      |
| BioSample       | BS      | BS694010       |
| Experiment      | EXP     | EXP15351       |
| Exp. Sample     | ES      | ES1602363      |
| Planned Visit   | PV      | PV66, PV2919   |
| Treatment       | TRT     | TRT001         |
| Adverse Event   | AE      | AE85785        |
| Protocol        | SPR     | SPR1           |
| Person          | PSN     | PSN85          |

## Which tool to use

API paths and parameters are not listed here: the data-query API's OpenAPI spec is the source of
truth — read it from the immport://api-schema resource when you need exact endpoints.

| Need                                                     | Tool                          | Auth? |
|----------------------------------------------------------|-------------------------------|-------|
| Find studies / subjects (full-text + facets)             | search_studies, search_subjects | No  |
| What data a study has (record counts per category)       | get_study_data_availability   | Yes   |
| Study metadata: arms, subjects, visits, protocols, … (one or up to 20 studies) | get_study_data | Yes |
| Assay results (ELISA, ELISPOT, flow, HAI, HLA, KIR, MBAA, mass spec, neut. Ab, PCR, file paths) | get_assay_results | Yes |
| Study files: manifest, stream text files, signed download URLs | get_study_files       | Yes   |
| Data releases and study release history / notes          | get_release_notes             | Yes   |
| Tables, columns, keys and joins of the release database   | get_data_model                | No    |
| Controlled vocabulary values (lk_* tables)               | lookup_controlled_vocabulary  | No    |

## Authentication

- claude.ai / remote connector (mcp.immport.org): the ImmPort login done when the connector was
  added (OAuth) is used for every authenticated call; nothing else to configure.
- Local stdio: put an ImmPort API key (https://www.immport.org/auth/api/keys, valid 30 days) in
  IMMPORT_API_KEY in .env.

Search, lookup and data model tools are **public** — no auth required.
"""

# ---------------------------------------------------------------------------
# Query Efficiency Tips
# ---------------------------------------------------------------------------
IMMPORT_QUERY_TIPS = """
# ImmPort Query Efficiency Tips

## Tool Selection — Single Study Detail

| Goal | Best tool | Why |
|------|-----------|-----|
| AI agent reading/analysing study data | `get_study_data(resource=...)` + `get_assay_results(...)` | Returns structured JSON directly — no download, unzip, or TSV parsing needed |
| Human researcher wants one download with everything | Tab.zip via `get_study_files(action="manifest")` → `get_study_files(action="download")` | All tables in one file; TSV format for spreadsheet/pipeline use |
| Need join tables not exposed by API (reagent, expsample_2_reagent, treatment joins) | Tab.zip | These tables have no equivalent API endpoint |
| Agent needs to read an individual file inline | `get_study_files(action="stream", file_uuid=...)` | Returns UTF-8 text directly for TSV/CSV files |
| Human needs a download link (browser/curl) | `get_study_files(action="download", file_uuid=...)` | Returns signed CloudFront URL |
| Cross-study result queries (ELISA, HAI, FCS…) | `get_assay_results(assay_type, study_accession=[...])` | Structured JSON, multi-study, rich filters |
| Download ALL metadata + raw instrument files | ALL_DATA zip via `get_study_files(action="manifest")` → `get_study_files(action="download")` | One archive; can be hundreds of GB — check `filesizeBytes` first |

**Decision rule for AI agents**: always use `get_study_data` + `get_assay_results` — they return JSON immediately with no shell work. Tab.zip requires download → unzip → TSV parsing, which adds latency and tool calls. Only reach for Tab.zip when you specifically need join tables that have no API equivalent (reagent specs, expsample-to-reagent mapping, treatment join tables) or when producing a file for a human to download.

## Workflow Patterns

### 1. Study Discovery → Deep Dive
```
search_studies(term="influenza vaccine", assay_method=["Flow Cytometry"])
  → get_study_files(action="manifest", study_accession="SDY305", file_type="release_file")
  → get_study_files(action="download", file_uuid=<tab_zip_uuid>)  # signed URL for Tab.zip
```
For quick structured lookups, individual API calls are preferred over downloading:
```
  get_study_data("SDY305", resource="summary")         # fast single-field check
  get_assay_results(assay_type="fcs_analyzed", study_accession=["SDY305"])
```

### 2. Cross-Study Cohort Discovery
```
search_subjects(sex=["Female"], race=["White"], condition_or_disease=["Influenza"])
  → group by study_accession from hits
  → get_study_data(study_accession, resource="summary") for each unique study (in parallel)
  → get_assay_results(assay_type="file_paths", study_accession=[list_of_studies])
```

### 3. Gene Expression Workflow
```
search_studies(assay_method=["RNA sequencing"])
  → get_study_data("SDY1", resource="experiments")      # confirm RNA-seq experiments
  → get_study_files(action="manifest", study_accession="SDY1", file_type="result_file")
  → get_study_files(action="download", file_uuid=<fastq_or_counts_uuid>)  # signed URL
```

### 4. Deep Single-Study Analysis — API Calls (PREFERRED for AI agents)

For AI agent workflows, **use individual API calls** rather than downloading Tab.zip.
Each call returns structured JSON immediately — no download, unzip, or TSV parsing needed.
Issue independent calls in parallel to minimise latency:

```
# In parallel:
get_study_data(study_accession, resource="summary")        # overview, PI, funding
get_study_data(study_accession, resource="arm")            # cohorts/treatment groups
get_study_data(study_accession, resource="planned_visits") # visit schedule
get_study_data(study_accession, resource="subjects")       # demographics
get_study_data(study_accession, resource="experiments")    # assay types
get_study_data(study_accession, resource="adverse_events") # AE profile

# Then fetch assay results (requires knowing the assay type from experiments above):
get_assay_results(assay_type="elisa", study_accession=[study_accession])
get_assay_results(assay_type="fcs_analyzed", study_accession=[study_accession])
# etc.
```

**When to use Tab.zip instead of API calls:**

| Situation | Use Tab.zip? |
|---|---|
| Need reagent specs with catalog/clone numbers | Yes — reagent.txt has no API equivalent |
| Need expsample→reagent or expsample→treatment join tables | Yes — not exposed via API |
| Need FCS channel map (fcs_header_marker.txt) | Yes — not exposed via API |
| Need all tables for a human to download / pipeline to process | Yes |
| Need 3+ standard metadata tables (arm, visits, subjects…) | No — parallel API calls are faster |
| Need assay results (ELISA, HAI, flow cytometry…) | No — use get_assay_results (JSON, filterable) |

Tab.zip workflow (when needed):
```
get_study_files(action="manifest", study_accession=..., file_type="release_file")
  → find fileName ending in "_Tab.zip", note filesizeBytes
  → get_study_files(action="download", file_uuid=...)  # signed URL — requires curl/wget to download
  → unzip + parse TSV files locally
```

⚠️ **Download limits**: ImmPort enforces a per-file daily download quota. Never download
the same file twice in a session. A 429 response means the limit is reached; advise the
user to wait 24 hours.

### 5. Individual File Download
```
get_study_files(action="manifest", study_accession="SDY3390")
  → get_study_files(action="download", file_uuid=...)  # signed CloudFront URL (time-limited)
  → get_study_files(action="stream",   file_uuid=...)  # inline text — best for CSV/TSV files
```

### 6. Full Study Download — ALL_DATA Zip (metadata + raw files)

Use this when you need raw instrument files (FCS, CEL, fastq, etc.) alongside all metadata.

```
get_study_files(action="manifest", study_accession=..., file_type="zip_file")
  → find SDY##_DR##_ALL_DATA.zip entry
  → CHECK filesizeBytes FIRST — some studies exceed 100 GB, some exceed 500 GB
  → get_study_files(action="download", file_uuid=...)  # signed CloudFront URL for wget/curl
```

⚠️ **Size check is mandatory before downloading.** Example sizes:
- Small study (serology only): ~50–200 MB
- Flow cytometry study (FCS files): 5–50 GB
- RNA-seq or CyTOF study: 50–500+ GB

**ALL_DATA zip vs Tab.zip — when to use which:**

| | Tab.zip (`release_file`) | ALL_DATA zip (`zip_file`) |
|---|---|---|
| Contains | All metadata TSVs only | All metadata TSVs + all raw result files |
| Typical size | 1–50 MB | 100 MB – 500+ GB |
| Use when | Analyzing study structure, demographics, assay design | Reprocessing raw data (FCS, CEL, fastq, images) |
| Download hits quota | 1 hit | 1 hit (but much larger transfer) |

If the user only needs specific raw files (e.g., one FCS file), use `file_info.txt` from the
Tab.zip to get individual fileUUIDs and download selectively — avoid pulling the full ALL_DATA
zip when only a subset of files is needed.

### 7. Serology / Vaccine Response
```
search_studies(condition_or_disease=["Influenza"], assay_method=["Neutralizing Antibody Titer Assay"])
  → get_assay_results(assay_type="hai", study_accession=["SDY306", "SDY208"])
  → get_assay_results(assay_type="neut_ab_titer", study_accession=["SDY306"])
  → filter by study_time_collected_gte=-7, study_time_collected_lte=28
```

## search_studies / search_subjects — v2 response shape and new parameters

`search_studies`/`search_subjects` return a compact shape, not a raw OpenSearch
response: `{total, returned, studies|subjects, facets}`. There is no `hits.total.value`
or `hits.hits[]._source` to unwrap — `total` and `returned` are plain integers, and
each item in `studies`/`subjects` is already flat (no `_index`/`_score`/`highlight`
envelope unless you opt in, see below).

- **`facets`** (list of facet names, e.g. `["assay_method", "condition_or_disease"]`):
  omit for discovery queries (default — no aggregations computed at all); pass it when
  the question is about distribution/breakdown. `facet_limit` (default 15) caps buckets
  per facet, with the remainder folded into `"other"`; raise it if `"other"` is large
  and you need the long tail.
- **`include_score`** (bool, default False): adds `relevance_score` per item. Result
  order already reflects relevance ranking regardless of this flag.
- **`include_highlights`** (bool, `search_studies` only, default False): adds a
  `highlights` field per item showing matched text snippets — useful for
  disambiguating e.g. "vaccine adjuvant" from "adjuvant chemotherapy" hits that share
  a keyword but differ in meaning.

## Filter Tips

- **Always use lookups first**: Call `lookup_controlled_vocabulary(table)` to get valid
  controlled vocabulary values before filtering. Wrong values silently return empty results.
  Example: `lookup_controlled_vocabulary("lkExpMeasurementTech")` → then filter with exact string

- **Use time range filters**: `study_time_collected_gte` / `study_time_collected_lte`
  to scope data to pre/post vaccination windows (e.g. day -7 to day 28)

- **studyAccession is required for /result/* endpoints** but accepts a list:
  e.g. `study_accession=["SDY406", "SDY208", "SDY268"]`

- **Page through search results**: `from_record` + `page_size` (hard cap 10000; see
  the tool description for per-row sizing guidance before requesting large pages)
  to retrieve all studies matching a query. Always check the response's top-level
  `total` field vs `returned`, and issue a second call with `from_record` advanced by
  `page_size` if `total > returned`.

- **Date fields for release date**: The correct field names in the search index are
  `latest_data_release_date` and `initial_data_release_date` (not `release_date`).
  Always include them explicitly via `source_fields`.

- **Sorting by date (server-side sort IS supported)**: use `sort_field` (e.g.
  `latest_data_release_date`, `initial_data_release_date`, `study_accession_number`)
  with `sort_field_direction="desc"` to get the most recent studies first — verified
  working (`sort_field="study_accession_number", sort_field_direction="desc"` returns
  the highest-numbered study first). Do not sort by `study_accession` for recency —
  it's a string field ("SDY10" sorts before "SDY9").
  ```
  search_studies(sort_field="latest_data_release_date", sort_field_direction="desc",
                 page_size=10, source_fields=["study_accession", "brief_title",
                                              "latest_data_release_date"])
  ```

- **⚠️ Avoid get_study_data(resource="expsample_mapping")** unless you need the full hierarchy —
  use `get_assay_results` for assay data instead

## Cross-Study vs. Single-Study: which tool to use

- **Question spans many/all studies** (e.g. "top journals", "publications per year",
  "which studies used antibody clone X", "studies with no publication"): use ONE
  `search_studies`/`search_subjects` call with `page_size` large enough to cover the
  result set (check `total` in the response) and `source_fields` restricted to exactly
  what you need. See `immport://search-fields` for the full field list, including
  nested objects like `pubmed`, `reagent`, `experiment`.
- **Question is about one specific study's full detail**: use `get_study_data`.
- Do NOT loop `get_study_data` over many studies to answer a cross-study question —
  the search index already has the field you need in bulk (confirmed for publications,
  reagents, experiments, personnel — see `immport://search-fields`); looping is slow
  and was the wrong approach even when it eventually produces a correct answer.

### Worked example: top journals where ImmPort data is published
```
search_studies(page_size=1533, source_fields=["study_accession", "pubmed.journal"])
# -> one call, ~90KB. Then client-side: flatten pubmed.journal arrays per study,
# normalize name variants (e.g. "Nat Immunol" vs "Nature Immunology"), count by journal.
# pubmed.journal is NOT a valid `facets` name (nested fields aren't facetable server-side
# today) — this client-side-count-after-one-bulk-call pattern is the correct approach,
# not a workaround.
```

## Common Research Focus Values (from lookup_research_focus)
- Allergy
- Atopic Dermatitis
- Autoimmune
- Diabetes
- HIV/AIDS
- Immune Response
- Influenza
- Malaria
- Transplantation
- Vaccine Response

## Example Study Accessions for Testing
SDY406, SDY208, SDY268, SDY1190, SDY691, SDY194, SDY2437, SDY2353, SDY1538
"""

# ---------------------------------------------------------------------------
# search_studies / search_subjects field reference
# ---------------------------------------------------------------------------
IMMPORT_SEARCH_FIELDS = """
# ImmPort Search Index — Field Reference

Full reference for `source_fields`, `facets`, and `sort_field` on `search_studies` and
`search_subjects`. Read this BEFORE looping per-study calls for any question that spans
many studies (publications, assay methods, funding, demographics, etc.) — a single
search call with the right `source_fields` almost always replaces hundreds of
`get_study_data` calls.

## How source_fields works

- Request fields by exact name: `source_fields=["study_accession", "brief_title"]`.
- `source_fields=["*"]` does NOT mean "all fields" — it returns an empty object `{}`.
  There is no wildcard; fields must be named explicitly.
- Nested objects (see table below) return an array of sub-records by default. Dot-path
  into one sub-field to shrink that to just the field you need, e.g.
  `source_fields=["pubmed.journal", "pubmed.year"]` returns `[{"journal": ..., "year": ...}, ...]`
  per study instead of the full `pubmed` record (pubmed_id, title, doi, authors, month too).
- Omitting `source_fields` entirely returns the full ~40-field default document — always
  restrict this for any multi-study or bulk query.

## Common top-level fields (study)

study_accession, brief_title, brief_description, condition_or_disease, research_focus,
species, study_pi, program_name, minimum_age, maximum_age, age_unit, actual_enrollment,
planned_visit_total_count, shared_subject_count, clinical_trial, has_assessment,
has_lab_test, assay_method, assay_method_count, biosample_type, arm_name, arm_accession,
doi, pubmed_id, contract_grant_name, contract_grant_external_id, study_accession_number,
initial_data_release_date, initial_data_release_version_number, latest_data_release_date,
latest_data_release_version_number, assessment_panel_count, lab_test_panel_count,
analyte_preferred_count, sex_included, ethnicity, race, gender_included,
condition_or_disease_preferred, condition_or_disease_reported, official_title,
description (full abstract), objectives, hypothesis, endpoints, treatment_name,
intervention_agent, expsample_descriptions, sponsoring_organization, target_enrollment,
actual_start_date, actual_completion_date, shared_study, dcl_id

## Common top-level fields (subject)

study_accession, subject_accession, condition_or_disease, research_focus, species, sex,
race, ethnicity, min_age, max_age, has_assessment, has_lab_test, assay_method,
biosample_type, clinical_trial, arm_name, arm_accession, treatment_name,
expsample_descriptions, condition_or_disease_preferred, condition_or_disease_reported

## Nested objects (both study and subject, except study_file which is study-only)

| Object | Sub-fields | Notes |
|---|---|---|
| `pubmed` | pubmed_id, journal, year, month, title, doi, authors | Use `pubmed.journal`/`pubmed.year` for cross-study publication analysis (e.g. "top journals") |
| `experiment` | experiment_accession, name, description, measurement_technique | |
| `reagent` | name, clone_name, analyte_preferred, antibody_registry_id, description | Antibody Registry IDs useful for reproducibility questions |
| `personnel` | site_name, title_in_study, role_in_study, name, organization | study-only |
| `study_file` | description, type | study-only |
| `cde` | cde_id, name, immport_table_name, immport_column_name, immport_column_value | |
| `expsample_public_repository` | repository_accession, repository_name | e.g. GEO accessions |
| `gene_name`, `virus_strain`, `allele` | assay-specific identifiers | Only populated for studies/subjects using that assay type — empty arrays otherwise |
| `pcr_genes`, `population`, `proteomics_mass_spectrometry`, `metabolomics_mass_spectrometry` | assay-specific | Same as above — empty unless that measurement type was used |

## Fields that exist but are NOT retrievable via source_fields

These are searchable (via `term`/`search_fields`) or appear in `highlights`, but always
return nothing when requested as a `source_fields` entry, regardless of how they're named:
`arm_or_cohort_all`, `reagent_all`, `pubmed_all`, `protocol`, `keywords`, `study_link`,
`planned_visit`, `data_completeness`, `study_type`.

## Facets (the `facets` parameter)

Valid facet names — requesting any other name now raises an error listing these:
`gender`, `race`, `ethnicity`, `species`, `clinical_trial`, `condition_or_disease`,
`research_focus`, `assay_method`, `program_name`, `biosample_type`, `has_lab_test`,
`has_assessment`, `age`.

Nested fields (e.g. `pubmed.journal`) are **not** facetable server-side today — there is
no aggregation support for them. For cross-study breakdowns on a nested field (e.g.
"top journals across all studies"), pull `source_fields=["pubmed.journal"]` for the full
result set in one call and count client-side — this is fast (one call, not one per study)
even though it isn't a server-side facet. See the worked example in `immport://query-tips`.

## sort_field (study)

study_accession_number, study_accession (string sort — do not use for recency),
latest_data_release_date, latest_data_release_version_number, initial_data_release_date,
initial_data_release_version_number, brief_title, min_age, max_age, condition_or_disease,
research_focus, species, study_pi, actual_enrollment, planned_visit_total_count,
shared_subject_count

## sort_field (subject)

subject_accession, study_accession (both string sort), assay_method, biosample_type,
clinical_trial, condition_or_disease, ethnicity, sex, has_assessment, has_lab_test,
min_age, race, research_focus, species

## Known data quality caveats

- `pubmed_id` is a free-text field, not strictly validated — values include malformed
  entries like `"PMID: 38019093"`, `"TBD"`, `"manuscript"`, `"Not Applicable"`, and PMC
  IDs like `"PMC12149311"` mixed in with real numeric PMIDs. Filter/clean client-side if
  doing numeric PMID analysis. The nested `pubmed.pubmed_id` has the same caveat.
- Journal names in `pubmed.journal` are not normalized — the same journal can appear as
  both an abbreviation and a full name (e.g. "Nat Immunol" vs "Nature Immunology").
  Merge variants client-side before counting/ranking by journal.
"""



def register(mcp: FastMCP) -> None:

    @mcp.resource("immport://data-model")
    def data_model() -> str:
        """ImmPort data model: entity hierarchy, accession formats, API endpoint map, auth guide."""
        return IMMPORT_DATA_MODEL

    @mcp.resource("immport://query-tips")
    def query_tips() -> str:
        """Best practices, workflow patterns, and filter tips for querying ImmPort efficiently."""
        return IMMPORT_QUERY_TIPS

    @mcp.resource("immport://search-fields")
    def search_fields() -> str:
        """Full field reference for search_studies/search_subjects: source_fields, nested objects, facets, sort_field."""
        return IMMPORT_SEARCH_FIELDS

    @mcp.resource("immport://api-schema")
    async def api_schema() -> dict:
        """Full OpenAPI v3 spec for the ImmPort Data Query API — endpoint paths, parameters, and response schemas."""
        return await api_get("/v3/api-docs", requires_auth=False)
