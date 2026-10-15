"""문서 넣기·목록·지우기. CLI(sasu docs)·웹(/api/docs)·평가가 함께 쓴다.

넣기: 읽기 → 비밀 가리기 → 조각 → 저장 → 코드와 잇기. 원본 파일은 복사하지 않는다.
같은 이름을 다시 넣으면 새 판으로 바꾸고, 내용(해시)까지 같으면 건너뛴다.
"""

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sasu.docs.chunker import document_date, make_chunks
from sasu.docs.linking import CodeNames, link_chunks
from sasu.docs.readers import SUPPORTED, DocReadError, read_document
from sasu.docs.redact import redact


@dataclass
class AddResult:
    name: str
    status: str  # added, replaced, unchanged, failed
    format: str = ""
    doc_id: int | None = None
    chunks: int = 0
    redactions: dict[str, int] = field(default_factory=dict)  # 종류별 가린 개수
    links: int = 0  # 코드에 있는 이름과 이은 수
    links_by_kind: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # 문서에는 있는데 코드에 없는 URL·테이블 (불일치 후보)
    doc_date: str | None = None
    date_source: str | None = None
    reason: str | None = None  # failed일 때 이유

    def to_dict(self) -> dict:
        return asdict(self)


def add_path(conn: sqlite3.Connection, path: Path, added_by: str | None = None,
             names: CodeNames | None = None) -> list[AddResult]:
    """파일 하나 또는 폴더(하위 폴더 포함)의 지원하는 문서를 모두 넣는다."""
    path = Path(path)
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                       and not p.name.startswith(("~$", ".")))
    else:
        files = [path]
    names = names or CodeNames(conn)
    return [add_document(conn, f, added_by, names=names) for f in files]


def add_document(conn: sqlite3.Connection, path: Path, added_by: str | None = None,
                 names: CodeNames | None = None, display_name: str | None = None) -> AddResult:
    """display_name: 웹 업로드처럼 임시 파일로 받았을 때 원래 파일 이름."""
    path = Path(path)
    name = display_name or path.name
    try:
        raw = path.read_bytes()
        doc = read_document(path)
    except DocReadError as e:
        return AddResult(name, "failed", path.suffix.lower().lstrip("."), reason=str(e))
    except OSError as e:
        return AddResult(name, "failed", path.suffix.lower().lstrip("."), reason=f"파일을 읽지 못했어요: {e}")
    sha1 = hashlib.sha1(raw).hexdigest()
    old = conn.execute("SELECT id, sha1 FROM documents WHERE name = ?", (name,)).fetchone()
    if old and old["sha1"] == sha1:
        return _result_of(conn, old["id"], "unchanged")

    chunks = make_chunks(doc)
    counts: Counter = Counter()
    rows = []
    for c in chunks:
        heading, n1 = redact(c.heading)
        text, n2 = redact(c.text)
        page, n3 = redact(c.page or "")
        counts.update(n1)
        counts.update(n2)
        counts.update(n3)
        rows.append((c.seq, c.section, heading, page or None, text))
    doc_date, date_source = document_date(doc, date.today())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if old:
        _delete_rows(conn, old["id"])
    cur = conn.execute(
        """INSERT INTO documents (name, format, sha1, doc_date, date_source, chunks, redactions, added_at, added_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, doc.format, sha1, doc_date, date_source, len(rows), json.dumps(dict(counts), ensure_ascii=False),
         now, (added_by or "").strip() or None),
    )
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO doc_chunks (doc_id, seq, section, heading, page, text) VALUES (?, ?, ?, ?, ?, ?)",
        [(doc_id, *r) for r in rows],
    )
    chunk_ids = [r["id"] for r in conn.execute("SELECT id FROM doc_chunks WHERE doc_id = ?", (doc_id,))]
    link_chunks(conn, chunk_ids, names)
    conn.commit()
    return _result_of(conn, doc_id, "replaced" if old else "added")


def _result_of(conn: sqlite3.Connection, doc_id: int, status: str) -> AddResult:
    d = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    by_kind = {r["kind"]: r["n"] for r in conn.execute(
        """SELECT l.kind, COUNT(*) AS n FROM doc_links l JOIN doc_chunks c ON c.id = l.chunk_id
           WHERE c.doc_id = ? AND l.found = 1 GROUP BY l.kind""", (doc_id,))}
    missing = [f"{r['kind']} {r['value']}" for r in conn.execute(
        """SELECT DISTINCT l.kind, l.value FROM doc_links l JOIN doc_chunks c ON c.id = l.chunk_id
           WHERE c.doc_id = ? AND l.found = 0 ORDER BY l.kind, l.value""", (doc_id,))]
    return AddResult(d["name"], status, d["format"], doc_id, d["chunks"], json.loads(d["redactions"] or "{}"),
                     sum(by_kind.values()), by_kind, missing, d["doc_date"], d["date_source"])


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute("SELECT * FROM documents ORDER BY name").fetchall()
    except sqlite3.OperationalError:
        return []
    result = []
    for d in rows:
        links = conn.execute(
            """SELECT COUNT(*) FROM doc_links l JOIN doc_chunks c ON c.id = l.chunk_id
               WHERE c.doc_id = ? AND l.found = 1""", (d["id"],)).fetchone()[0]
        result.append({
            "id": d["id"], "name": d["name"], "format": d["format"], "doc_date": d["doc_date"],
            "date_source": d["date_source"], "chunks": d["chunks"], "links": links,
            "redactions": json.loads(d["redactions"] or "{}"), "added_at": d["added_at"], "added_by": d["added_by"],
        })
    return result


def document_chunks(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    return [chunk_dict(conn, r) for r in conn.execute(
        """SELECT c.*, d.name, d.doc_date FROM doc_chunks c JOIN documents d ON d.id = c.doc_id
           WHERE c.doc_id = ? ORDER BY c.seq""", (doc_id,))]


def chunk_dict(conn: sqlite3.Connection, r: sqlite3.Row, links: bool = True) -> dict:
    """조각 하나를 화면·API용으로. title은 근거 제목과 같은 꼴 "인수인계서.docx 3.2 배치 운영 (2025-03)"."""
    data = {
        "id": r["id"], "doc_id": r["doc_id"], "doc": r["name"], "seq": r["seq"], "section": r["section"],
        "heading": r["heading"], "page": r["page"], "doc_date": r["doc_date"], "text": r["text"],
        "title": chunk_title(r["name"], r["section"], r["heading"], r["doc_date"], r["page"]),
    }
    if links:
        data["links"] = [{"kind": l["kind"], "value": l["value"], "found": bool(l["found"])} for l in conn.execute(
            "SELECT kind, value, found FROM doc_links WHERE chunk_id = ? ORDER BY found DESC, kind, value", (r["id"],))]
    return data


def chunk_title(name: str, section: str | None, heading: str | None, doc_date: str | None,
                page: str | None = None) -> str:
    from sasu.docs.chunker import heading_title

    last = heading_title((heading or "").split(" > ")[-1]) if heading else ""
    parts = [name]
    if section:
        parts.append(section)
    if last and last != page:
        parts.append(last)
    if page and not section:
        parts.append(f"{page}쪽" if page.isdigit() else f"[{page}]")
    title = " ".join(parts)
    return f"{title} ({doc_date[:7]})" if doc_date else title


def delete_document(conn: sqlite3.Connection, doc_id: int) -> bool:
    if conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone() is None:
        return False
    _delete_rows(conn, doc_id)
    conn.commit()
    return True


def _delete_rows(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM doc_links WHERE chunk_id IN (SELECT id FROM doc_chunks WHERE doc_id = ?)", (doc_id,))
    conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
