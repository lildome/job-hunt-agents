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
  "id": string (UUID),
  "positionName": string,
  "company": string (raw scraped company name),
  "location": string,
  "url": string,
  "description": string,
  "salary": string | null,
  "source": "indeed" | "linkedin" | "manual",
  "status": "new" | "applied" | "interviewing" | "offer" | "rejected" | "archived",
  "scrapedAt": ISO 8601 string,
  "postingDate": ISO 8601 string | null,

  // Screening pass output — populated by job-screener Lambda on DDB INSERT
  "screening": {
    "status": "pending" | "complete" | "failed",
    "canonical_company_name": string,
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
  "cover_letter_pdf_url_generated_at": ISO 8601 string
}
```

### Notes on the screening fields

- `canonical_company_name`: the public-facing brand or parent company name,
  resolved by the screening LLM. Falls back to the raw scraped name when
  `canonical_name_confidence == "low"`.
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
  "company_name": string (canonical name, primary key),
  "aliases": [string] (raw scraped names that resolve to this canonical name),
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
  "last_updated": ISO 8601 string,
  "job_count": integer
}
```

The screening pass writes a stub record (just `company_name`, `aliases`,
`job_count`, optionally `last_updated`) when it encounters a new canonical
name. The analysis pass populates all other fields when triggered by the
user.

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
  "canonical_company_name": string
}
```

Populated by the screener when it resolves a canonical name with `mid` or
`high` confidence. Looked up at the start of every screening call to
avoid re-resolving names already seen.

## `sessions` table

Unchanged from current implementation. PIN-based auth tokens.

```
{
  "token": string (primary key),
  "expires_at": integer (Unix timestamp)
}
```