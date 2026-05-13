# DynamoDB schema reference

This document captures the canonical shapes of records across the jobs,
companies, and candidate_profiles tables in the new two-pass architecture.

It is the single source of truth for schema design. Lambda implementations,
API responses, and frontend reads should match these shapes exactly. If
the implementation needs to deviate, update this document first and reflect
that deviation here.

## `jobs` table

Every record in the `jobs` table has the following structure. Fields under
`screening` and `analysis` are populated by their respective pipeline
passes; on-demand fields are populated by user-triggered actions.

```
{
  // Top-level scrape data — populated by the scraper at ingestion time
  "id": string (job_<uuid>),
  "positionName": string,
  "company": string (raw scraped company name),
  "location": string,
  "url": string,
  "description": string,
  "salary": string | null,
  "source": "indeed" | "linkedin" | "manual",
  "status": "new" | "applied" | "interviewing" | "offer" | "rejected",
  "scrapedAt": ISO 8601 string,
  "postingDate": ISO 8601 string | null,
  "status_changed_at": ISO 8601 string,   // optional; written server-side on every status change
  "archived_at": ISO 8601 string,          // optional; present only when the job is archived

  // Screening pass output — populated by job-screener Lambda on DDB INSERT
  "company_id": string (comp_<uuid>, FK to companies table),
  "screening": {
    "status": "pending" | "complete" | "failed",
    "canonical_name_confidence": "low" | "mid" | "high",
    "match_score": integer 1-10,
    "match_reasoning": string (one paragraph),
    "recommendation_score": integer 1-10,
    "recommendation_reasoning": string (one paragraph),
    "divergence_reasoning": string (one to two sentences)
  },

  // Full analysis pass output — populated by job-processor Lambda chain
  // when the user triggers POST /jobs/{id}/full-analysis or POST /jobs/full-analysis
  "analysis": {
    "status": "pending" | "complete" | "failed",
    "summary": {
      "job_title": string,
      "job_summary": string,
      "education_requirements": [ { "requirement": string, "confidence": "low" | "mid" | "high" } ],
      "experience_requirements": [ { "requirement": string, "confidence": "low" | "mid" | "high" } ],
      "skill_requirements": [ { "requirement": string, "confidence": "low" | "mid" | "high" } ],
      "salary": string,
      "red_flags": string
    },
    "match_score": integer 1-10,
    "match_reasoning": string (paragraph)
  },

  // On-demand outputs — populated by resume-tailor and cover-letter-generator Lambdas
  "tailored_resume_status": "generating" | "done" | "failed",
  "tailored_resume_started_at": ISO 8601 string,
  "tailored_resume_error": string | null,
  "tailored_resume_data": object,
  "tailored_resume": string (markdown),
  "tailored_resume_pdf_key": string (S3 key),
  "tailored_resume_pdf_url_cached": string (presigned URL),
  "tailored_resume_pdf_url_generated_at": ISO 8601 string,

  "cover_letter_status": "generating" | "done" | "failed",
  "cover_letter_started_at": ISO 8601 string,
  "cover_letter_error": string | null,
  "cover_letter_data": object,
  "cover_letter": string (markdown),
  "cover_letter_pdf_key": string (S3 key),
  "cover_letter_pdf_url_cached": string (presigned URL),
  "cover_letter_pdf_url_generated_at": ISO 8601 string,

  // URL ingestion fields — only present for jobs submitted via POST /jobs/from-url
  "ingestion_status": "success" | "failed",
  "ingestion_error": string     // only present when ingestion_status = "failed", truncated to 500 chars
}
```

### Notes on archive and status-change timestamps

- `archived_at` is the master exclusion flag for the active buckets (Screened, Analysed, Applied).
  Any job with `archived_at` set is excluded from those views regardless of any other field.
  It is absent on active jobs and present only on archived jobs. Never infer archive state from
  `status` — use `archived_at` as the sole source of truth.
- `archived_at` is written by two paths: the `POST /jobs/{id}/archive` endpoint (manual dismiss),
  and automatically by `PUT /jobs/{id}/status` when the new status is `rejected`.
- `status_changed_at` is written server-side on every call to `PUT /jobs/{id}/status`.
  The frontend does not write to this field directly. Used by the Applied bucket and Archive view
  for sort order.
- Setting status to `rejected` automatically sets `archived_at` server-side in the same update call.
  A rejected job can be manually restored via `POST /jobs/{id}/restore` (removes `archived_at`),
  at which point it appears in the Applied bucket (status is still `rejected`).
