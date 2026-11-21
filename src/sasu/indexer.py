"""분석 대상 저장소를 읽어 SQLite에 저장한다."""

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sasu import db
from sasu.linker import link
from sasu.parsers.build_info import read_build_info
from sasu.parsers.java_parser import JavaFile, parse_java
from sasu.parsers.mybatis_parser import extract_tables, parse_mapper
from sasu.parsers.python_parser import parse_python, route_paths
from sasu.parsers.web_refs import extract_refs, extract_template_refs

_DIALECT_SUFFIX = re.compile(r"_SQL_([A-Za-z]+)\.xml$", re.IGNORECASE)
_SKIP_DIRS = {
    ".git", "target", "build", "node_modules", "test", "tests", "venv", ".venv", "env", "__pycache__",
    "migrations", "alembic", "site-packages", "dist", ".tox", ".mypy_cache", ".pytest_cache",
}
_TEMPLATE_SUFFIXES = (".html", ".htm", ".jinja", ".jinja2", ".j2")


@dataclass
class IndexStats:
    java_files: int = 0
    python_files: int = 0
    classes: int = 0
    methods: int = 0
    mapper_files: int = 0
    statements: int = 0
    jsp_files: int = 0
    urls: int = 0
    edges: int = 0
    annotated_sql: list = field(default_factory=list)  # @Select·@Query (모든 파일을 읽은 뒤 저장)
    skipped: list = field(default_factory=list)  # 읽지 못한 파일 (권한·깨진 링크 등). 분석은 계속한다


def index_repository(conn: sqlite3.Connection, repo: Path, dialect: str) -> IndexStats:
    repo = repo.resolve()
    stats = IndexStats()
    db.reset(conn)
    walk_root = _long_path(repo)  # Windows 260자 넘는 경로도 읽는다 (예: ckeditor/samples/old/.../swfobject.js)
    roots = python_roots(walk_root)
    for path in _walk(walk_root):
        rel = path.relative_to(walk_root).as_posix()
        if f"/{rel}".find("/src/site/") >= 0:
            continue  # Maven 사이트 문서: 설명용 예제 Mapper XML이 들어 있어 실제 코드로 착각한다
        try:
            _index_file(conn, path, rel, stats, roots, dialect)
        except OSError:  # 파일 하나를 못 읽어도 전체 분석은 멈추지 않는다
            stats.skipped.append(rel)

    stats.statements += _store_annotated_sql(conn, stats.annotated_sql)
    link_stats = link(conn)
    stats.urls = link_stats.urls
    stats.edges = link_stats.edges
    _write_meta(conn, repo, dialect)
    conn.commit()
    return stats


_WIN_LONG = "\\\\?\\"  # \\?\ : Windows 긴 경로 접두어


def _long_path(path: Path) -> Path:
    r"""Windows에서 \\?\ 접두어를 붙여 MAX_PATH(260자) 제한을 피한다. 다른 OS는 그대로."""
    text = str(path)
    if os.name != "nt" or text.startswith(_WIN_LONG):
        return path
    if text.startswith("\\\\"):  # UNC (\\server\share)
        return Path(_WIN_LONG + "UNC\\" + text[2:])
    return Path(_WIN_LONG + text)


def _index_file(conn: sqlite3.Connection, path: Path, rel: str, stats: IndexStats, roots: list[str],
                dialect: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".java":
        _index_java(conn, path, rel, stats)
    elif suffix == ".py":
        _index_python(conn, path, rel, stats, roots)
    elif suffix in _TEMPLATE_SUFFIXES and "/templates/" in f"/{rel}":
        _index_refs(conn, path, rel, kind="template")
    elif suffix == ".xml":
        if not _index_mapper(conn, path, rel, stats, dialect):
            _index_refs(conn, path, rel, kind="config", methods=True)
    elif suffix == ".jsp":
        _index_refs(conn, path, rel, kind="jsp")
        stats.jsp_files += 1
    elif suffix in (".js", ".html", ".htm"):
        _index_refs(conn, path, rel, kind="web")
    elif suffix == ".properties":
        _index_refs(conn, path, rel, kind="config", methods=True)


def _walk(repo: Path):
    stack = [repo]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), reverse=True):
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            else:
                yield entry


