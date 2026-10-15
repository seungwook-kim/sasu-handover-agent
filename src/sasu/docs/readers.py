"""문서 형식별로 글자와 제목 구조를 꺼낸다.

결과는 Block 목록이다: 제목(heading, level 1~)과 문단(para). 쪽·시트·슬라이드 번호는 page에.
- md·txt·docx·pptx·hwpx: 표준 라이브러리 (docx·pptx·hwpx는 zip + XML)
- xlsx: openpyxl (기본 의존성)
- pdf: pypdf, hwp 5.0: olefile — 선택 의존성 (uv sync --extra docs)
읽지 못하면 DocReadError에 이유를 담는다 (화면·CLI가 그대로 보여 준다).
"""

import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

SUPPORTED = (".md", ".markdown", ".txt", ".docx", ".xlsx", ".pptx", ".pdf", ".hwpx", ".hwp")
_MAX_FILE_BYTES = 50 * 1024 * 1024


class DocReadError(Exception):
    """문서를 읽지 못한 이유."""


@dataclass
class Block:
    kind: str  # heading | para
    text: str
    level: int = 0  # heading일 때 1~
    page: str | None = None  # 쪽 번호·시트 이름·슬라이드 번호
    number: str | None = None  # 자동 번호 (Word·한글의 개요 번호는 글자에 없다)


@dataclass
class RawDoc:
    name: str
    format: str
    blocks: list[Block] = field(default_factory=list)
    prop_date: str | None = None  # 파일 속성의 수정·작성 날짜 (YYYY-MM-DD)


def read_document(path: Path) -> RawDoc:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise DocReadError(f"지원하지 않는 형식이에요 ({ext or '확장자 없음'}). 지원: {', '.join(SUPPORTED)}")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise DocReadError("50MB가 넘는 파일은 넣지 않아요. 나눠서 올려 주세요.")
    reader = {
        ".md": _read_markdown, ".markdown": _read_markdown, ".txt": _read_text,
        ".docx": _read_docx, ".xlsx": _read_xlsx, ".pptx": _read_pptx,
        ".pdf": _read_pdf, ".hwpx": _read_hwpx, ".hwp": _read_hwp,
    }[ext]
    try:
        doc = reader(path)
    except DocReadError:
        raise
    except zipfile.BadZipFile as e:
        raise DocReadError(f"{ext} 파일이 손상됐거나 다른 형식이에요 (zip이 아님)") from e
    except ET.ParseError as e:
        raise DocReadError(f"문서 안의 XML을 읽지 못했어요: {e}") from e
    doc.name = path.name
    doc.blocks = [b for b in doc.blocks if b.text.strip()]
    if not doc.blocks:
        raise DocReadError("글자를 찾지 못했어요 (그림만 있는 문서이거나 스캔한 PDF일 수 있어요. 글자 인식(OCR)은 하지 않아요)")
    return doc


# ── 글자 문서 (md·txt) ────────────────────────────────────

def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_markdown(path: Path) -> RawDoc:
    doc = RawDoc(path.name, "md")
    para: list[str] = []
    in_code = False

    def flush():
        if para:
            doc.blocks.append(Block("para", "\n".join(para)))
            para.clear()

    for line in _decode(path.read_bytes()).splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            flush()
            doc.blocks.append(Block("heading", m.group(2), len(m.group(1))))
        elif not line.strip() and not in_code:
            flush()
        else:
            para.append(line)
    flush()
    return doc


# 번호 붙은 제목 줄: "3.2 배치 운영", "3. 운영", "제2장 운영", "가. 장애 대응", "Ⅱ. 운영"
_NUMBERED = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,4})[.)]?\s+(\S.{0,60})$")
_CHAPTER = re.compile(r"^\s*제\s*(\d+)\s*[장절]\s*(.{0,60})$")
_ROMAN = re.compile(r"^\s*([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)\.\s*(\S.{0,60})$")


