# Ollama Learning Extraction Research Plan

Status: research and design reference. The README is the current user guide. Use this document for rationale behind the optional Ollama path, not as setup instructions for first local use.

I’ll ground this as a design-research pass using current Ollama, local model, and SQLite-vector capabilities, then map it into an implementation-ready `prlearn` pipeline, schema, prompts, tests, and rollout plan.

A key direction is a gated hybrid pipeline: typed evidence chunks, structured JSON outputs, embeddings for dedupe, deterministic hashes, and SQLite review state as source of truth.

## Executive Recommendation

Build `prlearn` v2 as a hybrid, evidence-first extraction system:

```text
deterministic selection + redaction + chunking + local embeddings + Ollama structured extraction + hybrid dedupe + human review
```

Do not replace the current heuristic extractor with a free-form LLM pass over whole PRs. The current 3-card result is low partly because the strongest learning signal in PRs is usually review feedback and failed-fix evidence, while your current ingestion list includes issue comments but does not mention PR review comments/reviews. GitHub treats issue/PR conversation comments and pull-request review comments as distinct APIs; review comments are the line-level code-review comments, while issue comments cover the general PR conversation.

The most important v2 principle is:

> The model may propose a lesson, but the database decides whether it is evidence-backed, non-duplicative, redacted, and reviewable.

Ollama is a good fit because it gives you a local HTTP API, structured JSON outputs, embeddings, model metadata, and local-only operation. Ollama’s docs say local runs do not expose your prompts/data to Ollama, cloud features can be disabled, and the default API/bind behavior is local-oriented.

## 1. Recommended Ollama Architecture

### Architecture Shape

Use a pipeline of specialized local steps, not one monolithic model call.

```text
GitHub SQLite data
  -> deterministic evidence selector
  -> redactor
  -> chunker
  -> local embeddings
  -> similar accepted-card retrieval
  -> PR/chunk classification
  -> candidate learning extraction
  -> deterministic validation
  -> hybrid dedupe / recurrence update
  -> human review
  -> accepted learning cards
  -> markdown + JSON exports
```

The LLM should operate only on redacted, selected, normalized chunks. It should never receive the whole raw PR JSON unless the PR is tiny and already redacted.

### Model Classes

Use four model classes:

| Role | Purpose | Recommended Default |
| --- | --- | --- |
| Embedding model | Find similar chunks/cards, cluster near-duplicates, power preflight retrieval. | `qwen3-embedding:0.6b` or `bge-m3` |
| Small classifier | Cheaply label chunks as review feedback, CI failure, self-status, noise, etc. | `qwen3.5:2b` or `qwen3.5:4b` |
| Extractor model | Produce candidate lessons from evidence bundles. | `qwen3.5:9b` default; `qwen3.5:4b` fallback |
| Judge/merge model | Resolve borderline dedupe and recurrence decisions. | Same as extractor, or stronger local model in “deep” mode |

Ollama supports structured outputs using a JSON schema and recommends low temperature, reusable Pydantic/Zod schemas, and grounding the model with the schema in the prompt. That is exactly what `prlearn` needs for deterministic-enough extraction.

### Recommended Local Model Profiles

#### Default Profile: Practical Daily Use

Use this for most developer machines:

```toml
[ollama]
base_url = "http://127.0.0.1:11434"
require_localhost = true
require_no_cloud = true

[models]
embedder = "qwen3-embedding:0.6b"
classifier = "qwen3.5:2b"
extractor = "qwen3.5:9b"
judge = "qwen3.5:9b"

[options]
temperature = 0
seed = 42
top_p = 0.1
num_ctx = 8192
stream = false
think = false
```

`qwen3.5` is attractive because Ollama lists small 0.8B, 2B, 4B, and 9B variants with 256K context tags and modest model file sizes: 2.7GB for 2B, 3.4GB for 4B, and 6.6GB for 9B. Larger 27B/35B variants are listed at 17GB/24GB, so they are workstation rather than everyday laptop defaults.

#### Low-Resource Profile

```toml
[models]
embedder = "embeddinggemma:300m-qat-q8_0"
classifier = "qwen3.5:0.8b"
extractor = "qwen3.5:4b"
judge = "qwen3.5:4b"
```

Use this when CPU-only or memory-constrained. `embeddinggemma` is only a few hundred MB depending on quantization, but its Ollama page lists a 2K context window, so chunk aggressively.

#### Higher-Quality Local Workstation Profile

```toml
[models]
embedder = "qwen3-embedding:4b"
classifier = "qwen3.5:4b"
extractor = "qwen3.5:27b"
judge = "gpt-oss:20b"
```

`gpt-oss:20b` is listed by Ollama as a 14GB local model designed for reasoning, agentic tasks, and developer use cases; `gpt-oss:120b` is 65GB and should be a deep/offline option only.

#### Do Not Make These Daily Defaults

`qwen3-coder-next` is highly relevant to coding, but Ollama lists the local tag at 52GB with a 256K context window, which makes it unsuitable as the default daily extractor. Use it only for “deep audit” mode on powerful machines.

### Embedding Choice

Default to `qwen3-embedding:0.6b`.

Reasons:

- Ollama lists qwen3-embedding sizes from 0.6B to 8B.
- The 0.6B model is listed at 639MB with a 32K context window.
- The Qwen3 embedding family is described as supporting retrieval, code retrieval, classification, clustering, and multilingual/code retrieval.

Alternative defaults:

