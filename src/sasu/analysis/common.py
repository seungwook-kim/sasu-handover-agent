"""공통화 진단: 여러 모듈이 함께 쓰는 공통 코드, 복사해 붙인 코드, 중복 SQL을 찾는다."""

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

# 이 정도 길이는 넘어야 "복사해 붙였다"고 볼 만하다 (대략 10줄)
MIN_CLONE_TOKENS = 60
MIN_SQL_LENGTH = 80


@dataclass
class SharedComponent:
    class_name: str
    module: str
    stereotype: str | None
    used_by_modules: list[str]
    call_sites: int


@dataclass
class CloneGroup:
    tokens: int
    methods: list[tuple[str, str]]  # (Class.method, 파일:줄)
    note: str | None = None  # 부모 클래스에 같은 코드가 이미 있는 경우 등


@dataclass
class DuplicateSql:
    sql_preview: str
    statements: list[tuple[str, str]]  # (쿼리 ID, 파일:줄)


@dataclass
class CommonReport:
    shared: list[SharedComponent] = field(default_factory=list)
    clones: list[CloneGroup] = field(default_factory=list)
    duplicate_sql: list[DuplicateSql] = field(default_factory=list)


def module_of(package: str, root: str) -> str:
    """egovframework.com.cop.bbs.service.impl → cop.bbs (공통 뿌리 다음 두 단계)."""
    rest = package[len(root):].lstrip(".") if root and package.startswith(root) else package
    parts = [p for p in rest.split(".") if p]
    return ".".join(parts[:2]) or "(root)"


def package_root(packages: list[str]) -> str:
    """모든 패키지가 공유하는 가장 긴 앞부분 (egovframework.com)."""
    split = [p.split(".") for p in packages if p]
    if not split:
        return ""
    common: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return ".".join(common)


def analyze_common(conn: sqlite3.Connection, top: int = 20) -> CommonReport:
    report = CommonReport()
    classes = {
        r["id"]: r
        for r in conn.execute(
            """SELECT c.id, c.name, c.fqcn, c.stereotype, f.path
               FROM classes c JOIN files f ON f.id = c.file_id"""
        )
    }
    packages = {cid: c["fqcn"].rsplit("." + c["name"], 1)[0] for cid, c in classes.items()}
    root = package_root(list(set(packages.values())))
    modules = {cid: module_of(pkg, root) for cid, pkg in packages.items()}

    # 1. 여러 모듈이 부르는 클래스
    method_class = {r["id"]: r["class_id"] for r in conn.execute("SELECT id, class_id FROM methods")}
    users: dict[int, set[str]] = defaultdict(set)
    sites: dict[int, int] = defaultdict(int)
    for e in conn.execute("SELECT src_id, dst_id FROM edges WHERE src_kind = 'method' AND dst_kind = 'method'"):
        src_class, dst_class = method_class[e["src_id"]], method_class[e["dst_id"]]
        if modules[src_class] != modules[dst_class]:
            users[dst_class].add(modules[src_class])
            sites[dst_class] += 1
    ranked = sorted(users, key=lambda cid: (-len(users[cid]), -sites[cid]))
    for cid in ranked[:top]:
        c = classes[cid]
        report.shared.append(
            SharedComponent(
                class_name=c["name"],
                module=modules[cid],
                stereotype=c["stereotype"],
                used_by_modules=sorted(users[cid]),
                call_sites=sites[cid],
            )
        )

    # 2. 식별자만 다른 복사-붙여넣기 메서드
    groups: dict[str, list] = defaultdict(list)
    for r in conn.execute(
        """SELECT m.body_hash, m.body_tokens, m.name, m.line_start, c.name AS class_name,
                  c.superclass, f.path
           FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE m.body_tokens >= ? AND m.body_hash IS NOT NULL""",
        (MIN_CLONE_TOKENS,),
    ):
        groups[r["body_hash"]].append(r)
    clone_groups = [g for g in groups.values() if len(g) >= 2]
    clone_groups.sort(key=lambda g: -(len(g) * g[0]["body_tokens"]))
    parent_names = {r["superclass"] for r in conn.execute("SELECT DISTINCT superclass FROM classes") if r["superclass"]}
    for g in clone_groups[:top]:
        report.clones.append(
            CloneGroup(
                tokens=g[0]["body_tokens"],
                methods=[(f"{r['class_name']}.{r['name']}", f"{r['path']}:{r['line_start']}") for r in g],
                note=_parent_note(g, parent_names),
            )
        )

    # 3. 같은 SQL이 다른 ID로 여러 번
    by_sql: dict[str, list] = defaultdict(list)
    for r in conn.execute(
        "SELECT s.full_id, s.line, s.sql, f.path FROM statements s JOIN files f ON f.id = s.file_id"
    ):
        normalized = re.sub(r"\s+", " ", r["sql"]).strip().upper()
        if len(normalized) >= MIN_SQL_LENGTH:
            by_sql[normalized].append(r)
    dup = [g for g in by_sql.values() if len(g) >= 2]
    dup.sort(key=lambda g: -len(g))
    for g in dup[:top]:
        preview = re.sub(r"\s+", " ", g[0]["sql"]).strip()
        report.duplicate_sql.append(
            DuplicateSql(
                sql_preview=preview[:120] + ("…" if len(preview) > 120 else ""),
                statements=[(r["full_id"], f"{r['path']}:{r['line']}") for r in g],
            )
        )
    return report


def _parent_note(group: list, parent_names: set[str]) -> str | None:
    """묶음 안에 누군가 상속하는 공통 부모 클래스가 있으면, 나머지는 상속 대신 복사한 것이다."""
    parents_in_group = [r for r in group if r["class_name"].split(".")[-1] in parent_names]
    if not parents_in_group:
        return None
    parent = parents_in_group[0]["class_name"].split(".")[-1]
    others = [r for r in group if r["class_name"].split(".")[-1] != parent]
    overriding = sum(1 for r in others if r["superclass"] == parent)
    copied = len(others) - overriding
    parts = [f"공통 부모 클래스 {parent}에 같은 코드가 있음"]
    if copied:
        parts.append(f"상속하지 않고 복사한 곳 {copied}")
    if overriding:
        parts.append(f"상속하면서 다시 구현한 곳 {overriding}")
    return ", ".join(parts)


def clone_stats(conn: sqlite3.Connection) -> tuple[int, int]:
    """(중복 그룹 수, 중복에 속한 메서드 수)."""
    rows = conn.execute(
        """SELECT COUNT(*) AS n FROM methods WHERE body_tokens >= ? AND body_hash IS NOT NULL
           GROUP BY body_hash HAVING COUNT(*) >= 2""",
        (MIN_CLONE_TOKENS,),
    ).fetchall()
    return len(rows), sum(r["n"] for r in rows)