def heading_of_line(line: str) -> tuple[int, str] | None:
    """번호로 시작하는 짧은 줄이면 (수준, 줄). 문장(마침표로 끝남·너무 긺)은 제목으로 보지 않는다."""
    text = line.strip()
    if not text or len(text) > 70 or text.endswith(("다.", "요.", "니다", ".", ",", ":")):
        if not (_CHAPTER.match(text) or _ROMAN.match(text)):
            return None
    if m := _CHAPTER.match(text):
        return 1, text
    if m := _ROMAN.match(text):
        return 1, text
    if m := _NUMBERED.match(text):
        number = m.group(1)
        if re.fullmatch(r"\d{4}", number) or re.match(r"^\d+\.\d+\.\d+\.\d+$", number):  # 연도·IP처럼 보이면
            return None
        if re.search(r"\d", m.group(2)[:1]):  # "1 2 3"처럼 숫자만 이어지는 줄
            return None
        return number.count(".") + 1, text
    return None


def _read_text(path: Path) -> RawDoc:
    doc = RawDoc(path.name, "txt")
    _lines_to_blocks(_decode(path.read_bytes()).splitlines(), doc.blocks)
    return doc


def _lines_to_blocks(lines: list[str], blocks: list[Block], page: str | None = None) -> None:
    para: list[str] = []

    def flush():
        if para:
            blocks.append(Block("para", "\n".join(para), page=page))
            para.clear()

    for line in lines:
        heading = heading_of_line(line)
        if heading:
            flush()
            blocks.append(Block("heading", heading[1], heading[0], page=page))
        elif not line.strip():
            flush()
        else:
            para.append(line.rstrip())
    flush()


# ── 공통: XML ─────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el: ET.Element, name: str) -> str | None:
    for key, value in el.attrib.items():
        if _local(key) == name:
            return value
    return None


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _first(el: ET.Element, name: str) -> ET.Element | None:
    for c in el.iter():
        if _local(c.tag) == name:
            return c
    return None


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        return ET.fromstring(z.read(name))
    except KeyError:
        return None


def _core_date(z: zipfile.ZipFile) -> str | None:
    """Office 문서 속성(docProps/core.xml)의 수정일, 없으면 작성일."""
    root = _xml(z, "docProps/core.xml")
    if root is None:
        return None
    for name in ("modified", "created"):
        el = _first(root, name)
        if el is not None and el.text and (m := re.match(r"(\d{4}-\d{2}-\d{2})", el.text.strip())):
            return m.group(1)
    return None


class _Outline:
    """Word·한글의 자동 개요 번호를 흉내 낸다 (1 → 1.1 → 1.2 → 2). 글자에 번호가 없을 때만 쓴다."""

    def __init__(self):
        self.counters: list[int] = []

    def next(self, level: int) -> str:
        level = max(1, min(level, 9))
        while len(self.counters) < level:
            self.counters.append(0)
        del self.counters[level:]
        self.counters[level - 1] += 1
        return ".".join(str(max(c, 1)) for c in self.counters)


# ── docx ──────────────────────────────────────────────────

def _read_docx(path: Path) -> RawDoc:
    doc = RawDoc(path.name, "docx")
    with zipfile.ZipFile(path) as z:
        root = _xml(z, "word/document.xml")
        if root is None:
            raise DocReadError("word/document.xml이 없어요 (Word 문서가 아님)")
        styles = _docx_styles(_xml(z, "word/styles.xml"))
        doc.prop_date = _core_date(z)
    body = _first(root, "body")
    outline = _Outline()
    for el in _docx_body(body if body is not None else root):
        if _local(el.tag) == "tbl":
            for row in _docx_rows(el):
                doc.blocks.append(Block("para", row))
            continue
        text = _docx_text(el).strip()
        if not text:
            continue
        level, numbered = _docx_level(el, styles)
        if level:
            number = outline.next(level) if numbered and not _NUMBERED.match(text) else None
            doc.blocks.append(Block("heading", text, level, number=number))
        else:
            doc.blocks.append(Block("para", text))
    return doc


def _docx_body(body: ET.Element):
    """본문의 문단·표를 순서대로. 내용 컨트롤(sdt) 안도 들어간다."""
    for el in body:
        name = _local(el.tag)
        if name in ("p", "tbl"):
            yield el
        elif name == "sdt":
            content = _first(el, "sdtContent")
            if content is not None:
                yield from _docx_body(content)


