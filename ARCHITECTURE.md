# Job Hunting AI Agent — Architecture Decision Log

## Project Overview
A personal job hunting assistant built from scratch using AWS Lambda and the 
Anthropic API. Built as both a functional tool and a portfolio project to 
demonstrate AI agent development fundamentals without relying on frameworks 
like LangChain or CrewAI.

---

## Initial Architecture Decisions

### [Pre-build] — Core approach
**What:** Build all agents from scratch without AI frameworks
**Why:** Demonstrates understanding of fundamentals over framework configuration. 
  More impressive to hiring panels and produces better learning outcomes.
**Alternatives considered:** LangChain, CrewAI, OpenAI Agents SDK

### [Pre-build] — Model strategy
**What:** Tiered model approach — Gemini 2.5 Flash (free) for lightweight tasks, 
  Claude Sonnet 4.6 (pay-as-you-go) for all LLM-heavy tasks
**Why:** Keeps costs near zero for high-volume low-stakes tasks while using 
  quality models where output matters. Haiku considered but cost difference 
  negligible at single-user scale.
**Alternatives considered:** Single model for everything, Claude Haiku for 
  mid-tier tasks

### [Pre-build] — Infrastructure
**What:** AWS Lambda with Docker containers, DynamoDB, SSM Parameter Store
**Why:** Lambda maps cleanly to agent architecture — one function per agent, 
  pay per invocation. Docker chosen over Lambda layers to resolve Python 
  dependency and architecture issues.
**Alternatives considered:** EC2, ECS, Lambda zip packages with layers

### [Pre-build] — Secrets management
**What:** AWS SSM Parameter Store (Standard tier)
**Why:** Free tier sufficient for static API keys. Secrets Manager adds 
  automatic rotation which is unnecessary for manually rotated API keys.
**Alternatives considered:** Secrets Manager, environment variables

---

## Scraping Decisions

### [Pre-build] — Job scraping approach
**What:** Apify Indeed and LinkedIn actors as primary job sources
**Why:** Major job boards have anti-scraping protections and charge heavily 
  for API access. Apify pre-built actors handle proxy rotation, CAPTCHAs, 
  and anti-bot measures.
**Alternatives considered:** Raw Playwright/BeautifulSoup scrapers, 
  Firecrawl, RSS feeds from niche boards

### [Pre-build] — Scraper architecture
**What:** Single Lambda function with source-based routing, internally 
  modular with separate scraper files per source
**Why:** Pragmatic for single-user scale. Internal modularity preserves 
  clean separation for easy future splitting if needed.
**Alternatives considered:** Separate Lambda per job board

### [Build] — LinkedIn scraper
**What:** LinkedIn scraper deferred
**Why:** Indeed working end-to-end, wanted to move on to agent development. 
  Can revisit if Apify costs become an issue.

---

## Database Decisions

### [Pre-build] — Two table structure
**What:** Separate Jobs and Companies DynamoDB tables
**Why:** Avoids duplicate company research when multiple jobs from same 
  company are scraped. Company data can be refreshed independently. 
  Enables reuse of company profiles across jobs.
**Alternatives considered:** Single table with company data embedded in 
  each job record

### [Pre-build] — Company data freshness
**What:** 30 day cache window on company research, job_count resets on refresh
**Why:** Company information changes slowly enough that 30 days is a 
  reasonable TTL. job_count treated as a rolling 30 day window rather 
  than a running total — more meaningful signal.
**Alternatives considered:** Never refresh, manual refresh only, 
  running total for job_count

---

## Pipeline Orchestration Decisions

### [Pre-build] — Step Functions for sequential pipeline
**What:** AWS Step Functions orchestrates Job Summariser → CV Matcher
**Why:** Sequential dependencies between these agents require guaranteed 
  ordering. Step Functions handles retry logic, error states, and state 
  passing between functions. Visual pipeline map in AWS console is good 
  portfolio material.
