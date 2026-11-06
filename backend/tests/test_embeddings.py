import io
import math
import urllib.error

import pytest

from app.config import Settings, validate_runtime_config
from app.constants import EMBEDDING_DIMENSION, FAKE_EMBEDDING_MODEL
from app.services.embeddings import FakeEmbeddingProvider
from app.services.provider_http import _http_error


def test_fake_vectors_are_deterministic_and_normalized():
    provider = FakeEmbeddingProvider()
    first = provider.embed_texts(["clause 12"])[0]
    assert first == provider.embed("clause 12")
    assert first == provider.embed("12 clause")
    assert first == provider.embed("Clause 12")
    assert first != provider.embed("clause 13")
    assert len(first) == EMBEDDING_DIMENSION
    assert abs(math.sqrt(sum(value * value for value in first)) - 1) < 1e-9
    assert provider.model_name == FAKE_EMBEDDING_MODEL


def test_shared_tokens_outrank_unrelated_text():
    provider = FakeEmbeddingProvider()

    def cosine(left: str, right: str) -> float:
        a = provider.embed(left)
        b = provider.embed(right)
        return sum(x * y for x, y in zip(a, b))

    matching = cosine("cargo deadline", "Berth 4 cargo deadline is eighteen hundred hours.")
    unrelated = cosine("cargo deadline", "The reefer setpoint is minus eighteen.")
    assert matching > unrelated
    assert matching > 0.3
    assert unrelated < 0.2


@pytest.mark.parametrize("text", ["", "a", "same\ntext", "cargo " * 30])
def test_fake_vector_dimension(text):
    assert len(FakeEmbeddingProvider().embed(text)) == EMBEDDING_DIMENSION


@pytest.mark.parametrize(
    "overrides",
    [
        {"embedding_dimension": 32},
        {"embedding_provider": "gemini"},
        {"embedding_model": "other-model"},
        {"chunk_overlap_tokens": 400},
        {"answer_provider": "openai"},
        {"retrieval_min_similarity": 1},
    ],
)
def test_runtime_config_rejects_incompatible_values(overrides):
    with pytest.raises(RuntimeError):
        validate_runtime_config(Settings(**overrides))


def _http(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com/v1beta/openai/embeddings",
        code=status,
        msg="error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def test_gemini_quota_stops_and_rate_limit_stays_retryable():
    quota = _http(
        429,
        b'{"error":{"code":429,"status":"RESOURCE_EXHAUSTED",'
        b'"message":"You exceeded your current quota, please check your plan."}}',
    )
    rate = _http(
        429,
        b'{"error":{"code":429,"status":"RESOURCE_EXHAUSTED","message":"Resource has been exhausted."}}',
    )
    billing = _http(400, b'{"error":{"code":"billing_hard_limit_reached","message":"billing"}}')
    assert str(_http_error(quota)) == "provider_quota"
    assert str(_http_error(rate)) == "provider_rate_limited"
    assert str(_http_error(billing)) == "provider_quota"
