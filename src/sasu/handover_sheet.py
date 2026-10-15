"""인수인계 질문지 엑셀: 내보내서 회의에서 답을 적고, 다시 가져온다.

SI 현장의 인수인계는 엑셀로 주고받는 경우가 많아서, 질문지도 엑셀 한 장으로 만든다.
가져올 때는 "번호" 열이 아니라 숨긴 "key" 열로 질문을 찾는다 (다시 분석해도 같은 질문).
"번호"는 화면과 같은 Q{id}라서 회의에서 "Q12번" 하고 화면에서 바로 찾을 수 있다.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from sasu.analysis.questions import KIND_LABEL

COLUMNS = [
    ("번호", 7), ("우선순위", 9), ("종류", 13), ("질문", 60), ("묻는 이유", 50),
    ("위치", 45), ("대상", 35), ("코드", 50), ("답변", 60), ("답한 사람", 12), ("상태", 10), ("key", 10),
]
_STATUS_LABEL = {"open": "미답변", "answered": "답변 완료", "skipped": "건너뜀"}
# 예전 질문지의 "답변함"도 답변 완료로 받는다
_LABEL_STATUS = {**{v: k for k, v in _STATUS_LABEL.items()}, "답변함": "answered"}


@dataclass
class ImportResult:
    saved: int = 0  # 실제로 바뀐 행
    unchanged: int = 0  # 이미 같은 답·상태
    missing: int = 0  # key로 질문을 찾지 못한 행


def export_sheet(conn: sqlite3.Connection, path: Path, include_answered: bool = True,
                 ids: list[int] | None = None) -> int:
    """ids가 있으면 그 질문만 (화면에서 거른 목록 그대로 내려받기)."""
    where = "stale = 0" + ("" if include_answered else " AND status = 'open'")
    params: list[int] = []
    if ids is not None:
        where += f" AND id IN ({','.join('?' * len(ids))})" if ids else " AND 0"
        params = list(ids)
    rows = conn.execute(f"SELECT * FROM questions WHERE {where} ORDER BY priority DESC, id", params).fetchall()
    wb = Workbook()
    ws = wb.active
    ws.title = "인수인계 질문"
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for col, (name, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        ws.column_dimensions[cell.column_letter].width = width
    answer_fill = PatternFill("solid", fgColor="FFF2CC")
    for i, q in enumerate(rows, 1):
        values = [
            f"Q{q['id']}", q["priority"], KIND_LABEL.get(q["kind"], q["kind"]), q["question"], q["why"],
            f"{q['file_path']}:{q['line']}" if q["file_path"] else "", q["target"] or "", q["code"] or "",
            q["answer"] or "", q["answered_by"] or "", _STATUS_LABEL.get(q["status"], q["status"]), q["key"],
        ]
        for col, value in enumerate(values, 1):
            cell = ws.cell(row=i + 1, column=col, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=i + 1, column=9).fill = answer_fill
    if rows:  # 질문이 없으면 K2:K1 같은 거꾸로 된 범위가 되어 openpyxl이 거부한다
        status_rule = DataValidation(type="list", formula1='"미답변,답변 완료,건너뜀"', allow_blank=True)
        ws.add_data_validation(status_rule)
        status_rule.add(f"K2:K{len(rows) + 1}")
    ws.column_dimensions["L"].hidden = True
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:K{len(rows) + 1}"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return len(rows)


def import_sheet(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """답변 열이 채워진 행을 저장한다. 돌려주는 값: (실제로 바뀐 답변 수, 찾지 못한 행 수)."""
    result = import_sheet_result(conn, path)
    return result.saved, result.missing


def import_sheet_result(conn: sqlite3.Connection, path: Path) -> ImportResult:
    """import_sheet와 같고, 이미 같은 행(unchanged) 수도 센다."""
    wb = load_workbook(path, read_only=True)
    try:
        return _import_rows(conn, wb.active)
    finally:
        wb.close()  # read_only는 파일을 열어 둔다. 닫지 않으면 Windows에서 파일을 지울 수 없다


def _import_rows(conn: sqlite3.Connection, ws) -> ImportResult:
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = {name: header.index(name) for name, _ in COLUMNS if name in header}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = ImportResult()
    for row in ws.iter_rows(min_row=2, values_only=True):
        key = row[idx["key"]] if "key" in idx else None
        answer = str(row[idx["답변"]]).strip() if row[idx["답변"]] is not None else ""
        status_label = row[idx["상태"]] if "상태" in idx else None
        if not key:
            continue
        status = _LABEL_STATUS.get(status_label or "", "open")
        if answer and status == "open":
            status = "answered"
        if not answer and status == "open":
            continue
        by = (str(row[idx["답한 사람"]]).strip() or None) if "답한 사람" in idx and row[idx["답한 사람"]] else None
        current = conn.execute("SELECT answer, answered_by, status FROM questions WHERE key = ?", (key,)).fetchone()
        if current is None:
            result.missing += 1
            continue
        if (current["answer"], current["answered_by"], current["status"]) == (answer or None, by, status):
            result.unchanged += 1  # 내보낸 질문지를 그대로 다시 올린 행: 답한 시각도 그대로 둔다
            continue
        conn.execute(
            """UPDATE questions SET answer = ?, answered_by = ?, status = ?, answered_at = ?, updated_at = ?
               WHERE key = ?""",
            (answer or None, by, status, now, now, key),
        )
        result.saved += 1
    conn.commit()
    return result
