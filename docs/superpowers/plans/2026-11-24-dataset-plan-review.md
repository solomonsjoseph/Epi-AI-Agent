# Dataset plan review implementation plan

> **For agentic workers:** Execute inline in the isolated worktree with verification checkpoints after each task. Use requesting-code-review before handoff.

**Goal:** Honor deselection, allow concept skipping, preserve revision intent,
and handle zero selections explicitly, in that order.

**Architecture:** Keep approved output fields separate from SQL support
references. Carry review scope through the existing strict resume contract;
branch revision before executable-plan validation where the selection is empty.

**Tech stack:** Python 3.12, FastAPI, LangGraph, DuckDB, sqlglot, React,
TypeScript, Vitest, Playwright.

## Global constraints

- Do not deploy or modify existing user conversations.
- Preserve joins, filters, and reductions when excluding output variables.
- Dedicated real feature smokes run once and never exceed 300 seconds.
- Build the production frontend and refresh its manifest for UI changes.

## Task 1: Enforce approved output selection

Files: new `db_rag/service/output_validation.py`, modify
`db_rag/service/sql_service.py`, `epi_agent/db_rag/tools.py`,
`epi_agent/db_rag/reviews.py`; new `tests/test_review_output_selection.py`
and `scripts/smoke_review_deselection_real.py`.

- [x] Add real review/extraction regression coverage: an operation retains a
  deselected source field, but projecting it must return SQL_REPAIR_REQUIRED
  before validated_sql or dataset artifacts exist. Corrected SQL must persist
  a dataset containing only the checked fields and retain exact plan identity.
- [x] Cover support-only WHERE references, CTE-renamed forbidden output,
  table-qualified identities, missing approved outputs and set operations.
- [x] Run `.venv/bin/python -m pytest -q tests/test_review_output_selection.py`
  and inspect expected failures before implementation.
- [x] Add optional `approved_output_columns` to `validate_extraction_sql`.
  Resolve outer output lineage from parsed expressions using sqlglot and
  compare physical table/column identities with the approved output list.
  Add output issues to existing structured SQL diagnostics. Pass the frozen
  output list from `_validate_and_extract`; include it in repair details.
- [x] Explicitly include selected/excluded field keys in approval tool output.
- [x] Run focused SQL/review tests and the dedicated browser smoke once.

## Task 2: Permit empty individual concepts

Files: `frontend/src/DbRagReview.tsx`, new
`frontend/src/DbRagReview.test.tsx`, `scripts/smoke_review_concept_skip_real.py`.

- [x] Write a component regression that clears the only field in the first
  concept, continues, returns, and verifies it remains unchecked in approval.
- [x] Run `npm --prefix frontend test -- DbRagReview.test.tsx` and observe failure.
- [x] Remove the current-concept selection restriction from continuation.
- [x] Build and refresh manifest, then verify focused tests and smoke once.

## Task 3: Preserve revision scope

Files: `frontend/src/types.ts`, `frontend/src/DbRagReview.tsx`,
`api/schemas.py`, `utils/review_interrupts.py`, `epi_agent/db_rag/reviews.py`,
`epi_agent/db_rag/prompt.py`, associated tests and dedicated smoke.

- [x] Add failing contract/component/tool tests for active-concept feedback,
  selected/excluded differences, invalid concept IDs, and legacy no-scope calls.
- [x] Carry optional `review_concept_id` on revise only; validate membership.
- [x] Include explicit scope and selection differences in revision provenance
  and tool messages. Instruct the agent to honor selections before asking
  additional scientific questions; do not guess unspecified scientific intent.
- [x] Build and refresh manifest, run focused tests, and smoke once.

## Task 4: Handle all-zero approval and revision

Files: review component, resume normalization, review tool, associated tests,
and `scripts/smoke_review_empty_selection_real.py`.

- [ ] Add failing tests for disabled empty approval, explanatory text, API
  rejection retaining the interrupt, and real LangGraph revision with no fields.
- [ ] Block empty approval visibly and server-side; keep revision/cancel usable.
- [ ] Process empty-selection revision without validating an empty executable
  plan; save original content plus explicit excluded keys as revision context.
- [ ] Build and refresh manifest. Verify tests, smoke once, and review final diff.

## Delivery

