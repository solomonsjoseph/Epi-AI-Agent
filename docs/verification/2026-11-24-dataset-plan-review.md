# Dataset plan review fixes — verification

Implemented and verified in order on `codex/dataset-plan-review-fixes`.

| Fix | Observed real browser outcome | Seconds |
| --- | --- | ---: |
| Honor deselection through extraction | Unchecked sex; saved SEQN, age, race/ethnicity | 19.8 |
| Skip an empty concept | Unchecked the only age field, continued, returned with age still unchecked; saved SEQN, sex, race/ethnicity | 17.3 |
| Preserve revision scope | Entered exactly “don't choose this” while viewing selected age; revised plan removed age without clarification; extraction excluded age | 22.6 |
| Handle zero selections | Approval disabled; missing feedback rejected; direct empty approval returned 422 and retained interrupt; feedback replaced all fields with marital status; saved SEQN and DMDMARTL | 48.6 |

All four browser checks launched real FastAPI and the compiled frontend, used
`bedrock:openai.gpt-5.6-terra` for the model and real
`bedrock-cohere-embed-v4` embeddings with matching installed NHANES 0.4.0 and
RePORT 0.5.0 packages. No model, API, embedding, database, or graph stubs.

Each dedicated check ran once on Bedrock, below five minutes. The first check
was an explicitly user-authorized rerun after the previous OpenAI attempt
failed on exhausted credits. That earlier failure remains documented and was
not silently replaced.

The backend regression suite passed 137 tests; component suite passed 7 tests.
Each new regression was observed failing before implementation. The production
frontend was rebuilt and its source manifest verified. Every implementation
step received independent read-only code review.

Yaoan's historical three-table plan was also replayed through the production
extraction entry point with isolated state and real NHANES data. Unchecked sex
was rejected before persistence, and repaired SQL persisted 6,401 rows with the
same approved plan and no sex output. No historical conversation was changed.

Raw API/checkpoint state, page text/HTML, screenshots, provider configuration
identities, and logs remain under:

- `/private/tmp/epi-review-deselection-bedrock-20261124/`
- `/private/tmp/epi-review-concept-skip-bedrock-20261124/`
- `/private/tmp/epi-review-revision-scope-bedrock-20261124/`
- `/private/tmp/epi-review-empty-selection-bedrock-20261124/`
- `/private/tmp/epi-review-yaoan-three-table-20261124.json`

The implementation was verified locally before production delivery. It is now
merged and deployed; see `2026-11-25-dataset-plan-deployment.md` for the exact
release and verification receipt. Existing CTE/window persistence metadata
limitations are outside this selection-review change.

The follow-up UI refinement also passed its own real Bedrock browser check
once (20.0 seconds), with **11 component tests passing**. In both review modes,
the empty-selection warning rendered bold red; Request revision rendered grey
and disabled for blank or whitespace-only feedback, enabled for nonempty
feedback, and disabled again after clearing it. Revision remained available
with meaningful feedback and zero selected fields. The production bundle and
manifest were rebuilt and verified. Evidence, including the screenshot
`warning-and-disabled-revision.png`, is in
`/private/tmp/epi-review-feedback-controls-bedrock-20261124/`.
