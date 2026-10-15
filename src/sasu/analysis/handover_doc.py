"""인수인계서 만들기: 코드 분석·받은 답변·문서 조각·아직 모르는 것을 화면/업무별 문서 한 권으로.

전임자가 떠나기 전에 "빈칸만 채우면 되는" 인수인계서를 만든다. 문장마다 근거 등급을 붙인다.
- [사실] 코드에서 확인한 것 (URL → 처리 코드 → SQL → 테이블, 읽기/쓰기, 업무 코드 분기, 주석)
- [답변] 전임자가 준 답 (답한 사람·날짜)
- [문서] 인수인계 문서 조각 (sasu docs add로 넣은 것이 있을 때만). 코드로 확인되기 전까지는 사실로 보지 않는다
- [확인 필요] 아직 답을 못 받은 질문·코드로 확정 못 한 연결. 답 칸을 비워 둔다

업무 묶음은 기존 동작 목록(list_behaviors)의 URL을 처리 클래스 업무명(한글 이름표)으로 묶은 것이다.
업무명이 없으면(한글 주석이 없는 코드, Django 등) 모듈로 묶는다 (클래스마다 나누면 절이 너무 잘게 쪼개진다).
md는 의존성 없이, docx는 python-docx(선택 의존성 `docx`)로 쓴다.
"""

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sasu.analysis.behaviors import Behavior, list_behaviors
from sasu.analysis.questions import KIND_LABEL

FACT, ANSWER, DOC, UNKNOWN = "사실", "답변", "문서", "확인 필요"
GRADES = [
    (FACT, "코드에서 확인한 내용입니다 (분석한 시점의 코드 기준)."),
    (ANSWER, "전임자가 준 답입니다. 답한 사람과 날짜를 함께 적습니다."),
    (DOC, "인수인계 문서에 적힌 내용입니다. 오래됐을 수 있어 코드로 확인되기 전까지는 사실로 보지 않습니다."),
    (UNKNOWN, "아직 모르는 것입니다. 전임자가 '답' 칸을 채워 주세요."),
]
BLANK = "＿＿＿＿＿＿＿＿＿＿"
_MAX_LIST = 8  # 한 줄에 늘어놓는 SQL·분기 값 수 (넘으면 "외 n")
_MAX_DOCS = 5  # 업무마다 붙이는 문서 조각 수
_DOC_TEXT = 160
OPERATION_BLANK = "운영 중 알아야 할 것 (배치·수동 작업, 장애 때 대응, 자주 받는 문의, 업무 담당 부서)"


@dataclass
class Line:
    grade: str
    text: str


@dataclass
class OpenItem:
    """전임자가 채울 빈칸 하나."""
    text: str
    why: str | None = None  # [사실] 묻는 이유 (코드 근거)
    where: str | None = None
    qid: int | None = None
    kind: str | None = None


@dataclass
class UrlPart:
    url: str
    title: str | None
    lines: list[Line] = field(default_factory=list)


@dataclass
class Business:
    name: str
    module: str
    has_label: bool
    behaviors: list[Behavior] = field(default_factory=list)
    overview: list[Line] = field(default_factory=list)
    urls: list[UrlPart] = field(default_factory=list)
    answers: list[Line] = field(default_factory=list)
    open_items: list[OpenItem] = field(default_factory=list)
    docs: list[Line] = field(default_factory=list)


@dataclass
class HandoverDoc:
    project: str
    scope: str | None
    generated_at: str
    indexed_at: str | None
    repo_head: str | None
    summary: list[Line] = field(default_factory=list)
    businesses: list[Business] = field(default_factory=list)
    loose_answers: list[Line] = field(default_factory=list)  # 어느 화면에서도 닿지 않는 코드의 답
    loose_open: list[OpenItem] = field(default_factory=list)
    has_docs: bool = False
    counts: dict = field(default_factory=dict)


# ── 만들기 ──────────────────────────────────────────────────

