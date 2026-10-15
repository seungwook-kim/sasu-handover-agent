"""문서와 코드가 다른 값을 말하는 곳을 규칙으로 찾는다 (LLM 없이).

문장 단위로 본다. 문장(또는 그 조각의 제목)이 코드와 이어져 있을 때(URL·Class.method·클래스·업무명)
그 코드 범위(처리 메서드에서 부르는 메서드 3단계까지)와 문장이 말하는 값을 비교한다.

| 종류 | 문서 | 코드 | 판정 |
| url_missing | 이 프로젝트 URL처럼 생긴 /x.do | url_mappings에 없음 | 다름 (비슷한 URL을 함께) |
| table_missing | 테이블처럼 생긴 이름(COMTN…·'테이블' 옆) | SQL·ORM 어디에도 없음 | 다름 |
| table | 같은 문장에 URL(또는 메서드·업무명)과 테이블 | 그 코드가 그 테이블을 안 씀 (다른 테이블은 씀) | 다름 |
| number | 같은 문장에 코드 + "5회·30일·10MB" | 코드 범위 어디에도 그 수가 없고, 조건에 다른 수가 있음 | 다름 |
| code_value | 같은 문장에 코드 + '03'·"Y" 같은 코드 값 | 코드 범위 어디에도 그 값이 없고, 조건에 같은 모양의 다른 값이 있음 | 다름 |

코드에 그 값이 아예 안 보이면(설정 파일에서 읽는 값 등) 다르다고 하지 않는다 (잘못 잡기보다 놓치는 쪽).
찾은 차이는 인수인계 질문 '문서 확인'(doc_check)으로 만든다.

규칙이 못 보는 것(설정 값, 상태값의 뜻, 물리/논리 삭제 같은 동작)은 선택 LLM 판정(judge.py, kind "llm")으로 더한다:
SASU_DOC_JUDGE=on이면 all_conflicts·sync_doc_questions가 기본으로 쓰고, 명령의 --llm-judge로도 켠다.
"""

import difflib
import re
import sqlite3
from dataclasses import asdict, dataclass

from sasu.docs.linking import CLASS, LABEL, METHOD, TABLE, URL, CodeNames, has_docs, last_heading

DOC_CHECK = "doc_check"
_MAX_SCOPE = 300  # 코드 범위가 이보다 크면 (업무명 하나에 클래스가 너무 많음) 숫자·코드 값은 비교하지 않는다

_NUMBER = re.compile(
    r"(?<![\d.,/:-])(\d{1,4})\s*(회|번|일|개월|달|년|시간|분|초|자리|글자|자|건|개|명|MB|KB|GB|메가|%|퍼센트|차례|차)"
    r"(?![가-힣]*(?:째|차례로))")
_SCHEDULE = re.compile(r"(매월|매달|매주|매일|매년|매시|오전|오후|새벽|밤|[월화수목금토일]요일)\s*$")
_QUOTED = re.compile(r"['\"‘’“”`]([A-Za-z0-9_]{1,10})['\"‘’“”`]")
_CODE_WORD_VALUE = re.compile(r"(?:코드|상태값|상태\s*값|구분값|구분\s*값|값)\s*[:=]?\s*([0-9]{2,3}|[A-Z]{1,3}[0-9]{1,3})(?![A-Za-z0-9])")
_SENTENCE = re.compile(r"(?<=[다요음함됨임])\.\s+|(?<=[.!?])\s+(?=[가-힣A-Z(\[])|\n+")
_COMPARE_NUM = re.compile(r"(?:[<>]=?|[!=]=)\s*\(?\s*(\d+)|(\d+)\s*\)?\s*(?:[<>]=?|[!=]=)")
_SQL_NUM = re.compile(r"(?:[<>]=?|=|LIMIT|ROWNUM\s*[<>]=?)\s*(\d+)\b", re.IGNORECASE)
_INT = re.compile(r"(?<![\w.])(\d+)(?![\w.])")
_SQL_QUOTED = re.compile(r"'([A-Za-z0-9_]{1,10})'")


@dataclass
class Conflict:
    kind: str  # url_missing, table_missing, table, number, code_value
    chunk_id: int
    doc_title: str  # "인수인계서.docx 3.2 배치 운영 (2025-03)"
    sentence: str
    doc_says: str
    code_says: str
    target: str | None  # 질문을 붙일 코드 (URL·Class.method·테이블)
    message: str  # "문서와 코드가 달라요: …"
    question: str  # 전임자에게 물을 질문

    def to_dict(self) -> dict:
        return asdict(self)


