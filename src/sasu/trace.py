"""URL 하나에서 출발해 Controller → Service → DAO → SQL → 테이블 경로를 따라간다."""

import sqlite3
from dataclasses import dataclass, field

from sasu.linker import normalize_url

MAX_DEPTH = 8


@dataclass
class TraceNode:
    kind: str  # url, method, statement, view, external
    label: str
    location: str | None = None  # 파일:줄
    detail: str | None = None  # 테이블 목록 등
    children: list["TraceNode"] = field(default_factory=list)
    repeated: bool = False  # 이미 위에서 펼친 노드
    note: str | None = None  # 동적 쿼리 ID, 저장소 밖 빈 등
    count: int = 1  # 같은 부모에서 같은 대상을 여러 번 부른 횟수
    method_id: int | None = None  # 메서드 노드일 때 (동작 목록이 조건 값·주석을 찾는 데 쓴다)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def find_urls(conn: sqlite3.Connection, query: str) -> list[str]:
    """정확히 일치하는 URL이 없으면 부분 일치로 찾는다."""
    exact = normalize_url(query)
    rows = conn.execute("SELECT DISTINCT url FROM url_mappings WHERE url = ?", (exact,)).fetchall()
    if rows:
        return [exact]
    rows = conn.execute(
        "SELECT DISTINCT url FROM url_mappings WHERE url LIKE ? ORDER BY url", (f"%{query}%",)
    ).fetchall()
    return [r["url"] for r in rows]


def trace_url(conn: sqlite3.Connection, url: str, full: bool = False) -> TraceNode:
    """full=False면 스테레오타입(Controller·Service·Repository·Component) 클래스만 펼치고,
    유틸 클래스(EgovProperties 등)는 한 줄로만 보여 준다. 흐름을 읽기 쉽게 하려는 것."""
    root = TraceNode(kind="url", label=url)
    seen: set[tuple[str, int]] = set()
    for row in conn.execute("SELECT method_id FROM url_mappings WHERE url = ?", (url,)):
        root.children.append(_method_node(conn, row["method_id"], seen, depth=1, full=full))
    return root


def _method_node(conn: sqlite3.Connection, method_id: int, seen: set, depth: int, full: bool) -> TraceNode:
    m = conn.execute(
        """SELECT m.name, m.line_start, c.name AS class_name, c.stereotype, f.path
           FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE m.id = ?""",
        (method_id,),
    ).fetchone()
    node = TraceNode(
        kind="method",
        label=f"{m['class_name']}.{m['name']}",
        location=f"{m['path']}:{m['line_start']}",
        detail=m["stereotype"],
        method_id=method_id,
    )
    key = ("method", method_id)
    if key in seen:
        node.repeated = True
        return node
    seen.add(key)
    if depth >= MAX_DEPTH:
        return node
    # 유틸 클래스는 한 줄로 끝낸다. 단, 유틸이 직접 SQL을 부르면 그건 보여 준다.
    expand_calls = full or depth == 1 or m["stereotype"] is not None

    for e in conn.execute(
        # 코드 호출을 줄 순서대로 먼저, 화면과 이동 URL은 마지막에
        "SELECT * FROM edges WHERE src_kind = 'method' AND src_id = ? "
        "ORDER BY line IS NULL, line, dst_kind",
        (method_id,),
    ).fetchall():
        kind = e["dst_kind"]
        if kind != "statement" and not expand_calls:
            continue
        if kind == "method":
            # 오버로드된 같은 이름의 메서드도 한 줄로 합친다
            name = _method_label(conn, e["dst_id"])
            existing = next((c for c in node.children if c.kind == "method" and c.label == name), None)
            if existing is not None:
                existing.count += 1
                continue
            child = _method_node(conn, e["dst_id"], seen, depth + 1, full)
        elif kind == "statement":
            child = _statement_node(conn, e["dst_id"])
            child.note = e["note"]
        elif kind == "view":
            child = TraceNode(kind="view", label=e["dst_name"], detail=None if e["dst_id"] else "파일 없음")
        elif kind == "url":
            child = TraceNode(kind="url", label=e["dst_name"], detail="이동")
        else:
            child = TraceNode(kind="external", label=e["dst_name"], note=e["note"])
        if kind != "method" and any(c.kind == kind and c.label == child.label for c in node.children):
            next(c for c in node.children if c.kind == kind and c.label == child.label).count += 1
            continue
        node.children.append(child)
    return node


def _method_label(conn: sqlite3.Connection, method_id: int) -> str:
    row = conn.execute(
        "SELECT c.name AS class_name, m.name FROM methods m JOIN classes c ON c.id = m.class_id WHERE m.id = ?",
        (method_id,),
    ).fetchone()
    return f"{row['class_name']}.{row['name']}"


def _statement_node(conn: sqlite3.Connection, statement_id: int) -> TraceNode:
    s = conn.execute(
        """SELECT s.full_id, s.kind, s.line, f.path FROM statements s JOIN files f ON f.id = s.file_id
           WHERE s.id = ?""",
        (statement_id,),
    ).fetchone()
    tables = [r["table_name"] for r in conn.execute(
        "SELECT table_name FROM statement_tables WHERE statement_id = ?", (statement_id,)
    )]
    return TraceNode(
        kind="statement",
        label=f"{s['kind'].upper()} {s['full_id']}",
        location=f"{s['path']}:{s['line']}",
        detail=", ".join(tables),
    )


@dataclass
class Coverage:
    """URL이 어디까지 이어지는지. 기반 그래프가 제대로 이어졌는지 보는 지표."""

    total: int = 0
    to_sql: int = 0  # SQL까지 도달
    code_only: int = 0  # 다른 메서드는 부르지만 SQL은 없음 (LDAP, 메시지, 검증 등)
    handler_only: int = 0  # 컨트롤러 메서드 하나로 끝남 (화면만 보여 주는 URL 등)


def coverage(conn: sqlite3.Connection) -> Coverage:
    result = Coverage()
    for row in conn.execute("SELECT DISTINCT url FROM url_mappings").fetchall():
        nodes = list(trace_url(conn, row["url"], full=True).walk())
        result.total += 1
        if any(n.kind == "statement" for n in nodes):
            result.to_sql += 1
        elif sum(1 for n in nodes if n.kind == "method") > 1:
            result.code_only += 1
        else:
            result.handler_only += 1
    return result
