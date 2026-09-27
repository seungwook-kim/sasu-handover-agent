"""인수인계 질문 생성: 코드만 봐서는 이유를 알 수 없는 곳을 찾아 떠나는 개발자에게 물을 질문으로 만든다.

규칙과 템플릿만으로 동작한다 (LLM 없이). 우선순위는
    신호의 기본 점수 × 영향 범위(이 코드에 닿는 URL 수)
로 정하고, 한 메서드·클래스에 질문이 몰리지 않게 나눈다.
"""

import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sasu.analysis.common import analyze_common
from sasu.analysis.unused import find_unused

# 흔해서 따로 물을 필요가 없는 조건 값
_TRIVIAL_VALUES = {
    "", "0", "1", "-1", "2", "Y", "N", "y", "n", "true", "false", "null", "/", ".", ",", " ",
    "GET", "POST", "PUT", "DELETE", "UTF-8", "utf-8",
    "get", "post", "put", "delete", "patch", "none", "id", "name", "type", "value", "data", "default",
}
# 이 값을 조건에 쓰는 메서드가 이보다 많으면 공통 규칙으로 보고 묻지 않는다
_RARE_VALUE_MAX_METHODS = 3
# 업무 코드처럼 생긴 값: USR03, NCRD, R3, 02, 9999, A-01, 한글 단어
_BUSINESS_CODE = re.compile(r"^(?:[A-Z]{1,8}[-_]?\d{0,4}|\d{2,6}|[A-Z]\d{1,4}|[가-힣]{1,10})$")
# Python은 코드 값을 소문자로 쓰는 일이 많다 (status == "pending"). Java·SI 코드는 대문자가 관례라 Python만 본다
_LOWER_CODE = re.compile(r"^[a-z][a-z0-9_]{1,19}$")
# 소문자여도 뜻이 뻔한 일반 단어
_COMMON_WORDS = {
    "http", "https", "insert", "update", "select", "detail", "list", "session", "password", "popup", "up",
    "down", "class", "method", "json", "xml", "html", "text", "file", "user", "admin", "test", "debug",
    "true", "false", "yes", "no", "on", "off", "asc", "desc", "all", "create", "read", "remove", "save",
}



def is_code_value(value: str, lang: str, source: str) -> bool:
    """조건에 쓰인 값이 업무 코드처럼 보이나 (USR03, '09', Python의 "partial_refund").
    뻔한 값(Y/N, 0/1, 경로, 클래스 이름)과 너무 긴 문자열은 뺀다."""
    v = value.strip("'\"")
    if v in _TRIVIAL_VALUES or len(v) > 40 or _SELF_EXPLANATORY.match(v):
        return False
    return bool(_BUSINESS_CODE.match(v)) or _python_code(v, lang, source)


def _python_code(value: str, lang: str, source: str) -> bool:
    return (lang == "python" and bool(_LOWER_CODE.match(value)) and value not in _COMMON_WORDS
            and _compared_as_code(value, source))


def _compared_as_code(value: str, source: str) -> bool:
    """조건에서 이 값이 같은지 비교에 쓰였나. x in ("a", "b")는 비교지만
    "password" in data(키 검사)나 name.startswith("a")는 아니다."""
    q = rf"[\"']{re.escape(value)}[\"']"
    return bool(
        re.search(rf"{q}\s*[!=]=|[!=]=\s*{q}", source)
        or re.search(rf"\bin\s*[(\[{{][^)\]}}]*{q}", source)
        or re.search(rf"{q}\s*\.equals|\.equals(?:IgnoreCase)?\(\s*{q}", source)
        or re.match(r"case\b", source)
    )