def build_handover_doc(conn: sqlite3.Connection, module: str | None = None, business: str | None = None,
                       project: str | None = None) -> HandoverDoc:
    behaviors = list_behaviors(conn, module, business)
    meta = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('repo_path', 'repo_head', 'indexed_at')")}
    if project is None:
        project = (meta.get("repo_path") or "project").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    scope = " · ".join(x for x in (module and f"모듈 {module}", business and f"업무 {business.strip()}") if x) or None
    doc = HandoverDoc(project=project, scope=scope, generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                      indexed_at=(meta.get("indexed_at") or "")[:10] or None, repo_head=(meta.get("repo_head") or "")[:10] or None)

    groups: dict[tuple[str, str], Business] = {}
    for b in behaviors:
        name = b.business or b.module or "(root)"
        key = (b.module, name)
        if key not in groups:
            groups[key] = Business(name=name, module=b.module, has_label=bool(b.business))
        groups[key].behaviors.append(b)
    doc.businesses = sorted(groups.values(), key=lambda g: (g.module, not g.has_label, g.name))

    # 질문을 업무에 붙인다: 질문 대상(Class.method·쿼리 ID·URL)을 가장 많은 화면이 거치는 업무 하나에
    reach: dict[str, Counter] = defaultdict(Counter)
    for i, g in enumerate(doc.businesses):
        for b in g.behaviors:
            for t in {*b.targets, b.url}:
                reach[t][i] += 1
    questions = _questions(conn)
    owner: dict[int, int | None] = {}
    asked_by_url: dict[str, list[int]] = defaultdict(list)
    answered_by_url: dict[str, list[int]] = defaultdict(list)
    scoped = bool(module or business)
    for q in questions:
        hits = reach.get(q["target"] or "")
        owner[q["id"]] = max(hits, key=lambda i: (hits[i], -i)) if hits else None
    by_target: dict[str, list] = defaultdict(list)
    for q in questions:
        if q["status"] in ("open", "answered"):
            by_target[q["target"] or ""].append(q)
    for b in behaviors:
        for t in {*b.targets, b.url}:
            for q in by_target.get(t, []):
                (asked_by_url if q["status"] == "open" else answered_by_url)[b.url].append(q["id"])

    for q in questions:
        i = owner[q["id"]]
        if i is None and scoped:
            continue
        if q["status"] == "answered":
            line = _answer_line(q)
            (doc.businesses[i].answers if i is not None else doc.loose_answers).append(line)
        elif q["status"] == "open":
            item = OpenItem(text=q["question"], why=q["why"], where=_where(q), qid=q["id"],
                            kind=KIND_LABEL.get(q["kind"], q["kind"]))
            (doc.businesses[i].open_items if i is not None else doc.loose_open).append(item)

    doc.has_docs = _docs_available(conn)
    conflicts = _doc_conflicts(conn) if doc.has_docs else []
    # 이미 질문 목록에 있는 '문서 확인' 질문 (같은 질문을 확인 필요에 두 번 넣지 않는다)
    doc_questions = {q["question"]: q for q in questions if q["kind"] == "doc_check"}
    external = 0
    shown_conflicts = 0
    for n, g in enumerate(doc.businesses):
        _fill_business(g, n, owner, asked_by_url, answered_by_url)
        external += sum(len(b.external) for b in g.behaviors)
        if doc.has_docs:
            shown_conflicts += _attach_docs(conn, doc, g, n, conflicts, doc_questions, owner)
        g.open_items.append(OpenItem(text=OPERATION_BLANK))

    opened = sum(1 for q in questions if q["status"] == "open" and (owner[q["id"]] is not None or not scoped))
    answered = sum(1 for q in questions if q["status"] == "answered" and (owner[q["id"]] is not None or not scoped))
    skipped = sum(1 for q in questions if q["status"] == "skipped" and (owner[q["id"]] is not None or not scoped))
    urls = len(behaviors)
    writing = sum(1 for b in behaviors if b.writes)
    tables = {t.split("(")[0] for b in behaviors for t in b.writes + b.reads}
    doc_lines = sum(1 for g in doc.businesses for x in g.docs if x.grade == DOC)
    doc.counts = {"businesses": len(doc.businesses), "urls": urls, "writing_urls": writing, "tables": len(tables),
                  "answered": answered, "open": opened, "skipped": skipped, "external": external,
                  "doc_chunks": doc_lines, "doc_conflicts": shown_conflicts, "questions_total": len(questions)}
    labeled = sum(1 for g in doc.businesses if g.has_label)
    doc.summary = [
        Line(FACT, f"업무 {len(doc.businesses):,}개 (한글 업무명이 있는 것 {labeled:,}개), 화면(URL) {urls:,}개, "
                   f"그중 데이터를 바꾸는 URL {writing:,}개, 읽거나 쓰는 테이블 {len(tables):,}개입니다."),
        Line(ANSWER, f"전임자에게 받은 답변은 {answered:,}건입니다."),
        Line(UNKNOWN, f"아직 답을 못 받은 질문 {opened:,}건, 코드로 확정하지 못한 연결 {external:,}건, "
                      f"업무마다 운영 확인 칸 {len(doc.businesses):,}개가 남아 있습니다."),
    ]
    if not questions:
        doc.summary.append(Line(UNKNOWN, "인수인계 질문을 아직 만들지 않았습니다. `sasu questions`로 만들면 확인할 것이 채워집니다."))
    if skipped:
        doc.summary.append(Line(ANSWER, f"묻지 않기로 한 질문 {skipped:,}건은 이 문서에서 뺐습니다."))
    if doc.has_docs:
        doc.summary.append(Line(DOC, f"업무에 이어진 인수인계 문서 조각 {doc_lines:,}개를 '관련 문서'에 붙였습니다."))
    if shown_conflicts:
        doc.summary.append(Line(UNKNOWN, f"문서와 코드가 다른 곳 {shown_conflicts:,}건을 '관련 문서'에 ⚠로 표시하고 "
                                         "그 업무의 '확인 필요'에 문서 확인 질문으로 넣었습니다."))
    return doc


