"""묻기 근거에 문서 조각을 [문서] 등급으로 붙이고, 코드 노드마다 이어진 문서 수를 센다.

근거로 고르는 조각 (최대 4개):
- 대상 코드와 이어진 조각: 대상 메서드(Class.method)·그 클래스·이 메서드를 거치는 URL은 강하게(2~3),
  쓰는 테이블·클래스 업무명은 약하게(1). 약한 연결만 있으면 질문 낱말도 맞아야 넣는다.
- 질문과 잘 맞는 조각: 연결이 없어도 질문 낱말(idf 가중)의 34% 이상이 있으면 (상위 3개 안에서).
"""

import sqlite3
from collections import defaultdict

from sasu.docs.linking import CLASS, LABEL, METHOD, TABLE, URL, has_docs
from sasu.docs.search import MIN_COVERAGE, DocIndex
from sasu.rag.evidence import DOC, Evidence, Target

MAX_DOC_EVIDENCE = 4
_MAX_TEXT = 900
_WEIGHT = {METHOD: 3.0, URL: 3.0, CLASS: 2.0, TABLE: 1.0, LABEL: 1.0}


def target_links(conn: sqlite3.Connection, target: Target) -> list[tuple[str, str]]:
    """대상 메서드와 관계있는 코드 이름 (종류, 이름)."""
    from sasu.analysis.labels import Labels
    from sasu.docs.conflicts import CodeFacts
    from sasu.rag.evidence import _urls_reaching

    cls = target.label.rsplit(".", 1)[0]
    links = [(METHOD, target.label), (CLASS, cls)]
    links += [(URL, u) for u in _urls_reaching(conn, target.method_id)[:20]]
    facts = CodeFacts(conn)
    links += [(TABLE, t) for t in sorted(facts.tables_of(facts.reach({target.method_id})))]
    names = Labels(conn)
    label = names.class_label(names.class_of(target.method_id))
    if label:
        links.append((LABEL, label))
    return links


def linked_chunks(conn: sqlite3.Connection, links: list[tuple[str, str]]) -> dict[int, float]:
    """조각 id → 연결 세기 합."""
    kinds: dict[int, set[str]] = defaultdict(set)
    strength: dict[int, float] = defaultdict(float)
    for kind, value in links:
        for r in conn.execute(
                "SELECT DISTINCT chunk_id FROM doc_links WHERE kind = ? AND found = 1 AND upper(value) = upper(?)",
                (kind, value)):
            kinds[r[0]].add(kind)
            strength[r[0]] += _WEIGHT.get(kind, 1.0)
    for cid, found in kinds.items():  # "Class.method"를 적으면 클래스도 함께 잡히므로 한 번만 센다
        if METHOD in found and CLASS in found:
            strength[cid] -= _WEIGHT[CLASS]
    return strength


def doc_evidence(conn: sqlite3.Connection, target: Target, question: str | None, start: int,
                 index: DocIndex | None = None) -> list[Evidence]:
    """start: 앞 근거 수 (E 번호를 이어 붙인다)."""
    from sasu.docs.store import chunk_title

    if not has_docs(conn):
        return []
    strength = linked_chunks(conn, target_links(conn, target))
    coverage: dict[int, float] = {}
    top_hits: list[int] = []
    if question and question.strip():
        if index is None:
            from sasu.rag.pipeline import embedder_or_none

            index = DocIndex(conn, embedder_or_none())
        for rank, hit in enumerate(index.search(question, top=10)):
            coverage[hit.chunk_id] = hit.coverage
            if rank < 3 and hit.coverage >= MIN_COVERAGE:
                top_hits.append(hit.chunk_id)
    scored: dict[int, float] = {}
    for cid, s in strength.items():
        cov = coverage.get(cid, 0.0)
        if s >= 2 or cov >= 0.2:
            scored[cid] = s + 3 * cov
    for cid in top_hits:
        scored.setdefault(cid, 3 * coverage[cid])
    chosen = sorted(scored.items(), key=lambda kv: -kv[1])[:MAX_DOC_EVIDENCE]
    items = []
    for cid, _ in chosen:
        r = conn.execute(
            """SELECT c.id, c.section, c.heading, c.page, c.text, d.name, d.doc_date
               FROM doc_chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?""", (cid,)).fetchone()
        text = r["text"] if len(r["text"]) <= _MAX_TEXT else r["text"][:_MAX_TEXT] + " …"
        if r["heading"]:
            text = f"({r['heading']})\n{text}"
        location = r["name"] + (f" {r['section']}" if r["section"] else "") + (
            f" {r['page']}쪽" if r["page"] and r["page"].isdigit() else f" [{r['page']}]" if r["page"] else "")
        items.append(Evidence(f"E{start + len(items) + 1}", DOC,
                              chunk_title(r["name"], r["section"], r["heading"], r["doc_date"], r["page"]),
                              text, location, chunk_id=cid))
    return items


class DocCounts:
    """경로 추적·질문 화면의 "문서 n건": 코드 이름마다 이어진 조각 id. 요청마다 한 번 읽는다."""

    def __init__(self, conn: sqlite3.Connection):
        self.by_name: dict[tuple[str, str], set[int]] = defaultdict(set)
        if has_docs(conn):
            for r in conn.execute("SELECT chunk_id, kind, value FROM doc_links WHERE found = 1"):
                self.by_name[(r["kind"], r["value"].upper())].add(r["chunk_id"])

    def __bool__(self) -> bool:
        return bool(self.by_name)

    def chunks(self, kind: str, value: str | None) -> set[int]:
        return self.by_name.get((kind, (value or "").upper()), set()) if value else set()

    def for_method(self, label: str | None) -> set[int]:
        """Class.method 노드: 그 메서드와 그 클래스 이름으로 이어진 조각."""
        if not label or "." not in label:
            return set()
        return self.chunks(METHOD, label) | self.chunks(CLASS, label.rsplit(".", 1)[0])

    def for_tables(self, tables: str | None) -> set[int]:
        """SQL 노드의 detail "COMTNA, COMTNB"."""
        found: set[int] = set()
        for t in (tables or "").split(","):
            found |= self.chunks(TABLE, t.strip())
        return found