- `bge-m3`: good if you want long-ish text chunks and multilingual support. Ollama lists it at 1.2GB with 8K context and describes it as multi-function, multilingual, and multi-granularity up to 8192 tokens.
- `embeddinggemma`: good for small machines, less ideal for long evidence chunks because of the 2K context window.
- `mxbai-embed-large`: good older local embedding option, but Ollama lists a 512-token context window, making it less convenient for PR evidence chunks.

### Rerankers

Do not make a reranker a required dependency in MVP.

Ollama’s documented API endpoints include generate, chat, model management, and embeddings, but not a first-class rerank endpoint. Use a hybrid retriever first:

- canonical-key match,
- embedding nearest-neighbor,
- SQLite FTS5 lexical match,
- LLM judge only for borderline cases.

Add optional reranking later through adjacent local tooling if it demonstrably improves precision.

### Determinism Controls

For every model call:

- use structured JSON schema,
- set `temperature=0`,
- set `seed`,
- set explicit `num_ctx`,
- use `stream=false`,
- pass `think=false` for thinking models when supported,
- validate with Pydantic,
- cache by input hash.

Ollama exposes temperature, seed, num_ctx, and num_predict through model options/Modelfile parameters. The API also exposes `think` for thinking models and structured-output `format` support.

Also, set context explicitly. Ollama’s docs currently show both a general 4096-token default in FAQ and VRAM-based defaults on the context-length page, so `prlearn` should not depend on implicit defaults.

## 2. Data Selection Strategy

### Highest-Signal Evidence Types

Send these to Ollama first:

1. PR review comments and review bodies  
   Especially non-author comments, requested changes, review-thread replies, and line-level comments.

2. Failed check annotations  
   Particularly typecheck, lint, build, test, migration, schema, security, or codegen failures.

3. Patch hunks linked to comments or failed checks  
   Include the changed lines around the failure, not the entire file.

4. Follow-up commits after feedback/failure  
   Commit messages and changed files can show what fixed the issue.

5. PR body only if it describes risk, bug, workaround, constraints, migration, or test plan  
   Many PR bodies are boilerplate.

6. Issue comments only when they contain review-like feedback  
   General PR comments are often status updates.

7. Timeline events only as metadata  
   `merged`, `closed`, `head_ref_deleted`, `ready_for_review`, `force_pushed` are usually not lessons by themselves.

Given your live ingestion counts, the most important immediate change is to add PR review data. Your current `issue_comment: 5` across 100 PRs is extremely sparse; line-level review comments are usually where the rich “do this differently next time” feedback lives.

### Exclude Or Downrank

Exclude from LLM processing unless directly linked to a failed check or review comment:

- `timeline_merged`, `timeline_closed`, `timeline_head_ref_deleted`,
- passing check runs,
- bot status comments with no failure detail,
- self-authored “done”, “fixed”, “pushed”, “addressed” updates,
- generated files,
- vendored code,
- minified files,
- lockfiles unless dependency/versioning is the lesson,
- snapshots unless snapshot update was the actual issue,
- huge diffs with no review/check signal,
- binary files,
- comments from known automation accounts except failed check annotations.

### Detect Review Feedback Vs Self-Status

Create a deterministic classifier before invoking Ollama.

A comment is likely review feedback when:

- `source_type in {review_comment, review_body}`
- `actor != pr_author`
- `author_association in {OWNER, MEMBER, COLLABORATOR}`
- text contains critique/directive/question about code behavior

Boost if:

- `review_state == CHANGES_REQUESTED`
- comment is on a diff line
- thread was later resolved
- later commit modified the same path/hunk

A comment is likely self-status when:

- `actor == pr_author`
- text matches: `done|fixed|addressed|pushed|updated|will do|working on it`
- it does not introduce a concrete technical failure/prevention rule

Self-status should be used as linking evidence, not primary learning evidence. Example: a reviewer says “This crashes on empty input,” and the author says “Fixed.” The reviewer comment is primary; the author comment confirms resolution.

### Chunking Strategy

#### PR Body

One chunk unless very long.

Include:

```json
{
  "kind": "pr_body",
  "title": "...",
  "body_sections": {
    "summary": "...",
    "risk": "...",
    "test_plan": "..."
  }
}
```

Strip:

- templates,
- checklist boilerplate,
- screenshots,
- unrelated release notes.

#### Review Comments

Chunk at review-thread level, not individual line only.

Each chunk should include:

```json
{
  "kind": "review_thread",
  "review_state": "CHANGES_REQUESTED",
  "actor_role": "reviewer",
  "file_path": "src/foo.py",
  "line": 123,
  "comment": "...",
  "thread_replies": ["..."],
  "linked_hunk_id": "...",
  "later_fix_commit_ids": ["..."]
}
```

#### Failed Check Annotations

Group by:

```text
check_run_name + path + failure_type
```

For example:

```json
{
  "kind": "check_failure_group",
  "check_name": "typecheck",
  "path": "src/foo.py",
  "annotations": [
    {"line": 42, "title": "...", "message": "..."}
  ],
  "linked_patch_hunks": ["..."]
}
```

Limit to the top N unique failure shapes per PR. For 28 annotations across 100 PRs, you can send all, but design for larger repos.

#### Commits

Do not send full commit diffs by default.

Send:

- commit message,
- author,
- timestamp,
- changed files,
- whether it occurred after review feedback/failure,
- whether it touched the same file/path.

Useful pattern:

```text
review comment at T1 -> commit at T2 touches same file -> patch hunk
```

#### File Patches

Use hunk-level chunks.

Include:

- file path,
- language,
- hunk header,
- only changed lines plus small context,
- nearby check/review references,
- deterministic summary of file size/change count.

Avoid whole-file patches unless the file is small.

Example chunk:

```json
{
  "kind": "patch_hunk",
  "file_path": "src/auth/session.py",
  "language": "python",
  "hunk_header": "@@ -41,7 +41,11 @@",
  "reason_selected": ["linked_to_review_comment", "touches_failed_check_path"],
  "diff": "..."
}
```

Priority order for patch hunks:

1. hunk line referenced by review comment,
2. hunk line referenced by failed check annotation,
3. hunk adding/removing tests,
4. hunk changing validation, error handling, auth, data migration, concurrency, retries, null handling,
5. hunk touching files changed again after feedback.

## 3. Learning Extraction Design

### What A Learning Card Should Be

A durable learning is not:

```text
Add tests.
```

A durable learning is:

```text
When changing validation for optional request fields, add regression tests for omitted, null, and empty-string inputs before opening the PR.
```

The extraction schema should force:

- trigger: when this situation appears,
- anti-pattern: what went wrong or was risky,
- prevention rule: what to do next time,
- evidence IDs: where this came from,
- scope: repo/language/file area,
- confidence: based on evidence quality, not model vibes.

### Candidate Schema

```json
{
  "candidate_id": "generated by prlearn, not model",
  "title": "string",
  "lesson_rule": "When <specific trigger>, do <specific prevention action> before <specific stage>.",
  "anti_pattern": "string",
  "prevention_checklist": ["string"],
  "why_it_matters": "string",
  "tags": ["validation", "testing", "typecheck", "api", "ci", "review-feedback"],
  "severity": "low|medium|high|critical",
  "confidence": 0.0,
  "specificity": 0.0,
  "learning_type": "bug_prevention|test_gap|review_feedback|ci_failure|architecture|security|performance|maintainability|process",
  "repo": "owner/name",
  "language": "python|typescript|go|unknown",
  "file_area": "string",
  "recurrence_key": "string",
  "evidence_ids": ["ev_..."],
  "evidence_quotes": [
    {
      "evidence_id": "ev_...",
      "quote": "short redacted quote"
    }
  ],
  "no_learning_reason": null
}
```

### Evidence Rules

A candidate is invalid unless:

- it has at least one primary evidence item:
  - non-author review feedback,
  - failed check annotation,
  - bug/regression label with patch,
  - test added after a failure,
  - code change clearly fixing the reviewed/failing condition;
- every evidence ID exists in SQLite;
- all evidence text was redacted before model input;
- the rule is actionable without opening the PR;
- the prevention action is concrete.

For high-quality cards, require two evidence types where possible:

- review comment + patch fix
- failed check + patch fix
- PR body risk + test addition
- comment + follow-up commit

### Confidence Scoring

Do not trust model confidence alone. Compute final confidence as:

```text
final_confidence =
  0.35 * evidence_strength
+ 0.25 * specificity_score
+ 0.20 * recurrence_score
+ 0.10 * model_confidence
+ 0.10 * human_feedback_prior
```

Evidence strength examples:

| Evidence | Strength |
| --- | --- |
| CHANGES_REQUESTED review comment + fix commit same file | 0.95 |
| failed check annotation + patch fixing same path | 0.90 |
| non-author review suggestion only | 0.75 |
| PR body risk + tests | 0.65 |
| self-authored note only | 0.25 |
| timeline event only | 0.05 |

### When The Model Should Return No Learning

The model should explicitly return `no_learning_found` when:

- evidence is only merge/close/delete timeline events,
- PR is routine and no feedback/failure/risk is present,
- all comments are status updates,
- change is purely mechanical,
- candidate would be generic,
- lesson cannot be supported by evidence,
- lesson is only “write better code,” “add tests,” or “run CI.”

This will prevent volume inflation.

## 4. Dedupe And Recurrence

### Use A Hybrid Dedupe System

Use all four:

1. Canonical keys
2. Embeddings
3. FTS5 lexical search
4. LLM merge judgment for borderline cases

Do not use LLM-only dedupe.

### Canonical Key

Generate deterministic keys like:

```text
<language>|<file_area>|<failure_mode>|<prevention_action>|<tool_or_test_type>
```

Examples:

```text
python|api-validation|null-empty-input|add-regression-tests|pytest
typescript|frontend-state|null-rendering|cover-empty-state|component-test
repo-process|ci|typecheck-failure|run-typecheck-before-pr|npm-run-typecheck
```

Normalize:

- lowercase,
- stem common verbs,
- collapse synonyms:
  - `null`, `none`, `nil` -> `null`
  - `empty array`, `empty list` -> `empty_collection`
  - `type check`, `typecheck`, `mypy`, `tsc` -> `typecheck`
- remove repo-specific nouns unless they define the file area.

### Embedding Text

Embed this canonical card text:

```text
Title: ...
Rule: ...
Trigger: ...
Anti-pattern: ...
Prevention: ...
Tags: ...
Language: ...
File area: ...
```

Do not embed long raw evidence by default for card dedupe. Evidence embeddings are useful for retrieval, but card dedupe should embed the normalized lesson.

### Thresholds

Start with tunable defaults:

```text
cosine >= 0.88     same lesson unless canonical conflict
0.78 to 0.88       ask LLM judge
0.68 to 0.78       possible cluster neighbor, not auto-merge
< 0.68             new lesson
```

These are starting points, not universal truths. Calibrate them on your fixtures and accepted/rejected history.