def _questions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM questions WHERE stale = 0 ORDER BY priority DESC, id").fetchall()


def _where(q) -> str | None:
    loc = f"{q['file_path']}:{q['line']}" if q["file_path"] else None
    return " ".join(x for x in (q["target"] and f"`{q['target']}`", loc and f"({loc})") if x) or None


def _answer_line(q) -> Line:
    who = q["answered_by"] or "답한 사람 미기록"
    when = (q["answered_at"] or "")[:10] or "날짜 미기록"
    return Line(ANSWER, f"Q{q['id']} {q['question']} → {q['answer']} ({who}, {when})")


def _more(items: list[str], limit: int = _MAX_LIST) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" 외 {len(items) - limit}" if len(items) > limit else "")


def _fill_business(g: Business, n: int, owner: dict, asked_by_url: dict, answered_by_url: dict) -> None:
    """n: 이 업무의 순서(0부터). 질문이 다른 업무 절에 적혀 있으면 "Q21(12절)"로 가리킨다."""

    def refs(ids: list[int]) -> str:
        return ", ".join(f"Q{i}" if owner.get(i) in (n, None) else f"Q{i}({owner[i] + 1}절)" for i in sorted(set(ids)))

    classes = list(dict.fromkeys(b.handler.split(".")[0] for b in g.behaviors))
    writes: dict[str, list[str]] = {}
    reads: list[str] = []
    for b in g.behaviors:
        for w in b.writes:
            table, kinds = w.split("(", 1) if "(" in w else (w, "")
            for k in kinds.rstrip(")").split("·"):
                if k and k not in writes.setdefault(table, []):
                    writes[table].append(k)
            writes.setdefault(table, [])
        reads += [t for t in b.reads if t not in reads]
    reads = [t for t in reads if t not in writes]
    g.overview.append(Line(FACT, f"화면(URL) {len(g.behaviors)}개, 처리 클래스 {', '.join(f'`{c}`' for c in classes)}."))
    if writes:
        g.overview.append(Line(FACT, "쓰는 테이블: " + _more([f"{t}({'·'.join(k)})" if k else t for t, k in writes.items()], 15) + "."))
    if reads:
        g.overview.append(Line(FACT, "읽기만 하는 테이블: " + _more(reads, 15) + "."))
    if not writes and not reads:
        g.overview.append(Line(FACT, "코드에서 찾은 테이블 접근이 없습니다."))

    for b in g.behaviors:
        title = b.label.split(" · ", 1)[-1] if b.label else None
        part = UrlPart(url=b.url, title=title)
        lines = part.lines
        lines.append(Line(FACT, f"처리 코드: `{b.handler}` ({b.location})"
                                + (f", 거치는 메서드 {b.methods}개." if b.methods > 1 else ".")))
        lines.append(Line(FACT, "SQL: " + (_more([f"`{s}`" for s in b.sqls]) + "." if b.sqls
                                            else "코드에서 찾은 SQL이 없습니다.")))
        if b.writes or b.reads:
            rw = []
            if b.writes:
                rw.append("쓰기 " + ", ".join(b.writes))
            if b.reads:
                rw.append("읽기 " + _more(b.reads))
            lines.append(Line(FACT, " · ".join(rw) + "."))
        if b.screens or b.redirects:
            shown = [f"화면 {s}" for s in b.screens] + [f"이동 {u}" for u in b.redirects]
            lines.append(Line(FACT, _more(shown, 4) + "."))
        if b.branches:
            lines.append(Line(FACT, "업무 코드로 나뉘는 곳: " + _more([f"{x}" for x in b.branches], 4) + "."))
        for n in b.notes:
            lines.append(Line(FACT, f"코드 주석: {n}"))
        if b.external:
            shown = "; ".join(b.external[:4]) + (f" 외 {len(b.external) - 4}" if len(b.external) > 4 else "")
            lines.append(Line(UNKNOWN, f"코드로 확정하지 못한 연결: {shown}"))
        if answered_by_url.get(b.url):
            lines.append(Line(ANSWER, f"이 화면이 거치는 코드에 받은 답변: {refs(answered_by_url[b.url])} (받은 답변 참고)."))
        if asked_by_url.get(b.url):
            lines.append(Line(UNKNOWN, f"이 화면이 거치는 코드에 답 못 받은 질문: {refs(asked_by_url[b.url])} (확인 필요 참고)."))
        g.urls.append(part)


