import io
import zipfile
from xml.sax.saxutils import escape


def words(count: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


def build_pdf(pages: list[list[str]]) -> bytes:
    objects: dict[int, bytes] = {}
    page_count = len(pages)
    kids: list[str] = []
    for index, lines in enumerate(pages):
        page_number = 4 + (2 * index)
        content_number = page_number + 1
        kids.append(f"{page_number} 0 R")
        stream = _content_stream(lines)
        objects[content_number] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_number} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode("ascii")
    objects[3] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    objects[2] = f"<< /Type /Pages /Count {page_count} /Kids [{' '.join(kids)}] >>".encode("ascii")
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    return _serialize(objects)


def build_pdf_with_broken_unicode_map() -> bytes:
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\n"
        b"begincmap\n"
        b"/CMapName /Adobe-Identity-UCS def\n"
        b"/CMapType 2 def\n"
        b"1 begincodespacerange\n"
        b"<0000> <FFFF>\n"
        b"endcodespacerange\n"
        b"2 beginbfchar\n"
        b"<0001> <0041>\n"
        b"<0002> <10300>\n"
        b"endbfchar\n"
        b"endcmap\n"
        b"CMapName currentdict /CMap defineresource pop\n"
        b"end\n"
        b"end\n"
    )
    content = b"BT /F1 12 Tf <0001> Tj ET"
    return _serialize(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Count 1 /Kids [6 0 R] >>",
            3: (
                b"<< /Type /Font /Subtype /Type0 /BaseFont /BrokenMap "
                b"/Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 5 0 R >>"
            ),
            4: (
                b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /BrokenMap "
                b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                b"/DW 500 >>"
            ),
            5: b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"endstream",
            6: (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Contents 7 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            ),
            7: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        }
    )


def build_docx(paragraphs: list[str]) -> bytes:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(paragraph)}</w:t></w:r></w:p>'
        for paragraph in paragraphs
    )
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{body}<w:sectPr/></w:body></w:document>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _content_stream(lines: list[str]) -> bytes:
    commands = ["BT", "/F1 12 Tf", "72 740 Td"]
    for index, line in enumerate(lines):
        if index:
            commands.append("T*")
        if line == "":
            continue
        commands.append(f"({_pdf_escape(line)}) Tj")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1")


def _pdf_escape(text: str) -> str:
    encoded = text.encode("latin-1")
    pieces: list[str] = []
    for byte in encoded:
        if byte in (0x28, 0x29, 0x5C):
            pieces.append("\\" + chr(byte))
        elif byte < 32:
            pieces.append(f"\\{byte:03o}")
        else:
            pieces.append(chr(byte))
    return "".join(pieces)


def _serialize(objects: dict[int, bytes]) -> bytes:
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    total = max(objects)
    for number in range(1, total + 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(objects[number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {total + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for number in range(1, total + 1):
        output.extend(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {total + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)