### Recurrence Update

If a candidate matches an accepted card:

- do not create a new card,
- append a recurrence row,
- attach new evidence,
- update `last_seen_at`,
- update `repos_seen`, `languages_seen`, `file_areas_seen`,
- increment recurrence count,
- optionally raise confidence if evidence is strong,
- optionally generalize the rule only after human review.

Example:

Existing:

```text
Cover null and empty states with regression tests.
```

New evidence:

```text
Reviewer found empty array state in Settings page.
```

Update:

- `recurrence_count += 1`
- add evidence
- add `file_area = frontend/settings`
- improve rule if accepted:

```text
When rendering data-driven UI states, cover null, empty collection, and loading states with regression tests.
```

### Avoid Duplicate Generic Cards

Reject candidates whose title/rule matches generic patterns unless they include a concrete trigger and prevention:

Bad:

- Add tests.
- Run typecheck.
- Handle edge cases.
- Improve validation.

Acceptable:

```text
When changing request validation for optional fields, add regression tests for omitted, null, and empty-string values.
```

## 5. SQLite Schema Changes

Keep accepted cards in `learning_cards`. Add a separate candidate/evidence/model layer.

### Core New Tables

```sql
CREATE TABLE evidence_items (
  id TEXT PRIMARY KEY,
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  source_table TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  actor_login TEXT,
  actor_role TEXT,
  author_association TEXT,
  created_at TEXT,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  commit_sha TEXT,
  file_path TEXT,
  line_start INTEGER,
  line_end INTEGER,
  raw_text_sha256 TEXT,
  redacted_text TEXT NOT NULL,
  redaction_policy_version TEXT NOT NULL,
  signal_score REAL NOT NULL DEFAULT 0,
  is_primary_signal INTEGER NOT NULL DEFAULT 0,
  is_self_authored INTEGER NOT NULL DEFAULT 0,
  is_bot INTEGER NOT NULL DEFAULT 0,
  is_noise INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_table, source_id, redaction_policy_version)
);

CREATE TABLE evidence_chunks (
  id TEXT PRIMARY KEY,
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  chunk_kind TEXT NOT NULL,
  chunk_hash TEXT NOT NULL,
  redacted_text TEXT NOT NULL,
  token_estimate INTEGER NOT NULL,
  repo TEXT NOT NULL,
  language TEXT,
  file_path TEXT,
  selected_reason_json TEXT NOT NULL DEFAULT '[]',
  source_evidence_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(chunk_hash)
);

CREATE TABLE model_runs (
  id TEXT PRIMARY KEY,
  run_type TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_digest TEXT,
  ollama_version TEXT,
  prompt_template_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  schema_hash TEXT,
  options_json TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  total_duration_ns INTEGER,
  load_duration_ns INTEGER,
  prompt_eval_count INTEGER,
  eval_count INTEGER,
  response_json TEXT,
  error TEXT,
  UNIQUE(run_type, model_name, prompt_template_id, prompt_version, input_hash, options_json)
);
```

Ollama API responses include useful runtime/token fields such as total duration, load duration, prompt token count, eval count, and eval duration; persist these for observability and cost/runtime tuning.

```sql
CREATE TABLE learning_candidates (
  id TEXT PRIMARY KEY,
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  model_run_id TEXT REFERENCES model_runs(id),
  status TEXT NOT NULL DEFAULT 'pending',
  title TEXT NOT NULL,
  lesson_rule TEXT NOT NULL,
  anti_pattern TEXT,
  prevention_checklist_json TEXT NOT NULL DEFAULT '[]',
  why_it_matters TEXT,
  learning_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  confidence REAL NOT NULL,
  specificity REAL NOT NULL,
  repo TEXT NOT NULL,
  language TEXT,
  file_area TEXT,
  recurrence_key TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]',
  nearest_card_id INTEGER REFERENCES learning_cards(id),
  dedupe_status TEXT,
  dedupe_score REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(pr_id, recurrence_key, title)
);

CREATE TABLE learning_candidate_evidence (
  candidate_id TEXT NOT NULL REFERENCES learning_candidates(id),
  evidence_item_id TEXT NOT NULL REFERENCES evidence_items(id),
  role TEXT NOT NULL,
  quote TEXT,
  PRIMARY KEY(candidate_id, evidence_item_id)
);

CREATE TABLE learning_card_recurrences (
  id TEXT PRIMARY KEY,
  card_id INTEGER NOT NULL REFERENCES learning_cards(id),
  candidate_id TEXT REFERENCES learning_candidates(id),
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  repo TEXT NOT NULL,
  language TEXT,
  file_area TEXT,
  recurrence_key TEXT NOT NULL,
  evidence_summary TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(card_id, pr_id, recurrence_key)
);

CREATE TABLE learning_card_feedback (
  id TEXT PRIMARY KEY,
  card_id INTEGER REFERENCES learning_cards(id),
  candidate_id TEXT REFERENCES learning_candidates(id),
  action TEXT NOT NULL,
  reason_code TEXT,
  user_note TEXT,
  created_at TEXT NOT NULL
);
```

### Embedding Tables

Start with simple BLOB storage plus optional `sqlite-vec`.

```sql
CREATE TABLE embeddings (
  id TEXT PRIMARY KEY,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_digest TEXT,
  dimensions INTEGER NOT NULL,
  vector_blob BLOB NOT NULL,
  text_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(owner_type, owner_id, model_name, dimensions, text_hash)
);
```

