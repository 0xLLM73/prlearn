# Ollama Implementation Plan

Status: design and implementation planning reference. The README is the current user guide. Ollama remains optional; the deterministic heuristic path is the default local-use path until real-history evaluation proves the Ollama/hybrid workflow should become a default.

This plan implements Ollama for `prlearn` as an optional, local-first, evidence-grounded extraction engine. The current heuristic extractor remains available and must continue to pass the original fixture workflow.

## Guiding Decisions

1. Ollama is a v2 engine, not a replacement for the existing MVP.
2. Default behavior stays safe: `heuristic` remains the fallback, and `hybrid` can become default only after fixture and live validation.
3. The model proposes candidates. SQLite validation, evidence IDs, dedupe rules, and human review decide what becomes memory.
4. No raw whole-PR dumping into Ollama. Only redacted, selected, typed evidence chunks are sent.
5. Runtime dependencies stay modest. Use standard-library HTTP for Ollama first; add optional vector extensions later.

## Current State

Implemented already:

- Local CLI with SQLite storage.
- GitHub sync through `gh`.
- Fixture sync and validation.
- Heuristic extraction in `prlearn/learn.py`.
- Exact canonical-key dedupe.
- Daily sync/extract/dedupe/export/report loop.
- Ingestion v2 for PR body, labels, mergeability, review decision, issue comments, reviews, review comments, commits, check runs, check annotations, timeline events, files, and file patches.

Known gaps before Ollama:

- Review data is flattened into `events`; it is not promoted into typed evidence rows.
- No author-association, bot/self-status, primary-signal, or noise flags.
- No redacted evidence/chunk layer.
- No model-run cache.
- No learning-candidate table separate from accepted cards.
- Dedupe is exact-key only.
- Export currently includes all cards, including pending/rejected, which is too loose once LLM candidates exist.
- Current `recurrence_count` is evidence-count, not strictly PR-count.

## Phase 0: Safety Baseline And Engine Switch

Goal: make room for Ollama without disturbing the current working CLI.

Files:

- `prlearn/cli.py`
- `prlearn/learn.py`
- `README.md`
- `tests/test_prlearn.py`
- `tests/test_prlearn_unittest.py`

Tasks:

1. Add extraction engine options:
   - `prlearn extract --engine heuristic|ollama|hybrid`
   - `prlearn daily --engine heuristic|ollama|hybrid`
2. Keep `heuristic` as the default initially.
3. Make `hybrid` run heuristic plus Ollama candidates once the Ollama path exists.
4. Make `ollama` strict: if Ollama is unavailable, return a clear non-zero error.
5. Make `hybrid` non-strict: if Ollama is unavailable, fall back to heuristic and report the skipped stage.
6. Update README with engine behavior.

Validation:

- Existing fixture daily loop still passes unchanged.
- `.venv/bin/python -m pytest -q` passes.
- `prlearn daily --fixture ... --engine heuristic` matches current output.
- `prlearn daily --fixture ... --engine hybrid` falls back cleanly when Ollama is unavailable.

## Phase 1: Config And Ollama Client

Goal: add local Ollama connectivity and doctor checks without requiring a model for normal CLI use.

Files:

- Add `prlearn/config.py`
- Add `prlearn/ollama_client.py`
- Update `prlearn/doctor.py`
- Update `prlearn/db.py`
- Update `README.md`

Config decision:

- Keep `~/.prlearn/config.json` as the source of truth.
- Do not introduce `config.toml` yet. Python can read TOML in 3.11, but writing TOML adds friction.
- Add an `ollama` section to `config.json`.

Default config:

```json
{
  "ollama": {
    "base_url": "http://127.0.0.1:11434",
    "require_localhost": true,
    "strict_local": true,
    "models": {
      "embedder": "qwen3-embedding:0.6b",
      "classifier": "qwen3.5:2b",
      "extractor": "qwen3.5:9b",
      "judge": "qwen3.5:9b"
    },
    "options": {
      "temperature": 0,
      "seed": 42,
      "top_p": 0.1,
      "num_ctx": 8192,
      "stream": false,
      "think": false
    }
  }
}
```

