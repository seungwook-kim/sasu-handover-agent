"""분석 대상 저장소를 읽어 SQLite에 저장한다."""

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sasu import db
from sasu.linker import link
from sasu.parsers.java_parser import parse_java
from sasu.parsers.mybatis_parser import parse_mapper

_DIALECT_SUFFIX = re.compile(r"_SQL_([A-Za-z]+)\.xml$", re.IGNORECASE)
_SKIP_DIRS = {".git", "target", "build", "node_modules", "test"}


@dataclass
class IndexStats:
    java_files: int = 0
    classes: int = 0
    methods: int = 0
    mapper_files: int = 0
    statements: int = 0
    jsp_files: int = 0
    urls: int = 0
    edges: int = 0


def index_repository(conn: sqlite3.Connection, repo: Path, dialect: str) -> IndexStats:
    repo = repo.resolve()
    stats = IndexStats()
    db.reset(conn)
    for path in _walk(repo):
        rel = path.relative_to(repo).as_posix()
        suffix = path.suffix.lower()
        if suffix == ".java":
            _index_java(conn, path, rel, stats)
        elif suffix == ".xml" and _dialect_matches(path.name, dialect):
            _index_mapper(conn, path, rel, stats)
        elif suffix == ".jsp":
            conn.execute("INSERT INTO files (path, kind) VALUES (?, 'jsp')", (rel,))
            stats.jsp_files += 1

    link_stats = link(conn)
    stats.urls = link_stats.urls
    stats.edges = link_stats.edges
    _write_meta(conn, repo, dialect)
    conn.commit()
    return stats


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
    file_id = conn.execute(
        "INSERT INTO files (path, kind, header_comment) VALUES (?, 'java', ?)",
        (rel, parsed.header_comment),
    ).lastrowid
    stats.java_files += 1
    for cls in parsed.classes:
        class_id = conn.execute(
            """INSERT INTO classes (file_id, fqcn, name, kind, stereotype, bean_name, superclass,
                                    interfaces, mapping_paths, line_start, line_end, doc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_id, cls.fqcn, cls.name, cls.kind, cls.stereotype, cls.bean_name,
                cls.superclass, json.dumps(cls.interfaces), json.dumps(cls.mapping_paths),
                cls.line_start, cls.line_end, cls.doc,
            ),
        ).lastrowid
        stats.classes += 1
        for f in cls.fields:
            conn.execute(
                "INSERT INTO fields (class_id, name, type_name, bean_name, line) VALUES (?, ?, ?, ?, ?)",
                (class_id, f.name, f.type_name, f.bean_name, f.line),
            )
        for m in cls.methods:
            method_id = conn.execute(
                """INSERT INTO methods (class_id, name, line_start, line_end, params, doc,
                                        complexity, returned_strings, mapping_paths)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    class_id, m.name, m.line_start, m.line_end, json.dumps(m.parameters),
                    m.doc, m.complexity, json.dumps(m.returned_strings, ensure_ascii=False),
                    json.dumps(m.mapping_paths),
                ),
            ).lastrowid
            stats.methods += 1
            conn.executemany(
                """INSERT INTO calls (method_id, receiver, callee, string_args, string_prefixes, line)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        method_id, c.receiver, c.method,
                        json.dumps(c.string_args, ensure_ascii=False),
                        json.dumps(c.string_prefixes, ensure_ascii=False),
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


def _index_mapper(conn: sqlite3.Connection, path: Path, rel: str, stats: IndexStats) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "<mapper" not in text:
        return
    parsed = parse_mapper(text)
    if parsed is None:
        return
    file_id = conn.execute(
        "INSERT INTO files (path, kind, header_comment) VALUES (?, 'mapper', ?)",
        (rel, parsed.header_comment),
    ).lastrowid
    stats.mapper_files += 1
    for s in parsed.statements:
        stmt_id = conn.execute(
            "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
            (file_id, f"{parsed.namespace}.{s.id}", s.kind, s.line, s.sql),
        ).lastrowid
        conn.executemany(
            "INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
            [(stmt_id, t) for t in s.tables],
        )
        stats.statements += 1


def _write_meta(conn: sqlite3.Connection, repo: Path, dialect: str) -> None:
    head = ""
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    values = {
        "repo_path": str(repo),
        "repo_head": head,
        "sql_dialect": dialect,
        "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", values.items())
