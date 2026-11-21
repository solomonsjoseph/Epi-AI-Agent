from __future__ import annotations

import io
import json
import logging

import chromadb
import pytest

from db_rag.bedrock_embeddings import BedrockCohereEmbeddingFunction
from db_rag.embedding_profiles import EmbeddingProfile, resolve_embedding_profile
from db_rag.embedding_routes import resolve_profile_route
from db_rag.embedding_startup import initialize_embedding


PROFILE = "bedrock-cohere-embed-v4"
MODEL = "cohere.embed-v4:0"
REGION = "us-east-1"
DIMENSIONS = 1536
IDENTITY = "Bedrock/cohere-embed-v4-1536"
ENDPOINT = "https://bedrock-runtime.us-east-1.amazonaws.com"
API_KEY = "unit-test-bedrock-key"
ENVIRONMENT = {"DB_RAG_EMBEDDING_PROFILE": PROFILE, "BEDROCK_API_KEY": API_KEY}


class RecordingBedrock:
    def __init__(self, make_embeddings=None):
        self.calls = []
        self.make_embeddings = make_embeddings or (
            lambda payload: {
                "float": [[float(text.rsplit(" ", 1)[-1])] * DIMENSIONS for text in payload["texts"]]
            }
        )

    def invoke_model(self, *, modelId, contentType, accept, body):
        payload = json.loads(body)
        self.calls.append((modelId, contentType, accept, payload))
        return {"body": io.BytesIO(json.dumps({"embeddings": self.make_embeddings(payload)}).encode())}


def _embedder(client, **overrides):
    return BedrockCohereEmbeddingFunction(
        **{
            "api_key": API_KEY,
            "region_name": REGION,
            "model_name": MODEL,
            "dimensions": DIMENSIONS,
            "timeout_seconds": 9.0,
            "base_url": ENDPOINT,
            "client": client,
            **overrides,
        }
    )


def test_document_batches_preserve_input_order_at_the_provider_limit():
    client = RecordingBedrock()
    function = _embedder(client)
    texts = [f"text {index}" for index in range(197)]

    vectors = function(texts)

    assert [vector[0] for vector in vectors] == list(range(197))
    assert [len(call[3]["texts"]) for call in client.calls] == [96, 96, 5]
    for index, (model, content_type, accept, payload) in enumerate(client.calls):
        assert (model, content_type, accept) == (MODEL, "application/json", "application/json")
        assert payload == {
            "texts": texts[index * 96 : (index + 1) * 96],
            "input_type": "search_document",
            "embedding_types": ["float"],
            "output_dimension": DIMENSIONS,
        }


def test_queries_request_search_query_and_accept_list_layout():
    client = RecordingBedrock(lambda _: [[0.75] * DIMENSIONS])
    function = _embedder(client)

    assert function.embed_query("query text") == [[0.75] * DIMENSIONS]
    assert client.calls[0][3]["input_type"] == "search_query"
    assert client.calls[0][3]["texts"] == ["query text"]


@pytest.mark.parametrize(
    "make_embeddings",
    [
        lambda _: [],
        lambda _: [[0.0] * DIMENSIONS] * 2,
        lambda _: [[0.0] * (DIMENSIONS - 1)],
        lambda _: [[True] + [0.0] * (DIMENSIONS - 1)],
        lambda _: [["1.0"] + [0.0] * (DIMENSIONS - 1)],
        lambda _: [[float("nan")] + [0.0] * (DIMENSIONS - 1)],
        lambda _: [[float("inf")] + [0.0] * (DIMENSIONS - 1)],
        lambda _: [[10**400] + [0.0] * (DIMENSIONS - 1)],
        lambda _: [[1e40] + [0.0] * (DIMENSIONS - 1)],
        lambda _: {"int8": [[0] * DIMENSIONS]},
        lambda _: {"float": [None]},
    ],
)
def test_rejects_malformed_provider_vectors(make_embeddings):
    function = _embedder(RecordingBedrock(make_embeddings))

    with pytest.raises(ValueError, match="Bedrock .*embedding"):
        function.embed_query(["query"])


@pytest.mark.parametrize("texts", [[], "", [" "], [None], ["valid", ""]])
def test_rejects_empty_and_invalid_inputs_before_invoking_provider(texts):
    client = RecordingBedrock()
    function = _embedder(client)

    with pytest.raises(ValueError, match="nonblank text"):
        function(texts)

    assert client.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "https://example.invalid"},
        {"base_url": f"{ENDPOINT}/alternate"},
        {"region_name": "us-west-2"},
        {"model_name": "cohere.embed-english-v3"},
        {"dimensions": 1024},
    ],
)
def test_rejects_invalid_cohere_contract_settings(overrides):
    with pytest.raises(ValueError, match="Bedrock"):
        _embedder(RecordingBedrock(), **overrides)


