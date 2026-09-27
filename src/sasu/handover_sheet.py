"""인수인계 질문지 엑셀: 내보내서 회의에서 답을 적고, 다시 가져온다.

SI 현장의 인수인계는 엑셀로 주고받는 경우가 많아서, 질문지도 엑셀 한 장으로 만든다.
가져올 때는 "번호" 열이 아니라 숨긴 "key" 열로 질문을 찾는다 (다시 분석해도 같은 질문).
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from sasu.analysis.questions import KIND_LABEL

COLUMNS = [
    ("번호", 6), ("우선순위", 9), ("종류", 13), ("질문", 60), ("이유(코드 근거)", 50),
    ("위치", 45), ("대상", 35), ("코드", 50), ("답변", 60), ("답한 사람", 12), ("상태", 10), ("key", 10),
]
_STATUS_LABEL = {"open": "미답변", "answered": "답변함", "skipped": "건너뜀"}
_LABEL_STATUS = {v: k for k, v in _STATUS_LABEL.items()}


def export_sheet(conn: sqlite3.Connection, path: Path, include_answered: bool = True) -> int:
    rows = conn.execute(
        f"""SELECT * FROM questions WHERE stale = 0 {'' if include_answered else "AND status = 'open'"}
            ORDER BY priority DESC, id"""
    ).fetchall()
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
            i, q["priority"], KIND_LABEL.get(q["kind"], q["kind"]), q["question"], q["why"],
            f"{q['file_path']}:{q['line']}" if q["file_path"] else "", q["target"] or "", q["code"] or "",
            q["answer"] or "", q["answered_by"] or "", _STATUS_LABEL.get(q["status"], q["status"]), q["key"],
        ]
        for col, value in enumerate(values, 1):
            cell = ws.cell(row=i + 1, column=col, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=i + 1, column=9).fill = answer_fill
    status_rule = DataValidation(type="list", formula1='"미답변,답변함,건너뜀"', allow_blank=True)
    ws.add_data_validation(status_rule)
    status_rule.add(f"K2:K{len(rows) + 1}")
    ws.column_dimensions["L"].hidden = True
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:K{len(rows) + 1}"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return len(rows)


def import_sheet(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """답변 열이 채워진 행을 저장한다. 돌려주는 값: (저장한 답변 수, 찾지 못한 행 수)."""
    ws = load_workbook(path, read_only=True).active
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = {name: header.index(name) for name, _ in COLUMNS if name in header}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    saved = missing = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        key = row[idx["key"]] if "key" in idx else None
        answer = (row[idx["답변"]] or "").strip() if row[idx["답변"]] else ""
        status_label = row[idx["상태"]] if "상태" in idx else None
        if not key:
            continue
        status = _LABEL_STATUS.get(status_label or "", "open")
        if answer and status == "open":
            status = "answered"
        if not answer and status == "open":
            continue
        cur = conn.execute(
            """UPDATE questions SET answer = ?, answered_by = ?, status = ?, answered_at = ?, updated_at = ?
               WHERE key = ?""",
            (answer or None, (row[idx["답한 사람"]] or None) if "답한 사람" in idx else None, status, now, now, key),
        )
        if cur.rowcount:
            saved += 1
        else:
            missing += 1
    conn.commit()
    return saved, missing