Ollama client features:

1. `GET /api/version`
2. `GET /api/tags`
3. `POST /api/chat` with JSON schema output
4. `POST /api/embed`
5. timeout handling
6. localhost-only guard
7. structured error types:
   - unavailable
   - remote-host-blocked
   - model-missing
   - invalid-json
   - timeout

Doctor checks:

- Ollama reachable.
- Base URL is localhost if `require_localhost` is true.
- Required models installed or missing.
- Show suggested `ollama pull ...` commands.
- Do not fail `doctor` overall unless strict Ollama mode is requested.

Validation:

- Unit tests mock Ollama HTTP responses.
- Doctor reports missing Ollama as warning.
- Doctor detects remote `OLLAMA_HOST` or non-local base URL.
- No live Ollama required in CI/tests.

## Phase 2: Additive Schema Migration

Goal: add the evidence/model/candidate layer without rewriting current cards.

Files:

- `prlearn/db.py`
- Add migration tests.

Add schema version 2:

- `evidence_items`
- `evidence_chunks`
- `model_runs`
- `learning_candidates`
- `learning_candidate_evidence`
- `learning_card_recurrences`
- `learning_card_feedback`
- `embeddings`

Implementation notes:

1. Keep `learning_cards` as the accepted-card export surface.
2. Use text primary keys for new deterministic IDs.
3. Store model-run success and failure.
4. Store redacted model input/output only.
5. Use unique constraints to make reruns idempotent.
6. Keep vector storage as BLOB fallback in v1 of the Ollama implementation.
7. Add optional `sqlite-vec` later after embedding dimensions are confirmed per model.

Important mapping decisions:

- Map LLM severity strings to current integer severity:
  - `low` -> 1
  - `medium` -> 3
  - `high` -> 4
  - `critical` -> 5
- Store model confidence separately from final deterministic confidence.
- Keep recurrence details in `learning_card_recurrences`; do not overload `learning_cards.contexts_json`.

Validation:

- Fresh DB migrates to version 2.
- Existing version 1 DB migrates without losing cards/evidence.
- Re-running migration is idempotent.
- Required unique constraints prevent duplicate evidence/chunk/model-run rows.

## Phase 3: Redaction, Evidence Selection, And Chunking

Goal: create a safe evidence layer before any model call.

Files:

- Add `prlearn/redaction.py`
- Add `prlearn/evidence.py`
- Add `prlearn/chunking.py`
- Update `prlearn/sync.py` if metadata promotion is needed.
- Update tests and fixtures.

Redaction:

- Expand current token redaction from `prlearn/util.py`.
- Redact before evidence chunks and before model-run storage.
- Redact model outputs too.
- Use stable placeholders with short hashes.

Redact:

- GitHub tokens and personal access tokens
- API keys
- bearer/basic auth headers
- private keys and PEM blocks
- JWTs
- database URLs with credentials
- `.env` assignments
- webhook secrets
- session cookies

Evidence selection:

Create deterministic `evidence_items` from existing `events`, `pr_files`, `check_runs`, and `prs`.

Primary evidence candidates:

- non-author review comments
- review bodies with changes requested
- issue comments that look like review feedback
- failed check annotations
- failed check runs with useful output
- patch hunks linked to failed checks or feedback

Context-only evidence:

- PR body
- timeline events
- passing checks
- commits
- file patches not linked to a failure or review

Flags:

- `is_primary_signal`
- `is_self_authored`
- `is_bot`
- `is_noise`
- `signal_score`
- `actor_role`

Chunking:

- Review thread chunks.
- Failed check group chunks.
- Patch hunk chunks.
- PR context chunks only when they include risk/test-plan language.

Validation:

- Stable evidence IDs.
- Stable chunk IDs.
- Secret fixtures are redacted before chunk/model-run storage.
- Self-status-only fixture produces no primary evidence.
- Bot noise fixture produces no primary evidence.
- Large patch fixture selects only relevant hunks.

## Phase 4: Structured Ollama Extraction

Goal: generate evidence-backed candidates into `learning_candidates`, not directly into accepted memory.

Files:

- Add `prlearn/prompts.py`
- Add `prlearn/schemas.py`
- Add `prlearn/extractors/heuristic.py`
- Add `prlearn/extractors/ollama.py`
- Add `prlearn/extractors/hybrid.py`
- Refactor `prlearn/learn.py` to orchestrate engines or keep backward-compatible wrappers.

Prompt templates:

1. Evidence chunk classification
2. Candidate learning extraction
3. Noisy candidate rejection

Defer these until later unless needed:

1. PR-level summarization
2. Dedupe/merge judge
3. Recurrence update
4. Preflight ranking

Candidate validation:

Reject model output if:

- invalid JSON
- schema missing required fields
- evidence IDs do not exist
- candidate has no primary evidence
- evidence quote is not found in redacted source
- title/rule is generic
- prevention rule lacks trigger and concrete action
- output contains unredacted secret pattern

Model-run caching:

`model_run.id = sha256(run_type, model_name, prompt_version, schema_hash, input_hash, options_json)`

If a successful model run exists, reuse it.

Failure behavior:

- `hybrid`: fall back to heuristic and report skipped Ollama stage.
- `ollama`: fail clearly.
- Store failed `model_runs` with error and status.

Validation:

- Mock Ollama returns one valid candidate.
- Mock Ollama returns `no_learning_found`.
- Invalid JSON retries once, then stores failed model run.
- Cached model run avoids a second model call.
- Candidate with fake/nonexistent evidence ID is rejected.
- Generic “add tests” candidate is rejected.
- Secret echo in model output is redacted or rejected.

## Phase 5: Hybrid Dedupe And Recurrence

Goal: prevent LLM candidate explosion and update existing cards with recurrence evidence.

Files:

- Add `prlearn/dedupe.py`
- Add `prlearn/embeddings.py`
- Update `prlearn/learn.py`
- Update tests.

Order of operations:

1. Deterministic canonical recurrence key.
2. Existing rejected-card check.
3. Exact key match.
4. FTS5 lexical candidate search.
5. Embedding cosine similarity from BLOB vectors.
6. Optional Ollama judge for borderline cases.

Initial thresholds:

- `>= 0.88`: same lesson or recurrence
- `0.78 - 0.88`: judge if enabled, otherwise pending duplicate review
- `< 0.78`: new candidate

Recurrence behavior:

- If candidate matches a card, do not create a duplicate card.
- Insert `learning_card_recurrences`.
- Attach new evidence.
- Update `learning_cards.recurrence_count`.
- Update `last_seen_at`.
- Only generalize card text after review or explicit merge.

Important correction:

- Decide recurrence count semantics before implementation.
- Recommended: count unique recurrence rows by PR/key, not raw evidence rows.
- Existing tests that expect evidence-count recurrence should be updated intentionally only after the recurrence table lands.

Validation:

- Duplicate null/empty-state candidate updates the existing card.
- Rejected card is not recreated.
- Same PR with multiple evidence items creates one recurrence.
- Borderline duplicate can stay pending for human review.

## Phase 6: Review, Export, And Preflight Changes

Goal: keep generated candidates out of durable memory until reviewed.

Files:

- `prlearn/cli.py`
- `prlearn/export.py`
- `prlearn/preflight.py`
- `prlearn/report.py`

Review:

- Extend `prlearn review` to include `learning_candidates`.
- Add filters:
  - `--repo`
  - `--tag`
  - `--severity`
  - `--confidence`
  - `--duplicates`
  - `--recurrences`
- Add non-interactive:
  - `accept-candidate ID`
  - `reject-candidate ID --reason`
  - `merge-candidate ID CARD_ID`

Export:

- Once Ollama candidates exist, exports should default to accepted cards only.
- Pending candidates can appear in reports, not rules exports.
- Add `--include-pending` only as an explicit option.

Preflight:

- Keep lexical scoring initially.
- Add embedding retrieval once accepted-card embeddings are stable.
- Use accepted cards by default.
- Include pending only with `--include-pending`.

Report:

Add daily stats:

- evidence items selected
- chunks created
- model runs created
- model runs reused
- candidates created
- candidates rejected by validation
- recurrences added
- pending candidate count
- redaction hits
- Ollama skipped/fallback status