def _docx_text(p: ET.Element) -> str:
    parts = []
    for el in p.iter():
        name = _local(el.tag)
        if name == "t" and el.text:
            parts.append(el.text)
        elif name == "tab":
            parts.append("\t")
        elif name in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_rows(tbl: ET.Element) -> list[str]:
    rows = []
    for tr in _children(tbl, "tr"):
        cells = [" ".join(_docx_text(p).strip() for p in tc.iter() if _local(p.tag) == "p").strip()
                 for tc in _children(tr, "tc")]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _docx_styles(root: ET.Element | None) -> dict[str, tuple[int, bool]]:
    """스타일 id → (제목 수준, 자동 번호 여부). 한국어 Word는 id가 "1"·"2"라서 이름(heading 1)으로 본다."""
    found: dict[str, tuple[int, bool]] = {}
    if root is None:
        return found
    for st in root.iter():
        if _local(st.tag) != "style" or _attr(st, "type") != "paragraph":
            continue
        sid = _attr(st, "styleId") or ""
        name_el = _first(st, "name")
        name = (_attr(name_el, "val") if name_el is not None else "") or ""
        level = 0
        if m := re.match(r"^(?:heading|제목)\s*(\d)$", name.strip(), re.IGNORECASE):
            level = int(m.group(1))
        elif name.strip().lower() == "title" or name.strip() == "제목":
            level = 1
        outline = _first(st, "outlineLvl")
        if not level and outline is not None and (_attr(outline, "val") or "").isdigit():
            level = int(_attr(outline, "val")) + 1
        if level:
            found[sid] = (level, _first(st, "numPr") is not None)
    return found


def _docx_level(p: ET.Element, styles: dict[str, tuple[int, bool]]) -> tuple[int, bool]:
    ppr = _first(p, "pPr")
    if ppr is None:
        return 0, False
    numbered = _first(ppr, "numPr") is not None
    style = _first(ppr, "pStyle")
    sid = _attr(style, "val") if style is not None else None
    if sid in styles:
        level, style_numbered = styles[sid]
        return level, numbered or style_numbered
    outline = _first(ppr, "outlineLvl")
    if outline is not None and (_attr(outline, "val") or "").isdigit() and int(_attr(outline, "val")) < 9:
        return int(_attr(outline, "val")) + 1, numbered
    return 0, False


# ── pptx ──────────────────────────────────────────────────

def _read_pptx(path: Path) -> RawDoc:
    doc = RawDoc(path.name, "pptx")
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
        )
        if not slides:
            raise DocReadError("슬라이드를 찾지 못했어요 (PowerPoint 문서가 아님)")
        doc.prop_date = _core_date(z)
        for i, name in enumerate(slides, 1):
            root = ET.fromstring(z.read(name))
            page = str(i)
            title, paras = None, []
            for sp in root.iter():
                tag = _local(sp.tag)
                if tag == "sp":
                    ph = _first(sp, "ph")
                    texts = ["".join(t.text or "" for t in p.iter() if _local(t.tag) == "t").strip()
                             for p in sp.iter() if _local(p.tag) == "p" and _first(p, "t") is not None]
                    texts = [t for t in texts if t]
                    if ph is not None and _attr(ph, "type") in ("title", "ctrTitle") and title is None:
                        title = " ".join(texts)
                    else:
                        paras.extend(texts)
                elif tag == "tbl":
                    for tr in sp.iter():
                        if _local(tr.tag) == "tr":
                            cells = ["".join(t.text or "" for t in tc.iter() if _local(t.tag) == "t").strip()
                                     for tc in tr if _local(tc.tag) == "tc"]
                            if any(cells):
                                paras.append(" | ".join(cells))
            doc.blocks.append(Block("heading", title or f"슬라이드 {i}", 1, page=page))
            if paras:
                doc.blocks.append(Block("para", "\n".join(paras), page=page))
    return doc


# ── xlsx ──────────────────────────────────────────────────