# 이름만 봐도 뜻을 아는 값: 16진수, 경로·URL·MIME, 클래스 이름, 기호
_SELF_EXPLANATORY = re.compile(r"^(?:0x[0-9A-Fa-f]+|.*[/\\].*|[A-Z][a-z]+(?:[A-Z][a-z]+)+|[^\w가-힣]+|unknown|null)$")
# Controller·Service·DAO의 규칙이 유틸보다 인수인계에서 중요하다
_STEREOTYPE_FACTOR = {"Controller": 1.0, "RestController": 1.0, "Service": 1.0, "Repository": 1.0, "Component": 0.8}
_UTIL_FACTOR = 0.5
_COMPLEXITY_THRESHOLD = 15
_MULTI_SIGNAL_BONUS = 0.5  # 신호 종류가 하나 더 겹칠 때마다 우선순위 +50%
_MAX_PER_METHOD = 2
_MAX_PER_CLASS = 4

BASE = {
    "broken_sql": 100,
    "dynamic_sql": 90,
    "note": 80,
    "magic_value": 70,
    "external_bean": 60,
    "empty_catch": 50,
    "copied_code": 45,
    "complex": 70,
    "unused_sql": 30,
}

KIND_LABEL = {
    "broken_sql": "끊어진 쿼리",
    "dynamic_sql": "동적 쿼리 ID",
    "note": "주의 주석",
    "magic_value": "특정 값 분기",
    "external_bean": "저장소 밖 설정",
    "empty_catch": "무시하는 예외",
    "copied_code": "복사한 코드",
    "complex": "복잡한 분기",
    "unused_sql": "안 쓰는 SQL",
}


@dataclass
class Question:
    key: str
    kind: str
    priority: float
    question: str
    why: str
    file_path: str | None
    line: int | None
    target: str | None
    method_id: int | None = None  # 이번 분석 안에서만 쓰는 값 (저장하지 않음)