- [ ] Record each checkpoint's exact tests and smoke outcome in this file.
- [ ] Review changes against the approved design. Preserve failed smoke
  artifacts, if any, and report limitations without claiming a live fix.

## First-fix verification checkpoint

Baseline: 34 existing SQL/API tests passed before implementation. New tests
first reproduced DATASET_PERSISTENCE_FAILED for an unchecked SQL projection.
Code review identified set-operation edge cases; additional failing tests were
added before closing UNION BY NAME, mismatched UNION branches, EXCEPT output
coverage, duplicate source outputs, and reduction-support false positives.

Final focused command:

```sh
.venv/bin/python -m pytest -q tests/test_review_output_selection.py tests/test_llm_sql_extraction.py tests/test_sql_validation_diagnostics.py tests/test_api_review_contract.py tests/test_execution_artifact_lifecycle.py tests/test_dataset_plan_filter_validation.py tests/test_complete_variable_metadata.py
```

Result: **104 passed**. Actual extraction/persistence is covered for direct
filter support, aggregate FILTER, ordered FIRST, and ARG_MAX while excluded sex
remains available only as support. Rejected output creates neither validated SQL
nor a dataset, and corrected SQL reuses the unchanged approved plan.

The dedicated real browser smoke ran **once** and failed at the first model
call with **OPENAI_CREDITS_EXHAUSTED**, before dataset-plan review. It did not
verify the live checkbox/extraction path and was not rerun. Logs, page text,
API state, traceback, and raw checkpoint are retained at
`/private/tmp/epi-review-deselection-20261124/`. The first failed attempt did not
capture a screenshot; the runner now captures page HTML/screenshots and raw
checkpoint diagnostics for subsequent explicitly authorized runs.

A separate read-only model-catalog check confirmed that
`bedrock:openai.gpt-5.6-terra` is available from the local configured Bedrock
credentials. The smoke now supports selecting that exact model through the UI
with `--model`. No second smoke or model inference was attempted.

Limit: positive validator tests for complex CTE/window expressions establish
output-permission behavior only. Their existing persistence metadata binding
has independent limitations; this change does not refactor that subsystem.

## Bedrock verification and second-fix checkpoint

The user explicitly authorized another live run using Amazon Bedrock OpenAI.
Prepared real NHANES 0.4.0 and RePORT 0.5.0 Cohere packages in an isolated study
root. The child runtime uses Bedrock for chat/title and real Cohere embeddings;
the primary workspace configuration and credentials are unchanged.

First-fix browser result: **PASS**, 19.8 seconds, model
`bedrock:openai.gpt-5.6-terra`, embeddings `bedrock-cohere-embed-v4` available.
Unchecked sex and persisted exactly `SEQN`, `RIDAGEYR`, `RIDRETH3`.
Evidence: `/private/tmp/epi-review-deselection-bedrock-20261124/`.

Second fix: two component regressions failed before removing the intermediate
selection restriction; all **3 component tests pass** afterward. The frontend
was rebuilt and its manifest verified using the existing delivery verifier
restored from the development checkout. Independent code review passed.

Dedicated concept-skip browser result: **PASS**, 17.3 seconds, same real Bedrock
model and embeddings. Cleared the only age field, continued, navigated back to
confirm it stayed unchecked, then persisted `SEQN`, `RIAGENDR`, `RIDRETH3`.
Evidence: `/private/tmp/epi-review-concept-skip-bedrock-20261124/`.
The smoke now asserts the excluded field stays unchecked after switching modes
instead of redundantly unchecking it; component coverage already verifies mode
switching. No real smoke was rerun automatically.

## Third-fix checkpoint

Four backend regressions and one UI regression failed before implementation.
After implementation, **35 focused backend tests and 5 component tests pass**,
with an independent read-only code review finding no substantive issues.
Frontend build and manifest checks pass.

Dedicated scoped-feedback browser result: **PASS**, 22.6 seconds on the same
real Bedrock model/embeddings. While the age checkbox was still selected, the
browser submitted exactly `don't choose this` in step-by-step review. The agent
returned a revised plan excluding age without asking another scientific/model
clarification, then extracted `SEQN`, `RIAGENDR`, `RIDRETH3` after approval.
Evidence: `/private/tmp/epi-review-revision-scope-bedrock-20261124/`.