**Alternatives considered:** Pure Lambda chaining, SQS queues

### [Build] — Company Researcher decoupled from Step Functions
**What:** Company Researcher triggered independently via DynamoDB Stream, 
  runs in parallel with Step Functions pipeline
**Why:** Company Researcher and Job Summariser have no dependencies on each 
  other's output. Parallel execution is faster and cleaner. Failure in 
  company research doesn't block job summarisation.
**Alternatives considered:** Company Researcher as first Step Functions state,
  Company Researcher triggered by Job Summariser completion

### [Pre-build] — DynamoDB Stream configuration
**What:** NEW_IMAGE stream view type, batch size 1
**Why:** NEW_IMAGE required to include full job data in stream event. 
  Batch size 1 ensures each job triggers its own independent pipeline 
  execution rather than batching multiple jobs together.
**Alternatives considered:** NEW_AND_OLD_IMAGES, larger batch sizes

### [Pre-build] — On-demand triggers
**What:** Resume Tailor and Cover Letter Generator run on-demand via 
  frontend rather than automatically
**Why:** These are high-cost, high-value steps that benefit from human 
  review of CV match score before running. Automated triggers deferred 
  to future maturity stage.
**Alternatives considered:** Automatic trigger above score threshold

### [Post-build] — SQS queue + job-processor replaces Step Functions + dual EventBridge Pipes
**What:** Replaced EventBridge Pipe → Step Functions (job-summariser → cv-matcher) 
  and separate EventBridge Pipe → company-researcher with a single 
  EventBridge Pipe → SQS → job-processor Lambda. job-processor invokes 
  job-summariser, company-researcher, and cv-matcher sequentially within 
  a single execution. SQS event source mapping configured with 
  MaximumConcurrency=2.
**Why:** Five jobs scraping simultaneously caused all five pipeline 
  executions to fire at once. This exhausted the 30,000 token/min 
  Anthropic rate limit (each summariser call is ~4,000 tokens × 5 = 20,000+ 
  tokens in under a minute) and approached the AWS account-level Lambda 
  concurrency limit of 10. Step Functions retries on rate limit errors 
  backed off insufficiently (SDK max ~8s, rate limit window is 60s). 
  SQS MaximumConcurrency=2 limits concurrent executions at the queue 
  level and does not consume reserved concurrency, bypassing the account 
  minimum-unreserved-concurrency constraint.
**Alternatives considered:** Reserved concurrency on individual Lambdas 
  (blocked by account minimum of 10 unreserved), Step Functions with 
  longer retry intervals, processing jobs one at a time (MaximumConcurrency=1 
  not supported by SQS — minimum is 2)

### [Post-build] — Company Researcher moved into sequential pipeline
**What:** Company Researcher now runs as the second step inside job-processor 
  (after job-summariser, before cv-matcher), replacing its own dedicated 
  EventBridge Pipe trigger.
**Why:** Parallel execution of Company Researcher was the original design, 
  but with the move to sequential SQS processing there is no longer a 
  separate parallel execution path. Putting Company Researcher in the 
  sequential chain keeps all three API-calling Lambdas under the same 
  concurrency cap and simplifies the triggering infrastructure from two 
  EventBridge Pipes to one.
**Alternatives considered:** Keep Company Researcher on separate Pipe A 
  (retained original parallel design)

---

## Agent Decisions

### [Build] — Company Researcher prompt structure
**What:** XML input tags, structured flat output, user preferences as 
  multi-select fields plus free text, 1-10 fit score with reasoning, 
  research_confidence field
**Why:** XML tags parse cleanly with Claude. Structured output easier 
  to parse programmatically than JSON. Multi-select preferences capture 
  nuance better than single select. Confidence field handles low 
  web-presence companies without hallucination.
**Alternatives considered:** JSON output, hard character limits on fields, 
  binary required/optional for preferences