def generate_questions(conn: sqlite3.Connection) -> list[Question]:
    reach = url_reach(conn)
    methods = {
        r["id"]: r
        for r in conn.execute(
            """SELECT m.id, m.name, m.line_start, m.complexity, c.id AS class_id, c.name AS class_name,
                      c.stereotype, f.path
               FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id"""
        )
    }

    def weight(method_id: int | None) -> float:
        if method_id is None:
            return 1.0
        m = methods[method_id]
        n = reach.get(method_id, 0)
        reach_factor = (1 + math.log2(1 + n)) if n else 0.5
        # 복잡한 메서드일수록 사람에게 물어볼 가치가 크다 (과거 기록 평가에서 가장 강한 신호)
        complexity_factor = 1 + math.log2(max(m["complexity"], 1))
        return reach_factor * complexity_factor * _STEREOTYPE_FACTOR.get(m["stereotype"], _UTIL_FACTOR)

    def target(mid: int) -> str:
        m = methods[mid]
        return f"{m['class_name']}.{m['name']}"

    found: list[Question] = []

    def add(kind: str, key_detail: str, mid: int | None, question: str, why: str,
            file_path: str | None = None, line: int | None = None, target_name: str | None = None) -> None:
        m = methods.get(mid) if mid is not None else None
        found.append(Question(
            key=f"{kind}|{(m['path'] if m else file_path) or ''}|{target(mid) if m else target_name or ''}|{key_detail}",
            kind=kind,
            priority=round(BASE[kind] * weight(mid), 1),
            question=question,
            why=why,
            file_path=m["path"] if m and not file_path else file_path,
            line=line if line is not None else (m["line_start"] if m else None),
            target=target(mid) if m else target_name,
            method_id=mid,
        ))

    # 1. 끊어진 쿼리 참조 (URL에서 이어지는 것만. 죽은 코드는 정리 대상이지 질문거리가 아님)
    unused = find_unused(conn)
    for f in unused.broken_sql:
        if "죽은 코드" in f.reason:
            continue
        path, line = f.location.rsplit(":", 1)
        mid = _method_at(conn, path, int(line))
        add("broken_sql", f.name, mid,
            f"이 기능은 실제로 동작하나요? 코드가 부르는 쿼리 {f.name}가 Mapper에 없습니다.",
            f"{f.reason}. {f.evidence or ''}".strip(), path, int(line))

    # 2. 동적 쿼리 ID
    dynamic: dict[tuple[int, str], list[str]] = defaultdict(list)
    for e in conn.execute(
        """SELECT e.src_id, e.note, e.line, s.full_id FROM edges e JOIN statements s ON s.id = e.dst_id
           WHERE e.dst_kind = 'statement' AND e.note LIKE '동적 ID%'"""
    ):
        dynamic[(e["src_id"], e["note"])].append(e["full_id"])
    for (mid, note), ids in dynamic.items():
        prefix = note.split('"')[1] if '"' in note else note
        line = conn.execute(
            "SELECT line FROM calls WHERE method_id = ? AND string_prefixes LIKE ?", (mid, f'%"{prefix}"%')
        ).fetchone()
        add("dynamic_sql", prefix, mid,
            f"쿼리 ID가 실행 때 정해집니다 (\"{prefix}\" + 값). 가능한 값은 무엇이고, 각 쿼리는 언제 쓰이나요?",
            f"이 접두어로 시작하는 쿼리 {len(ids)}개: {', '.join(sorted(ids))}",
            line=line["line"] if line else None)

    # 3. 주의 주석 (TODO·FIXME·임시 처리 등)
    for n in conn.execute("SELECT method_id, tag, text, line FROM notes"):
        text = n["text"]
        if re.fullmatch(r"TODO\s*", text, re.IGNORECASE):
            question = "TODO만 적혀 있고 내용이 없습니다. 남은 작업이 있나요?"
        elif "Auto-generated" in text:
            question = "자동 생성된 뼈대(TODO Auto-generated)가 그대로 있습니다. 구현이 필요한 메서드인가요?"
        else:
            question = f"주석에 \"{text}\"라고 적혀 있습니다. 아직 유효한 내용인가요? 처리됐다면 어떻게 됐나요?"
        add("note", f"{n['tag']}:{text[:40]}", n["method_id"], question,
            f"{n['tag']} 표시가 있는 주석 ({n['line']}번째 줄)", line=n["line"])

    # 4. 드문 값으로 분기하는 조건 (매직 값)
    value_methods: Counter = Counter()
    for r in conn.execute("SELECT value, COUNT(DISTINCT method_id) AS n FROM condition_literals GROUP BY value"):
        value_methods[r["value"]] = r["n"]
    per_method: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(
        """SELECT cl.method_id, cl.value, cl.line, cl.source, f.kind AS lang
           FROM condition_literals cl JOIN methods m ON m.id = cl.method_id
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           ORDER BY cl.line"""
    ):
        if value_methods[r["value"]] > _RARE_VALUE_MAX_METHODS or not is_code_value(r["value"], r["lang"], r["source"]):
            continue
        per_method[r["method_id"]].append(r)
    for mid, rows in per_method.items():
        values = list(dict.fromkeys(r["value"] for r in rows))
        shown = ", ".join(f"'{v}'" for v in values[:5])
        first = rows[0]
        these = "이 값은" if len(values) == 1 else "이 값들은"
        add("magic_value", ",".join(values[:5]), mid,
            f"값이 {shown}일 때 따로 처리합니다. {these} 무슨 뜻이고, 왜 따로 처리하나요?",
            f"조건: {first['source']} ({first['line']}번째 줄). 코드 전체에서 이 값을 쓰는 곳이 {_RARE_VALUE_MAX_METHODS}곳 이하",
            line=first["line"])

    # 5. 저장소 밖 설정 빈 (같은 타입은 한 질문으로: EgovIdGnrService 빈 30개 → 질문 1개)
    external: dict[str, list] = defaultdict(list)
    for e in conn.execute("SELECT src_id, dst_name, note, line FROM edges WHERE dst_kind = 'external'"):
        external[e["dst_name"].split(".")[0]].append(e)
    for type_name, edges in external.items():
        beans = sorted({e["note"].split("'")[1] for e in edges if "'" in e["note"]})
        best = max(edges, key=lambda e: reach.get(e["src_id"], 0))
        users = sorted({target(e["src_id"]) for e in edges})
        bean_text = f"'{beans[0]}'" if len(beans) == 1 else f"{len(beans)}개 (예: '{beans[0]}')"
        add("external_bean", type_name, best["src_id"],
            f"코드에는 없고 설정에만 있는 빈: {type_name} {bean_text}. 어디서 설정하고, 운영 환경 값은 어떻게 맞추나요?",
            f"빈: {', '.join(beans[:6])}{' 외' if len(beans) > 6 else ''}. 쓰는 곳 {len(users)}개",
            line=best["line"])

    # 6. 무시하는 예외
    for r in conn.execute(
        """SELECT e.method_id, e.exception_type, e.line FROM empty_catches e
           JOIN methods m ON m.id = e.method_id JOIN classes c ON c.id = m.class_id
           WHERE c.stereotype IS NOT NULL"""
    ):
        add("empty_catch", f"{r['exception_type']}:{r['line']}", r["method_id"],
            f"{r['exception_type'] or '예외'}를 잡고 아무것도 하지 않습니다. 실패해도 괜찮은 이유가 있나요?",
            f"비어 있는 catch 블록 ({r['line']}번째 줄)", line=r["line"])

    # 7. 복잡한 분기
    for mid, m in methods.items():
        if m["complexity"] >= _COMPLEXITY_THRESHOLD and m["stereotype"] in ("Controller", "Service", "Repository", "Component"):
            add("complex", str(m["complexity"]), mid,
                f"분기가 많습니다 (순환 복잡도 {m['complexity']}). 꼭 알아야 할 규칙과 예외 케이스는 무엇인가요?",
                "조건·반복·예외 처리가 많아 흐름을 읽기 어려운 메서드")

    # 8. 공통 부모가 있는데 복사해 쓴 코드
    for group in analyze_common(conn, top=50).clones:
        if not group.note:
            continue
        name, loc = group.methods[0]
        path, line = loc.rsplit(":", 1)
        add("copied_code", name.split(".")[-1], None,
            f"같은 코드가 {len(group.methods)}곳에 있습니다 ({name.split('.')[-1]}). 바꿔야 하면 전부 바꿔야 하나요? 공통 부모를 상속하지 않은 이유가 있나요?",
            group.note, path, int(line), name)

    # 9. 사람이 "안 쓴다"고 남긴 SQL
    for f in unused.sql:
        if not f.evidence:
            continue
        path, line = f.location.rsplit(":", 1)
        add("unused_sql", f.name, None,
            f"쿼리 {f.name}는 부르는 곳이 없습니다. 지워도 되나요, 다른 시스템이 쓰나요?",
            f"수정 이력: {f.evidence}", path, int(line), f.name)

    # 여러 종류의 신호가 겹친 메서드(드문 값 분기 + 복잡한 분기 + 무시하는 예외 …)일수록 먼저 묻는다
    kinds_per_method: dict[int, set[str]] = defaultdict(set)
    for q in found:
        if q.method_id is not None:
            kinds_per_method[q.method_id].add(q.kind)
    for q in found:
        extra = len(kinds_per_method.get(q.method_id, ())) - 1 if q.method_id is not None else 0
        if extra > 0:
            q.priority = round(q.priority * (1 + _MULTI_SIGNAL_BONUS * extra), 1)

    # 같은 key(같은 메서드의 같은 주석이 두 줄 등)는 우선순위가 높은 하나만
    unique: dict[str, Question] = {}
    for q in sorted(found, key=lambda q: -q.priority):
        unique.setdefault(q.key, q)
    ordered = _diversify(list(unique.values()), methods)
    # 지금 실행하면 오류가 나는 것(끊어진 쿼리)은 예측이 아니라 확인된 고장이라 맨 앞에 둔다.
    # 목록·엑셀이 우선순위로 정렬하므로 점수도 가장 높은 점수 위로 올린다.
    top = max((q.priority for q in ordered if q.kind != "broken_sql"), default=0.0)
    broken = [q for q in ordered if q.kind == "broken_sql"]
    for q in broken:
        q.priority = round(top + q.priority, 1)
    return broken + [q for q in ordered if q.kind != "broken_sql"]