- `archived` is no longer a valid value for the `status` field. Archiving is a separate operation
  from status progression.

### Notes on the URL ingestion fields

- `ingestion_status` and `ingestion_error` are written exclusively by the `job-url-ingester` Lambda.
  Scraped jobs (source `"indeed"` or `"linkedin"`) never set these fields.
- The EventBridge Pipe from the jobs DDB stream → `job-screening-queue` carries a filter that passes
  only records where `ingestion_status` is absent or `"success"`. Records with `ingestion_status = "failed"`
  sit inert in the table and never enter the screening or analysis pipeline.
- A failed-ingestion record contains only `id`, `url`, `source`, `scrapedAt`, `ingestion_status`,
  and `ingestion_error`. It has no `positionName`, `company`, `description`, or pipeline fields.

### Notes on the screening fields

- `company_id`: foreign key to the companies table (comp_<uuid>). Written
  by the screener after resolving or creating the company record. The canonical
  name lives on the company record; the raw scraped name is on `company`.
- `match_score`: how well the candidate's technical background fits what
  the role requires. Strict technical question. 1-10 integer.
- `recommendation_score`: whether the candidate should spend further effort
  investigating this job. Includes match plus role type, environment
  signals, and overall fit. 1-10 integer.
- `divergence_reasoning`: always populated. Short sentence when scores are
  aligned (differ by 1 point or less); identifies the specific causing
  factor when scores diverge (differ by 2 or more).

### Notes on the analysis fields

- `analysis.match_score` and `analysis.match_reasoning` are renamed from
  the previous flat `match_score` and `match_summary` fields. The naming
  matches the screening side for cross-pass consistency.
- `analysis.summary` retains its previous structure and is unchanged from
  what `job-summariser` produces today.

## `companies` table

```
{
  "id": string (comp_<uuid>, partition key),
  "canonical_name": string,
  "aliases": [string] (all names that resolve to this record),
  "website": string,
  "industry": string,
  "company_size": string,
  "summary": string,
  "culture_notes": [string],
  "recent_news": string,
  "hiring_reputation": string,
  "candidate_fit_score": integer 1-10,
  "candidate_fit_reasoning": string,
  "research_confidence": "low" | "medium" | "high",
  "research_last_updated": ISO 8601 string,
  "job_count": integer
}
```

A GSI named `canonical-name-index` indexes `canonical_name` for fast lookup
by name without scanning the table.

The screening pass writes a stub record (`id`, `canonical_name`, `aliases`,
`job_count`) when it encounters a new company. On subsequent jobs for the
same company it increments `job_count` and appends new aliases. Research
fields (`website`, `industry`, `summary`, etc.) and `research_last_updated`
are written exclusively by `company-researcher`. A missing `research_last_updated`
field indicates research has not yet been performed for that company.

## `candidate_profiles` table

Single record with `profile_id == "primary"`.

```
{
  "profile_id": "primary",
  "name": string,
  "location": string,
  "phone": string,
  "email": string,
  "linkedin": string,
  "github": string,
  "summary": string (used by resume-tailor for the structured resume),
  "experience": [ ... structured ... ],
  "skills": [ ... structured ... ],
  "education": { ... structured ... },
  "projects": [ ... structured ... ],
  "preferences": object (used by company-researcher for fit scoring),
  "cv_summary_text": string (used by job-screener for screening pass)
}
```

### Notes

- `cv_summary_text` is a plain-prose paragraph form of the candidate's
  background, written specifically for the screening pass. It is not
  derived from the structured CV — the candidate maintains it manually.
- The structured CV (`experience`, `skills`, `education`, `projects`,
  `summary`) is used by `resume-tailor` and `cover-letter-generator`.
- `preferences` is used by `company-researcher` only — the screening pass
  does not consume it.

## `company_name_mappings` table

```
{
  "raw_name": string (primary key),
  "company_id": string (comp_<uuid>)
}
```

Indexes any historical name pointing to a company record. Both the screener
(writing raw scraped names) and the researcher (writing old canonical names
when a canonical name is overwritten) populate this table. Looked up during
screening to short-circuit re-resolution of names already seen.

## ID prefixing conventions

All generated IDs carry a type prefix so that any stored ID is self-describing:

- `job_<uuid>` — job records (generated by the scrapers)
- `comp_<uuid>` — company records (generated by the screener on first encounter)
- No prefix — session tokens (opaque strings, not UUIDs)

## `sessions` table

Unchanged from current implementation. PIN-based auth tokens.

```
{
  "token": string (primary key),
  "expires_at": integer (Unix timestamp)
}
```