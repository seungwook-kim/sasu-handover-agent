"""질문에 답할 근거를 모은다. 근거마다 등급을 붙인다.

- 확인된 사실: 코드·SQL·호출 관계처럼 분석으로 확정한 것
- 인수인계 답변: 떠난 개발자가 질문지에 적은 답
- 기록: 주석, 파일 머리말 수정 이력, 커밋 메시지 (사람이 남겼지만 지금도 맞는지는 모름)
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sasu.githistory import blame_notes
from sasu.linker import normalize_url
from sasu.trace import TraceNode, trace_url

FACT, ANSWER, RECORD = "확인된 사실", "인수인계 답변", "기록"
_MAX_CODE_LINES = 150  # 긴 메서드의 뒷부분(예외 처리·null 검사)이 잘리지 않게


@dataclass
class Evidence:
    id: str  # E1, E2 …
    level: str
    title: str
    text: str
    location: str | None = None


@dataclass
class Target:
    method_id: int
    label: str  # Class.method
    path: str
    line_start: int
    line_end: int


def resolve_target(conn: sqlite3.Connection, text: str) -> list[Target]:
    """Class.method, 파일:줄, URL을 메서드로 바꾼다. 못 찾으면 빈 목록."""
    text = text.strip()
    rows = []
    if text.startswith("/"):
        rows = conn.execute(
            "SELECT DISTINCT method_id AS id FROM url_mappings WHERE url = ?", (normalize_url(text),)
        ).fetchall()
    elif m := re.match(r"^(.+\.(?:java|py)):(\d+)$", text):
        path, line = m.group(1).replace("\\", "/"), int(m.group(2))
        rows = conn.execute(
            """SELECT m.id FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
               WHERE f.path LIKE ? AND m.line_start <= ? AND m.line_end >= ?
               ORDER BY m.line_start DESC LIMIT 1""",
            (f"%{path}", line, line),
        ).fetchall()
    elif re.match(r"^[\w.]+\.\w+$", text):
        cls, method = text.rsplit(".", 1)
        rows = conn.execute(
            """SELECT m.id FROM methods m JOIN classes c ON c.id = m.class_id
               WHERE (c.name = ? OR c.name LIKE ?) AND m.name = ? ORDER BY m.line_start""",
            (cls, f"%.{cls}", method),
        ).fetchall()
    return [target_of(conn, r["id"]) for r in rows]


def target_of(conn: sqlite3.Connection, method_id: int) -> Target:
    r = conn.execute(
        """SELECT m.id, m.name, m.line_start, m.line_end, c.name AS class_name, f.path
           FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id WHERE m.id = ?""",
        (method_id,),
    ).fetchone()
    return Target(r["id"], f"{r['class_name']}.{r['name']}", r["path"], r["line_start"], r["line_end"])


def gather(conn: sqlite3.Connection, target: Target, repo: Path | None, cache_dir: Path,
           use_git: bool = True) -> list[Evidence]:
    items: list[Evidence] = []

    def add(level: str, title: str, text: str, location: str | None = None) -> None:
        if text and text.strip():
            items.append(Evidence(f"E{len(items) + 1}", level, title, text.strip(), location))

    loc = f"{target.path}:{target.line_start}"
    method = conn.execute(
        "SELECT doc, complexity FROM methods WHERE id = ?", (target.method_id,)
    ).fetchone()

    # 1. 코드 원문
    code = _source(repo, target.path, target.line_start, target.line_end)
    if code:
        add(FACT, f"{target.label} 코드", code, loc)

    # 2. 이 메서드를 거치는 URL
    urls = _urls_reaching(conn, target.method_id)
    if urls:
        add(FACT, "이 메서드를 거치는 URL", "\n".join(urls[:8]) + (f"\n… 외 {len(urls) - 8}개" if len(urls) > 8 else ""))

    # 3. 이 메서드가 부르는 것 (서비스·DAO·SQL·테이블·저장소 밖 빈)
    calls = _calls_summary(conn, target.method_id)
    if calls:
        add(FACT, "이 메서드가 부르는 코드와 SQL", calls)

    # 4. 사람이 남긴 설명: Javadoc·docstring, 주의 주석, 파일 머리말 수정 이력
    if method["doc"]:
        add(RECORD, "docstring" if target.path.endswith(".py") else "Javadoc", _clean_comment(method["doc"]), loc)
    for n in conn.execute("SELECT tag, text, line FROM notes WHERE method_id = ?", (target.method_id,)):
        add(RECORD, f"{n['tag']} 주석", n["text"], f"{target.path}:{n['line']}")
    header = conn.execute("SELECT header_comment FROM files WHERE path = ?", (target.path,)).fetchone()
    history = _history_lines(header["header_comment"] if header else None, target.label.rsplit(".", 1)[1])
    if history:
        add(RECORD, "파일 머리말 수정 이력", history, target.path)
    for mapper_line in _mapper_history(conn, target.method_id):
        add(RECORD, "Mapper 수정 이력", mapper_line[1], mapper_line[0])

    # 5. 인수인계 답변 (이 메서드를 대상으로 한 질문, 또는 이 메서드 범위 안의 줄)
    for q in conn.execute(
        """SELECT id, question, answer, answered_by, answered_at FROM questions
           WHERE answer IS NOT NULL AND (target = ? OR (file_path = ? AND line BETWEEN ? AND ?))""",
        (target.label, target.path, target.line_start, target.line_end),
    ):
        who = f" ({q['answered_by']}, {q['answered_at'][:10]})" if q["answered_by"] else ""
        add(ANSWER, f"인수인계 답변{who}", f"질문: {q['question']}\n답: {q['answer']}")

    # 6. 커밋 기록 (포맷 정리·릴리스 커밋은 건너뜀)
    if use_git and repo is not None:
        notes = blame_notes(repo, target.path, target.line_start, target.line_end, cache_dir)
        if notes:
            add(RECORD, "이 코드를 마지막으로 바꾼 커밋",
                "\n".join(f"{n.date} {n.sha} {n.author}: {n.summary} ({n.lines}줄)" for n in notes))
    return items


def _source(repo: Path | None, path: str, start: int, end: int) -> str | None:
    if repo is None:
        return None
    try:
        lines = (repo / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    chosen = lines[start - 1:end]
    if len(chosen) > _MAX_CODE_LINES:
        chosen = chosen[:_MAX_CODE_LINES] + [f"… ({len(lines[start - 1:end]) - _MAX_CODE_LINES}줄 생략)"]
    return "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(chosen))


def _urls_reaching(conn: sqlite3.Connection, method_id: int) -> list[str]:
    callers: dict[int, list[int]] = {}
    for e in conn.execute("SELECT src_id, dst_id FROM edges WHERE src_kind = 'method' AND dst_kind = 'method'"):
        callers.setdefault(e["dst_id"], []).append(e["src_id"])
    seen, stack = {method_id}, [method_id]
    while stack:
        for prev in callers.get(stack.pop(), []):
            if prev not in seen:
                seen.add(prev)
                stack.append(prev)
    placeholders = ",".join("?" * len(seen))
    return [r["url"] for r in conn.execute(
        f"SELECT DISTINCT url FROM url_mappings WHERE method_id IN ({placeholders}) ORDER BY url", list(seen)
    )]


def _calls_summary(conn: sqlite3.Connection, method_id: int) -> str:
    """trace를 이 메서드에서 시작해 한 줄씩 들여 쓴 글로 만든다."""
    from sasu.trace import _method_node  # 같은 규칙(유틸은 한 줄)으로 펼친다

    root = _method_node(conn, method_id, set(), depth=1, full=False)
    lines: list[str] = []

    def walk(node: TraceNode, depth: int) -> None:
        for child in node.children:
            pad = "  " * depth
            if child.kind == "method":
                lines.append(f"{pad}- {child.label}" + (f" ×{child.count}" if child.count > 1 else ""))
            elif child.kind == "statement":
                note = f" [{child.note}]" if child.note else ""
                lines.append(f"{pad}- SQL {child.label} (테이블: {child.detail or '-'}){note}")
            elif child.kind == "external":
                lines.append(f"{pad}- {child.label} [{child.note}]")
            elif child.kind == "view":
                lines.append(f"{pad}- 화면 {child.label}")
            elif child.kind == "url":
                lines.append(f"{pad}- 이동 {child.label}")
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines[:60])


def _mapper_history(conn: sqlite3.Connection, method_id: int, depth: int = 3) -> list[tuple[str, str]]:
    """이 메서드가 (DAO를 거쳐) 부르는 쿼리를 Mapper 머리말 수정 이력이 언급하면 그 줄."""
    methods, frontier = {method_id}, [method_id]
    for _ in range(depth):
        nxt = []
        for mid in frontier:
            for e in conn.execute(
                "SELECT dst_id FROM edges WHERE src_kind = 'method' AND src_id = ? AND dst_kind = 'method'", (mid,)
            ):
                if e["dst_id"] not in methods:
                    methods.add(e["dst_id"])
                    nxt.append(e["dst_id"])
        frontier = nxt
    placeholders = ",".join("?" * len(methods))
    result = []
    for r in conn.execute(
        f"""SELECT DISTINCT s.full_id, f.path, f.header_comment FROM edges e JOIN statements s ON s.id = e.dst_id
            JOIN files f ON f.id = s.file_id
            WHERE e.src_kind = 'method' AND e.src_id IN ({placeholders}) AND e.dst_kind = 'statement'
              AND f.kind = 'mapper'""",
        list(methods),
    ):
        short = r["full_id"].split(".", 1)[-1]
        lines = _history_lines(r["header_comment"], short)
        if lines:
            result.append((r["path"], f"{r['full_id']}: {lines}"))
    return result


def _history_lines(comment: str | None, word: str) -> str | None:
    if not comment or len(word) < 4:
        return None
    lines = [" ".join(line.split()) for line in comment.splitlines() if word in line]
    return "\n".join(lines) or None


def _clean_comment(text: str) -> str:
    lines = [line.strip().lstrip("/*").strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line and not line.startswith("@"))