Add `sqlite-vec` virtual tables when available:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS vec_learning_cards USING vec0(
  embedding_id TEXT PRIMARY KEY,
  card_id INTEGER,
  repo TEXT,
  language TEXT,
  recurrence_key TEXT,
  embedding float[1024]
);
```

Prefer `sqlite-vec` over `sqlite-vss` for the default path because `sqlite-vec` is a pure-C SQLite extension with no dependencies and supports metadata/auxiliary/partition columns, while `sqlite-vss` is Faiss-based and therefore more dependency-heavy.

Also add FTS5 over accepted cards and candidates. SQLite FTS5 external-content tables can avoid duplicating content, but you must keep the FTS index consistent with triggers or rebuilds.

### Migration Design

- Keep migrations additive.
- Never rewrite existing `learning_cards` destructively.
- Backfill evidence/chunks from existing raw JSON.
- Introduce `raw_json_redacted` before using raw JSON in prompts.
- Add `redaction_policy_version`.
- Add `model_run` records even on failure.
- Add unique constraints for idempotency.
- Support fallback when `sqlite-vec` is unavailable:
  - use BLOB embeddings,
  - do brute-force cosine in Python for small card counts,
  - warn in doctor.

## 6. Daily V2 Pipeline

Current:

```text
doctor -> sync --incremental -> extract -> dedupe -> export -> report
```

Recommended:

```text
doctor
  -> sync --incremental
  -> ingest reviews/review-comments
  -> select evidence
  -> redact
  -> chunk
  -> embed chunks/cards
  -> retrieve similar accepted cards
  -> classify chunks
  -> extract candidates
  -> validate candidates
  -> dedupe / recurrence update
  -> score
  -> human review queue
  -> export accepted cards
  -> report
```

### Idempotency

Every stage should use stable IDs.

Examples:

```text
evidence_item.id =
  sha256(repo, pr_number, source_table, source_id, redaction_policy_version)

chunk.id =
  sha256(pr_id, chunk_kind, normalized_redacted_text, source_evidence_ids)

model_run.id =
  sha256(run_type, model_name, model_digest, prompt_version, schema_hash, input_hash, options_json)

candidate.id =
  sha256(pr_id, model_run_id, recurrence_key, normalized_lesson_rule)