class CodeFacts:
    """비교에 쓰는 코드 쪽 사실. 한 번 읽어 여러 조각에 쓴다."""

    def __init__(self, conn: sqlite3.Connection, names: CodeNames | None = None):
        from sasu.analysis.labels import Labels
        from sasu.handover import repo_of

        self.conn = conn
        self._names = names
        self.labels = Labels(conn)
        self.repo = repo_of(conn)
        self.callees: dict[int, list[int]] = {}
        for e in conn.execute("SELECT src_id, dst_id FROM edges WHERE src_kind = 'method' AND dst_kind = 'method'"):
            self.callees.setdefault(e["src_id"], []).append(e["dst_id"])
        self._lines: dict[str, list[str]] = {}
        self._config_numbers: set[int] | None = None

    @property
    def names(self) -> CodeNames:
        if self._names is None:
            self._names = CodeNames(self.conn)
        return self._names

    # 코드 범위 ─────────────────────────────────────────────

    def methods_of(self, links: list[tuple[str, str]]) -> set[int]:
        """연결(종류, 이름)이 가리키는 메서드 (URL은 처리 메서드, 클래스·업무명은 그 클래스의 메서드 전부)."""
        ids: set[int] = set()
        for kind, value in links:
            if kind == URL:
                ids |= {r[0] for r in self.conn.execute("SELECT method_id FROM url_mappings WHERE url = ?", (value,))}
            elif kind == METHOD:
                cls, name = value.rsplit(".", 1)
                ids |= {r[0] for r in self.conn.execute(
                    "SELECT m.id FROM methods m JOIN classes c ON c.id = m.class_id WHERE c.name = ? AND m.name = ?",
                    (cls, name))}
            elif kind == CLASS:
                ids |= {r[0] for r in self.conn.execute(
                    "SELECT m.id FROM methods m JOIN classes c ON c.id = m.class_id WHERE c.name = ?", (value,))}
            elif kind == LABEL:
                class_ids = [cid for cid, label in self.labels.classes().items() if label == value]
                for cid in class_ids:
                    ids |= {r[0] for r in self.conn.execute("SELECT id FROM methods WHERE class_id = ?", (cid,))}
        return ids

    def reach(self, start: set[int], depth: int = 3) -> set[int]:
        seen, frontier = set(start), list(start)
        for _ in range(depth):
            nxt = []
            for mid in frontier:
                for c in self.callees.get(mid, []):
                    if c not in seen:
                        seen.add(c)
                        nxt.append(c)
            frontier = nxt
        return seen

    def statements_of(self, methods: set[int]) -> list[sqlite3.Row]:
        if not methods:
            return []
        marks = ",".join("?" * len(methods))
        return self.conn.execute(
            f"""SELECT DISTINCT s.id, s.full_id, s.sql FROM edges e JOIN statements s ON s.id = e.dst_id
                WHERE e.src_kind = 'method' AND e.dst_kind = 'statement' AND e.src_id IN ({marks})""",
            list(methods)).fetchall()

    def tables_of(self, methods: set[int]) -> set[str]:
        ids = [s["id"] for s in self.statements_of(methods)]
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {r[0].upper() for r in self.conn.execute(
            f"SELECT DISTINCT table_name FROM statement_tables WHERE statement_id IN ({marks})", ids)}

    def source(self, method_id: int) -> str:
        r = self.conn.execute(
            """SELECT m.line_start, m.line_end, f.path FROM methods m JOIN classes c ON c.id = m.class_id
               JOIN files f ON f.id = c.file_id WHERE m.id = ?""", (method_id,)).fetchone()
        if r is None or self.repo is None:
            return ""
        if r["path"] not in self._lines:
            try:
                self._lines[r["path"]] = (self.repo / r["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._lines[r["path"]] = []
        return "\n".join(self._lines[r["path"]][r["line_start"] - 1:r["line_end"]])

    def config_numbers(self) -> set[int]:
        """설정 파일(*.properties·*.yml·*.yaml·*.env·*.ini·*.conf)의 숫자 값. 코드가 설정에서 읽는 기준값일 수 있어
        문서의 수가 여기 있으면 다르다고 하지 않는다."""
        if self._config_numbers is None:
            found: set[int] = set()
            if self.repo is not None:
                skip = {".git", "node_modules", "target", "build", "dist", ".venv", "venv"}
                for ext in ("*.properties", "*.yml", "*.yaml", "*.env", "*.ini", "*.conf", "*.cfg"):
                    for f in self.repo.rglob(ext):
                        if skip & set(f.parts) or f.stat().st_size > 1_000_000:
                            continue
                        try:
                            text = f.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            continue
                        for m in re.finditer(r"[=:]\s*['\"]?(\d{1,6})['\"]?\s*(?:#.*)?$", text, re.MULTILINE):
                            found.add(int(m.group(1)))
            self._config_numbers = found
        return self._config_numbers

    def numbers(self, methods: set[int]) -> tuple[set[int], set[int]]:
        """(코드 범위에 나오는 모든 정수, 조건·SQL 비교에 쓰인 2 이상의 정수)."""
        everything: set[int] = set()
        compared: set[int] = set()
        marks = ",".join("?" * len(methods)) or "NULL"
        for r in self.conn.execute(f"SELECT value, source FROM condition_literals WHERE method_id IN ({marks})",
                                   list(methods)):
            everything |= {int(x) for x in _INT.findall(r["source"])}
            for a, b in _COMPARE_NUM.findall(r["source"]):
                n = int(a or b)
                if n >= 2:
                    compared.add(n)
        for r in self.conn.execute(f"SELECT value FROM string_literals WHERE method_id IN ({marks})", list(methods)):
            everything |= {int(x) for x in _INT.findall(r["value"])}
        for s in self.statements_of(methods):
            everything |= {int(x) for x in _INT.findall(s["sql"])}
            compared |= {int(x) for x in _SQL_NUM.findall(s["sql"]) if int(x) >= 2}
        for mid in methods:
            everything |= {int(x) for x in _INT.findall(self.source(mid))}
        return everything, compared

    def code_values(self, methods: set[int]) -> tuple[set[str], set[str]]:
        """(코드 범위의 모든 문자열 값, 조건에서 업무 코드로 비교한 값)."""
        from sasu.analysis.questions import is_code_value

        everything: set[str] = set()
        compared: set[str] = set()
        marks = ",".join("?" * len(methods)) or "NULL"
        for r in self.conn.execute(
                f"""SELECT cl.value, cl.source, f.kind FROM condition_literals cl JOIN methods m ON m.id = cl.method_id
                    JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
                    WHERE cl.method_id IN ({marks})""", list(methods)):
            value = r["value"].strip("'\"")
            everything.add(value)
            if is_code_value(r["value"], r["kind"] or "java", r["source"]):
                compared.add(value)
        for r in self.conn.execute(f"SELECT value FROM string_literals WHERE method_id IN ({marks})", list(methods)):
            everything.add(r["value"])
        for s in self.statements_of(methods):
            values = set(_SQL_QUOTED.findall(s["sql"]))
            everything |= values
            compared |= {v for v in values if re.fullmatch(r"[0-9]{2,3}|[A-Z]{1,3}[0-9]{1,3}", v)}
        for mid in methods:
            everything |= set(re.findall(r"[\"']([A-Za-z0-9_]{1,10})[\"']", self.source(mid)))
        return everything, compared

    def handler(self, target: str | None) -> tuple[str | None, int | None]:
        """질문에 적을 코드 위치 (URL·Class.method)."""
        if not target:
            return None, None
        if target.startswith("/"):
            row = self.conn.execute(
                """SELECT f.path, m.line_start FROM url_mappings u JOIN methods m ON m.id = u.method_id
                   JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id WHERE u.url = ?
                   ORDER BY m.id LIMIT 1""", (target,)).fetchone()
        elif "." in target:
            cls, name = target.rsplit(".", 1)
            row = self.conn.execute(
                """SELECT f.path, m.line_start FROM methods m JOIN classes c ON c.id = m.class_id
                   JOIN files f ON f.id = c.file_id WHERE c.name = ? AND m.name = ? LIMIT 1""", (cls, name)).fetchone()
        else:
            return None, None
        return (row["path"], row["line_start"]) if row else (None, None)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]


def chunk_conflicts(conn: sqlite3.Connection, chunk_id: int, facts: CodeFacts | None = None) -> list[Conflict]:
    from sasu.docs.store import chunk_title

    r = conn.execute(
        """SELECT c.id, c.section, c.heading, c.page, c.text, d.name, d.doc_date
           FROM doc_chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?""", (chunk_id,)).fetchone()
    if r is None:
        return []
    facts = facts or CodeFacts(conn)
    names = facts.names
    title = chunk_title(r["name"], r["section"], r["heading"], r["doc_date"], r["page"])
    heading_links = [(l.kind, l.value) for l in names.find(last_heading(r["heading"])) if l.found]
    chunk_links = [(l.kind, l.value) for l in names.find(r["text"]) if l.found and l.kind in (URL, METHOD, CLASS)]
    found: list[Conflict] = []
    seen: set[tuple] = set()

    def add(c: Conflict):
        key = (c.kind, c.doc_says, c.target)
        if key not in seen:
            seen.add(key)
            found.append(c)

    for sentence in sentences(r["text"]):
        links = names.find(sentence)
        for l in links:
            if l.found:
                continue
            if l.kind == URL:
                close = difflib.get_close_matches(l.value, list(names.urls), n=2, cutoff=0.8)
                code = "코드에 이 URL이 없어요" + (f" (비슷한 URL: {', '.join(close)})" if close else "")
                add(Conflict("url_missing", chunk_id, title, sentence, l.value, code, close[0] if close else None,
                             f"문서와 코드가 달라요: 문서에 나온 URL이 코드에 없어요 ({l.value})",
                             f"문서({title})에 나온 URL {l.value} — 코드에서 찾지 못했어요. "
                             + (f"코드의 비슷한 URL은 {close[0]}예요. 같은 기능인가요, 없어진 기능인가요?" if close
                                else "없어진 기능인가요, 다른 이름으로 바뀌었나요?")))
            elif l.kind == TABLE:
                close = difflib.get_close_matches(l.value.upper(), list(names.tables), n=2, cutoff=0.8)
                code = "코드(SQL)에 이 테이블이 없어요" + (f" (비슷한 테이블: {', '.join(close)})" if close else "")
                add(Conflict("table_missing", chunk_id, title, sentence, l.value, code, close[0] if close else None,
                             f"문서와 코드가 달라요: 문서에 나온 테이블이 코드(SQL)에 없어요 ({l.value})",
                             f"문서({title})에 나온 테이블 {l.value} — 코드에서 찾지 못했어요. "
                             + (f"코드의 비슷한 테이블은 {close[0]}예요. 같은 테이블인가요?" if close
                                else "지금은 어느 테이블에 저장하나요?")))
        code_links = [(l.kind, l.value) for l in links if l.found and l.kind in (URL, METHOD, CLASS, LABEL)]
        # 업무명은 URL·메서드보다 넓다: 문장에 더 구체적인 연결이 있으면 그것만
        if any(k in (URL, METHOD) for k, _ in code_links):
            code_links = [(k, v) for k, v in code_links if k in (URL, METHOD)]
        elif sum(k == LABEL for k, _ in code_links) > 1:
            code_links = [(k, v) for k, v in code_links if k != LABEL]
        if not code_links:
            code_links = [(k, v) for k, v in heading_links if k in (URL, METHOD, CLASS, LABEL)]
        if not code_links and 0 < len(set(chunk_links)) <= 2:
            code_links = list(set(chunk_links))
        if not code_links:
            continue
        target = next((v for k, v in code_links if k in (URL, METHOD)), code_links[0][1])
        start = facts.methods_of(code_links)
        if not start:
            continue
        scope = facts.reach(start)

        # 테이블: 문장의 테이블을 이 코드가 쓰나
        doc_tables = [l.value for l in links if l.kind == TABLE and l.found]
        if doc_tables:
            code_tables = facts.tables_of(scope)
            wrong = [t for t in doc_tables if t.upper() not in code_tables]
            if code_tables and wrong:
                shown = ", ".join(sorted(code_tables)[:6])
                for t in wrong:
                    add(Conflict("table", chunk_id, title, sentence, f"{target} → {t}", f"{target}의 코드가 쓰는 테이블: {shown}",
                                 target, f"문서와 코드가 달라요: 문서는 {target} → {t} 테이블, 코드가 쓰는 테이블은 {shown}",
                                 f"문서({title})에는 {target}에서 {t} 테이블을 쓴다고 돼 있는데, "
                                 f"코드가 쓰는 테이블은 {shown}예요. 어느 쪽이 맞나요?"))
        # 숫자·코드 값은 문장(또는 가장 가까운 제목)이 URL·메서드·클래스를 직접 가리킬 때만 비교한다.
        # 업무명만 걸린 운영 문장("장애 나면 30분 안에 보고")을 코드 조건과 비교하면 잘못 잡는다
        specific = [(l.kind, l.value) for l in links if l.found and l.kind in (URL, METHOD, CLASS)]
        if not specific:
            specific = [(k, v) for k, v in heading_links if k in (URL, METHOD, CLASS)]
        if not specific:
            continue
        start = facts.methods_of(specific)
        scope = facts.reach(start)
        if len(scope) > _MAX_SCOPE:
            continue
        clean = _strip_names(sentence)
        # 숫자: "5회", "30일", "10MB"
        doc_numbers = [(int(m.group(1)), m.group(2)) for m in _NUMBER.finditer(clean)
                       if 2 <= int(m.group(1)) < 1900 and not _SCHEDULE.search(clean[:m.start()])]
        if doc_numbers:
            everything, _ = facts.numbers(scope)
            _, compared = facts.numbers(facts.reach(start, depth=1))  # 비교 값은 가까운 코드에서만
            everything |= facts.config_numbers()  # 설정 파일 값(Globals.login.LockCount = 5)일 수 있다
            for n, unit in doc_numbers:
                if n not in everything and compared - {n}:
                    shown = ", ".join(str(x) for x in sorted(compared)[:6])
                    add(Conflict("number", chunk_id, title, sentence, f"{n}{unit}", f"코드 조건의 수: {shown}", target,
                                 f"문서와 코드가 달라요: 문서는 {n}{unit}, 코드({target}) 조건의 수는 {shown}",
                                 f"문서({title})에는 {n}{unit}로 돼 있는데, 코드({target}) 조건에 나오는 수는 {shown}예요. "
                                 "지금 기준은 무엇인가요?"))
        # 코드 값: '03', "Y"
        doc_values = {v for v in _QUOTED.findall(clean) + _CODE_WORD_VALUE.findall(clean) if _looks_like_code(v)}
        if doc_values:
            everything, _ = facts.code_values(scope)
            _, compared = facts.code_values(facts.reach(start, depth=1))
            for v in sorted(doc_values):
                same_shape = {c for c in compared if _shape(c) == _shape(v) and c != v}
                if v not in everything and same_shape:
                    shown = ", ".join(f"'{x}'" for x in sorted(same_shape)[:6])
                    add(Conflict("code_value", chunk_id, title, sentence, f"'{v}'", f"코드가 비교하는 값: {shown}", target,
                                 f"문서와 코드가 달라요: 문서는 코드 값 '{v}', 코드({target})가 비교하는 값은 {shown}",
                                 f"문서({title})에는 코드 값 '{v}'가 나오는데, 코드({target})가 비교하는 값은 {shown}예요. "
                                 "값이 바뀌었나요?"))
    return found


def _strip_names(sentence: str) -> str:
    """숫자를 볼 때 URL·식별자·날짜 안의 숫자는 뺀다."""
    s = re.sub(r"/[A-Za-z0-9_\-./{}]+", " ", sentence)
    s = re.sub(r"[A-Za-z_][A-Za-z0-9_]*\d[A-Za-z0-9_]*", " ", s)
    s = re.sub(r"\d{4}\s*[-./년]\s*\d{1,2}(?:\s*[-./월]\s*\d{1,2}\s*일?)?", " ", s)
    return s


def _looks_like_code(value: str) -> bool:
    """'03'·'A01'·'APR'은 코드 값, 'Y'·'0'·'admin'은 아니다."""
    if value in ("Y", "N", "0", "1") or len(value) > 6:
        return False
    return not value.isalpha() or (value.isupper() and len(value) >= 2)


def _shape(value: str) -> tuple[int, str]:
    return len(value), "".join("9" if ch.isdigit() else "A" if ch.isalpha() else "_" for ch in value)


_DEFAULT = object()


def all_conflicts(conn: sqlite3.Connection, judge=_DEFAULT) -> list[Conflict]:
    """규칙 판정 + (judge가 있으면) LLM 판정. judge 기본값: SASU_DOC_JUDGE=on이면 설정대로 만든 판정기, 아니면 없음."""
    if not has_docs(conn):
        return []
    facts = CodeFacts(conn)
    result: list[Conflict] = []
    for r in conn.execute("SELECT c.id FROM doc_chunks c JOIN documents d ON d.id = c.doc_id ORDER BY d.name, c.seq").fetchall():
        result.extend(chunk_conflicts(conn, r["id"], facts))
    if judge is _DEFAULT:
        from sasu.docs.judge import default_judge

        judge = default_judge()
    if judge is not None:
        from sasu.docs.judge import merge

        result = merge(result, judge.conflicts(conn))
    return result


_PRIORITY = {"table": 75.0, "number": 75.0, "code_value": 70.0, "llm": 70.0, "url_missing": 60.0,
             "table_missing": 60.0}


def doc_questions(conn: sqlite3.Connection, conflicts: list[Conflict] | None = None, judge=_DEFAULT):
    """불일치마다 인수인계 질문 '문서 확인' (analysis.questions.Question)."""
    from sasu.analysis.questions import Question

    conflicts = all_conflicts(conn, judge) if conflicts is None else conflicts
    facts = None
    questions = []
    keys = set()
    for c in conflicts:
        row = conn.execute("SELECT d.name FROM doc_chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?",
                           (c.chunk_id,)).fetchone()
        doc = row[0] if row else c.doc_title  # 파일 이름 (판이 바뀌어도 같은 질문)
        key = f"{DOC_CHECK}|{doc}|{c.kind}|{c.doc_says}|{c.target or ''}"
        if key in keys:
            continue
        keys.add(key)
        if facts is None:
            facts = CodeFacts(conn)
        path, line = facts.handler(c.target)
        questions.append(Question(
            key=key,
            kind=DOC_CHECK, priority=_PRIORITY.get(c.kind, 60.0), question=c.question,
            why=f"{c.message}. 문서 문장: \"{c.sentence[:160]}\"", file_path=path, line=line,
            target=c.target if c.target and not c.target.startswith("/") else _target_label(conn, c.target),
        ))
    return questions


def _target_label(conn: sqlite3.Connection, url: str | None) -> str | None:
    """URL이면 처리 메서드 Class.method (질문 목록이 대상으로 코드를 찾는다). 없으면 URL 그대로."""
    if not url:
        return None
    row = conn.execute(
        """SELECT c.name || '.' || m.name FROM url_mappings u JOIN methods m ON m.id = u.method_id
           JOIN classes c ON c.id = m.class_id WHERE u.url = ? ORDER BY m.id LIMIT 1""", (url,)).fetchone()
    return row[0] if row else url


def sync_doc_questions(conn: sqlite3.Connection, judge=_DEFAULT) -> tuple[int, int, int]:
    """'문서 확인' 질문만 다시 맞춘다 (문서를 넣거나 지운 뒤). 다른 질문은 건드리지 않는다.
    받은 답은 지키고, 이제 안 나오는 질문은 답이 없으면 지우고 답이 있으면 stale로 남긴다.
    돌려주는 값: (새 질문, 갱신, 사라진 질문)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = {r["key"]: r for r in conn.execute("SELECT key, status FROM questions WHERE kind = ?", (DOC_CHECK,))}
    new = updated = 0
    seen = set()
    for q in doc_questions(conn, judge=judge):
        if q.key in seen:
            continue
        seen.add(q.key)
        if q.key in existing:
            conn.execute(
                """UPDATE questions SET priority=?, question=?, why=?, file_path=?, line=?, target=?, stale=0, updated_at=?
                   WHERE key=?""", (q.priority, q.question, q.why, q.file_path, q.line, q.target, now, q.key))
            updated += 1
        else:
            conn.execute(
                """INSERT INTO questions (key, kind, priority, question, why, file_path, line, target, code,
                                          created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (q.key, q.kind, q.priority, q.question, q.why, q.file_path, q.line, q.target, now, now))
            new += 1
    gone = [k for k in existing if k not in seen]
    conn.executemany("UPDATE questions SET stale = 1, updated_at = ? WHERE key = ?", [(now, k) for k in gone])
    conn.execute("DELETE FROM questions WHERE kind = ? AND stale = 1 AND status = 'open'", (DOC_CHECK,))
    conn.commit()
    return new, updated, len(gone)
