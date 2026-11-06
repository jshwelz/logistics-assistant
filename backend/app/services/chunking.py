import re
from dataclasses import dataclass

TOKEN_RE = re.compile(r"\S+")
NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+){0,4}[.)]?\s+\S")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    page: int
    section: str | None
    text: str
    start_offset: int
    end_offset: int
    token_count: int


def chunk_pages(
    pages: list[tuple[int, str]],
    *,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[ChunkDraft]:
    if max_tokens < 2 or overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("invalid chunk parameters")
    drafts: list[ChunkDraft] = []
    ordinal = 0
    for page_number, text in pages:
        # Overlap stops at the page boundary; the next page starts a new window.
        for piece in _chunk_page(text, max_tokens=max_tokens, overlap_tokens=overlap_tokens):
            drafts.append(
                ChunkDraft(
                    ordinal=ordinal,
                    page=page_number,
                    section=piece.section,
                    text=piece.text,
                    start_offset=piece.start_offset,
                    end_offset=piece.end_offset,
                    token_count=piece.token_count,
                )
            )
            ordinal += 1
    return drafts


@dataclass(frozen=True)
class _Piece:
    section: str | None
    text: str
    start_offset: int
    end_offset: int
    token_count: int


def _chunk_page(text: str, *, max_tokens: int, overlap_tokens: int) -> list[_Piece]:
    tokens = [(match.start(), match.end()) for match in TOKEN_RE.finditer(text)]
    if not tokens:
        return []
    paragraphs = _paragraph_spans(text)
    bounds = _paragraph_token_bounds(tokens, paragraphs)
    if not bounds:
        return []
    pieces: list[_Piece] = []
    start = 0
    total = len(tokens)
    while start < total:
        end = _pack_end(start, bounds, total, max_tokens)
        char_start, char_end = _char_span(text, tokens, paragraphs, bounds, start, end)
        chunk_text = text[char_start:char_end]
        token_count = end - start
        if chunk_text != text[char_start:char_end] or len(chunk_text) != char_end - char_start:
            raise RuntimeError("chunk span mismatch")
        if len(TOKEN_RE.findall(chunk_text)) != token_count:
            raise RuntimeError("chunk token mismatch")
        pieces.append(
            _Piece(
                section=_section_at(text, paragraphs, bounds, start),
                text=chunk_text,
                start_offset=char_start,
                end_offset=char_end,
                token_count=token_count,
            )
        )
        if end >= total:
            break
        next_start = end - overlap_tokens
        # A chunk shorter than the overlap cannot slide backward without repeating itself.
        if next_start <= start:
            next_start = end
        start = next_start
    return pieces


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] == "\n":
            index += 1
            continue
        start = index
        while index < length:
            if text[index] == "\n":
                nxt = index + 1
                if nxt >= length or text[nxt] == "\n":
                    break
            index += 1
        if text[start:index].strip():
            spans.append((start, index))
        if index < length and text[index] == "\n":
            index += 1
    return spans


def _paragraph_token_bounds(
    tokens: list[tuple[int, int]],
    paragraphs: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    token_index = 0
    for start, end in paragraphs:
        while token_index < len(tokens) and tokens[token_index][0] < start:
            token_index += 1
        first = token_index
        while token_index < len(tokens) and tokens[token_index][0] < end:
            token_index += 1
        if first < token_index:
            bounds.append((first, token_index))
    return bounds


def _pack_end(start: int, bounds: list[tuple[int, int]], total: int, max_tokens: int) -> int:
    paragraph_index = _paragraph_index(start, bounds)
    paragraph_end = bounds[paragraph_index][1]
    if paragraph_end - start > max_tokens:
        return min(start + max_tokens, total)
    end = paragraph_end
    paragraph_index += 1
    while paragraph_index < len(bounds):
        next_end = bounds[paragraph_index][1]
        if next_end - start > max_tokens:
            break
        end = next_end
        paragraph_index += 1
    return min(end, total)


def _paragraph_index(token_index: int, bounds: list[tuple[int, int]]) -> int:
    for index, (start, end) in enumerate(bounds):
        if start <= token_index < end:
            return index
    raise RuntimeError("token outside paragraph")


def _char_span(
    text: str,
    tokens: list[tuple[int, int]],
    paragraphs: list[tuple[int, int]],
    bounds: list[tuple[int, int]],
    start: int,
    end: int,
) -> tuple[int, int]:
    del text
    char_start = tokens[start][0]
    char_end = tokens[end - 1][1]
    start_paragraph = _paragraph_index(start, bounds)
    end_paragraph = _paragraph_index(end - 1, bounds)
    if start == bounds[start_paragraph][0]:
        char_start = paragraphs[start_paragraph][0]
    if end == bounds[end_paragraph][1]:
        char_end = paragraphs[end_paragraph][1]
    return char_start, char_end


def _section_at(
    text: str,
    paragraphs: list[tuple[int, int]],
    bounds: list[tuple[int, int]],
    chunk_start: int,
) -> str | None:
    section: str | None = None
    for (start, end), (token_start, _token_end) in zip(paragraphs, bounds, strict=True):
        if token_start > chunk_start:
            break
        paragraph = text[start:end]
        if _is_heading(paragraph):
            section = paragraph.strip()
    return section


def _is_heading(paragraph: str) -> bool:
    line = paragraph.strip()
    if not line or "\n" in line or len(line) > 80:
        return False
    if line[-1] in ".!?":
        return False
    if len(line.split()) > 12:
        return False
    if len(line) >= 4 and line.isupper():
        return True
    return NUMBERED_HEADING.match(line) is not None
