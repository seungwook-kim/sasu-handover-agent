"""프로젝트 지도: 투입 첫날 보는 전체 구조 (모듈, 기술 스택, 핵심 테이블)."""

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sasu.analysis.common import module_of, package_root
from sasu.trace import trace_url


@dataclass
class ModuleSummary:
    name: str
    controllers: int = 0
    services: int = 0
    daos: int = 0
    urls: int = 0
    statements: int = 0
    tables: Counter = field(default_factory=Counter)


@dataclass
class TableUsage:
    name: str
    statements: int
    modules: list[str]
    kinds: dict[str, int]  # select·insert·update·delete 개수


@dataclass
class Overview:
    build: dict | None
    counts: dict[str, int]
    modules: list[ModuleSummary]
    tables: list[TableUsage]


def class_modules(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute("SELECT id, name, fqcn FROM classes").fetchall()
    packages = {r["id"]: r["fqcn"].rsplit("." + r["name"], 1)[0] for r in rows}
    root = package_root(list(set(packages.values())))
    return {cid: module_of(pkg, root) for cid, pkg in packages.items()}


def statement_modules(conn: sqlite3.Connection, modules: dict[int, str]) -> dict[int, str]:
    """쿼리는 그 쿼리를 부르는 DAO 클래스의 모듈에 속한다. 부르는 곳이 없으면 빠진다."""
    result = {}
    for r in conn.execute(
        """SELECT e.dst_id, m.class_id FROM edges e JOIN methods m ON m.id = e.src_id
           WHERE e.src_kind = 'method' AND e.dst_kind = 'statement'"""
    ):
        result.setdefault(r["dst_id"], modules[r["class_id"]])
    return result


def build_overview(conn: sqlite3.Connection, top_tables: int = 15) -> Overview:
    modules = class_modules(conn)
    summaries: dict[str, ModuleSummary] = {}

    def summary(name: str) -> ModuleSummary:
        return summaries.setdefault(name, ModuleSummary(name=name))

    for r in conn.execute("SELECT id, stereotype FROM classes WHERE stereotype IS NOT NULL"):
        s = summary(modules[r["id"]])
        if r["stereotype"] in ("Controller", "RestController"):
            s.controllers += 1
        elif r["stereotype"] == "Service":
            s.services += 1
        elif r["stereotype"] == "Repository":
            s.daos += 1
    for r in conn.execute(
        "SELECT COUNT(DISTINCT u.url) AS n, m.class_id FROM url_mappings u JOIN methods m ON m.id = u.method_id GROUP BY m.class_id"
    ):
        summary(modules[r["class_id"]]).urls += r["n"]

    stmt_module = statement_modules(conn, modules)
    table_modules: dict[str, set[str]] = defaultdict(set)
    table_statements: Counter = Counter()
    table_kinds: dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute(
        "SELECT s.id, s.kind, t.table_name FROM statements s JOIN statement_tables t ON t.statement_id = s.id"
    ):
        table_statements[r["table_name"]] += 1
        table_kinds[r["table_name"]][r["kind"]] += 1
        module = stmt_module.get(r["id"])
        if module:
            table_modules[r["table_name"]].add(module)
            summary(module).tables[r["table_name"]] += 1
    for sid, module in stmt_module.items():
        summary(module).statements += 1

    tables = [
        TableUsage(
            name=t,
            statements=n,
            modules=sorted(table_modules[t]),
            kinds=dict(table_kinds[t]),
        )
        for t, n in table_statements.most_common()
    ]
    tables.sort(key=lambda t: (-len(t.modules), -t.statements))

    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    counts = {
        "모듈": len(summaries),
        "URL": count("SELECT COUNT(DISTINCT url) FROM url_mappings"),
        "컨트롤러": count("SELECT COUNT(*) FROM classes WHERE stereotype IN ('Controller', 'RestController')"),
        "서비스": count("SELECT COUNT(*) FROM classes WHERE stereotype = 'Service'"),
        "DAO": count("SELECT COUNT(*) FROM classes WHERE stereotype = 'Repository'"),
        "SQL": count("SELECT COUNT(*) FROM statements"),
        "테이블": count("SELECT COUNT(DISTINCT table_name) FROM statement_tables"),
        "JSP": count("SELECT COUNT(*) FROM files WHERE kind = 'jsp'"),
    }
    build_row = conn.execute("SELECT value FROM meta WHERE key = 'build_info'").fetchone()
    return Overview(
        build=json.loads(build_row["value"]) if build_row else None,
        counts=counts,
        modules=sorted(summaries.values(), key=lambda s: (-s.urls, s.name)),
        tables=tables[:top_tables],
    )


@dataclass
class UrlRow:
    url: str
    handler: str
    tables: list[str]
    writes: list[str]  # insert·update·delete 하는 테이블


def module_urls(conn: sqlite3.Connection, module: str) -> list[UrlRow]:
    """한 모듈의 URL마다 거치는 테이블. 쓰기(insert·update·delete) 테이블은 따로 보여 준다."""
    modules = class_modules(conn)
    rows = []
    for r in conn.execute(
        """SELECT DISTINCT u.url, m.class_id, c.name AS class_name, m.name AS method_name
           FROM url_mappings u JOIN methods m ON m.id = u.method_id JOIN classes c ON c.id = m.class_id
           ORDER BY u.url"""
    ):
        if modules[r["class_id"]] != module and not modules[r["class_id"]].startswith(module + "."):
            continue
        tables, writes = [], []
        for node in trace_url(conn, r["url"], full=True).walk():
            if node.kind != "statement" or not node.detail:
                continue
            for t in node.detail.split(", "):
                if t not in tables:
                    tables.append(t)
                if not node.label.startswith("SELECT") and t not in writes:
                    writes.append(t)
        rows.append(UrlRow(url=r["url"], handler=f"{r['class_name']}.{r['method_name']}", tables=tables, writes=writes))
    return rows
