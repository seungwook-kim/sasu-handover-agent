"""분석 결과를 담는 SQLite 저장소.

서버 없는 파일 하나라 노트북에서도 가볍고, 폐쇄망에 반입하기도 쉽다.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,      -- 분석 대상 저장소 기준 상대 경로 (/ 구분)
    kind TEXT NOT NULL,             -- java, mapper, jsp
    header_comment TEXT
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    fqcn TEXT NOT NULL,
    name TEXT NOT NULL,             -- 중첩 클래스는 Outer.Inner
    kind TEXT NOT NULL,             -- class, interface, enum
    stereotype TEXT,                -- Controller, Service, Repository, Component
    bean_name TEXT,
    superclass TEXT,
    interfaces TEXT NOT NULL,       -- JSON 배열
    mapping_paths TEXT NOT NULL,    -- JSON 배열. 클래스 수준 @RequestMapping (URL 접두어)
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    doc TEXT
);
CREATE INDEX IF NOT EXISTS idx_classes_name ON classes(name);
CREATE INDEX IF NOT EXISTS idx_classes_bean ON classes(bean_name);

CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    name TEXT NOT NULL,
    type_name TEXT NOT NULL,
    bean_name TEXT,
    line INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS methods (
    id INTEGER PRIMARY KEY,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    name TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    params TEXT NOT NULL,           -- JSON 배열 (타입)
    doc TEXT,
    complexity INTEGER NOT NULL,
    returned_strings TEXT NOT NULL, -- JSON 배열
    mapping_paths TEXT NOT NULL     -- JSON 배열. 메서드 수준 URL 매핑
);
CREATE INDEX IF NOT EXISTS idx_methods_class ON methods(class_id, name);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    method_id INTEGER NOT NULL REFERENCES methods(id),
    receiver TEXT,
    callee TEXT NOT NULL,
    string_args TEXT NOT NULL,      -- JSON 배열
    line INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS empty_catches (
    id INTEGER PRIMARY KEY,
    method_id INTEGER NOT NULL REFERENCES methods(id),
    exception_type TEXT NOT NULL,
    line INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS string_literals (
    id INTEGER PRIMARY KEY,
    method_id INTEGER NOT NULL REFERENCES methods(id),
    value TEXT NOT NULL,
    line INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS url_mappings (
    url TEXT NOT NULL,
    method_id INTEGER NOT NULL REFERENCES methods(id)
);
CREATE INDEX IF NOT EXISTS idx_url ON url_mappings(url);

CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    full_id TEXT NOT NULL,          -- 네임스페이스.쿼리ID
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    sql TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_statements_full ON statements(full_id);

CREATE TABLE IF NOT EXISTS statement_tables (
    statement_id INTEGER NOT NULL REFERENCES statements(id),
    table_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_statement_tables ON statement_tables(table_name);

-- 해석이 끝난 연결. src/dst 종류: method, statement, view, url
CREATE TABLE IF NOT EXISTS edges (
    src_kind TEXT NOT NULL,
    src_id INTEGER NOT NULL,
    dst_kind TEXT NOT NULL,
    dst_id INTEGER,                 -- view·url은 NULL
    dst_name TEXT,                  -- view 경로, url
    line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_kind, src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_kind, dst_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def reset(conn: sqlite3.Connection) -> None:
    """다시 색인하기 전에 이전 분석 결과를 지운다."""
    tables = [
        "edges", "statement_tables", "statements", "url_mappings", "string_literals",
        "empty_catches", "calls", "methods", "fields", "classes", "files", "meta",
    ]
    for table in tables:
        conn.execute(f"DELETE FROM {table}")
