import hashlib
import math
import re
from typing import Protocol

from app.config import Settings
from app.constants import EMBEDDING_DIMENSION, FAKE_EMBEDDING_MODEL
from app.errors import PermanentIngestionError, ProviderFailed
from app.services.provider_http import post_json

_TOKEN = re.compile(r"\S+")


class EmbeddingProvider(Protocol):
    model_name: str
    dimension: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        pass


# Token hashes stay this wide so fake-mode cosine matches the original demonstration.
# Stored rows are padded to the column width. They are not Gemini embeddings.
_FAKE_SIGNAL_WIDTH = 64


class FakeEmbeddingProvider:
    """Sum of stable per-token vectors. Shared tokens raise cosine; this is not a semantic model."""

    model_name = FAKE_EMBEDDING_MODEL
    dimension = EMBEDDING_DIMENSION

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    def embed(self, text: str) -> list[float]:
        totals = [0.0] * _FAKE_SIGNAL_WIDTH
        for token in _TOKEN.findall(text.casefold()):
            for index, value in enumerate(_token_vector(token, _FAKE_SIGNAL_WIDTH)):
                totals[index] += value
        signal = _normalize(totals)
        return signal + [0.0] * (self.dimension - len(signal))


def _token_vector(token: str, dimension: int) -> list[float]:
    values: list[float] = []
    for index in range(dimension):
        digest = hashlib.sha256(f"{index}\n{token}".encode()).digest()
        unit = int.from_bytes(digest[:4], "big") / 2**32
        values.append(unit * 2 - 1)
    return _normalize(values)


def _normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [value / norm for value in values]


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_dimension != EMBEDDING_DIMENSION:
        raise PermanentIngestionError("embedding_config_mismatch")
    if settings.embedding_provider == "fake":
        if settings.embedding_model != FAKE_EMBEDDING_MODEL:
            raise PermanentIngestionError("embedding_config_mismatch")
        return FakeEmbeddingProvider()
    if settings.embedding_provider == "openai":
        if settings.embedding_model == FAKE_EMBEDDING_MODEL:
            raise PermanentIngestionError("embedding_config_mismatch")
        return OpenAIEmbeddingProvider(settings)
    raise PermanentIngestionError("embedding_config_mismatch")


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embeddings forced to the schema dimension. Not mixed with fake vectors."""

    dimension = EMBEDDING_DIMENSION

    def __init__(self, settings: Settings):
        self.model_name = settings.embedding_model
        self._settings = settings

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        url = self._settings.provider_base_url.rstrip("/") + "/embeddings"
        body = post_json(
            url,
            {"model": self.model_name, "input": texts, "dimensions": self.dimension},
            self._settings.provider_api_key,
            self._settings.embedding_timeout_seconds,
        )
        try:
            rows = body["data"]
            if rows and all("index" in row for row in rows):
                rows = sorted(rows, key=lambda item: item["index"])
            vectors = [row["embedding"] for row in rows]
        except (KeyError, TypeError) as exc:
            raise ProviderFailed("provider_malformed") from exc
        if len(vectors) != len(texts) or any(len(vector) != self.dimension for vector in vectors):
            raise ProviderFailed("provider_malformed")
        return vectors

    def embed(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]
