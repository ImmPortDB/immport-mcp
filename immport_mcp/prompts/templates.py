"""
Reusable MCP prompt templates for common ImmPort research workflows.
Each prompt provides step-by-step instructions that guide the model
to call the right tools in the right order with the right parameters.
"""
from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:

    @mcp.prompt(
        description="Discover and summarize ImmPort studies for a disease or research topic."
    )
    def explore_disease_area(topic: str, max_studies: int = 5) -> str:
        return f"""
You are an expert in immunology data analysis using the ImmPort repository.

## Goal
Find all ImmPort studies related to "{topic}" and produce a structured summary.

## Steps
1. Issue these calls IN PARALLEL (lookup_controlled_vocabulary is zero-cost, no need to wait):
   - `lookup_controlled_vocabulary("lkResearchFocus")` — find exact term matching "{topic}"
   - `lookup_controlled_vocabulary("lkDisease")` — find exact disease term matching "{topic}"
   - `search_studies(term="{topic}", page_size=25, source_fields=["study_accession", "brief_title", "condition_or_disease", "research_focus", "assay_method", "actual_enrollment", "has_lab_test", "has_assessment"])`

2. Check hits.total.value — if total > 25, issue another call with from_record=25.
   Use exact vocabulary terms from step 1 to refine:
   - `search_studies(condition_or_disease=["<exact_term>"], page_size=25, source_fields=[...])`

3. For the top {max_studies} studies by relevance, issue ALL of these IN PARALLEL:
   - `get_study_data(study_accession, resource="summary")` for each study
   (These are independent calls — do not wait for one before starting the next.)

4. For each study, also issue IN PARALLEL:
   - `get_study_data(study_accession, resource="planned_visits")` — study timeline
   - `get_study_data(study_accession, resource="experiments")` — assay types performed

5. Present a **comparison table** with columns:
   | Study | Title | PI | Subjects | Assay Types | Key Condition | Data Completeness |

6. Highlight the **2 most data-rich** studies and explain what analyses are possible.

Refer to `immport://data-model` for entity relationships and accession ID formats.
Refer to `immport://query-tips` for workflow guidance.
"""

    @mcp.prompt(
        description="Comprehensive deep-dive analysis of a single ImmPort study."
    )
    def study_deep_dive(study_accession: str) -> str:
        return f"""
You are an expert in immunology data analysis using the ImmPort repository.

## Goal
Perform a comprehensive analysis of ImmPort study **{study_accession}**.

## Steps

### Step 1 — Orientation (single call first)
`get_study_data("{study_accession}", resource="summary")` — extract:
- Objectives, hypothesis, study design (interventional/observational)
- Subject count, data completeness level, publications, funding, assay types

> **Sparse data check:** If the summary shows very few subjects or limited assay data,
> do NOT stop at API calls. Many ImmPort studies — especially older studies and recently
> released data — have minimal parsed data in the database. The full detail lives in the
> study files. After Step 2, always run Step 3a to check file availability.

### Step 2 — Parallel metadata calls
Issue ALL of these IN PARALLEL (they are independent, JSON responses, no download needed):

```
get_study_data("{study_accession}", resource="arm")             # cohorts/treatment groups
get_study_data("{study_accession}", resource="planned_visits")  # visit schedule
get_study_data("{study_accession}", resource="subjects")        # demographics
get_study_data("{study_accession}", resource="experiments")     # assay types performed
get_study_data("{study_accession}", resource="adverse_events")  # AE profile
get_study_data("{study_accession}", resource="immune_exposure") # vaccines/infections
get_study_data("{study_accession}", resource="inclusion_exclusion") # eligibility criteria
```

### Step 3a — Study files (always check; critical for sparse studies)
```
get_study_files(action="manifest", study_accession="{study_accession}")
```
Review what is available:
- `study_file` / `protocol_file` — plain text or TSV documents; stream them inline:
  `get_study_files(action="stream", file_uuid="<uuid>")` for AI analysis
- `release_file` ending `_Tab.zip` — every metadata table as a TSV; provide signed URL:
  `get_study_files(action="download", file_uuid="<uuid>")` → present URL to user
- `zip_file` (`ALL_DATA.zip`) — full dataset including raw files; warn user it can be
  very large (some studies >100 GB); provide signed URL only if user requests it

### Step 3b — Assay results
From the experiments response, identify measurement techniques, then query results.
Issue assay calls IN PARALLEL if multiple assay types are present:

| Technique | assay_type |
|---|---|
| Flow Cytometry / CyTOF | fcs_analyzed |
| ELISA | elisa |
| ELISPOT | elispot |
| HAI (influenza antibody) | hai |
| Neutralizing Antibody Titer | neut_ab_titer |
| PCR / Q-PCR | pcr |
| Luminex / MBAA / Cytokine | mbaa |
| Mass Spectrometry | mass_spectrometry |
| Lab Tests (clinical) | lab_test |

```
get_assay_results(assay_type="<technique>", study_accession=["{study_accession}"])
```

Use `study_time_collected_gte` / `study_time_collected_lte` to scope to time windows.
For RNA-seq / gene expression: use the manifest (Step 3a) with `file_type="result_file"`
to locate raw files, then `get_study_files(action="download", file_uuid="<uuid>")` for
the signed download URL.

### Step 4 — Tab.zip (only if you need join tables with no API equivalent)
Use Tab.zip ONLY when you specifically need:
- `reagent.txt` — full reagent specs with catalog numbers and clone IDs
- `expsample_2_reagent.txt` — which reagents were used per sample
- `fcs_header_marker.txt` — FCS channel map (inspect before downloading raw FCS files)
- `expsample_2_treatment.txt` — treatment-to-sample join table

```
get_study_files(action="manifest", study_accession="{study_accession}", file_type="release_file")
  → find fileName ending in "_Tab.zip", check filesizeBytes
  → get_study_files(action="download", file_uuid="<uuid>")  # returns signed URL
```

Do NOT use Tab.zip as a replacement for Steps 2–3 — API calls return JSON directly
and require no download, unzip, or TSV parsing.

## Output
Produce a structured report:
- **Study Overview** (design, objectives, funding, data completeness)
- **Cohort Summary** (demographics: N, age range, sex, race breakdown per arm)
- **Study Timeline** (visit schedule with day ranges)
- **Available Data** (assay types + record counts)
- **Adverse Events** (severity distribution, notable Grade 3+ events)
- **Research Questions** this dataset can answer

Refer to `immport://query-tips` for workflow guidance and filter tips.
"""

    @mcp.prompt(
        description="Compare immune responses across multiple ImmPort studies for a vaccine or pathogen."
    )
    def compare_immune_responses(
        pathogen_or_vaccine: str,
        assay_focus: str = "all",
    ) -> str:
        assay_note = (
            f"Focus specifically on **{assay_focus}** assay data."
            if assay_focus != "all"
            else "Consider all available assay types."
        )
        return f"""
You are an expert immunologist analyzing cross-study immune response data in ImmPort.

## Goal
Compare immune responses to **{pathogen_or_vaccine}** across multiple ImmPort studies.
{assay_note}

## Steps

1. Issue IN PARALLEL:
   - `lookup_controlled_vocabulary("lkDisease")` — find exact disease term
   - `lookup_controlled_vocabulary("lkExpMeasurementTech")` — find exact assay technique names
   - `search_studies(term="{pathogen_or_vaccine}", page_size=50, source_fields=["study_accession", "brief_title", "assay_method", "actual_enrollment", "condition_or_disease"])`

2. Check hits.total.value — page if needed. Filter results to studies with assay data.

3. Issue IN PARALLEL for all candidate studies:
   - `get_study_data(study_accession, resource="experiments")` for each
   (Independent calls — do not wait for one before starting the next.)

4. Retrieve assay data for confirmed studies. Scope to key time windows
   using `study_time_collected_gte` and `study_time_collected_lte`:
   - Pre-vaccination: days -14 to 0
   - Acute response: days 1–14
   - Peak response: days 14–28
   - Memory: days 90–180

   Issue assay calls IN PARALLEL across all confirmed study accessions:

5. For serology:
   ```
   get_assay_results(assay_type="hai",          study_accession=[list])  # HAI titers (influenza)
   get_assay_results(assay_type="neut_ab_titer", study_accession=[list])  # neutralization
   get_assay_results(assay_type="elisa",         study_accession=[list])  # antibody concentrations
   ```

6. For cellular immunity:
   ```
   get_assay_results(assay_type="fcs_analyzed", study_accession=[list])  # T/B cell populations
   get_assay_results(assay_type="elispot",      study_accession=[list])  # antigen-specific T cells
   ```
   ⚠️ fcs_analyzed rows are per-gated-population (dozens per subject) — a single visit across
   a whole study can be 10,000+ rows. Narrow with `planned_visit_accession` + `arm_accession`,
   and use `name_contains` (e.g. "CD95+") or `summarize=True` to avoid pulling raw rows you'll
   just filter/aggregate yourself afterward.


7. For transcriptomics / gene expression:
   ```
   get_study_files(action="manifest", study_accession=<accession>, file_type="result_file")
     → get_study_files(action="download", file_uuid="<uuid>")  # signed URL for each raw file
   ```

## Output
- **Studies compared** table (accession, subjects, assay types, time points)
- **Response magnitude** comparison by study and time point
- **Cohort differences** that may explain variation
- **Studies with most comprehensive longitudinal data** (top 2-3)
- **Recommended studies** for meta-analysis
"""

    @mcp.prompt(
        description="Build a cross-study subject cohort matching specific demographic and clinical criteria."
    )
    def build_cohort(
        condition: str,
        min_age: int = 18,
        max_age: int = 65,
        gender: str = "",
        species: str = "Homo sapiens",
    ) -> str:
        gender_str = f", gender=['{gender}']" if gender else ""
        return f"""
You are building a cross-study subject cohort for immunological analysis in ImmPort.

## Cohort Criteria
- Condition: **{condition}**
- Age range: **{min_age}–{max_age} years**
- Species: **{species}**
{"- Gender: **" + gender + "**" if gender else ""}

## Steps

1. Issue IN PARALLEL (lookups are zero-cost, no need to wait before searching):
   - `lookup_controlled_vocabulary("lkDisease")` — find exact term for "{condition}"
   - `lookup_controlled_vocabulary("lkResearchFocus")` — alternate focus term
   - `lookup_controlled_vocabulary("lkSpecies")` — confirm species name
   - `search_subjects(term="{condition}", species=["{species}"]{gender_str}, page_size=100, source_fields=["study_accession", "subject_accession", "sex", "race", "min_age", "max_age", "condition_or_disease"])`

2. Check hits.total.value — if total > 100, page with from_record=100, 200, etc.
   Extract unique `study_accession` values from the hits.

3. For each unique study, issue ALL of these IN PARALLEL:
   - `get_study_data(study_accession, resource="summary")` — confirm it matches criteria
   - `get_study_data(study_accession, resource="experiments")` — data completeness
   (Independent calls — do not wait for one before starting the next.)

4. Filter subjects to age range {min_age}–{max_age} using `minSubjectAge`/`maxSubjectAge`
   fields from subject records.

5. For subjects of interest, retrieve clinical data IN PARALLEL across studies:
   - `get_assay_results(assay_type="lab_test", study_accession=[list])` — clinical labs
   - `get_study_data(study_accession, resource="adverse_events")` — safety profile

## Output
- **Cohort definition table**: study, N subjects, demographics, available data types
- **Data completeness matrix**: which assays are available per study
- **Recommended primary dataset** for analysis
- **Exclusion criteria** to note (e.g., animal studies, incomplete data)
- Total cross-study N and demographic breakdown
"""

    @mcp.prompt(
        description="Find and download data files for a study."
    )
    def download_study_files(
        study_accession: str,
        file_type: str = "",
    ) -> str:
        ftype_note = f"Focus on file type: **{file_type}**." if file_type else "Retrieve all file types."
        return f"""
You are helping a researcher access data files from ImmPort study **{study_accession}**.
{ftype_note}

## File types available
| file_type | Contents |
|---|---|
| study_file | Supplemental study documents (PDFs, text, protocols) |
| protocol_file | Protocol documents |
| result_file | Individual assay result files (FCS, CEL, txt, gz, etc.) |
| release_file | Current DR package: Tab zip, MySQL zip, summary, subject mapping |
| archive_file | Older DR versions: SDY##-DR{{n}}.zip (Tab + MySQL, no raw files) |
| zip_file | SDY##_DR##_ALL_DATA.zip — ALL metadata + ALL raw result files |

## Steps

1. Get the full file manifest:
   ```
   get_study_files(action="manifest", study_accession="{study_accession}"{', file_type=' + repr(file_type) if file_type else ''})
   ```
   Review the list — note `fileUUID`, `fileName`, and `filesizeBytes` for files of interest.

2. For **text / TSV / small files** (study_file, protocol_file, individual result TSVs):
   Stream the content inline for direct analysis:
   ```
   get_study_files(action="stream", file_uuid="<fileUUID>")
   ```
   Returns `{{content: "..."}}` for text files — read directly without downloading.

3. For **zip files and large binaries** (Tab.zip, ALL_DATA.zip, FCS, CEL files):
   Get a signed download URL:
   ```
   get_study_files(action="download", file_uuid="<fileUUID>")
   ```
   Returns `{{"url": "<signed URL>"}}` — provide this URL to the user for browser download
   or use with curl/wget:
   ```bash
   wget -O {{fileName}} "{{signed_url}}"
   ```

## Common patterns

**All structured metadata tables (current release):**
```
get_study_files(action="manifest", study_accession="{study_accession}", file_type="release_file")
  → find fileName ending "_Tab.zip"
  → get_study_files(action="download", file_uuid="<uuid>")  # returns signed URL
```
The Tab.zip contains every study table as a TSV file (subjects, experiments, samples, reagents, etc.)

**Complete dataset including raw result files:**
```
get_study_files(action="manifest", study_accession="{study_accession}", file_type="zip_file")
  → check filesizeBytes first — can be >100 GB for large studies
  → get_study_files(action="download", file_uuid="<uuid>")  # returns signed URL
```

**Older data release version:**
```
get_study_files(action="manifest", study_accession="{study_accession}", file_type="archive_file")
  → fileName format: SDY##-DR{{n}}.zip
  → get_study_files(action="download", file_uuid="<uuid>")
```

## Notes
- Signed URLs are time-limited (~1 hour) — download promptly after receiving the URL
- ImmPort enforces a per-file daily download limit — avoid redundant calls
- For integrity verification, cross-check file sizes against `filesizeBytes` in the manifest
"""
