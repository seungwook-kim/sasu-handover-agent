"""Java 데이터 접근 연결: MyBatis 문자열 ID 말고도 SI 현장에서 흔한 방식들.

- iBatis·MyBatis 짧은 ID: list("selectBoardList") → 네임스페이스 없이 하나뿐인 쿼리
- MyBatis 매퍼 인터페이스: memberMapper.selectMembers() → XML namespace="…MemberMapper" id="selectMembers",
  또는 메서드에 붙은 @Select("…") (쿼리 ID가 클래스.메서드)
- JPA: Spring Data 저장소(JpaRepository<Order, Long>)의 선언 메서드(findByStatus, @Query)와
  상속 메서드(save, findById, deleteById) → 엔티티 테이블
- Java 코드 안의 SQL 문자열: jdbcTemplate.update("INSERT …"), em.createQuery("select o from Order o")
"""

import json
import re
import sqlite3
from collections import defaultdict

from sasu.indexer import entity_tables, map_entity_tables
from sasu.linker_python import sql_kind
from sasu.parsers.mybatis_parser import extract_tables

# 쿼리 ID 문자열을 받는 DAO 메서드 (MyBatis SqlSession, iBatis SqlMapClient, 전자정부 EgovAbstractDAO·Mapper)
DAO_CALLS = {
    "selectOne", "selectList", "selectMap", "selectCursor", "select", "insert", "update", "delete",
    "queryForObject", "queryForList", "queryForMap", "queryForPaginatedList", "list", "listWithPaging",
    "selectByPk", "selectListWithPaging",
}
_JPA_KIND = [
    (re.compile(r"^(find|get|read|query|search|stream|count|exists)"), "select"),
    (re.compile(r"^save"), "merge"),
    (re.compile(r"^(delete|remove)"), "delete"),
]


def jpa_kind(method: str) -> str | None:
    """Spring Data 메서드 이름 → 쿼리 종류. flush 같은 것은 None."""
    return next((kind for pattern, kind in _JPA_KIND if pattern.match(method)), None)


def link_java_data(conn: sqlite3.Connection, edges: set, classes: dict, methods: dict, methods_by_class: dict,
                   statements: dict[str, int], python_classes: set, calls_resolved: list[tuple]) -> None:
    """calls_resolved: (호출한 메서드, 호출 행, 대상 클래스 목록) — linker가 필드·타입으로 푼 결과."""
    # 1. 짧은 ID: 네임스페이스를 떼면 하나뿐인 Mapper 쿼리
    short_ids: dict[str, list[int]] = defaultdict(list)
    for r in conn.execute(
        "SELECT s.id, s.full_id FROM statements s JOIN files f ON f.id = s.file_id WHERE f.kind = 'mapper'"
    ):
        short_ids[r["full_id"].rsplit(".", 1)[-1]].append(r["id"])
    for caller, call, _targets in calls_resolved:
        if call["callee"] not in DAO_CALLS:
            continue
        for arg in json.loads(call["string_args"]):
            if arg not in statements and re.fullmatch(r"\w+", arg) and len(short_ids.get(arg, [])) == 1:
                edges.add(("method", caller, "statement", short_ids[arg][0], None, call["line"], None))

    # 2. 매퍼 인터페이스·@Select·@Query: 쿼리 ID가 "패키지.클래스.메서드"
    java_classes = {cid: c for cid, c in classes.items() if cid not in python_classes}
    for cid, c in java_classes.items():
        for name, mids in methods_by_class[cid].items():
            sid = statements.get(f"{c['fqcn']}.{name}")
            if sid is not None:
                for mid in mids:
                    edges.add(("method", mid, "statement", sid, None, methods[mid]["line_start"], None))

    # 3. JPA 저장소
    repo_entity = {
        int(r["key"].split(":", 1)[1]): r["value"]
        for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'jparepo:%'")
    }
    entities = {c["name"].split(".")[-1]: cid for cid, c in java_classes.items() if c["table_name"]}
    created: dict[tuple[str, str], int] = {}

    def jpa_statement(entity: str, kind: str) -> int | None:
        if entity not in entities:
            return None
        if (entity, kind) not in created:
            c = classes[entities[entity]]
            sid = conn.execute(
                "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
                (c["file_id"], f"JPA {entity}", kind, c["line_start"], f"{entity} 엔티티 ({c['table_name']}) JPA {kind}"),
            ).lastrowid
            conn.execute("INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
                         (sid, c["table_name"].upper()))
            created[(entity, kind)] = sid
        return created[(entity, kind)]

    # 3-1. 저장소에 선언한 메서드: @Query가 없으면 이름으로 (findByStatus → 조회)
    for cid, entity in repo_entity.items():
        for name, mids in methods_by_class[cid].items():
            if f"{classes[cid]['fqcn']}.{name}" in statements:
                continue
            kind = jpa_kind(name)
            sid = jpa_statement(entity, kind) if kind else None
            if sid is not None:
                for mid in mids:
                    edges.add(("method", mid, "statement", sid, None, methods[mid]["line_start"], "메서드 이름으로 추정"))
    # 3-2. 상속 메서드 호출 (orderRepository.save(o), findById(id))
    for caller, call, targets in calls_resolved:
        for target in targets:
            entity = repo_entity.get(target)
            if entity is None or call["callee"] in methods_by_class[target]:
                continue
            kind = jpa_kind(call["callee"])
            sid = jpa_statement(entity, kind) if kind else None
            if sid is not None:
                edges.add(("method", caller, "statement", sid, None, call["line"], None))

    # 4. Java 코드 안의 SQL·JPQL 문자열
    known = entity_tables(conn)
    for lit in conn.execute(
        """SELECT s.method_id, s.value, s.line, f.path, c.file_id FROM string_literals s
           JOIN methods m ON m.id = s.method_id JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'java'"""
    ).fetchall():
        kind = sql_kind(lit["value"])
        if kind is None or lit["value"] in statements:
            continue
        names = extract_tables(lit["value"])
        tables = map_entity_tables(names, known) if any(n in known for n in names) else names
        sid = conn.execute(
            "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
            (lit["file_id"], f"SQL {lit['path']}:{lit['line']}", kind, lit["line"], lit["value"]),
        ).lastrowid
        conn.executemany("INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
                         [(sid, t) for t in tables])
        edges.add(("method", lit["method_id"], "statement", sid, None, lit["line"], None))