def _read_xlsx(path: Path) -> RawDoc:
    from openpyxl import load_workbook

    doc = RawDoc(path.name, "xlsx")
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # 암호가 걸렸거나 손상된 파일
        raise DocReadError(f"엑셀을 열지 못했어요 (암호가 걸렸거나 손상된 파일일 수 있어요): {e}") from e
    props = wb.properties
    for value in (getattr(props, "modified", None), getattr(props, "created", None)):
        if isinstance(value, datetime):
            doc.prop_date = value.strftime("%Y-%m-%d")
            break
    for ws in wb.worksheets:
        header: list[str] | None = None
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else _cell(v) for v in row]
            while cells and not cells[-1]:
                cells.pop()
            if not any(cells):
                continue
            if header is None and len([c for c in cells if c]) >= 2 and all(
                    not re.fullmatch(r"[\d.,\-: ]+", c) for c in cells if c):
                header = cells
                rows.append(" | ".join(cells))
                continue
            if header:
                # 머리글을 붙여 둬야 표 중간에서 잘린 조각도 뜻이 통한다
                pairs = [f"{header[i]}: {c}" if i < len(header) and header[i] else c
                         for i, c in enumerate(cells) if c]
                rows.append(" · ".join(pairs))
            else:
                rows.append(" | ".join(c for c in cells if c))
        if rows:
            doc.blocks.append(Block("heading", ws.title, 1, page=ws.title))
            doc.blocks.extend(Block("para", r, page=ws.title) for r in rows)
    wb.close()
    return doc