def url_reach(conn: sqlite3.Connection) -> dict[int, int]:
    """메서드마다 닿는 URL 수. 많을수록 바뀌면 영향이 크다."""
    adjacency: dict[int, list[int]] = defaultdict(list)
    for e in conn.execute("SELECT src_id, dst_id FROM edges WHERE src_kind = 'method' AND dst_kind = 'method'"):
        adjacency[e["src_id"]].append(e["dst_id"])
    counts: Counter = Counter()
    for row in conn.execute("SELECT DISTINCT url, method_id FROM url_mappings"):
        seen = {row["method_id"]}
        stack = [row["method_id"]]
        while stack:
            current = stack.pop()
            for nxt in adjacency.get(current, []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        counts.update(seen)
    return dict(counts)


def _method_at(conn: sqlite3.Connection, path: str, line: int) -> int | None:
    row = conn.execute(
        """SELECT m.id FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE f.path = ? AND m.line_start <= ? AND m.line_end >= ? ORDER BY m.line_start DESC LIMIT 1""",
        (path, line, line),
    ).fetchone()
    return row["id"] if row else None


def _diversify(questions: list[Question], methods: dict) -> list[Question]:
    """한 메서드·클래스에 질문이 몰리지 않게 뒤로 미룬다 (지우지는 않음)."""
    per_method: Counter = Counter()
    per_class: Counter = Counter()
    first, later = [], []
    for q in questions:
        m = methods.get(q.method_id) if q.method_id else None
        class_key = m["class_id"] if m else q.target
        if (q.method_id and per_method[q.method_id] >= _MAX_PER_METHOD) or per_class[class_key] >= _MAX_PER_CLASS:
            later.append(q)
            continue
        if q.method_id:
            per_method[q.method_id] += 1
        per_class[class_key] += 1
        first.append(q)
    return first + later


def save_questions(conn: sqlite3.Connection, questions: list[Question], repo: Path | None) -> tuple[int, int, int]:
    """질문을 저장한다. 이미 답한 질문은 내용만 갱신하고 답은 지키며, 이번에 안 나온 질문은 stale로 표시한다.

    돌려주는 값: (새 질문 수, 갱신한 질문 수, stale이 된 질문 수)
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = {r["key"]: r for r in conn.execute("SELECT key, status FROM questions")}
    new = updated = 0
    seen = set()
    for q in questions:
        seen.add(q.key)
        code = _code_line(repo, q.file_path, q.line)
        if q.key in existing:
            conn.execute(
                """UPDATE questions SET kind=?, priority=?, question=?, why=?, file_path=?, line=?, target=?,
                   code=?, stale=0, updated_at=? WHERE key=?""",
                (q.kind, q.priority, q.question, q.why, q.file_path, q.line, q.target, code, now, q.key),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO questions (key, kind, priority, question, why, file_path, line, target, code,
                                          created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (q.key, q.kind, q.priority, q.question, q.why, q.file_path, q.line, q.target, code, now, now),
            )
            new += 1
    stale_keys = [k for k in existing if k not in seen]
    conn.executemany("UPDATE questions SET stale = 1, updated_at = ? WHERE key = ?", [(now, k) for k in stale_keys])
    # 답하지 않은 채로 사라진 질문은 지운다. 답한 질문은 기록으로 남긴다.
    conn.execute("DELETE FROM questions WHERE stale = 1 AND status = 'open'")
    conn.commit()
    return new, updated, len(stale_keys)


def _code_line(repo: Path | None, path: str | None, line: int | None) -> str | None:
    if repo is None or path is None or line is None:
        return None
    file = repo / path
    try:
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return lines[line - 1].strip()[:200] if 0 < line <= len(lines) else None