### [Build] — Company website resolution
**What:** Company Researcher finds website itself rather than pre-resolving
**Why:** Trivial subtask for a research agent. Adding a separate Lambda 
  just to resolve URLs adds unnecessary infrastructure complexity. 
  research_confidence covers failure cases.
**Alternatives considered:** Pre-resolve website in separate Lambda, 
  include website from Apify scraper data

### [Build] — Job Summariser emphasis scoring
**What:** Each requirement bullet scored low/mid/high based on emphasis 
  in job description, defined once in a scoring guide rather than per field
**Why:** Captures nice-to-have vs hard requirement nuance better than 
  binary required/optional. Single scoring guide avoids repetition and 
  produces more consistent scoring across fields. Feeds directly into 
  CV Matcher weighting.
**Alternatives considered:** Binary required/optional, separate 
  nice-to-have field, no scoring

### [Build] — CV Matcher input
**What:** Uses existing CV DynamoDB table from prior resume website project, 
  fetched at module level with hardcoded key
**Why:** CV already exists in structured format with education/experience/
  skills/summary sections that map directly to job summary structure. 
  Module level fetch caches across invocations.
**Alternatives considered:** CV in user JSON config, CV hardcoded in prompt, 
  CV stored in SSM

### [Build] — CV Matcher framing
**What:** "Expert recruiter evaluating from the candidate's perspective"
**Why:** Candidate-perspective framing produces honest gap identification 
  rather than screening-out behaviour. More useful for deciding whether 
  to apply.
**Alternatives considered:** Neutral recruiter framing, no persona

### [Post-build] — Rate limit retry pattern
**What:** All LLM Lambdas (job-summariser, company-researcher, cv-matcher) 
  catch RateLimitError explicitly and sleep 65 seconds before retrying, 
  in addition to setting max_retries=8 on the Anthropic client.
**Why:** The Anthropic SDK's built-in exponential backoff caps at ~8 seconds 
  total wait time. The 30,000 token/min rate limit window resets after 60 
  seconds — the SDK exhausts all retries before the window resets, causing 
  permanent failure. The 65s sleep guarantees the window has reset before 
  the single manual retry attempt.
**Alternatives considered:** Rely entirely on SDK retries (insufficient), 
  exponential backoff loop (more complex, same outcome at this scale)

### [Post-build] — Job Summariser parser bug fix
**What:** Changed `line.split(": ", 1)` to `line.split(":", 1)` in the 
  response parsing loop in job-summariser/handler.py
**Why:** Claude outputs list section headers as `education_requirements:` 
  with no trailing space. The original split on `": "` (colon + space) 
  never matched these lines, so `list_section` was never set and all 
  requirement bullet items were silently dropped. All five tested jobs 
  had empty education, experience, and skill requirement lists as a result.
**Alternatives considered:** Adjust system prompt to force a value on the 
  same line as the section header (fragile), strip trailing colon and 
  match by key name only

---

## Docker / Deployment Decisions

### [Build] — BuildKit attestation manifests
**What:** --provenance=false flag required on all Lambda image builds
**Why:** BuildKit adds OCI attestation manifests by default that Lambda 
  rejects with InvalidParameterValueException. Flag prevents this.
**Alternatives considered:** Disable BuildKit globally

### [Build] — Multi-file Lambda imports
**What:** __init__.py required in all subdirectories
**Why:** Without __init__.py Python does not treat subdirectories as 
  packages and imports fail silently.

### [Build] — SSM SecureString permissions
**What:** kms:Decrypt permission required on Lambda execution role 
  in addition to ssm:GetParameter
**Why:** SecureString parameters are KMS encrypted. Standard SSM read 
  permissions are insufficient without explicit KMS decrypt access.

---

## Deferred / Planned

- LinkedIn scraper implementation
- Scraper trigger (scheduled or manual)
- Resume Tailor Lambda
- Cover Letter Generator Lambda
- Testing and refinement pass across all agents
- Onboarding agent (chat-based user config generation)
- Multi-user support (user preferences and CV storage)