def _cell(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d") if not (value.hour or value.minute) else value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


# ── pdf (선택: pypdf) ─────────────────────────────────────

def _read_pdf(path: Path) -> RawDoc:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise DocReadError("PDF를 읽으려면 선택 의존성을 설치하세요: uv sync --extra docs (pypdf)") from e
    doc = RawDoc(path.name, "pdf")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocReadError("암호가 걸린 PDF예요. 암호를 푼 파일을 올려 주세요.")
    except DocReadError:
        raise
    except Exception as e:
        raise DocReadError(f"PDF를 열지 못했어요: {e}") from e
    try:
        meta = reader.metadata
        for value in (getattr(meta, "modification_date", None), getattr(meta, "creation_date", None)):
            if isinstance(value, datetime):
                doc.prop_date = value.strftime("%Y-%m-%d")
                break
    except Exception:
        pass
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        _lines_to_blocks(text.splitlines(), doc.blocks, page=str(i))
    return doc


# ── hwpx (한글 2014+, zip + XML) ──────────────────────────

def _read_hwpx(path: Path) -> RawDoc:
    doc = RawDoc(path.name, "hwpx")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        sections = sorted(
            (n for n in names if re.fullmatch(r"Contents/section\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
        )
        if not sections:
            raise DocReadError("본문(Contents/section0.xml)을 찾지 못했어요 (한글 hwpx 문서가 아님)")
        if "META-INF/manifest.xml" in names:
            manifest = z.read("META-INF/manifest.xml").decode("utf-8", "replace")
            if "encryption-data" in manifest:
                raise DocReadError("암호가 걸린 한글 문서예요. 암호를 푼 파일을 올려 주세요.")
        styles, para_levels = _hwpx_header(_xml(z, "Contents/header.xml"))
        doc.prop_date = _hwpx_date(z, names)
        outline = _Outline()
        for name in sections:
            root = ET.fromstring(z.read(name))
            for p in _children(root, "p"):
                _hwpx_paragraph(p, styles, para_levels, outline, doc.blocks)
    return doc


def _hwpx_header(root: ET.Element | None) -> tuple[dict[str, int], dict[str, int]]:
    """스타일 id → 개요 수준 ("개요 1"·"Outline 1"), 문단 모양 id → 개요 수준 (heading type=OUTLINE)."""
    styles: dict[str, int] = {}
    paras: dict[str, int] = {}
    if root is None:
        return styles, paras
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "style":
            name = f"{_attr(el, 'name') or ''} {_attr(el, 'engName') or ''}"
            if m := re.search(r"(?:개요|Outline)\s*(\d)", name):
                styles[_attr(el, "id") or ""] = int(m.group(1))
        elif tag == "paraPr":
            heading = _first(el, "heading")
            if heading is not None and (_attr(heading, "type") or "").upper() == "OUTLINE":
                level = _attr(heading, "level") or "0"
                if level.isdigit():
                    paras[_attr(el, "id") or ""] = int(level) + 1
    return styles, paras


def _hwpx_text(p: ET.Element) -> str:
    """문단의 글자 (표 안 글자는 뺀다: 표는 행 단위로 따로 꺼낸다)."""
    parts: list[str] = []

    def walk(el: ET.Element):
        for c in el:
            tag = _local(c.tag)
            if tag in ("tbl", "subList"):
                continue
            if tag == "t":
                parts.append(c.text or "")
                for sub in c:  # <hp:t>앞<hp:tab/>뒤</hp:t>
                    sub_tag = _local(sub.tag)
                    parts.append("\t" if sub_tag == "tab" else "\n" if sub_tag == "lineBreak" else "")
                    parts.append(sub.tail or "")
            else:
                walk(c)

    walk(p)
    return "".join(parts)


def _hwpx_paragraph(p: ET.Element, styles: dict[str, int], para_levels: dict[str, int],
                    outline: _Outline, blocks: list[Block]) -> None:
    text = _hwpx_text(p).strip()
    if text:
        level = styles.get(_attr(p, "styleIDRef") or "") or para_levels.get(_attr(p, "paraPrIDRef") or "", 0)
        if level:
            number = outline.next(level) if not _NUMBERED.match(text) else None
            blocks.append(Block("heading", text, level, number=number))
        else:
            heading = heading_of_line(text)
            if heading:
                blocks.append(Block("heading", heading[1], heading[0]))
            else:
                blocks.append(Block("para", text))
    for tbl in (el for el in p.iter() if _local(el.tag) == "tbl"):
        for tr in _children(tbl, "tr"):
            cells = []
            for tc in _children(tr, "tc"):
                cell_ps = [x for x in tc.iter() if _local(x.tag) == "p"]
                cells.append(" ".join(_hwpx_text(x).strip() for x in cell_ps).strip())
            if any(cells):
                blocks.append(Block("para", " | ".join(cells)))


def _hwpx_date(z: zipfile.ZipFile, names: list[str]) -> str | None:
    for name in ("Contents/content.hpf", "docProps/core.xml"):
        if name in names:
            text = z.read(name).decode("utf-8", "replace")
            for key in ("ModifiedDate", "modified", "CreatedDate", "created"):
                if m := re.search(rf"{key}[^>]*>\s*(\d{{4}}-\d{{2}}-\d{{2}})", text):
                    return m.group(1)
    return None


# ── hwp 5.0 (선택: olefile) ───────────────────────────────

_HWPTAG_STYLE = 0x10 + 10
_HWPTAG_PARA_HEADER = 0x10 + 50
_HWPTAG_PARA_TEXT = 0x10 + 51
# 글자 중간에 들어가는 제어 문자: 1글자짜리(char)와 8글자(16바이트)짜리(inline·extended)
_HWP_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}


def _read_hwp(path: Path) -> RawDoc:
    try:
        import olefile
    except ImportError as e:
        raise DocReadError("HWP(한글 97~2010 형식)를 읽으려면 선택 의존성을 설치하세요: uv sync --extra docs (olefile). "
                           "또는 한글에서 hwpx로 다시 저장해 올려 주세요.") from e
    if not olefile.isOleFile(str(path)):
        raise DocReadError("HWP 5.0 형식이 아니에요 (한글 97 이전 HWP 3.x 등). 한글에서 hwpx로 다시 저장해 올려 주세요.")
    doc = RawDoc(path.name, "hwp")
    ole = olefile.OleFileIO(str(path))
    try:
        if not ole.exists("FileHeader"):
            raise DocReadError("한글 문서 머리(FileHeader)가 없어요. 손상됐거나 한글 문서가 아니에요.")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise DocReadError("한글 문서 서명이 없어요. 손상됐거나 한글 문서가 아니에요.")
        flags = struct.unpack_from("<I", header, 36)[0] if len(header) >= 40 else 0
        compressed, encrypted, distribution = bool(flags & 1), bool(flags & 2), bool(flags & 4)
        if encrypted:
            raise DocReadError("암호가 걸린 한글 문서예요. 암호를 푼 파일을 올려 주세요.")
        if distribution:
            raise DocReadError("배포용(복사 방지) 한글 문서는 본문이 암호화돼 있어 읽지 못해요. 원본을 hwpx로 저장해 올려 주세요.")
        styles: dict[int, int] = {}
        if ole.exists("DocInfo"):
            styles = _hwp_styles(_hwp_stream(ole.openstream("DocInfo").read(), compressed))
        sections = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and re.fullmatch(r"Section\d+", e[1])),
            key=lambda e: int(e[1][7:]),
        )
        if not sections:
            raise DocReadError("본문(BodyText/Section0)이 없어요.")
        outline = _Outline()
        for entry in sections:
            data = _hwp_stream(ole.openstream(list(entry)).read(), compressed)
            _hwp_paragraphs(data, styles, outline, doc.blocks)
        doc.prop_date = _hwp_date(ole)
    except DocReadError:
        raise
    except (zlib.error, struct.error, OSError) as e:
        raise DocReadError(f"한글 본문을 풀지 못했어요 ({e}). 한글에서 hwpx로 다시 저장해 올려 주세요.") from e
    finally:
        ole.close()
    return doc