def _dialect_matches(filename: str, dialect: str) -> bool:
    m = _DIALECT_SUFFIX.search(filename)
    return m is None or m.group(1).lower() == dialect.lower()


def _index_java(conn: sqlite3.Connection, path: Path, rel: str, stats: IndexStats) -> None:
    parsed = parse_java(path.read_bytes())
    stats.java_files += 1
    _store_code(conn, parsed, rel, "java", None, stats)


def _index_python(conn: sqlite3.Connection, path: Path, rel: str, stats: IndexStats, roots: list[str]) -> None:
    module = python_module(rel, roots)
    parsed = parse_python(path.read_bytes(), module)
    stats.python_files += 1
    _store_code(conn, parsed, rel, "python", module, stats)


_PYTHON_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "manage.py")


def python_roots(repo: Path) -> list[str]:
    """pyproject.toml·setup.py·manage.py가 있는 폴더 = import가 시작되는 소스 루트 (backend/ 등). 깊은 것부터."""
    roots = set()
    for path in _walk(repo):
        if path.name in _PYTHON_ROOT_MARKERS:
            rel = path.parent.relative_to(repo).as_posix()
            roots.add("" if rel == "." else rel)
    return sorted(roots, key=len, reverse=True)


def python_module(rel: str, roots: list[str] | None = None) -> str:
    """backend/app/api/routes/users.py (루트 backend) → app.api.routes.users, pkg/__init__.py → pkg.
    소스 루트와 맨 앞 src/는 뗀다. import 문의 경로와 맞추기 위해서다."""
    for root in roots or []:
        if root and rel.startswith(root + "/"):
            rel = rel[len(root) + 1:]
            break
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _store_code(conn: sqlite3.Connection, parsed: JavaFile, rel: str, kind: str, module: str | None,
                stats: IndexStats) -> None:
    """Java·Python 파서가 만든 공통 모델을 저장한다."""
    file_id = conn.execute(
        "INSERT INTO files (path, kind, header_comment, module, imports) VALUES (?, ?, ?, ?, ?)",
        (rel, kind, parsed.header_comment, module, json.dumps(parsed.imports)),
    ).lastrowid
    conn.executemany(
        "INSERT INTO url_patterns (file_id, route, target, line, is_include) VALUES (?, ?, ?, ?, ?)",
        [(file_id, u.route, u.target, u.line, int(u.is_include)) for u in parsed.url_patterns],
    )
    for cls in parsed.classes:
        class_id = conn.execute(
            """INSERT INTO classes (file_id, fqcn, name, kind, stereotype, bean_name, superclass,
                                    interfaces, mapping_paths, line_start, line_end, doc, table_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_id, cls.fqcn, cls.name, cls.kind, cls.stereotype, cls.bean_name,
                cls.superclass, json.dumps(cls.interfaces), json.dumps(cls.mapping_paths),
                cls.line_start, cls.line_end, cls.doc, cls.table_name,
            ),
        ).lastrowid
        stats.classes += 1
        for f in cls.fields:
            conn.execute(
                "INSERT INTO fields (class_id, name, type_name, bean_name, line, doc) VALUES (?, ?, ?, ?, ?, ?)",
                (class_id, f.name, f.type_name, f.bean_name, f.line, f.doc),
            )
        if cls.repository_entity:
            # Spring Data 저장소가 다루는 엔티티 (linker가 save·findById 같은 상속 메서드를 테이블로 잇는다)
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (f"jparepo:{class_id}", cls.repository_entity))
        if kind == "java":
            stats.annotated_sql.extend(_annotated_sql(cls, file_id))
        if cls.py_info:
            # Django 모델 Meta·앱 설정 label·get_class로 불러 둔 뷰 (linker_python이 모델 상속·URL include에 쓴다)
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                         (f"pyclass:{class_id}", json.dumps(cls.py_info, ensure_ascii=False)))
        if kind == "python" and cls.kind == "module":
            # 모듈 수준 router = APIRouter(prefix=...)는 URL 접두어 계산에 쓴다 (인자가 없어도 저장)
            for f in cls.fields:
                if f.type_name:
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?)",
                        (f"pyassign:{file_id}:{f.name}", json.dumps({"type": f.type_name, "args": f.call_args},
                                                                    ensure_ascii=False)),
                    )
        for m in cls.methods:
            routes = route_paths(m) if kind == "python" else []
            method_id = conn.execute(
                """INSERT INTO methods (class_id, name, line_start, line_end, params, doc,
                                        complexity, returned_strings, mapping_paths, routes, annotations,
                                        body_hash, body_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    class_id, m.name, m.line_start, m.line_end, json.dumps(m.parameters),
                    m.doc, m.complexity, json.dumps(m.returned_strings, ensure_ascii=False),
                    json.dumps([] if kind == "python" else m.mapping_paths), json.dumps(routes),
                    json.dumps([a.name for a in m.annotations]), m.body_hash, m.body_tokens,
                ),
            ).lastrowid
            stats.methods += 1
            conn.executemany(
                """INSERT INTO calls (method_id, receiver, callee, string_args, string_prefixes, name_args,
                                      keyword_args, line)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        method_id, c.receiver, c.method,
                        json.dumps(c.string_args, ensure_ascii=False),
                        json.dumps(c.string_prefixes, ensure_ascii=False),
                        json.dumps(c.name_args), json.dumps(c.keyword_args, ensure_ascii=False),
                        c.line,
                    )
                    for c in m.calls
                ],
            )
            conn.executemany(
                "INSERT INTO empty_catches (method_id, exception_type, line) VALUES (?, ?, ?)",
                [(method_id, e.exception_type, e.line) for e in m.empty_catches],
            )
            conn.executemany(
                "INSERT INTO string_literals (method_id, value, line) VALUES (?, ?, ?)",
                [(method_id, value, line) for value, line in m.string_literals],
            )
            conn.executemany(
                "INSERT INTO condition_literals (method_id, value, line, source) VALUES (?, ?, ?, ?)",
                [(method_id, c.value, c.line, c.source) for c in m.condition_literals],
            )
            conn.executemany(
                "INSERT INTO notes (method_id, tag, text, line) VALUES (?, ?, ?, ?)",
                [(method_id, n.tag, n.text, n.line) for n in m.notes],
            )


_SQL_ANNOTATIONS = {"Select": "select", "Insert": "insert", "Update": "update", "Delete": "delete", "Query": None}


def _annotated_sql(cls, file_id: int) -> list[tuple]:
    """MyBatis @Select("…")·JPA @Query("…")처럼 메서드에 붙은 SQL. 쿼리 ID는 클래스.메서드.
    돌려주는 값: (file_id, full_id, kind, line, sql, jpql 여부)."""
    result = []
    for m in cls.methods:
        for ann in m.annotations:
            if ann.name not in _SQL_ANNOTATIONS or not ann.args.get("value"):
                continue
            sql = " ".join(ann.args["value"]).strip()
            kind = _SQL_ANNOTATIONS[ann.name]
            if kind is None:  # @Query: 본문 첫 단어로
                word = sql.split(None, 1)[0].lower() if sql else ""
                kind = word if word in ("select", "insert", "update", "delete") else "select"
            jpql = ann.name == "Query" and (ann.first("nativeQuery") or "").lower() != "true"
            result.append((file_id, f"{cls.fqcn}.{m.name}", kind, m.line_start, sql, jpql))
    return result


def _store_annotated_sql(conn: sqlite3.Connection, pending: list[tuple]) -> int:
    """모든 파일을 읽은 뒤에 저장한다. JPQL의 엔티티 이름(Order)을 테이블(TB_ORDER)로 바꾸려면 엔티티를 다 알아야 한다."""
    entities = entity_tables(conn)
    for file_id, full_id, kind, line, sql, jpql in pending:
        sid = conn.execute(
            "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
            (file_id, full_id, kind, line, sql),
        ).lastrowid
        tables = map_entity_tables(extract_tables(sql), entities) if jpql else extract_tables(sql)
        conn.executemany("INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
                         [(sid, t) for t in tables])
    return len(pending)


def entity_tables(conn: sqlite3.Connection) -> dict[str, str]:
    """JPA 엔티티 이름(대문자) → 테이블."""
    return {
        r["name"].split(".")[-1].upper(): r["table_name"].upper()
        for r in conn.execute(
            """SELECT c.name, c.table_name FROM classes c JOIN files f ON f.id = c.file_id
               WHERE f.kind = 'java' AND c.table_name IS NOT NULL"""
        )
    }


def map_entity_tables(names: list[str], entities: dict[str, str]) -> list[str]:
    """JPQL: FROM Order o JOIN o.items i → TB_ORDER. 별칭 경로(O.ITEMS)는 테이블이 아니라서 뺀다."""
    result = []
    for name in names:
        if name in entities:
            name = entities[name]
        elif "." in name and len(name.split(".")[0]) <= 3:
            continue
        if name not in result:
            result.append(name)
    return result


_MAX_REF_FILE_BYTES = 2_000_000


def _index_refs(conn: sqlite3.Connection, path: Path, rel: str, kind: str, methods: bool = False) -> None:
    """JSP·템플릿은 항상 저장하고(화면 연결용), 나머지는 참조가 있을 때만 저장한다."""
    text = ""
    if path.stat().st_size <= _MAX_REF_FILE_BYTES:
        text = path.read_text(encoding="utf-8", errors="replace")
    refs = extract_refs(text, methods=methods) if text else []
    if kind == "template" and text:
        refs += extract_template_refs(text)  # Django·Jinja {% url %}·{% include %}·{{ obj.attr }}
    if not refs and kind not in ("jsp", "template"):
        return
    file_id = conn.execute("INSERT INTO files (path, kind) VALUES (?, ?)", (rel, kind)).lastrowid
    conn.executemany(
        "INSERT INTO refs (file_id, kind, value, line) VALUES (?, ?, ?, ?)",
        [(file_id, r.kind, r.value, r.line) for r in refs],
    )


def _index_mapper(
    conn: sqlite3.Connection, path: Path, rel: str, stats: IndexStats, dialect: str
) -> bool:
    """Mapper XML이면 저장하고 True. 다른 DB용 Mapper도 True(건너뜀). Mapper가 아니면 False."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if "<mapper" not in text and "<sqlMap" not in text:
        return False
    if not _dialect_matches(path.name, dialect):
        return True
    parsed = parse_mapper(text)
    if parsed is None:
        return False
    file_id = conn.execute(
        "INSERT INTO files (path, kind, header_comment) VALUES (?, 'mapper', ?)",
        (rel, parsed.header_comment),
    ).lastrowid
    stats.mapper_files += 1
    for s in parsed.statements:
        # MyBatis 규칙: id가 이미 "네임스페이스."로 시작하면 네임스페이스를 다시 붙이지 않는다.
        # iBatis는 네임스페이스가 없을 수 있다 (그때는 id만으로 부른다)
        if not parsed.namespace or s.id.startswith(parsed.namespace + "."):
            full_id = s.id
        else:
            full_id = f"{parsed.namespace}.{s.id}"
        stmt_id = conn.execute(
            "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
            (file_id, full_id, s.kind, s.line, s.sql),
        ).lastrowid
        conn.executemany(
            "INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
            [(stmt_id, t) for t in s.tables],
        )
        stats.statements += 1
    return True


def _write_meta(conn: sqlite3.Connection, repo: Path, dialect: str) -> None:
    head = ""
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    build = read_build_info(repo)
    values = {
        "repo_path": str(repo),
        "repo_head": head,
        "sql_dialect": dialect,
        "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "build_info": json.dumps(build.to_dict() if build else None, ensure_ascii=False),
    }
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", values.items())
