from __future__ import annotations

import json
import tarfile

import chromadb
import duckdb
import pytest

from db_rag.bedrock_embeddings import BedrockCohereEmbeddingFunction
from study_package.installer import stage_study_archive
from study_package.manifest import parse_study_package_manifest


COHERE = "Bedrock/cohere-embed-v4-1536"
OPENAI = "OpenAI/text-embedding-3-large"


def _manifest(embedding_model):
    return {
        "format_version": 2,
        "study_id": "example-study",
        "label": "Example Study",
        "package_version": "1.0.0",
        "database": {
            "source_id": "example-source",
            "duckdb": "database/study.duckdb",
            "catalog": "database/schema_catalog.json",
            "index": "database/index",
            "embedding_model": embedding_model,
        },
    }


@pytest.mark.parametrize("embedding_model", [OPENAI, COHERE])
def test_manifest_accepts_supported_embedding_identities(embedding_model):
    assert (
        parse_study_package_manifest(_manifest(embedding_model)).database.embedding_model
        == embedding_model
    )


@pytest.mark.parametrize("embedding_model", ["Other/embed-model", "Bedrock/cohere-embed-v4-1024"])
def test_manifest_rejects_unknown_embedding_identities(embedding_model):
    with pytest.raises(ValueError, match="embedding_model"):
        parse_study_package_manifest(_manifest(embedding_model))


def test_installer_stages_a_cohere_chroma_archive_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    root = tmp_path / "source"
    database = root / "database"
    database.mkdir(parents=True)
    (root / "study-package.json").write_text(json.dumps(_manifest(COHERE)), encoding="utf-8")
    (database / "schema_catalog.json").write_text(
        json.dumps(
            {
                "catalog_version": 2,
                "join_keys": {"participant_key": "id"},
                "relationships": [],
                "tables": [
                    {
                        "table": "participants",
                        "text": "Participant records",
                        "has_participant_key_join": True,
                    }
                ],
                "columns": [{"table": "participants", "column": "id", "text": "Identifier"}],
            }
        ),
        encoding="utf-8",
    )
    with duckdb.connect(str(database / "study.duckdb")) as connection:
        connection.execute("CREATE TABLE participants (id INTEGER)")

    function = BedrockCohereEmbeddingFunction.build_from_config(
        {"model_name": "cohere.embed-v4:0", "region_name": "us-east-1", "dimensions": 1536}
    )
    chroma = chromadb.PersistentClient(path=str(database / "index"))
    for name in ("table_summaries", "column_chunks"):
        collection = chroma.create_collection(name, embedding_function=function)
        collection.add(ids=[name], embeddings=[[0.1] * 1536])
    archive = tmp_path / "example-study.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(root.iterdir()):
            bundle.add(path, arcname=path.name)

    stage = stage_study_archive(archive, tmp_path / "studies")

    assert stage.manifest.database.embedding_model == COHERE
    installed = chromadb.PersistentClient(path=str(stage.extracted_root / "database" / "index"))
    assert installed.get_collection("table_summaries").count() == 1