def _hwp_stream(data: bytes, compressed: bool) -> bytes:
    if not compressed:
        return data
    return zlib.decompressobj(-15).decompress(data)  # 뒤에 붙은 채움 바이트는 무시


def _hwp_records(data: bytes):
    pos = 0
    while pos + 4 <= len(data):
        head = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag, level, size = head & 0x3FF, (head >> 10) & 0x3FF, (head >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > len(data):
                return
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        yield tag, level, data[pos:pos + size]
        pos += size


def _hwp_styles(docinfo: bytes) -> dict[int, int]:
    """스타일 번호 → 개요 수준 (스타일 이름 "개요 1" 등)."""
    styles: dict[int, int] = {}
    index = 0
    for tag, _level, body in _hwp_records(docinfo):
        if tag != _HWPTAG_STYLE:
            continue
        try:
            n = struct.unpack_from("<H", body, 0)[0]
            name = body[2:2 + n * 2].decode("utf-16-le", "replace")
            pos = 2 + n * 2
            m = struct.unpack_from("<H", body, pos)[0]
            eng = body[pos + 2:pos + 2 + m * 2].decode("utf-16-le", "replace")
        except struct.error:
            name, eng = "", ""
        if found := re.search(r"(?:개요|Outline)\s*(\d)", f"{name} {eng}"):
            styles[index] = int(found.group(1))
        index += 1
    return styles


def _hwp_text(body: bytes) -> str:
    chars: list[str] = []
    i, n = 0, len(body) // 2
    while i < n:
        code = struct.unpack_from("<H", body, i * 2)[0]
        if code < 32:
            if code in _HWP_CHAR_CONTROLS:
                if code in (10, 13):
                    chars.append("\n")
                elif code in (24, 30, 31):
                    chars.append(" " if code != 24 else "-")
                i += 1
            else:
                if code == 9:
                    chars.append("\t")
                i += 8  # 컨트롤 하나가 8글자를 차지한다 (표·그림 등)
            continue
        chars.append(chr(code))
        i += 1
    return "".join(chars)


def _hwp_paragraphs(data: bytes, styles: dict[int, int], outline: _Outline, blocks: list[Block]) -> None:
    style_id = None
    for tag, _level, body in _hwp_records(data):
        if tag == _HWPTAG_PARA_HEADER:
            style_id = body[10] if len(body) > 10 else None
        elif tag == _HWPTAG_PARA_TEXT:
            text = _hwp_text(body).strip()
            if not text:
                continue
            level = styles.get(style_id, 0) if style_id is not None else 0
            if level:
                number = outline.next(level) if not _NUMBERED.match(text) else None
                blocks.append(Block("heading", text, level, number=number))
            elif heading := heading_of_line(text):
                blocks.append(Block("heading", heading[1], heading[0]))
            else:
                blocks.append(Block("para", text))


def _hwp_date(ole) -> str | None:
    try:
        props = ole.getproperties("\x05HwpSummaryInformation", convert_time=True)
    except Exception:
        return None
    for pid in (13, 12):  # 마지막 저장, 만든 날
        value = props.get(pid)
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")
    return None