Validation:

- Rejected candidates do not export.
- Pending LLM candidates do not appear in `rules.md` by default.
- Accepting a candidate creates or updates a learning card.
- Daily report reflects model-run cache and candidate counts.

## Phase 7: Embeddings And Optional Vector Search

Goal: improve dedupe and preflight without making native vector extensions mandatory.

Files:

- `prlearn/embeddings.py`
- `prlearn/preflight.py`
- `prlearn/dedupe.py`
- `prlearn/doctor.py`

MVP embedding path:

- Use Ollama `/api/embed`.
- Store BLOB vectors in `embeddings`.
- Brute-force cosine in Python for small card counts.
- Include model name, digest if available, dimensions, text hash.

Optional path:

- Detect `sqlite-vec`.
- Use a model-aware dimension, not hardcoded `float[1024]`.
- Keep BLOB fallback as the default portability path.

Validation:

- Embedding cache hit avoids repeated calls.
- Dimension mismatch is detected.
- Missing embedder model produces doctor warning.
- Dedupe can work without sqlite-vec.

## Phase 8: Evaluation Harness

Goal: measure quality, not just card volume.

Files:

- Add `tests/fixtures/ollama_*.json`
- Add `tests/fixtures/ollama_responses/*.json`
- Add `prlearn/eval.py` or CLI command `prlearn eval`
- Add tests.

Fixtures:

- Review feedback -> edge-case validation
- CI failure -> typecheck
- Self-status only
- Generic test addition
- Review comment + follow-up commit
- Large patch with one relevant hunk
- Duplicate recurrence
- Secret redaction
- Bot noise
- Security-sensitive review

Metrics:

- candidate yield per PR
- human acceptance rate
- duplicate rate
- evidence coverage
- specificity score
- generic rejection rate
- recurrence merge accuracy
- runtime per PR
- redaction hit rate
- no-learning accuracy

Validation:

- Mock Ollama only. No live model dependency in tests.
- Golden fixtures compare heuristic vs Ollama vs hybrid.
- Prompt/schema changes run through regression tests.

## Suggested Execution Order

Implement in this order:

1. Phase 0: engine switch and compatibility.
2. Phase 1: config, Ollama client, doctor checks.
3. Phase 2: schema migration.
4. Phase 3: redaction/evidence/chunking.
5. Phase 4: mocked structured Ollama extraction.
6. Phase 6 subset: candidate review and accepted-only export.
7. Phase 5: hybrid dedupe and recurrence table.
8. Phase 7: embeddings.
9. Phase 8: evaluation harness.
10. Enable `hybrid` as the recommended engine after fixture and live validation.

This order keeps each step testable and avoids building embeddings or vector search before candidate quality and evidence safety are proven.

## Acceptance Criteria For First Ollama Milestone

The first shippable Ollama milestone is complete when:

1. `prlearn extract --engine hybrid --fixture ...` works without live Ollama by falling back to heuristic.
2. Mocked Ollama tests create `learning_candidates`, not direct accepted cards.
3. Candidate outputs require valid evidence IDs.
4. Model calls are cached in `model_runs`.
5. Secrets are redacted before model-run storage.
6. Invalid JSON and schema failures are stored and do not create candidates.
7. `prlearn review` can accept/reject candidates.
8. Accepted candidates create or update `learning_cards`.
9. Exports include accepted cards only by default.
10. Existing fixture daily loop remains idempotent.
11. `.venv/bin/python -m pytest -q` passes.

## Open Decisions Before Coding

1. Should `hybrid` remain opt-in until we review live candidate quality? Recommended: yes.
2. Should unredacted raw JSON remain in the existing v1 tables? Recommended: leave existing data alone for now, but redact all new model-facing data and add `scrub` later.
3. Should `config.json` or `config.toml` be canonical? Recommended: keep `config.json`.
4. Should recurrence count mean evidence count or PR recurrence count? Recommended: migrate to PR/key recurrence count when `learning_card_recurrences` lands.
5. Should accepted-only exports happen immediately or only after candidate tables land? Recommended: change exports during candidate-review phase, not before.
