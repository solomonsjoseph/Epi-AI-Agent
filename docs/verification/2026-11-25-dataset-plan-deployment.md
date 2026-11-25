# Dataset-plan review production delivery

Deployed at https://epiagent.org on November 25, 2026.

- Main implementation: `0094ebcc60a5de7e37507a7d3cabed37b624ef64` on `master` in Epi-AI-Agent.
- Deployed AWS implementation: `900b8915394e9406622ca0d641e3b43ad5e149f0` on `master-aws` in Epi-AI-Agent-AWS.
- Archive SHA256: `302683c196ced8be7dccdefe95c62d945f810c31180056165a87f0584d787958`; 2,827,229 bytes. Two deterministic builds matched.
- Existing guarded SSM document version 9; deployment command `ee10f5e1-a925-4776-954d-1e758d11e491`.
- Transaction `5e329f6c162449dca0008a80e51d664c` committed. Previous release `4085ec9e6da32c83f4acd4c56089d542f3fbaed9` and its snapshots remain available to rollback.

## Behavior delivered

Unchecked fields are excluded from extraction outputs, including when stale operation references remain. Invalid SQL returns repair feedback before persistence. Users can continue past an empty concept. Revision feedback carries the active concept and checkbox exclusions. Empty final approval is blocked; meaningful revision feedback remains available. The empty-selection warning is bold red, and blank or whitespace-only revision buttons are grey and disabled in both review modes.

## Verification

The main implementation passed 111 focused backend tests and 11 review UI tests. The AWS integration passed 268 backend/deployment tests and all 453 frontend tests. The subsequent prompt refinement passed 38 focused backend tests on each branch; these counts overlap rather than forming an aggregate total. Independent integration and prompt reviews found no critical or important issues. The compiled frontend and source manifest matched.

Five dedicated AWS-candidate browser checks used real FastAPI, compiled frontend, NHANES data, Bedrock Terra, and Cohere embeddings:

| Check | Outcome | Seconds |
| --- | --- | ---: |
| Deselection through extraction | Excluded sex | 22.8 |
| Continue past an empty concept | Excluded age and retained navigation state | 16.7 |
| Scoped removal feedback, authorized retest | Removed checked age for “don't choose this”; extracted SEQN, sex, race/ethnicity | 20.5 |
| Zero selections | Empty approval 422 retained interrupt; meaningful revision saved SEQN and marital status | 49.9 |
| Warning and feedback controls | Bold red warning; grey blank/whitespace revision button in both modes | 17.3 |

The first AWS-candidate scoped-feedback check failed because the model asked an unnecessary clarification despite receiving the scope. That failed run is retained. A narrow prompt/reflected-review instruction refinement explicitly explains removal intent even with a checked default. The user explicitly authorized one retest, which passed. No failed test or deployment was silently retried.

Production verification confirmed the exact running release and deployed source/bundle hashes, committed transaction, active services, cleared maintenance, and preserved private environment, study versions, executor image, authentication and cost sources. Public home, signup, health, readiness, and configuration returned 200; anonymous runtime remained 401 and operations status remained 403. The published HTML and all three JS/CSS/worker assets matched the packaged bytes. A read-only live browser check found no page errors or write requests and preserved signup password fields and mobile containment. These production checks do not claim a signed-in production extraction run.

Private raw evidence: `.dataset-plan-deploy-2026-11-24/` in the parent workspace. The three pre-existing primary-checkout modifications were preserved byte for byte. The earlier adversarial revision audit did not add a production restriction on restoring older plans.
