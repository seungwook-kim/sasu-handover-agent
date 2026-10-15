"""문서를 조각으로 자른다: 제목(머리글) 단위, 너무 길면 문단으로 나눈다.

조각마다 절 번호(3.2), 제목 경로("3 운영 > 3.2 배치 운영"), 쪽·시트를 남긴다.
문서 날짜는 본문의 "작성일·개정일" 날짜 → 파일 속성 → 본문 첫 날짜 → 올린 날 순서로 정한다.
"""

import re
from dataclasses import dataclass
from datetime import date

from sasu.docs.readers import Block, RawDoc

MAX_CHARS = 1200  # 조각 하나의 최대 글자 수 (넘으면 문단 경계에서 나눈다)
_MIN_MERGE = 80  # 이보다 짧은 문단 조각은 앞 조각에 붙인다


@dataclass
class Chunk:
    seq: int
    section: str | None  # 가장 깊은 번호 붙은 제목의 번호 (3.2)
    heading: str  # 제목 경로
    page: str | None
    text: str


_SECTION_NUMBER = re.compile(r"^\s*(?:제\s*)?(\d{1,2}(?:\.\d{1,2}){0,4})[.)장절]?\s+")


def section_number(block: Block) -> str | None:
    if block.number:
        return block.number
    m = _SECTION_NUMBER.match(block.text)
    return m.group(1) if m else None


def heading_title(text: str) -> str:
    """제목 글에서 번호를 뗀 이름 ("3.2 배치 운영" → "배치 운영")."""
    return _SECTION_NUMBER.sub("", text, count=1).strip() or text.strip()


def make_chunks(doc: RawDoc) -> list[Chunk]:
    chunks: list[Chunk] = []
    path: list[tuple[int, Block]] = []  # (수준, 제목)
    body: list[Block] = []

    def flush():
        if not body:
            return
        section = next((n for _, b in reversed(path) if (n := section_number(b))), None)
        heading = " > ".join(_heading_label(b) for _, b in path)
        page = body[0].page or (path[-1][1].page if path else None)
        for text in _split([b.text for b in body]):
            chunks.append(Chunk(len(chunks) + 1, section, heading, page, text))
        body.clear()

    for block in _marker_headings(doc.blocks):
        if block.kind == "heading":
            flush()
            while path and path[-1][0] >= block.level:
                path.pop()
            path.append((block.level, block))
        else:
            # 시트·슬라이드·쪽이 바뀌면 조각을 나눈다 (조각마다 쪽 하나)
            if body and block.page != body[-1].page:
                flush()
            body.append(block)
    flush()
    # 본문 없이 제목만 있는 문서 (목차만 등)
    if not chunks and path:
        chunks.append(Chunk(1, None, " > ".join(_heading_label(b) for _, b in path), path[0][1].page,
                            "\n".join(b.text for _, b in path)))
    return chunks


# 한글·워드 문서에서 개요 스타일 없이 "■ 2023-05-17 휴가 승인 오류"처럼 기호로 쓴 소제목
_MARKER_HEADING = re.compile(r"^\s*([■◆▶●□◇▷○◎])\s*(\S.{1,78})$")
_MARKER_MAJOR = "■◆▶●"
_SENTENCE_END = re.compile(r"(?:다|요|음|함|됨|임)[.!?]?\s*$|[.!?]\s*$")


def _marker_headings(blocks: list[Block]) -> list[Block]:
    """기호로 시작하는 짧은 한 줄 문단을 소제목으로 본다 (■◆▶●는 한 단계, □◇▷○◎는 두 단계 아래).
    가장 가까운 진짜 제목 아래에 둔다. 문장으로 끝나는 줄("■ 반드시 백업할 것.")은 본문으로 둔다."""
    out: list[Block] = []
    base = 0  # 가장 가까운 진짜 제목의 수준
    for b in blocks:
        if b.kind == "heading":
            base = b.level
            out.append(b)
            continue
        m = _MARKER_HEADING.match(b.text) if "\n" not in b.text.strip() else None
        if m and not _SENTENCE_END.search(m.group(2)):
            level = base + (1 if m.group(1) in _MARKER_MAJOR else 2)
            out.append(Block("heading", m.group(2).strip(), level, b.page, None))
        else:
            out.append(b)
    return out


def _heading_label(block: Block) -> str:
    if block.number and not _SECTION_NUMBER.match(block.text):
        return f"{block.number} {block.text.strip()}"
    return block.text.strip()


def _split(paragraphs: list[str]) -> list[str]:
    """문단을 MAX_CHARS까지 모은다. 한 문단이 너무 길면 문장 경계에서 자른다."""
    pieces: list[str] = []
    for p in paragraphs:
        p = p.strip()
        while len(p) > MAX_CHARS:
            cut = max(p.rfind(sep, 0, MAX_CHARS) for sep in ("\n", ". ", "다. ", " "))
            cut = cut + 1 if cut > MAX_CHARS // 2 else MAX_CHARS
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            pieces.append(p)
    out: list[str] = []
    for p in pieces:
        if out and (len(out[-1]) + len(p) + 1 <= MAX_CHARS or len(p) < _MIN_MERGE and len(out[-1]) < MAX_CHARS * 1.2):
            out[-1] += "\n" + p
        else:
            out.append(p)
    return out


# ── 문서 날짜 ─────────────────────────────────────────────

_DATE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})\s*[.\-/년]\s*(\d{1,2})(?:\s*[.\-/월]\s*(\d{1,2})\s*일?)?(?!\d)")
_DATE_KEYWORD = re.compile(r"(작성\s*일|개정\s*일|수정\s*일|최종\s*수정|변경\s*일|작성\s*일자|기준\s*일|date|updated)", re.IGNORECASE)


def _valid(y: str, m: str, d: str | None) -> str | None:
    year, month = int(y), int(m)
    if not (1 <= month <= 12) or not (1990 <= year <= date.today().year + 1):
        return None
    if d:
        day = int(d)
        if not 1 <= day <= 31:
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    return f"{year:04d}-{month:02d}"


def document_date(doc: RawDoc, added: date | None = None) -> tuple[str, str]:
    """(날짜, 어디서 정했나: 본문·파일 속성·올린 날)."""
    texts = [b.text for b in doc.blocks[:60]]
    for text in texts:  # "작성일: 2025.03.02"처럼 낱말 옆의 날짜
        for line in text.splitlines():
            if _DATE_KEYWORD.search(line) and (m := _DATE.search(line)):
                if found := _valid(*m.groups()):
                    return found, "본문"
    if doc.prop_date:
        return doc.prop_date, "파일 속성"
    for text in texts[:20]:
        if m := _DATE.search(text):
            if found := _valid(*m.groups()):
                return found, "본문"
    return (added or date.today()).isoformat(), "올린 날"
