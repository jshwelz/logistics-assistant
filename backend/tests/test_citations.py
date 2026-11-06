from uuid import uuid4

import pytest

from app.errors import ProviderFailed
from app.services.citations import parse_model_answer


def _answered(chunk_id) -> str:
    return (
        '{"status":"answered","answer":"The cargo declaration lists the weight.",'
        '"claims":[{"text":"The cargo declaration lists the weight.","chunk_ids":["'
        + str(chunk_id)
        + '"]}],"cited_chunk_ids":["'
        + str(chunk_id)
        + '"]}'
    )


def test_valid_citation_is_accepted():
    chunk_id = uuid4()
    answer = parse_model_answer(_answered(chunk_id), {chunk_id})
    assert answer.status == "answered"
    assert answer.cited_chunk_ids == [chunk_id]


@pytest.mark.parametrize(
    "raw",
    [
        '{"status":"answered","answer":"x","claims":[],"cited_chunk_ids":[]}',
        '{"status":"answered","answer":"x","claims":[{"text":"x","chunk_ids":["%s"]}],"cited_chunk_ids":["%s"]}',
        '{"status":"insufficient_evidence","answer":"no","claims":[{"text":"no","chunk_ids":["%s"]}],"cited_chunk_ids":[]}',
        '{"status":"insufficient_evidence","answer":"no","claims":[],"cited_chunk_ids":["%s"]}',
        '{"status":"answered","answer":"BANANA","claims":[],"cited_chunk_ids":[]}',
        "not-json",
        '{"status":"answered","answer":"x","excerpt":"invented"}',
    ],
)
def test_rejects_missing_fabricated_and_mismatched_citations(raw):
    known = uuid4()
    other = uuid4()
    body = raw.replace("%s", str(other))
    with pytest.raises(ProviderFailed) as caught:
        parse_model_answer(body, {known})
    assert caught.value.code == "provider_malformed"
