"""인수인계 질문 다루기: 새로 만들기, 답 저장, 떠난 개발자에게 보낼 질문 초안. CLI와 웹이 함께 쓴다."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sasu.analysis.questions import generate_questions, save_questions


def meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else "-"


def repo_of(conn: sqlite3.Connection) -> Path | None:
    path = meta(conn, "repo_path")
    return Path(path) if path != "-" and Path(path).exists() else None


def refresh_questions(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    """질문을 다시 만든다. 받은 답은 그대로 둔다. (전체, 새 질문, 갱신, 사라진 질문)"""
    generated = generate_questions(conn)
    new, updated, stale = save_questions(conn, generated, repo_of(conn))
    return len(generated), new, updated, stale


def save_answer(conn: sqlite3.Connection, question_id: int, text: str, by: str | None) -> bool:
    """답을 저장한다. 빈 답이면 답을 지우고 다시 '물어볼 것'으로 돌린다."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = text.strip()
    if text:
        cur = conn.execute(
            """UPDATE questions SET answer = ?, answered_by = ?, status = 'answered', answered_at = ?, updated_at = ?
               WHERE id = ?""",
            (text, (by or "").strip() or None, now, now, question_id),
        )
    else:
        cur = conn.execute(
            """UPDATE questions SET answer = NULL, answered_by = NULL, status = 'open', answered_at = NULL,
                                    updated_at = ? WHERE id = ?""",
            (now, question_id),
        )
    conn.commit()
    return cur.rowcount > 0


def draft_message(conn: sqlite3.Connection, question_id: int) -> str | None:
    """떠난 개발자에게 보낼 질문 메시지. 한 번에 정확히 묻도록 코드 위치와 근거를 담는다."""
    q = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if q is None:
        return None
    project = meta(conn, "repo_path").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    location = f"{q['file_path']}:{q['line']}" if q["file_path"] else "-"
    return (
        f"안녕하세요, {project} 인수인계를 받은 담당자입니다. 코드 한 곳만 여쭤보고 싶어 연락드렸습니다.\n\n"
        f"■ 위치: {q['target'] or '-'} ({location})\n"
        + (f"■ 코드: {q['code']}\n" if q["code"] else "")
        + f"■ 질문: {q['question']}\n"
        f"■ 제가 확인한 것: {q['why']}\n\n"
        "짧게라도 답을 주시면 인수인계 기록에 남겨 다음 담당자도 볼 수 있게 하겠습니다. 감사합니다."
    )
