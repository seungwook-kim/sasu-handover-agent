"""기존 동작 목록: 고도화 전에 URL(화면·기능)마다 지금 무엇을 하는지 적어 둔다.

고도화·리뉴얼에서 가장 흔한 사고는 "원래 되던 것이 조용히 빠지는 것"이다. 바꾸기 전 동작을
URL마다 한 줄로 남겨 두면, 끝난 뒤 같은 목록으로 하나씩 확인할 수 있다 (엑셀 체크리스트).

한 줄에 담는 것: 처리 메서드, 보여 주는 화면·이동, 읽는 테이블, 쓰는 테이블(등록·수정·삭제),
업무 코드 값으로 나뉘는 분기, 주의 주석, 받은 인수인계 답변.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sasu.analysis.labels import Labels
from sasu.analysis.overview import class_modules
from sasu.analysis.questions import is_code_value
from sasu.trace import trace_url

_WRITE_LABEL = {"INSERT": "등록", "UPDATE": "수정", "DELETE": "삭제", "MERGE": "저장", "CALL": "프로시저"}  # MERGE: JPA save·MERGE INTO


@dataclass
class Behavior:
    module: str
    url: str
    handler: str
    location: str
    screens: list[str] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)  # "COMTNBBS(등록·수정)"
    branches: list[str] = field(default_factory=list)  # "'09' ← sTemp.equals("09")"
    notes: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)  # 저장소 밖 빈·동적 쿼리 등 확인이 필요한 연결
    methods: int = 0
    label: str | None = None  # 한글 기능 이름 "휴가관리 · 휴가를 등록한다" (주석에 없으면 None)


def list_behaviors(conn: sqlite3.Connection, module: str | None = None) -> list[Behavior]:
    modules = class_modules(conn)
    names = Labels(conn)
    lang = {r["id"]: r["kind"] for r in conn.execute(
        "SELECT m.id, f.kind FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id")}
    labels_with_answers = {
        r["target"]: r for r in conn.execute(
            "SELECT target, question, answer, answered_by FROM questions WHERE answer IS NOT NULL AND status != 'skipped' AND stale = 0")
    }
    result = []
    for r in conn.execute(
        """SELECT u.url, MIN(m.id) AS method_id, c.id AS class_id, c.name AS class_name, m.name AS method_name,
                  f.path, m.line_start
           FROM url_mappings u JOIN methods m ON m.id = u.method_id JOIN classes c ON c.id = m.class_id
           JOIN files f ON f.id = c.file_id GROUP BY u.url ORDER BY u.url"""
    ).fetchall():
        mod = modules.get(r["class_id"], "")
        if module and mod != module and not mod.startswith(module + "."):
            continue
        b = Behavior(module=mod, url=r["url"], handler=f"{r['class_name']}.{r['method_name']}",
                     location=f"{r['path']}:{r['line_start']}", label=names.full(r["method_id"]))
        writes: dict[str, list[str]] = {}
        method_ids = []
        for node in trace_url(conn, r["url"]).walk():
            if node.kind == "method" and node.method_id is not None and not node.repeated:
                method_ids.append(node.method_id)
                answer = labels_with_answers.get(node.label)
                if answer is not None:
                    _add(b.answers, f"{node.label}: {answer['answer']}" + (f" ({answer['answered_by']})" if answer["answered_by"] else ""))
            elif node.kind == "statement":
                verb = node.label.split(" ", 1)[0]
                for table in filter(None, (node.detail or "").split(", ")):
                    if verb == "SELECT":
                        _add(b.reads, table)
                    else:
                        _add(writes.setdefault(table, []), _WRITE_LABEL.get(verb, verb.lower()))
                if node.note:
                    _add(b.external, f"{node.label.split(' ', 1)[-1]}: {node.note}")
            elif node.kind == "view":
                _add(b.screens, node.label + (f" ({node.detail})" if node.detail else ""))
            elif node.kind == "url" and node.detail == "이동":
                _add(b.redirects, node.label)
            elif node.kind == "external":
                _add(b.external, f"{node.label}: {node.note}" if node.note else node.label)
        b.methods = len(method_ids)
        b.writes = [f"{t}({'·'.join(kinds)})" for t, kinds in writes.items()]
        b.reads = [t for t in b.reads if t not in writes]
        if method_ids:
            marks = ",".join("?" * len(method_ids))
            for c in conn.execute(
                f"SELECT method_id, value, source FROM condition_literals WHERE method_id IN ({marks}) ORDER BY method_id, line",
                method_ids,
            ):
                if is_code_value(c["value"], lang.get(c["method_id"], "java"), c["source"]):
                    _add(b.branches, f"{_branch_label(c['value'], c['source'])} ← {c['source'][:80]}")
            for n in conn.execute(
                f"SELECT tag, text FROM notes WHERE method_id IN ({marks}) ORDER BY method_id, line", method_ids,
            ):
                _add(b.notes, n['text'][:100])  # 주석 글에 TODO 같은 표시가 이미 들어 있다
        result.append(b)
    return result


def _branch_label(value: str, source: str) -> str:
    """같은지 비교하는 코드 값은 '09', 크기를 비교하는 숫자는 "기준 20" (비밀번호 길이 8~20 같은 규칙)."""
    v = value.strip("'\"")
    if v.isdigit() and re.search(rf"[<>]=?\s*{v}\b|\b{v}\s*[<>]", source):
        return f"기준 {v}"
    return f"'{v}'"


def _add(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


# ── 엑셀 체크리스트 ─────────────────────────────────────────

COLUMNS = [
    ("번호", 6), ("모듈", 14), ("URL", 40), ("기능(한글)", 30), ("처리 메서드", 38), ("화면·이동", 40), ("읽는 테이블", 30),
    ("쓰는 테이블", 30), ("업무 코드 분기", 45), ("주의 주석", 35), ("인수인계 답변", 45), ("확인할 연결", 35),
    ("위치", 45), ("고도화 후 확인", 14), ("비고", 30),
]
_CHECK_OPTIONS = '"같음,달라짐(의도),달라짐(확인 필요),없어짐"'


def export_behaviors(behaviors: list[Behavior], path: Path, title: str = "기존 동작 목록") -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    header = PatternFill("solid", fgColor="1F4E78")
    for col, (name, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header
        ws.column_dimensions[cell.column_letter].width = width
    write_fill = PatternFill("solid", fgColor="FCE4D6")
    check_fill = PatternFill("solid", fgColor="FFF2CC")
    for i, b in enumerate(behaviors, 1):
        values = [
            i, b.module, b.url, b.label or "", b.handler, "\n".join(b.screens + [f"→ {u}" for u in b.redirects]),
            ", ".join(b.reads), ", ".join(b.writes), "\n".join(b.branches), "\n".join(b.notes),
            "\n".join(b.answers), "\n".join(b.external), b.location, "", "",
        ]
        for col, value in enumerate(values, 1):
            cell = ws.cell(row=i + 1, column=col, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        if b.writes:
            ws.cell(row=i + 1, column=_col("쓰는 테이블")).fill = write_fill
        ws.cell(row=i + 1, column=_col("고도화 후 확인")).fill = check_fill
    check = get_column_letter(_col("고도화 후 확인"))
    rule = DataValidation(type="list", formula1=_CHECK_OPTIONS, allow_blank=True)
    ws.add_data_validation(rule)
    rule.add(f"{check}2:{check}{len(behaviors) + 1}")
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(behaviors) + 1}"
    _usage_sheet(wb.create_sheet("사용법"))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return len(behaviors)


def _col(name: str) -> int:
    return next(i for i, (n, _) in enumerate(COLUMNS, 1) if n == name)


USAGE_STEPS = [
    "1. 고도화가 끝나면 이 목록을 위에서부터 보며, URL마다 새 시스템에서 같은 기능을 실행해 본다.",
    "2. 화면·이동, 읽는·쓰는 테이블, 업무 코드 분기, 주의 주석이 그대로인지 보고 '고도화 후 확인' 칸에서 값을 고른다.",
    "3. '달라짐(확인 필요)'·'없어짐'이면 비고에 무엇이 다른지 적는다. '고도화 후 확인' 열을 필터로 거르면 남은 확인 목록이 된다.",
]
CHECK_MEANINGS = [
    ("같음", "예전과 똑같이 동작한다."),
    ("달라짐(의도)", "고도화 요구대로 바뀌었다. 비고에 요구 번호나 근거를 적는다."),
    ("달라짐(확인 필요)", "바뀌었는데 의도한 것인지 모른다. 담당자에게 확인한다."),
    ("없어짐", "이 URL·기능이 새 시스템에 없다. 빠뜨린 것인지 확인한다."),
]


def _usage_sheet(ws) -> None:
    """목록을 받은 사람이 '고도화 후 확인' 열을 어떻게 채우는지."""
    from openpyxl.styles import Alignment, Font

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 90
    ws.cell(row=1, column=1, value="고도화 후 확인 열 채우는 법").font = Font(bold=True, size=13)
    row = 3
    for step in USAGE_STEPS:
        ws.cell(row=row, column=1, value=step).alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        row += 1
    row += 1
    ws.cell(row=row, column=1, value="값").font = Font(bold=True)
    ws.cell(row=row, column=2, value="뜻").font = Font(bold=True)
    for value, meaning in CHECK_MEANINGS:
        row += 1
        ws.cell(row=row, column=1, value=value)
        ws.cell(row=row, column=2, value=meaning)
    row += 2
    ws.cell(row=row, column=1, value="참고").font = Font(bold=True)
    note = ws.cell(row=row + 1, column=1, value=(
        "분석한 시점의 코드로 만든 목록이다. '확인할 연결'은 저장소 밖 빈·동적 쿼리처럼 "
        "정적 분석으로 확정하지 못한 연결이라 직접 확인해야 한다."))
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=row + 1, start_column=1, end_row=row + 1, end_column=2)