def test_chroma_persists_the_shared_configuration_and_uses_query_mode(tmp_path):
    provider = RecordingBedrock()
    function = _embedder(provider)
    client = chromadb.PersistentClient(path=str(tmp_path / "index"))
    collection = client.create_collection("example_index", embedding_function=function)
    collection.add(ids=["item"], documents=["document 7"])

    persisted = client.get_collection("example_index")
    rebound = client.get_collection("example_index", embedding_function=function)
    result = rebound.query(query_texts=["query 7"], n_results=1)

    assert persisted.count() == 1
    assert persisted.configuration_json["embedding_function"] == {
        "name": PROFILE,
        "type": "known",
        "config": {"model_name": MODEL, "region_name": REGION, "dimensions": DIMENSIONS},
    }
    assert result["ids"] == [["item"]]
    assert [request[3]["input_type"] for request in provider.calls] == [
        "search_document",
        "search_query",
    ]


def test_model_versions_allow_one_numeric_suffix_without_changing_other_identifiers():
    openai_profile = resolve_embedding_profile({}).profile
    assert openai_profile is not None
    payload = openai_profile.model_dump()
    payload["model"] = MODEL

    assert EmbeddingProfile.model_validate(payload).model == MODEL

    for field, invalid in (
        ("model", "cohere.embed-v4:latest"),
        ("model", "cohere.embed-v4:0:1"),
        ("id", "profile:0"),
        ("provider", "bedrock:0"),
        ("transport", "transport:0"),
        ("index_compatibility", "Bedrock/cohere:0"),
    ):
        with pytest.raises(ValueError, match="safe"):
            EmbeddingProfile.model_validate({**payload, field: invalid})


def test_profile_routes_the_explicit_key_and_fixed_endpoint_to_the_scoped_factory(monkeypatch):
    import langchain_aws.utils

    provider = RecordingBedrock(lambda _: {"float": [[0.1] * DIMENSIONS]})
    calls = []

    def create_client(**kwargs):
        calls.append(kwargs)
        return provider

    monkeypatch.setattr(langchain_aws.utils, "create_aws_client", create_client)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "unrelated-ambient-value")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "https://example.invalid")

    result = initialize_embedding(ENVIRONMENT)

    assert result.status.available
    assert result.status.profile_id == PROFILE
    assert result.status.index_compatibility == IDENTITY
    assert result.status.retrieval_mode == "hybrid_vector_lexical"
    assert result.route.credential_env == "BEDROCK_API_KEY"
    assert API_KEY not in repr(result)
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["service_name"] == "bedrock-runtime"
    assert kwargs["region_name"] == REGION
    assert kwargs["endpoint_url"] == ENDPOINT
    assert kwargs["api_key"].get_secret_value() == API_KEY
    assert kwargs["config"].connect_timeout == kwargs["config"].read_timeout == 10
    assert kwargs["config"].retries["total_max_attempts"] == 1
    assert provider.calls[0][3]["input_type"] == "search_query"


def test_bedrock_route_uses_only_supplied_credential_mapping(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_KEY", "ambient")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ambient-alias")
    selection = {"DB_RAG_EMBEDDING_PROFILE": PROFILE}
    resolution = resolve_embedding_profile(selection)

    missing = resolve_profile_route(selection, resolution)
    supplied = resolve_profile_route(
        {**selection, "AWS_BEARER_TOKEN_BEDROCK": "supplied-alias"}, resolution
    )

    assert missing.unavailable_reason_code == "EMBEDDING_CREDENTIALS_MISSING"
    assert supplied.available
    assert supplied.credential_env == "AWS_BEARER_TOKEN_BEDROCK"
    assert supplied.api_key == "supplied-alias"


@pytest.mark.parametrize("failure", [ValueError, TimeoutError])
def test_bedrock_provider_failure_produces_safe_lexical_startup_status(
    failure, monkeypatch, caplog
):
    import langchain_aws.utils

    def reject(_):
        raise failure(API_KEY)

    provider = RecordingBedrock(reject)
    monkeypatch.setattr(langchain_aws.utils, "create_aws_client", lambda **_: provider)
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    result = initialize_embedding(ENVIRONMENT)

    assert result.status.profile_id == PROFILE
    assert result.status.retrieval_mode == "lexical_fallback"
    assert result.status.reason_code == (
        "EMBEDDING_PROBE_TIMEOUT" if failure is TimeoutError else "EMBEDDING_PROVIDER_UNAVAILABLE"
    )
    assert API_KEY not in caplog.text
    assert API_KEY not in result.status.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    [
        {"index_compatibility": "OpenAI/text-embedding-3-large"},
        {"transport": "openai_embeddings"},
    ],
)
def test_profile_rejects_mismatched_cohere_index_and_transport(mutation, tmp_path):
    selection = resolve_embedding_profile(ENVIRONMENT)
    assert selection.profile is not None
    payload = selection.profile.model_dump() | mutation
    path = tmp_path / "embedding_models.json"
    path.write_text(json.dumps({"default_profile": PROFILE, "profiles": [payload]}), encoding="utf-8")

    route = resolve_profile_route(
        ENVIRONMENT, resolve_embedding_profile(ENVIRONMENT, registry_path=path)
    )

    assert not route.available
    assert route.unavailable_reason_code == "EMBEDDING_PROFILE_INVALID"