```

On rerun, reuse successful `model_runs` unless:

- prompt version changed,
- model changed,
- model digest changed,
- redaction policy changed,
- input chunks changed,
- schema changed.

### Failure Handling

| Failure | Behavior |
| --- | --- |
| Ollama unavailable | Fall back to heuristic extraction; mark LLM jobs queued. |
| Model missing | Doctor reports `ollama pull ...`; extraction skips LLM. |
| Invalid JSON | Retry once with same input and repair instruction; then mark failed. |
| Schema validation failure | Store response, status `invalid_schema`, do not create candidate. |
| Timeout | Mark run `timeout`, retry with smaller chunk bundle. |
| 503 / overloaded | Exponential backoff; honor max retries; keep queue idempotent. |
| Context overflow | Rechunk smaller; do not allow Ollama truncation silently. |
| Secret redaction hit | Replace secret with stable placeholder or skip tainted chunk. |
| Duplicate candidate | Convert to recurrence or reject as duplicate. |

For embeddings, set `truncate=false` where possible so oversized chunks error instead of silently losing evidence. Ollama’s embedding API exposes truncate, dimensions, batch input, and prompt_eval_count, which are useful for this.

### Stale Locks

Add:

```sql
CREATE TABLE run_locks (
  name TEXT PRIMARY KEY,
  owner_pid INTEGER,
  owner_host TEXT,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
```

Rules:

- One writer lock for daily pipeline.
- Heartbeat every stage.
- If `expires_at` passed and PID is dead or host differs, take lock.
- Never hold lock during long model inference if the stage can be safely resumed by idempotent job records.
- Use SQLite transactions for state transitions, not for entire extraction runs.

### Caching

Cache:

- redacted evidence text,
- chunks,
- embeddings,
- PR summaries,
- chunk classifications,
- extraction outputs,
- dedupe judgments.

Do not cache unredacted prompts.

## 7. Prompt Templates

Below are compact prompt templates. Use each with a JSON schema in Ollama’s `format` parameter and the same schema embedded in the prompt text.

### 7.1 PR-Level Summarization

Purpose: summarize the PR for downstream extraction without inventing lessons.

```text
SYSTEM:
You summarize GitHub pull requests for a local learning extraction tool.
You must only use the provided redacted evidence.
Do not infer facts not present in the evidence.
Do not produce learning cards.
Return JSON only.

USER:
PR:
{{ pr_metadata_json }}

Evidence chunks:
{{ evidence_chunks_json }}

Task:
Create a compact factual summary that helps later extraction.

Rules:
- Include only facts supported by evidence IDs.
- Mark routine/noisy evidence.
- Identify risk areas and changed behavior.
- Do not mention secrets, raw tokens, or unredacted values.
- If the PR contains no meaningful learning signal, say so.

Return JSON matching this schema:
{
  "pr_summary": "string",
  "changed_behavior": ["string"],
  "risk_areas": ["string"],
  "test_or_check_outcomes": ["string"],
  "review_feedback_summary": ["string"],
  "important_evidence_ids": ["string"],
  "noise_evidence_ids": ["string"],
  "has_learning_signal": true,
  "no_learning_reason": "string|null"
}
```

### 7.2 Evidence Chunk Classification

```text
SYSTEM:
You classify redacted GitHub PR evidence for learning extraction.
You are conservative. A chunk is useful only if it can support a durable coding lesson.
Return JSON only.

USER:
Evidence chunk:
{{ chunk_json }}

Classify the chunk.

Definitions:
- review_feedback: non-author feedback about code behavior, design, correctness, testing, maintainability, security, or performance.
- self_status: author says they fixed/addressed/pushed something.
- ci_failure: failed check, failed test, typecheck, lint, build, security, migration, or codegen failure.
- patch_fix: code hunk likely fixing a failure or review point.
- noise: merge/close/delete/ready events, generic status, boilerplate, passing checks.

Return JSON:
{
  "chunk_id": "string",
  "primary_class": "review_feedback|self_status|ci_failure|patch_fix|pr_context|noise",
  "learning_signal": "none|weak|medium|strong",
  "is_external_feedback": true,
  "is_self_authored_status": false,
  "is_bot_noise": false,
  "technical_topics": ["string"],
  "file_area": "string|null",
  "language": "string|null",
  "reason": "string"
}
```

### 7.3 Candidate Learning Extraction

```text
SYSTEM:
You extract durable personal coding lessons from redacted GitHub PR evidence.
You must be evidence-backed, specific, and conservative.
Return JSON only.

Hard rules:
- Use only provided evidence.
- Every learning must cite evidence_ids from the input.
- Do not create generic lessons such as "add tests", "run CI", "handle edge cases", or "write clearer code".
- A lesson must include a specific trigger and a specific prevention action.
- If evidence is weak or only status/noise, return no_learning_found.
- Do not include secrets, tokens, credentials, private URLs, or raw logs.
- Do not output code unless the evidence itself requires a tiny redacted snippet.

USER:
PR summary:
{{ pr_summary_json }}

Evidence bundle:
{{ evidence_bundle_json }}

Similar accepted cards:
{{ similar_cards_json }}

Task:
Extract zero or more candidate learning cards.

A good lesson has this shape:
"When <specific situation>, do <specific prevention action> before <stage>, because <risk>."

Reject:
- lessons already covered by similar accepted cards unless this is recurrence evidence,
- vague lessons,
- lessons without evidence,
- one-off mechanical changes,
- self-authored status-only lessons.

Return JSON:
{
  "no_learning_found": false,
  "no_learning_reason": "string|null",
  "candidates": [
    {
      "title": "string",
      "lesson_rule": "string",
      "anti_pattern": "string",
      "prevention_checklist": ["string"],
      "why_it_matters": "string",
      "learning_type": "bug_prevention|test_gap|review_feedback|ci_failure|architecture|security|performance|maintainability|process",
      "severity": "low|medium|high|critical",
      "model_confidence": 0.0,
      "specificity": 0.0,
      "tags": ["string"],
      "repo": "string",
      "language": "string|null",
      "file_area": "string|null",
      "recurrence_key": "string",
      "evidence_ids": ["string"],
      "evidence_quotes": [
        {"evidence_id": "string", "quote": "short redacted quote"}
      ],
      "possible_duplicate_card_ids": ["string"]
    }
  ]
}
```

### 7.4 Dedupe / Merge Decision

```text
SYSTEM:
You decide whether a candidate learning is the same durable lesson as an existing accepted card.
You are conservative about creating duplicates.
Return JSON only.

USER:
Candidate:
{{ candidate_json }}

Existing accepted card:
{{ existing_card_json }}

Evidence for candidate:
{{ candidate_evidence_json }}

Decision rules:
- SAME: same trigger, failure mode, and prevention action.
- RECURRENCE: same lesson, new evidence instance should update existing card.
- MERGE: overlapping lessons where existing card should be generalized slightly.
- NEW: meaningfully different trigger or prevention action.
- REJECT_DUPLICATE_GENERIC: candidate is vague or generic version of existing card.

Return JSON:
{
  "decision": "same|recurrence|merge|new|reject_duplicate_generic",
  "confidence": 0.0,
  "reason": "string",
  "fields_to_update": {
    "title": "string|null",
    "lesson_rule": "string|null",
    "tags_to_add": ["string"],
    "file_areas_to_add": ["string"]
  },
  "recurrence_summary": "string|null"
}
```

### 7.5 Recurrence Update

```text
SYSTEM:
You update an accepted learning card with new recurrence evidence.
Do not rewrite the card unless the new evidence clearly broadens or sharpens it.
Return JSON only.

USER:
Accepted card:
{{ card_json }}

New candidate:
{{ candidate_json }}

New evidence:
{{ evidence_json }}

Task:
Produce a recurrence update.

Return JSON:
{
  "append_recurrence": true,
  "recurrence_summary": "string",
  "new_evidence_ids": ["string"],
  "should_update_card_text": false,
  "proposed_title": "string|null",
  "proposed_lesson_rule": "string|null",
  "tags_to_add": ["string"],
  "confidence_delta": 0.0,
  "reason": "string"
}
```

### 7.6 Preflight Retrieval/Ranking

For `prlearn preflight`, rank accepted cards against current local diff or staged changes.

```text
SYSTEM:
You rank existing learning cards by relevance to a new proposed code change.
You do not create new lessons.
Return JSON only.

USER:
Current change summary:
{{ change_summary_json }}

Retrieved accepted cards:
{{ retrieved_cards_json }}

Task:
Rank cards that the developer should consider before opening a PR.

Ranking rules:
- Prefer cards matching language, file area, failure mode, or prevention action.
- Penalize generic cards.
- Include a concrete reminder action.
- If no cards are relevant, return an empty list.

Return JSON:
{
  "ranked_cards": [
    {
      "card_id": "string",
      "rank": 1,
      "relevance": 0.0,
      "reason": "string",
      "reminder": "string"
    }
  ]
}
```

### 7.7 Reject Noisy Candidate Classifier

```text
SYSTEM:
You classify whether a candidate learning should be rejected as noisy.
You are strict. Prefer rejection when evidence is weak.
Return JSON only.

USER:
Candidate:
{{ candidate_json }}

Evidence:
{{ evidence_json }}

Similar accepted cards:
{{ similar_cards_json }}

Reject if:
- generic,
- unsupported by evidence,
- only self-status,
- only timeline noise,
- duplicate of an accepted card,
- contains secrets or unredacted sensitive data,
- not actionable,
- not durable.

Return JSON:
{
  "reject": true,
  "reason_code": "generic|unsupported|self_status_only|timeline_noise|duplicate|secret_risk|not_actionable|not_durable|other",
  "reason": "string",
  "suggested_fix": "string|null"
}
```

## 8. Evaluation Strategy

### Measure Quality, Not Only Volume

Track:

| Metric | Meaning |
| --- | --- |
| Candidate yield per PR | Are you finding more than heuristic extraction? |
| Human acceptance rate | Main precision metric. |
| Duplicate rate | How many accepted/reviewed candidates are duplicates? |
| Evidence coverage | Percent of candidates with valid evidence IDs. |
| Specificity score | How often cards contain trigger + prevention + context. |
| Generic rejection rate | Whether “add tests”/“run CI” is being blocked. |
| Recurrence merge accuracy | Whether repeated lessons update existing cards. |
| Review edit distance | How much humans rewrite accepted cards. |
| Runtime per PR | Practical daily usability. |
| Redaction hit rate | Security signal. |
| No-learning accuracy | Whether routine PRs correctly produce nothing. |

### Fixtures To Add

Create deterministic fixture PRs with synthetic JSON and patches:

- Review feedback -> edge-case validation  
  Reviewer: “What happens when items is empty?” Patch adds empty-state handling and test. Expected: specific empty-state lesson.
- CI failure -> typecheck  
  Failed annotation from mypy or tsc. Patch fixes type. Expected: typecheck preflight lesson or recurrence.
- Self-status only  
  Author says “fixed, thanks.” No reviewer/failure. Expected: no learning.
- Generic test addition  
  Patch adds tests but no failure/risk. Expected: no generic “add tests” card.
- Review comment + follow-up commit  
  Reviewer identifies issue. Later commit changes same file. Expected: evidence chain.
- Large patch with one relevant hunk  
  Many files, one check annotation. Expected: only relevant hunk sent.
- Duplicate recurrence  
  Same null/empty-state lesson appears in another repo. Expected: recurrence update, not new card.
- Secret redaction  
  Fake token/API key in comment or patch. Expected: redacted before storage/model/export.
- Bot noise  
  “CI passed”, “deployed preview.” Expected: noise.
- Security-sensitive evidence  
  Reviewer flags auth bypass. Expected: high-severity security lesson with redacted evidence.

### Golden Tests

Add tests for:

- stable evidence IDs,
- stable chunk IDs,
- redaction before persistence,
- no unredacted secrets in `model_runs`,
- schema validation,
- invalid JSON retry behavior,
- model output cache hit,
- duplicate recurrence behavior,
- accepted/rejected review actions,
- export excludes rejected candidates,
- export includes accepted evidence references,
- doctor detects missing Ollama/model/sqlite-vec.

Mock Ollama in tests. Store fixture model responses as JSON. Do not depend on live model behavior for unit tests.

### Comparing Deterministic Vs Ollama Extraction

Run both on the same fixture set and recent PR sample:

- `heuristic_only`
- `ollama_only`
- `hybrid = heuristic + ollama + dedupe`

Compare:

- accepted cards,
- unique durable lessons,
- duplicate count,
- rejected generic count,
- missed-gold lessons,
- runtime,
- human review burden.

The goal is not “many more cards.” The goal is:

```text
more accepted, evidence-backed, non-duplicative cards per reviewed PR
```

### Human Feedback Loop

Every accept, reject, archive, and merge should write feedback rows.

Use feedback to:

- tune prompt examples,
- tune generic rejection,
- tune dedupe thresholds,
- improve recurrence keys,
- identify noisy repos/checks/bots,
- create new fixtures.

Do not fine-tune models in MVP. Use human feedback for deterministic policy and prompt calibration first.

## 9. Safety And Privacy

### Local-Only Controls

`doctor` should verify:

- Ollama host is `127.0.0.1` or `localhost`,
- `OLLAMA_NO_CLOUD=1` or `~/.ollama/server.json` disables cloud where user requests strict local-only mode,
- no remote `OLLAMA_HOST`,
- no cloud model tags selected,
- prompts are redacted before model calls,
- exports are redacted.

Ollama documents local operation, a way to disable cloud features, and default localhost binding.

### Redaction Before LLM Processing

Redact before:

- storing new raw JSON,
- creating evidence items,
- creating chunks,
- embedding,
- prompting,
- storing model runs,
- exporting.

Redact:

- GitHub tokens,
- API keys,
- OAuth secrets,
- private keys,
- JWTs,
- bearer tokens,
- basic auth headers,
- database URLs with credentials,
- `.env` assignments,
- cloud credentials,
- webhook secrets,
- SSH keys,
- PEM blocks,
- session cookies,
- internal hostnames if configured,
- email addresses if configured,
- customer/user identifiers if configured.

GitHub’s secret-scanning docs describe detection of hardcoded credentials such as API keys, passwords, and tokens, plus support for custom patterns and non-provider patterns; use those pattern classes as a baseline, then add local custom patterns for the user’s org/repo conventions.

Use stable placeholders:

```text
<github-token> -> [REDACTED_GITHUB_TOKEN:sha256_8]
<database-url-with-credentials> -> [REDACTED_DATABASE_URL:sha256_8]
<private-key-block> -> [REDACTED_PRIVATE_KEY:sha256_8]
```

Stable placeholders let recurrence/dedupe work without exposing values.

### What Should Never Be Sent To A Model, Even Local

Even local models should be treated as untrusted text processors. Never send:

- unredacted secrets,
- private keys,
- auth headers,
- full environment files,
- full CI logs with secrets,
- core dumps,
- production data,
- customer PII,
- binary artifacts,
- credentials embedded in URLs,
- unrelated proprietary documents,
- entire huge diffs when a small hunk is enough.

Also scan model outputs. Local models can echo input text; output redaction is mandatory.

### Raw JSON Policy

Your current system stores raw JSON for reprocessing. For v2 privacy, change the default:

- default: store redacted raw JSON
- optional: store unredacted raw JSON only with explicit config

Suggested config:

```toml
[privacy]
store_unredacted_raw_json = false
redact_before_persist = true
redact_before_model = true
redact_model_outputs = true
export_evidence_quotes = true
max_evidence_quote_chars = 240
```

Add a one-time command:

```bash
prlearn scrub --redact-existing-raw
```

## 10. Implementation Roadmap

### Stage 1: MVP Local Ollama Extraction

Deliver:

- `ollama_client.py`
- model config in `~/.prlearn/config.toml`
- doctor Ollama checks
- redaction module
- evidence selection from existing tables
- chunk table
- model run table
- structured candidate extraction
- candidate review queue
- fallback to heuristic extraction

Also add ingestion for:

- PR reviews,
- PR review comments,
- review state,
- author association,
- line/path metadata.

This stage alone should substantially improve yield because it introduces the highest-signal PR data.

### Stage 2: Embeddings And Dedupe

Deliver:

- embedding generation via Ollama `/api/embed`,
- card and candidate embeddings,
- BLOB fallback,
- optional `sqlite-vec`,
- FTS5 accepted-card index,
- canonical recurrence keys,
- hybrid duplicate detection,
- recurrence updates.

### Stage 3: Richer Scoring And Review UI

Deliver:

- confidence scoring,
- specificity scoring,
- evidence-strength scoring,
- reason codes for rejection,
- review UI filters:
  - `--repo`,
  - `--tag`,
  - `--severity`,
  - `--confidence`,
  - `--duplicates`,
  - `--recurrences`,
- editable acceptance:
  - `accept --edit`,
  - `merge --into CARD_ID`,
  - `reject --reason generic`.

### Stage 4: Full Daily Automation

Deliver:

- job queue,
- locks,
- retries,
- stale lock recovery,
- model-run cache,
- daily report,
- export accepted cards only,
- pending candidate summary,
- runtime stats.

Daily report should include:

- PRs synced
- evidence items selected
- chunks embedded
- model calls reused from cache
- new candidates
- rejected noisy candidates
- duplicates merged
- recurrences added
- accepted cards pending review
- runtime by stage
- redaction hits

### Stage 5: Evaluation Harness

Deliver:

- fixture PR generator,
- golden expected cards,
- mock Ollama responses,
- deterministic tests,
- quality metrics,
- comparison report:
  - heuristic vs Ollama vs hybrid,
- regression suite for prompt/model changes.

## 11. Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Hallucinated lessons | Require evidence IDs, validate IDs, reject unsupported candidates. |
| Generic card explosion | Prompt constraints + deterministic generic-pattern rejection. |
| Duplicate recurring cards | Canonical keys + embeddings + recurrence table + LLM judge only for borderline cases. |
| Too much model input | Evidence selection, chunking, linked hunks, no whole-PR dumping. |
| Slow daily runtime | Small classifier, cached model runs, embeddings cache, only recent PRs, deep mode optional. |
| Model drift | Store model name, digest, prompt version, schema hash, options, and evaluation snapshots. |
| Secrets leakage | Redact before persist/model/export; scan outputs; taint/skip unsafe chunks. |
| Missing learnings | Add PR review comments/reviews; evaluate recall against golden fixtures. |
| Overconfidence | Treat model confidence as one weak feature; compute final confidence deterministically. |
| sqlite vector dependency friction | BLOB brute-force fallback; optional sqlite-vec. |
| Context overflow | Explicit num_ctx, token estimates, truncate=false, rechunk on error. |

## Final Recommended Design

For `prlearn`, the best v2 is:

- local SQLite remains source of truth
- Ollama proposes structured candidates
- deterministic validators enforce evidence, redaction, specificity, and dedupe
- human review remains mandatory before memory export

Default stack:

```text
extractor:   qwen3.5:9b
classifier:  qwen3.5:2b
embedder:    qwen3-embedding:0.6b
judge:       qwen3.5:9b
vector:      sqlite-vec optional, BLOB fallback
output:      accepted cards only, with redacted evidence
```

The first practical fix is not the model: it is ingesting PR reviews and review comments. After that, Ollama should be used as an evidence-grounded extractor, not a general summarizer over raw PR data.
