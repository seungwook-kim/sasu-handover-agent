"""분석 결과를 담는 SQLite 저장소.

서버 없는 파일 하나라 노트북에서도 가볍고, 폐쇄망에 반입하기도 쉽다.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,      -- 분석 대상 저장소 기준 상대 경로 (/ 구분)
    kind TEXT NOT NULL,             -- java, python, mapper, jsp, template, config, web
    header_comment TEXT,
    module TEXT,                    -- Python 모듈 경로 (app.api.routes.users)
    imports TEXT                    -- JSON 배열. Python: "full.path as alias"
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    fqcn TEXT NOT NULL,
    name TEXT NOT NULL,             -- 중첩 클래스는 Outer.Inner
    kind TEXT NOT NULL,             -- class, interface, enum, module(Python 모듈의 최상위 함수 묶음)
    stereotype TEXT,                -- Controller, Service, Repository, Component
    bean_name TEXT,
    superclass TEXT,
    interfaces TEXT NOT NULL,       -- JSON 배열
    mapping_paths TEXT NOT NULL,    -- JSON 배열. 클래스 수준 @RequestMapping (URL 접두어)
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    doc TEXT,
    table_name TEXT                 -- ORM 모델이 매핑하는 테이블 (Python)
);
CREATE INDEX IF NOT EXISTS idx_classes_name ON classes(name);
CREATE INDEX IF NOT EXISTS idx_classes_bean ON classes(bean_name);

CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    name TEXT NOT NULL,
    type_name TEXT NOT NULL,
    bean_name TEXT,
    line INTEGER NOT NULL,
    doc TEXT                        -- 필드 주석 (게시물 아이디 등)
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
    mapping_paths TEXT NOT NULL,    -- JSON 배열. 메서드 수준 URL 매핑
    routes TEXT NOT NULL DEFAULT '[]', -- JSON 배열 [경로, 데코레이터 객체]. Python @router.get("/x")
    annotations TEXT NOT NULL,      -- JSON 배열. 애노테이션 이름
    body_hash TEXT,                 -- 식별자·리터럴을 지운 본문 토큰의 해시 (중복 코드 찾기)
    body_tokens INTEGER NOT NULL    -- 본문 토큰 수
);
CREATE INDEX IF NOT EXISTS idx_methods_hash ON methods(body_hash);
CREATE INDEX IF NOT EXISTS idx_methods_class ON methods(class_id, name);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    method_id INTEGER NOT NULL REFERENCES methods(id),
    receiver TEXT,
    callee TEXT NOT NULL,
    string_args TEXT NOT NULL,      -- JSON 배열
    string_prefixes TEXT NOT NULL,  -- JSON 배열. "문자열" + 변수 인자의 앞부분 (동적 쿼리 ID)
    name_args TEXT NOT NULL DEFAULT '[]',    -- JSON 배열. 이름으로 넘긴 인자 (User, users.router)
    keyword_args TEXT NOT NULL DEFAULT '{}', -- JSON 객체. 키워드 인자 원문 (prefix="/api")
    line INTEGER NOT NULL
);

-- Django urls.py의 path("posts/", views.post_list)
CREATE TABLE IF NOT EXISTS url_patterns (
    file_id INTEGER NOT NULL REFERENCES files(id),
    route TEXT NOT NULL,
    target TEXT NOT NULL,
    line INTEGER NOT NULL,
    is_include INTEGER NOT NULL
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

-- 해석이 끝난 연결. dst 종류: method, statement, view, url, external(저장소 밖 빈)
CREATE TABLE IF NOT EXISTS edges (
    src_kind TEXT NOT NULL,
    src_id INTEGER NOT NULL,
    dst_kind TEXT NOT NULL,
    dst_id INTEGER,                 -- view·url·external은 NULL일 수 있음
    dst_name TEXT,                  -- view 경로, url, 외부 빈 호출
    line INTEGER,
    note TEXT                       -- 동적 쿼리 ID, 외부 빈 이름 등 보충 설명
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_kind, src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_kind, dst_id);

CREATE TABLE IF NOT EXISTS condition_literals (
    method_id INTEGER NOT NULL REFERENCES methods(id),
    value TEXT NOT NULL,
    line INTEGER NOT NULL,
    source TEXT NOT NULL            -- 조건식 원문
);
CREATE INDEX IF NOT EXISTS idx_condition_literals ON condition_literals(value);

CREATE TABLE IF NOT EXISTS notes (
    method_id INTEGER NOT NULL REFERENCES methods(id),
    tag TEXT NOT NULL,              -- TODO, FIXME, 임시, 주의 ...
    text TEXT NOT NULL,
    line INTEGER NOT NULL
);

-- JSP·JS·설정 파일이 가리키는 URL, JSP, include, 메서드 이름
CREATE TABLE IF NOT EXISTS refs (
    file_id INTEGER NOT NULL REFERENCES files(id),
    kind TEXT NOT NULL,             -- url, jsp, include, method
    value TEXT NOT NULL,
    line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refs ON refs(kind, value);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 인수인계 질문과 답변. 사람이 입력한 데이터라 다시 색인해도, 스키마가 바뀌어도 지우지 않는다.
-- 코드 id는 색인할 때마다 바뀌므로 key(종류·파일·메서드·세부)로 같은 질문을 알아본다.
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    priority REAL NOT NULL,
    question TEXT NOT NULL,
    why TEXT NOT NULL,              -- 이 질문을 하는 이유 (코드 근거)
    file_path TEXT,
    line INTEGER,
    target TEXT,                    -- Class.method 또는 쿼리 ID
    code TEXT,                      -- 해당 줄 코드
    status TEXT NOT NULL DEFAULT 'open',   -- open, answered, skipped
    stale INTEGER NOT NULL DEFAULT 0,      -- 코드가 바뀌어 이번 분석에서 다시 나오지 않음
    answer TEXT,
    answered_by TEXT,
    answered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 사람이 입력한 데이터. 색인 초기화·스키마 변경 때도 남긴다.
PRESERVED_TABLES = {"questions"}


# 스키마를 바꾸면 올린다. 버전이 다른 분석 파일은 지우고 새로 만든다 (다시 index 하면 됨).
SCHEMA_VERSION = 7


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _load_vector_extension(conn)
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        if "vec_methods" in tables:
            conn.execute("DROP TABLE IF EXISTS vec_methods")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            # sqlite_*는 SQLite 내부 테이블, vec_methods_*는 벡터 색인을 지울 때 함께 사라지는 보조 테이블
            if table not in PRESERVED_TABLES and not table.startswith(("sqlite_", "vec_methods")):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _load_vector_extension(conn: sqlite3.Connection) -> None:
    """벡터 색인(vec_methods, sqlite-vec 가상 테이블)을 읽고 지우려면 확장이 있어야 한다."""
    from sasu.rag.vectors import load_extension

    load_extension(conn)


def reset(conn: sqlite3.Connection) -> None:
    """다시 색인하기 전에 이전 분석 결과를 지운다. 메서드 id가 바뀌므로 벡터 색인도 지운다."""
    conn.execute("DROP TABLE IF EXISTS vec_methods")
    conn.execute("DELETE FROM meta WHERE key = 'embed_model'")
    tables = [
        "condition_literals", "notes", "url_patterns",
        "refs", "edges", "statement_tables", "statements", "url_mappings", "string_literals",
        "empty_catches", "calls", "methods", "fields", "classes", "files", "meta",
    ]
    for table in tables:
        conn.execute(f"DELETE FROM {table}")
