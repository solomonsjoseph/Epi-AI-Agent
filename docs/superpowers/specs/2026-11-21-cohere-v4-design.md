# Cohere Embed v4 migration

Approved by the user on 2026-11-21: rebuild NHANES 2017–2018 and RePORT India Synthetic with Cohere Embed v4 on Amazon Bedrock, 1,536 dimensions, including matching Epi-Agent search support and new versioned packages.

## Behavior

Rebuild the existing table, column, publication, and study-design chunks from the current sources. Preserve raw data, reviewed literature, catalog join declarations, and the NHANES pyreadstat 1.3.7 decoder. Generate NHANES 0.4.0 and RePORT India Synthetic 0.5.0 archives with checksums. Preserve prior archives and installed packages.

Use `cohere.embed-v4:0` in `us-east-1`, floating-point output at 1536 dimensions, `search_document` for indexing and `search_query` for retrieval. The exact index compatibility identity is `Bedrock/cohere-embed-v4-1536`. Chroma embedding-function name is `bedrock-cohere-embed-v4`; its nonsecret configuration is `model_name`, `region_name`, and `dimensions`. Batch at most 96 texts per request. Reject wrong vector counts, wrong dimensions, nonfinite values, and empty inputs. Never persist provider credentials in an index, manifest, or diagnostic.

The Database repository owns a standalone provider adapter and the two builders. The independently deployable Epi-Agent repository owns its runtime adapter and profile registration. Both adapters implement the same Chroma contract. The package builder declares the actual model rather than a fixed OpenAI identity, validates index identity and dimensions for Cohere releases, and retains existing OpenAI behavior. The installer accepts the two supported identities; runtime compatibility prevents cross-model searches.

Add the Bedrock profile without removing the OpenAI profile. Select Cohere locally through `DB_RAG_EMBEDDING_PROFILE`. Use existing project Bedrock credentials, scoped to each runtime client, and fixed AWS regional endpoints. Cloud production deployment is outside this rebuild.

## Verification and delivery

Use test-first adapter, routing, manifest, builder, and release-validation tests. Rebuild from existing source files without regeneration. Compare decoded database tables and catalog identities to prior releases, validate NHANES source values, and verify every index collection has 1536 dimensions and expected counts. Run one dedicated real smoke, with a maximum of 300 seconds, installing both archives through the real installer, binding production retrieval, querying schema and knowledge, and inspecting the compiled UI and real FastAPI startup state. Preserve evidence on failure and never automatically rerun this smoke. Review the changes independently before applying them to the original checkouts, preserving unrelated user edits.
