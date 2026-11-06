import re

import pytest

from app.services.chunking import chunk_pages
from tests.builders import words

TOKEN_RE = re.compile(r"\S+")


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def _assert_contract(pages: list[tuple[int, str]], drafts, max_tokens: int) -> None:
    by_page = {number: text for number, text in pages}
    covered = {number: set() for number, _text in pages}
    last_start: dict[int, int] = {}
    ordinals = [draft.ordinal for draft in drafts]
    assert ordinals == list(range(len(drafts)))
    for draft in drafts:
        page = by_page[draft.page]
        assert page[draft.start_offset : draft.end_offset] == draft.text
        assert len(draft.text) == draft.end_offset - draft.start_offset
        assert 0 < draft.token_count <= max_tokens
        assert draft.token_count == len(_tokens(draft.text))
        for match in TOKEN_RE.finditer(page):
            if draft.start_offset <= match.start() < draft.end_offset:
                covered[draft.page].add(match.start())
        previous = last_start.get(draft.page)
        if previous is not None:
            assert draft.start_offset > previous
        last_start[draft.page] = draft.start_offset
    for number, text in pages:
        assert covered[number] == {match.start() for match in TOKEN_RE.finditer(text)}


@pytest.mark.parametrize(
    ("name", "pages", "max_tokens", "overlap", "expected_counts"),
    [
        ("empty", [(1, "")], 400, 60, []),
        ("whitespace", [(1, " \n\n\t ")], 400, 60, []),
        ("short", [(1, words(10))], 400, 60, [10]),
        ("exact_target", [(1, words(400))], 400, 60, [400]),
        ("one_token_over", [(1, words(401))], 400, 60, [400, 61]),
        (
            "two_paragraphs",
            [(1, words(300, "a") + "\n\n" + words(300, "b"))],
            400,
            60,
            [300, 360],
        ),
        (
            "short_paragraphs_skip_overlap",
            [(1, words(50, "a") + "\n\n" + words(50, "b"))],
            80,
            60,
            [50, 50],
        ),
        ("hard_split", [(1, words(100))], 80, 20, [80, 40]),
    ],
)
def test_chunk_windows(name, pages, max_tokens, overlap, expected_counts):
    drafts = chunk_pages(pages, max_tokens=max_tokens, overlap_tokens=overlap)
    assert [draft.token_count for draft in drafts] == expected_counts
    _assert_contract(pages, drafts, max_tokens)
    if name == "one_token_over":
        assert _tokens(drafts[0].text)[-60:] == _tokens(drafts[1].text)[:60]
    if name == "two_paragraphs":
        assert _tokens(drafts[0].text)[-60:] == _tokens(drafts[1].text)[:60]
        assert "b0" not in drafts[0].text
        assert "b0" in drafts[1].text
    if name == "hard_split":
        assert _tokens(drafts[0].text)[-20:] == _tokens(drafts[1].text)[:20]
    if name == "short_paragraphs_skip_overlap":
        assert _tokens(drafts[0].text)[-1] not in _tokens(drafts[1].text)


def test_overlap_stays_on_its_page():
    pages = [(1, words(500, "a")), (2, words(10, "b"))]
    drafts = chunk_pages(pages)
    _assert_contract(pages, drafts, 400)
    assert {draft.page for draft in drafts} == {1, 2}
    assert all("b0" not in draft.text for draft in drafts if draft.page == 1)
    assert all("a0" not in draft.text for draft in drafts if draft.page == 2)
    assert [draft.token_count for draft in drafts if draft.page == 2] == [10]


def test_heading_is_section_metadata_and_exact_text():
    text = "1. Cargo rules\n\nThe vessel must file a manifest."
    drafts = chunk_pages([(1, text)])
    assert len(drafts) == 1
    assert drafts[0].section == "1. Cargo rules"
    assert drafts[0].text == text
    assert text[drafts[0].start_offset : drafts[0].end_offset] == drafts[0].text


def test_indentation_stays_in_the_slice():
    text = "  alpha  beta"
    drafts = chunk_pages([(1, text)])
    assert drafts[0].text == text
    assert drafts[0].token_count == 2
    assert drafts[0].section is None
