"""문서 넣기 테스트용 작은 문서 만들기 (docx·pptx·hwpx·hwp·pdf). 외부 라이브러리 없이 형식의 최소 구조만."""

import struct
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path: Path, items: list[tuple[str, str]], modified: str | None = "2025-03-02T09:00:00Z",
              numbered_headings: bool = False) -> Path:
    """items: [("h1"|"h2"|"p"|"table", 글)]. table은 행을 \\n, 칸을 |로."""
    body = []
    for kind, text in items:
        if kind.startswith("h"):
            num = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if numbered_headings else ""
            body.append(f'<w:p><w:pPr><w:pStyle w:val="{kind[1]}"/>{num}</w:pPr><w:r><w:t>{escape(text)}</w:t></w:r></w:p>')
        elif kind == "table":
            rows = "".join(
                "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c.strip())}</w:t></w:r></w:p></w:tc>" for c in row.split("|"))
                + "</w:tr>" for row in text.split("\n"))
            body.append(f"<w:tbl>{rows}</w:tbl>")
        else:
            body.append(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>")
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{"".join(body)}</w:body></w:document>'
    # 한국어 Word처럼 스타일 id는 숫자, 이름이 heading N
    styles = (f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{W}">'
              '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="heading 2"/></w:style></w:styles>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        if modified:
            z.writestr("docProps/core.xml",
                       '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                       'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                       f'<dcterms:modified xsi:type="dcterms:W3CDTF">{modified}</dcterms:modified></cp:coreProperties>')
    return path


def make_pptx(path: Path, slides: list[tuple[str, list[str]]]) -> Path:
    ns = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
    with zipfile.ZipFile(path, "w") as z:
        for i, (title, lines) in enumerate(slides, 1):
            body = "".join(f"<a:p><a:r><a:t>{escape(l)}</a:t></a:r></a:p>" for l in lines)
            z.writestr(f"ppt/slides/slide{i}.xml",
                       f'<?xml version="1.0"?><p:sld {ns}><p:cSld><p:spTree>'
                       f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>{escape(title)}</a:t></a:r></a:p></p:txBody></p:sp>'
                       f'<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr><p:txBody>{body}</p:txBody></p:sp>'
                       '</p:spTree></p:cSld></p:sld>')
    return path


HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HH = "http://www.hancom.co.kr/hwpml/2011/head"
HS = "http://www.hancom.co.kr/hwpml/2011/section"


def make_hwpx(path: Path, items: list[tuple[str, str]], created: str = "2024-11-05T10:00:00Z") -> Path:
    """items: [("h1"|"h2"|"p"|"table", 글)]. 제목은 '개요 N' 스타일(글자에 번호 없음 → 자동 번호)."""
    paras = []
    for kind, text in items:
        if kind.startswith("h"):
            paras.append(f'<hp:p styleIDRef="{kind[1]}" paraPrIDRef="0"><hp:run><hp:t>{escape(text)}</hp:t></hp:run></hp:p>')
        elif kind == "table":
            rows = "".join(
                "<hp:tr>" + "".join(
                    f'<hp:tc><hp:subList><hp:p><hp:run><hp:t>{escape(c.strip())}</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                    for c in row.split("|")) + "</hp:tr>" for row in text.split("\n"))
            paras.append(f'<hp:p styleIDRef="0"><hp:run><hp:tbl>{rows}</hp:tbl></hp:run></hp:p>')
        else:
            paras.append(f'<hp:p styleIDRef="0"><hp:run><hp:t>{escape(text)}</hp:t></hp:run></hp:p>')
    section = f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">{"".join(paras)}</hs:sec>'
    header = (f'<?xml version="1.0" encoding="UTF-8"?><hh:head xmlns:hh="{HH}"><hh:refList><hh:styles>'
              '<hh:style id="0" type="PARA" name="바탕글" engName="Normal"/>'
              '<hh:style id="1" type="PARA" name="개요 1" engName="Outline 1"/>'
              '<hh:style id="2" type="PARA" name="개요 2" engName="Outline 2"/>'
              '</hh:styles></hh:refList></hh:head>')
    hpf = (f'<?xml version="1.0"?><opf:package xmlns:opf="http://www.idpf.org/2007/opf/"><opf:metadata>'
           f'<opf:meta name="CreatedDate" content="text">{created}</opf:meta></opf:metadata></opf:package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/header.xml", header)
        z.writestr("Contents/section0.xml", section)
        z.writestr("Contents/content.hpf", hpf)
    return path


# ── hwp 5.0: OLE 복합 문서를 직접 쓴다 (스트림은 4096바이트 이상으로 채워 미니 스트림 없이) ──

_END, _FREE, _FAT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD


def _record(tag: int, body: bytes, level: int = 0) -> bytes:
    size = len(body)
    if size >= 0xFFF:
        return struct.pack("<I", tag | (level << 10) | (0xFFF << 20)) + struct.pack("<I", size) + body
    return struct.pack("<I", tag | (level << 10) | (size << 20)) + body


def _para(text: str, style: int = 0) -> bytes:
    header = struct.pack("<IIHB", len(text) + 1, 0, 0, style) + b"\x00" * 11
    return _record(0x10 + 50, header) + _record(0x10 + 51, (text + "\r").encode("utf-16-le"), level=1)


def _style(name: str, eng: str) -> bytes:
    return _record(0x10 + 10, struct.pack("<H", len(name)) + name.encode("utf-16-le")
                   + struct.pack("<H", len(eng)) + eng.encode("utf-16-le") + b"\x00" * 8)


def make_hwp(path: Path, items: list[tuple[str, str]], compressed: bool = True,
             encrypted: bool = False) -> Path:
    """items: [("h1"|"p", 글)]. 제목은 스타일 1번('개요 1')."""
    flags = (1 if compressed else 0) | (2 if encrypted else 0)
    file_header = b"HWP Document File".ljust(32, b"\x00") + struct.pack("<II", 0x05000300, flags)
    docinfo = _style("바탕글", "Normal") + _style("개요 1", "Outline 1")
    section = b"".join(_para(text, 1 if kind.startswith("h") else 0) for kind, text in items)
    if compressed:
        def deflate(data: bytes) -> bytes:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            return c.compress(data) + c.flush()
        docinfo, section = deflate(docinfo), deflate(section)
    _write_cfb(path, {("FileHeader",): file_header, ("DocInfo",): docinfo, ("BodyText", "Section0"): section})
    return path


def _write_cfb(path: Path, streams: dict[tuple[str, ...], bytes]) -> None:
    sector = 512
    data_sectors: list[bytes] = []
    starts: dict[tuple[str, ...], tuple[int, int]] = {}
    first_data = 3  # 0: FAT, 1~2: 디렉터리 (항목 5개 × 128바이트 → 섹터 2개)
    for key, data in streams.items():
        size = max(len(data), 4096)
        padded = data.ljust(((size + sector - 1) // sector) * sector, b"\x00")
        starts[key] = (first_data + len(data_sectors), size)
        data_sectors += [padded[i:i + sector] for i in range(0, len(padded), sector)]
    fat = [_FAT, 2, _END]
    for key, (start, size) in starts.items():
        n = (size + sector - 1) // sector
        fat += [start + i + 1 for i in range(n - 1)] + [_END]
    fat += [_FREE] * (128 - len(fat))

    def entry(name: str, kind: int, child=_FREE, right=_FREE, start=_END, size=0) -> bytes:
        raw = name.encode("utf-16-le") + b"\x00\x00"
        return (raw.ljust(64, b"\x00") + struct.pack("<HBB", len(raw), kind, 1)
                + struct.pack("<III", _FREE, right, child) + b"\x00" * 16 + b"\x00" * 4 + b"\x00" * 16
                + struct.pack("<II", start, size) + b"\x00" * 4)

    # 0 Root, 1 FileHeader, 2 DocInfo, 3 BodyText(저장소), 4 Section0
    dirs = [
        entry("Root Entry", 5, child=1),
        entry("FileHeader", 2, right=2, start=starts[("FileHeader",)][0], size=starts[("FileHeader",)][1]),
        entry("DocInfo", 2, right=3, start=starts[("DocInfo",)][0], size=starts[("DocInfo",)][1]),
        entry("BodyText", 1, child=4),
        entry("Section0", 2, start=starts[("BodyText", "Section0")][0], size=starts[("BodyText", "Section0")][1]),
    ]
    directory = b"".join(dirs).ljust(2 * sector, b"\x00")
    header = (bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
              + b"\x00" * 6 + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, _END, 0, _END, 0)
              + struct.pack("<I", 0) + struct.pack("<108I", *([_FREE] * 108)))  # DIFAT[0] = FAT은 0번 섹터
    assert len(header) == 512, len(header)
    path.write_bytes(header + struct.pack("<128I", *fat) + directory + b"".join(data_sectors))


def make_pdf(path: Path, lines: list[str]) -> Path:
    """ASCII 글자만 (기본 Helvetica)."""
    content = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(
        f"({l.replace('(', '[').replace(')', ']')}) Tj T*" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path