# ── 문서 조각 (sasu docs add) ───────────────────────────────

def _docs_available(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT 1 FROM doc_links WHERE found = 1 LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:  # 문서 테이블이 없는 예전 분석 파일
        return False


def _doc_links(g: Business) -> list[tuple[str, str, float]]:
    """업무와 관계있는 코드 이름 (종류, 이름, 세기). 문서 넣기(linking)의 종류 이름과 같다."""
    links: list[tuple[str, str, float]] = []
    for b in g.behaviors:
        links.append(("url", b.url, 3.0))
        links.append(("method", b.handler, 3.0))
    links += [("class", c, 2.0) for c in dict.fromkeys(b.handler.split(".")[0] for b in g.behaviors)]
    if g.has_label:
        links.append(("label", g.name, 1.0))
    links += [("table", t.split("(")[0], 1.0) for t in dict.fromkeys(w for b in g.behaviors for w in b.writes)]
    return links


def related_doc_chunk_ids(conn: sqlite3.Connection, links: list[tuple[str, str, float]],
                          limit: int = _MAX_DOCS) -> list[int]:
    """연결 세기 순 조각 id. 문서 테이블이 없으면 빈 목록."""
    strength: dict[int, float] = defaultdict(float)
    try:
        for kind, value, weight in links:
            for r in conn.execute("SELECT DISTINCT chunk_id FROM doc_links WHERE kind = ? AND found = 1 AND value = ?",
                                  (kind, value)):
                strength[r[0]] += weight
    except sqlite3.OperationalError:
        return []
    return sorted(strength, key=lambda c: (-strength[c], c))[:limit]


def doc_chunk_lines(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, str]:
    """조각 id → "이름 절 제목 (날짜): 앞부분"."""
    if not chunk_ids:
        return {}
    marks = ",".join("?" * len(chunk_ids))
    try:
        rows = conn.execute(
            f"""SELECT c.id, c.section, c.heading, c.page, c.text, d.name, d.doc_date
                FROM doc_chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})""", chunk_ids).fetchall()
    except sqlite3.OperationalError:
        return {}
    result = {}
    for r in rows:
        text = " ".join(r["text"].split())
        result[r["id"]] = f"{_doc_title(r)}: {text[:_DOC_TEXT]}{'…' if len(text) > _DOC_TEXT else ''}"
    return result


def related_doc_chunks(conn: sqlite3.Connection, links: list[tuple[str, str, float]],
                       limit: int = _MAX_DOCS) -> list[str]:
    """연결된 문서 조각을 "[문서] 이름 절 제목 (날짜): 앞부분" 꼴 글로. 문서 테이블이 없으면 빈 목록.
    문서 넣기 모듈이 바뀌어도 여기만 고치면 되도록 테이블을 바로 읽는다."""
    top = related_doc_chunk_ids(conn, links, limit)
    lines = doc_chunk_lines(conn, top)
    return [lines[c] for c in top if c in lines]


def _doc_conflicts(conn: sqlite3.Connection) -> list:
    """문서와 코드가 다른 곳 (sasu.docs.conflicts, 규칙 기반). 찾다가 실패해도 인수인계서는 만든다."""
    try:
        from sasu.docs.conflicts import all_conflicts

        return all_conflicts(conn)
    except Exception:  # noqa: BLE001 — 불일치 찾기는 덧붙이는 정보
        return []


def _attach_docs(conn: sqlite3.Connection, doc: HandoverDoc, g: Business, n: int, conflicts: list,
                 doc_questions: dict, owner: dict) -> int:
    """업무의 '관련 문서'에 조각과 그 조각의 불일치(⚠)를, '확인 필요'에 문서 확인 질문을 붙인다.
    불일치는 조각이 이 업무와 이어졌거나, 불일치 대상(URL·Class.method)이 이 업무의 코드일 때 이 업무에 둔다.
    돌려주는 값: 붙인 불일치 수."""
    top = related_doc_chunk_ids(conn, _doc_links(g))
    codes = {b.url for b in g.behaviors} | {b.handler for b in g.behaviors} | {t for b in g.behaviors for t in b.targets}
    chunk_ids = list(dict.fromkeys(top + [c.chunk_id for c in conflicts if c.target and c.target in codes]))
    mine = [c for c in conflicts if c.chunk_id in chunk_ids]  # 보여 주는 조각의 불일치는 모두
    lines = doc_chunk_lines(conn, chunk_ids)
    by_chunk: dict[int, list] = defaultdict(list)
    for c in mine:
        by_chunk[c.chunk_id].append(c)
    for cid in chunk_ids:
        if cid in lines:
            g.docs.append(Line(DOC, lines[cid]))
        for c in by_chunk.get(cid, []):
            g.docs.append(Line(UNKNOWN, f"⚠ {c.message}" + (f" — 문서 문장: \"{c.sentence[:120]}\"" if c.sentence else "")))
    seen: set[str] = set()
    for c in mine:
        if c.question in seen:
            continue
        seen.add(c.question)
        q = doc_questions.get(c.question)
        if q is None:  # 아직 질문 목록에 없다 (질문을 다시 맞추기 전)
            g.open_items.append(OpenItem(text=c.question, why=f"{c.message}. 문서: {c.doc_title}",
                                         where=c.target and f"`{c.target}`", kind=KIND_LABEL.get("doc_check", "문서 확인")))
        elif q["status"] == "open" and owner.get(q["id"]) is None:
            # 어느 화면에도 닿지 않아 '업무에 묶이지 않은 것'에 가던 질문을 이 업무로 옮긴다
            doc.loose_open[:] = [x for x in doc.loose_open if x.qid != q["id"]]
            owner[q["id"]] = n
            g.open_items.append(OpenItem(text=q["question"], why=q["why"], where=_where(q), qid=q["id"],
                                         kind=KIND_LABEL.get(q["kind"], q["kind"])))
    return len(mine)


def _doc_title(r) -> str:
    last = (r["heading"] or "").split(" > ")[-1].strip()
    parts = [r["name"]]
    if r["section"] and not last.startswith(r["section"]):
        parts.append(r["section"])
    if last:
        parts.append(last)
    elif r["page"]:
        parts.append(f"{r['page']}쪽" if str(r["page"]).isdigit() else f"[{r['page']}]")
    title = " ".join(parts)
    return f"{title} ({r['doc_date'][:7]})" if r["doc_date"] else title


# ── 목차·제목 ───────────────────────────────────────────────

def business_title(g: Business) -> str:
    if not g.has_label:
        return f"{g.name} 모듈 (한글 업무명 없음)"
    return f"{g.name} ({g.module or '(root)'})"


def outline(doc: HandoverDoc) -> list[str]:
    """문서 차례 (CLI·테스트가 쓴다)."""
    heads = ["요약", "근거 등급 범례", "차례"]
    heads += [f"{i}. {business_title(g)}" for i, g in enumerate(doc.businesses, 1)]
    if doc.loose_answers or doc.loose_open:
        heads.append("업무에 묶이지 않은 것")
    return heads


def _per_business_sections(g: Business, has_docs: bool) -> list[tuple[str, list]]:
    sections = [("개요", g.overview), ("화면(URL)별 처리", g.urls), ("받은 답변", g.answers),
                ("확인 필요 (전임자 기입)", g.open_items)]
    if has_docs:
        sections.append(("관련 문서", g.docs))
    return sections


# ── Markdown ────────────────────────────────────────────────

def render_markdown(doc: HandoverDoc) -> str:
    out: list[str] = [f"# {doc.project} 인수인계서", ""]
    info = [f"만든 날 {doc.generated_at}"]
    if doc.indexed_at:
        info.append(f"코드 분석 {doc.indexed_at}" + (f" (커밋 {doc.repo_head})" if doc.repo_head else ""))
    if doc.scope:
        info.append(f"범위 {doc.scope}")
    out += [" · ".join(info), ""]
    out += ["## 요약", ""] + [_md_line(x) for x in doc.summary] + [""]
    out += ["## 근거 등급 범례", ""] + [f"- **[{g}]** {meaning}" for g, meaning in GRADES] + [""]
    out += ["모든 문장 앞에 등급을 붙였습니다. '확인 필요'의 답 칸만 채우면 인수인계가 끝납니다.", ""]
    out += ["## 차례", ""]
    for i, g in enumerate(doc.businesses, 1):
        left = len(g.open_items)
        out.append(f"{i}. {business_title(g)} — URL {len(g.urls)}개 · 답변 {len(g.answers)} · 확인 필요 {left}")
    if doc.loose_answers or doc.loose_open:
        out.append(f"{len(doc.businesses) + 1}. 업무에 묶이지 않은 것")
    out.append("")
    for i, g in enumerate(doc.businesses, 1):
        out += [f"## {i}. {business_title(g)}", ""]
        for title, items in _per_business_sections(g, doc.has_docs):
            out += [f"### {title}", ""]
            if title == "화면(URL)별 처리":
                for part in items:
                    out.append(f"#### `{part.url}`" + (f" — {part.title}" if part.title else ""))
                    out += [_md_line(x) for x in part.lines] + [""]
                continue
            if not items:
                out += [_md_line(Line(FACT if title == "관련 문서" else ANSWER,
                                      "이어진 문서 조각이 없습니다." if title == "관련 문서" else "아직 받은 답변이 없습니다.")), ""]
                continue
            for x in items:
                out += _md_open(x) if isinstance(x, OpenItem) else [_md_line(x)]
            out.append("")
    if doc.loose_answers or doc.loose_open:
        out += [f"## {len(doc.businesses) + 1}. 업무에 묶이지 않은 것", "",
                "어느 화면(URL)에서도 닿지 않는 코드(배치·공통 유틸·안 쓰는 SQL 등)에 대한 것입니다.", ""]
        out += [_md_line(x) for x in doc.loose_answers]
        for x in doc.loose_open:
            out += _md_open(x)
        out.append("")
    return "\n".join(out)


def _md_line(x: Line) -> str:
    return f"- [{x.grade}] {x.text}"


def _md_open(x: OpenItem) -> list[str]:
    head = f"Q{x.qid} " if x.qid else ""
    kind = f"({x.kind}) " if x.kind else ""
    lines = [f"- [{UNKNOWN}] {head}{kind}{x.text}"]
    if x.why or x.where:
        lines.append(f"  - [{FACT}] " + " — ".join(v for v in (x.why, x.where and f"위치 {x.where}") if v))
    lines.append(f"  - 답: {BLANK} · 답한 사람: {BLANK[:5]} · 날짜: {BLANK[:5]}")
    return lines


def write_markdown(doc: HandoverDoc, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(doc), encoding="utf-8")
    return path


# ── Word (python-docx) ──────────────────────────────────────

_GRADE_COLOR = {FACT: "1F4E78", ANSWER: "2E7D32", DOC: "6A1B9A", UNKNOWN: "C62828"}


def docx_available() -> bool:
    try:
        import docx  # noqa: F401
    except ImportError:
        return False
    return True


def write_docx(doc: HandoverDoc, path: Path) -> Path:
    """python-docx가 없으면 ImportError (설치: extra `docx`)."""
    from docx import Document
    from docx.enum.text import WD_COLOR_INDEX
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    d = Document()
    for style_name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4", "List Bullet", "List Bullet 2"):
        style = d.styles[style_name]
        style.font.name = "Malgun Gothic"
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(fonts)
        fonts.set(qn("w:eastAsia"), "맑은 고딕")
    d.styles["Normal"].font.size = Pt(10)
    colors = {g: RGBColor.from_string(c) for g, c in _GRADE_COLOR.items()}
    style_ids = {n: d.styles[n].style_id for n in ("List Bullet", "List Bullet 2", "Heading 1", "Heading 2", "Heading 3")}

    def para(style: str | None = None, text: str = ""):
        # 스타일 이름으로 넘기면 python-docx가 문단마다 스타일 목록을 다시 훑어 eGovFrame 전체(8천 문단)에서 6초가 걸린다
        p = d.add_paragraph(text)
        if style:
            p._p.style = style_ids[style]
        return p

    def text_runs(p, text: str) -> None:
        # `코드` 표시는 고정폭 글꼴로
        for i, piece in enumerate(re.split(r"`([^`]*)`", text)):
            if not piece:
                continue
            run = p.add_run(piece)
            if i % 2:
                run.font.name = "Consolas"

    def graded(grade: str, text: str, level: int = 1) -> None:
        p = para("List Bullet" if level == 1 else "List Bullet 2")
        mark = p.add_run(f"[{grade}] ")
        mark.bold = True
        mark.font.color.rgb = colors[grade]
        text_runs(p, text)

    def open_item(x: OpenItem) -> None:
        head = f"Q{x.qid} " if x.qid else ""
        kind = f"({x.kind}) " if x.kind else ""
        graded(UNKNOWN, f"{head}{kind}{x.text}")
        if x.why or x.where:
            graded(FACT, " — ".join(v for v in (x.why, x.where and f"위치 {x.where}") if v), level=2)
        p = para("List Bullet 2")
        p.add_run("답: ")
        blank = p.add_run(BLANK)
        blank.font.highlight_color = WD_COLOR_INDEX.YELLOW
        p.add_run(f"  답한 사람: {BLANK[:5]}  날짜: {BLANK[:5]}")

    d.add_heading(f"{doc.project} 인수인계서", level=0)
    info = [f"만든 날 {doc.generated_at}"]
    if doc.indexed_at:
        info.append(f"코드 분석 {doc.indexed_at}" + (f" (커밋 {doc.repo_head})" if doc.repo_head else ""))
    if doc.scope:
        info.append(f"범위 {doc.scope}")
    d.add_paragraph(" · ".join(info))
    d.add_heading("요약", level=1)
    for x in doc.summary:
        graded(x.grade, x.text)
    d.add_heading("근거 등급 범례", level=1)
    for g, meaning in GRADES:
        p = para("List Bullet")
        mark = p.add_run(f"[{g}] ")
        mark.bold = True
        mark.font.color.rgb = colors[g]
        p.add_run(meaning)
    d.add_paragraph("모든 문장 앞에 등급을 붙였습니다. '확인 필요'의 답 칸만 채우면 인수인계가 끝납니다.")
    d.add_heading("차례", level=1)
    for i, g in enumerate(doc.businesses, 1):
        d.add_paragraph(f"{i}. {business_title(g)} — URL {len(g.urls)}개 · 답변 {len(g.answers)} · 확인 필요 {len(g.open_items)}")
    if doc.loose_answers or doc.loose_open:
        d.add_paragraph(f"{len(doc.businesses) + 1}. 업무에 묶이지 않은 것")
    for i, g in enumerate(doc.businesses, 1):
        d.add_page_break()
        para("Heading 1", f"{i}. {business_title(g)}")
        for title, items in _per_business_sections(g, doc.has_docs):
            para("Heading 2", title)
            if title == "화면(URL)별 처리":
                for part in items:
                    h = para("Heading 3")
                    text_runs(h, f"`{part.url}`" + (f" — {part.title}" if part.title else ""))
                    for x in part.lines:
                        graded(x.grade, x.text)
                continue
            if not items:
                if title == "관련 문서":
                    graded(FACT, "이어진 문서 조각이 없습니다.")
                else:
                    graded(ANSWER, "아직 받은 답변이 없습니다.")
                continue
            for x in items:
                open_item(x) if isinstance(x, OpenItem) else graded(x.grade, x.text)
    if doc.loose_answers or doc.loose_open:
        d.add_page_break()
        d.add_heading(f"{len(doc.businesses) + 1}. 업무에 묶이지 않은 것", level=1)
        d.add_paragraph("어느 화면(URL)에서도 닿지 않는 코드(배치·공통 유틸·안 쓰는 SQL 등)에 대한 것입니다.")
        for x in doc.loose_answers:
            graded(x.grade, x.text)
        for x in doc.loose_open:
            open_item(x)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)
    return path
