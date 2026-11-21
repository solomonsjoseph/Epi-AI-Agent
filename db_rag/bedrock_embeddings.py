from __future__ import annotations

from contextlib import closing
import json
from typing import Any, Literal

from chromadb.api.types import Documents, EmbeddingFunction
from chromadb.utils.embedding_functions import register_embedding_function
import numpy as np

from utils.performance import timing_stage


MODEL_NAME = "cohere.embed-v4:0"
REGION_NAME = "us-east-1"
DIMENSIONS = 1536
ENDPOINT = "https://bedrock-runtime.us-east-1.amazonaws.com"
_MAX_BATCH_SIZE = 96
_FLOAT32_MAX = float(np.finfo(np.float32).max)


@register_embedding_function
class BedrockCohereEmbeddingFunction(EmbeddingFunction[Documents]):
    def __init__(
        self,
        *,
        api_key: str,
        region_name: str,
        model_name: str,
        dimensions: int,
        timeout_seconds: float = 10.0,
        base_url: str = ENDPOINT,
        client: Any = None,
    ) -> None:
        if region_name != REGION_NAME or base_url != ENDPOINT:
            raise ValueError("Bedrock Cohere embeddings require the us-east-1 runtime endpoint.")
        if model_name != MODEL_NAME or type(dimensions) is not int or dimensions != DIMENSIONS:
            raise ValueError("Bedrock Cohere embeddings require Embed v4 with 1536 dimensions.")

        self._client = client
        if client is None and api_key.strip():
            from botocore.config import Config
            from langchain_aws.utils import create_aws_client
            from pydantic import SecretStr

            self._client = create_aws_client(
                service_name="bedrock-runtime",
                region_name=region_name,
                endpoint_url=ENDPOINT,
                api_key=SecretStr(api_key.strip()),
                config=Config(
                    connect_timeout=timeout_seconds,
                    read_timeout=timeout_seconds,
                    retries={"total_max_attempts": 1},
                ),
            )

    @staticmethod
    def name() -> str:
        return "bedrock-cohere-embed-v4"

    def get_config(self) -> dict[str, Any]:
        return {"model_name": MODEL_NAME, "region_name": REGION_NAME, "dimensions": DIMENSIONS}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> BedrockCohereEmbeddingFunction:
        # Chroma can reconstruct the contract without loading ambient credentials.
        return BedrockCohereEmbeddingFunction(
            api_key="",
            model_name=config["model_name"],
            region_name=config["region_name"],
            dimensions=config["dimensions"],
        )

    def __call__(self, input: str | list[str]) -> list[list[float]]:
        return self._embed(input, input_type="search_document")

    def embed_query(self, input: str | list[str]) -> list[list[float]]:
        return self._embed(input, input_type="search_query")

    def _embed(
        self,
        texts: str | list[str],
        *,
        input_type: Literal["search_document", "search_query"],
    ) -> list[list[float]]:
        inputs = [texts] if isinstance(texts, str) else list(texts)
        if not inputs or any(not isinstance(text, str) or not text.strip() for text in inputs):
            raise ValueError("Bedrock Cohere embedding inputs must contain nonblank text.")
        if self._client is None:
            raise RuntimeError("Bedrock Cohere embedding credentials are required.")

        vectors = []
        for start in range(0, len(inputs), _MAX_BATCH_SIZE):
            vectors.extend(self._embed_batch(inputs[start : start + _MAX_BATCH_SIZE], input_type))
        return vectors

    def _embed_batch(self, batch: list[str], input_type: str) -> list[list[float]]:
        with timing_stage(
            "db_rag.embedding", provider="bedrock", model=MODEL_NAME, batch_size=len(batch)
        ):
            response = self._client.invoke_model(
                modelId=MODEL_NAME,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "texts": batch,
                        "input_type": input_type,
                        "embedding_types": ["float"],
                        "output_dimension": DIMENSIONS,
                    }
                ),
            )
            try:
                with closing(response["body"]) as body:
                    payload = json.load(body)
            except (KeyError, TypeError, ValueError):
                raise ValueError("Bedrock returned an invalid embedding response.") from None

        embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
        if isinstance(embeddings, dict):
            embeddings = embeddings.get("float")
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            raise ValueError("Bedrock returned an invalid embedding count.")

        vectors = []
        for vector in embeddings:
            if not isinstance(vector, list) or len(vector) != DIMENSIONS:
                raise ValueError("Bedrock returned an invalid embedding dimension.")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not -_FLOAT32_MAX <= value <= _FLOAT32_MAX
                for value in vector
            ):
                raise ValueError("Bedrock returned an invalid embedding value.")
            vectors.append([float(value) for value in vector])
        return vectors
