"""분석한 저장소에서 메서드 원문 코드를 읽는다 (임베딩 조각·재순위 후보에 붙인다)."""

import sqlite3
from pathlib import Path


def repo_root(conn: sqlite3.Connection) -> Path | None:
    """분석할 때 적어 둔 저장소 폴더. 없거나 옮겨져 없으면 None."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'repo_path'").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    root = Path(row[0])
    return root if root.is_dir() else None


class SourceReader:
    """메서드 id → 원문 코드 (파일은 한 번만 읽는다). 못 읽으면 빈 문자열."""

    def __init__(self, conn: sqlite3.Connection, root: Path | None = None):
        self.conn = conn
        self.root = root if root is not None else repo_root(conn)
        self._files: dict[str, list[str] | None] = {}

    def __call__(self, method_id: int, max_chars: int = 3000) -> str:
        if self.root is None:
            return ""
        r = self.conn.execute(
            """SELECT m.line_start, m.line_end, f.path FROM methods m JOIN classes c ON c.id = m.class_id
               JOIN files f ON f.id = c.file_id WHERE m.id = ?""",
            (method_id,),
        ).fetchone()
        if r is None:
            return ""
        path = r[2]
        if path not in self._files:
            try:
                self._files[path] = (self.root / path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._files[path] = None
        lines = self._files[path]
        if not lines:
            return ""
        return "\n".join(lines[r[0] - 1:r[1]])[:max_chars]
