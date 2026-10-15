"""부르는 곳을 찾을 수 없는 SQL·URL·메서드·JSP를 찾는다.

"안 쓴다"고 단정하지 않는다. 정적 분석으로 못 찾는 호출(외부 링크, 리플렉션,
다른 시스템)이 있을 수 있어서, 종류마다 확신 정도를 붙인다.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field

from sasu.analysis.django_signals import load_facts

# 코드가 아니라 프레임워크가 부르는 메서드
FRAMEWORK_CALLED = {
    "PostConstruct", "PreDestroy", "Scheduled", "Bean", "EventListener", "ExceptionHandler",
    "InitBinder", "ModelAttribute", "KafkaListener", "JmsListener", "RabbitListener", "Async",
    "AfterReturning", "Around", "Before", "After", "AfterThrowing", "Pointcut",
}
STEREOTYPES = ("Service", "Repository", "Component")


@dataclass
class Finding:
    kind: str  # sql, url, method, jsp
    name: str
    location: str
    confidence: str  # 높음, 후보
    reason: str
    evidence: str | None = None  # 수정 이력 주석 등 사람이 남긴 근거


@dataclass
class UnusedReport:
    broken_sql: list[Finding] = field(default_factory=list)  # 코드가 부르는데 Mapper에 없는 쿼리
    sql: list[Finding] = field(default_factory=list)
    url: list[Finding] = field(default_factory=list)
    method: list[Finding] = field(default_factory=list)
    jsp: list[Finding] = field(default_factory=list)

    def all(self) -> list[Finding]:
        return self.broken_sql + self.sql + self.url + self.method + self.jsp


_QUERY_ID = re.compile(r"^([A-Za-z_]\w*)\.([A-Za-z_]\w*)$")


def find_unused(conn: sqlite3.Connection) -> UnusedReport:
    report = UnusedReport()
    literals = {r["value"] for r in conn.execute("SELECT DISTINCT value FROM string_literals")}
    literal_blob = "\n".join(literals)
    report.broken_sql = _broken_sql(conn)
    report.sql = _unused_sql(conn, literal_blob)
    report.url = _unused_urls(conn, literal_blob)
    report.method = _unused_methods(conn) + _unused_python_methods(conn)
    report.jsp = _unused_jsps(conn, literal_blob)
    return report


@dataclass
class BrokenCall:
    """코드가 부르는데 Mapper에 없는 쿼리 하나."""

    method_id: int
    query_id: str
    caller: str  # Class.method
    location: str  # 파일:줄
    hint: str
    reachable: bool  # URL(또는 설정)에서 이어지는 메서드가 부른다 → 실행하면 오류


def broken_sql_calls(conn: sqlite3.Connection) -> list[BrokenCall]:
    """DAO 안의 "네임스페이스.쿼리ID" 문자열인데 Mapper에 그런 쿼리가 없는 경우.

    네임스페이스가 실제로 있거나, 같은 쿼리 ID가 다른 네임스페이스에 있을 때만 본다.
    ("a.b" 모양의 아무 문자열이나 잡지 않으려는 것)
    """
    full_ids = {r["full_id"] for r in conn.execute("SELECT full_id FROM statements")}
    namespaces = {f.split(".", 1)[0] for f in full_ids}
    by_short: dict[str, list[str]] = {}
    for f in full_ids:
        by_short.setdefault(f.rsplit(".", 1)[-1], []).append(f)
    rows = conn.execute(
        """SELECT DISTINCT s.method_id, s.value, s.line, f.path, c.name AS class_name, m.name AS method_name
           FROM string_literals s JOIN methods m ON m.id = s.method_id
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE c.stereotype = 'Repository' ORDER BY f.path, s.line"""
    ).fetchall()
    reachable = reachable_methods(conn)
    calls = []
    for r in rows:
        match = _QUERY_ID.match(r["value"])
        if match is None or r["value"] in full_ids:
            continue
        # "LoginUsr.updateLoginIncorrect" + userSe 의 앞부분: 그 자체는 쿼리가 아니다
        if any(f.startswith(r["value"]) for f in full_ids):
            continue
        namespace, short = match.groups()
        similar = by_short.get(short, [])
        if namespace not in namespaces and not similar:
            continue
        hint = f"비슷한 쿼리: {', '.join(similar)}" if similar else f"네임스페이스 {namespace}에 {short}가 없음"
        calls.append(BrokenCall(
            method_id=r["method_id"], query_id=r["value"], caller=f"{r['class_name']}.{r['method_name']}",
            location=f"{r['path']}:{r['line']}", hint=hint, reachable=r["method_id"] in reachable,
        ))
    return calls


def _broken_sql(conn: sqlite3.Connection) -> list[Finding]:
    findings = []
    for call in broken_sql_calls(conn):
        if call.reachable:
            reason = f"{call.caller}가 부르는 쿼리가 Mapper에 없음. URL에서 이어지는 경로가 있어 실행하면 오류가 날 수 있음"
        else:
            reason = f"{call.caller}가 부르는 쿼리가 Mapper에 없음. 어떤 URL에서도 이어지지 않음 (죽은 코드)"
        findings.append(
            Finding(
                kind="broken_sql",
                name=call.query_id,
                location=call.location,
                confidence="높음",
                reason=reason,
                evidence=call.hint,
            )
        )
    # 실제로 호출되는 것부터
    findings.sort(key=lambda f: "죽은 코드" in f.reason)
    return findings


def reachable_methods(conn: sqlite3.Connection) -> set[int]:
    """URL 진입점과 설정이 부르는 메서드에서 출발해 닿을 수 있는 메서드 전부."""
    adjacency: dict[int, list[int]] = {}
    for e in conn.execute("SELECT src_id, dst_id FROM edges WHERE src_kind = 'method' AND dst_kind = 'method'"):
        adjacency.setdefault(e["src_id"], []).append(e["dst_id"])
    frontier = [r["method_id"] for r in conn.execute("SELECT DISTINCT method_id FROM url_mappings")]
    config_methods = [r["value"] for r in conn.execute("SELECT DISTINCT value FROM refs WHERE kind = 'method'")]
    if config_methods:
        frontier += [
            r["id"] for r in conn.execute(
                f"SELECT id FROM methods WHERE name IN ({','.join('?' * len(config_methods))})", config_methods
            )
        ]
    seen = set(frontier)
    while frontier:
        current = frontier.pop()
        for nxt in adjacency.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


def _unused_sql(conn: sqlite3.Connection, literal_blob: str) -> list[Finding]:
    rows = conn.execute(
        """SELECT s.id, s.full_id, s.line, f.path, f.header_comment
           FROM statements s JOIN files f ON f.id = s.file_id
           WHERE NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst_kind = 'statement' AND e.dst_id = s.id)
           ORDER BY f.path, s.line"""
    ).fetchall()
    findings = []
    for r in rows:
        short_id = r["full_id"].split(".", 1)[-1]
        if _in_text(r["full_id"], literal_blob):
            continue  # 문자열로 어딘가에서 쓰이고 있다
        evidence = _mention(r["header_comment"], short_id)
        findings.append(
            Finding(
                kind="sql",
                name=r["full_id"],
                location=f"{r['path']}:{r['line']}",
                confidence="높음",
                reason="이 쿼리 ID를 부르는 Java 코드가 없음",
                evidence=evidence,
            )
        )
    return findings


def _unused_urls(conn: sqlite3.Connection, literal_blob: str) -> list[Finding]:
    referenced = {r["value"] for r in conn.execute("SELECT DISTINCT value FROM refs WHERE kind = 'url'")}
    referenced |= {
        r["dst_name"] for r in conn.execute("SELECT DISTINCT dst_name FROM edges WHERE dst_kind = 'url'")
    }
    rows = conn.execute(
        """SELECT DISTINCT u.url, m.line_start, f.path, c.name AS class_name, m.name AS method_name, f.kind AS lang
           FROM url_mappings u JOIN methods m ON m.id = u.method_id
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           ORDER BY u.url"""
    ).fetchall()
    url_names, used_names = _django_url_names(conn)
    findings = []
    seen_python: set[str] = set()
    for r in rows:
        url = r["url"]
        if r["lang"] == "python":
            # Django는 {% url 'basket:summary' %}·reverse()로 이름을 불러 경로 문자열이 코드에 없다.
            # 이름이 있으면 이름(네임스페이스를 뗀 끝부분)으로 찾고, 이름도 없고 경로에 변수가 있으면 판단하지 않는다
            if url in seen_python:
                continue  # 클래스 뷰는 get·post·get_queryset 등 여러 메서드가 같은 URL에 걸린다
            seen_python.add(url)
            names = url_names.get(url, [])
            if any(n in used_names for n in names) or (not names and ("<" in url or "(" in url)):
                continue
        if url in referenced or _in_text(url, literal_blob) or "{" in url or url == "/":
            continue  # "/"는 브라우저가 처음 여는 주소라 부르는 곳이 없어도 쓰인다
        findings.append(
            Finding(
                kind="url",
                name=url,
                location=f"{r['path']}:{r['line_start']}",
                confidence="후보",
                reason=f"{r['class_name']}.{r['method_name']}. 화면·스크립트·설정·코드에서 부르는 곳을 못 찾음",
            )
        )
    return findings


def _unused_methods(conn: sqlite3.Connection) -> list[Finding]:
    config_methods = {r["value"] for r in conn.execute("SELECT DISTINCT value FROM refs WHERE kind = 'method'")}
    called = {r["dst_id"] for r in conn.execute("SELECT DISTINCT dst_id FROM edges WHERE dst_kind = 'method'")}

    # 저장소 안 인터페이스·부모 클래스가 선언한 메서드 이름. @Override인데 여기 없으면 프레임워크 콜백이다.
    declared: dict[str, set[str]] = {}
    for r in conn.execute("SELECT c.name, m.name AS method FROM classes c JOIN methods m ON m.class_id = c.id"):
        declared.setdefault(r["name"].split(".")[-1], set()).add(r["method"])

    rows = conn.execute(
        f"""SELECT m.id, m.name, m.line_start, m.line_end, m.annotations, m.body_tokens,
                   c.name AS class_name, c.superclass, c.interfaces, f.path
            FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
            WHERE c.kind IN ('class', 'module') AND c.stereotype IN ({",".join("?" * len(STEREOTYPES))})
                  AND f.kind != 'python'
            ORDER BY f.path, m.line_start""",
        STEREOTYPES,
    ).fetchall()
    findings = []
    for r in rows:
        if r["id"] in called or r["name"] in config_methods:
            continue
        simple_class = r["class_name"].split(".")[-1]
        if r["name"] == simple_class or r["name"] == "<module>":  # 생성자, Python 모듈 최상위 코드
            continue
        if r["name"].startswith("__") and r["name"].endswith("__"):  # Python 특수 메서드
            continue
        annotations = set(json.loads(r["annotations"]))
        if annotations & FRAMEWORK_CALLED:
            continue
        if "Override" in annotations:
            parents = json.loads(r["interfaces"]) + ([r["superclass"]] if r["superclass"] else [])
            if not any(r["name"] in declared.get(p, set()) for p in parents):
                continue  # 저장소 밖 인터페이스의 메서드 → 프레임워크가 부름
        if re.match(r"^(get|set|is)[A-Z]", r["name"]) and r["body_tokens"] < 20:
            continue  # 단순 getter/setter
        findings.append(
            Finding(
                kind="method",
                name=f"{r['class_name']}.{r['name']}",
                location=f"{r['path']}:{r['line_start']}",
                confidence="후보",
                reason=f"부르는 코드·설정이 없음 ({r['line_end'] - r['line_start'] + 1}줄)",
            )
        )
    return findings


def _django_url_names(conn: sqlite3.Connection) -> tuple[dict[str, list[str]], set[str]]:
    """(URL → path(name=...) 이름들, 템플릿·코드가 부르는 이름). 이름은 네임스페이스를 뗀 끝부분으로 맞춘다
    (basket:summary → summary). 네임스페이스까지 맞추지 않아 "쓰는 것"으로 넉넉하게 본다."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'pyurlnames'").fetchone()
    if row is None:
        return {}, set()
    facts = load_facts(conn)
    # path("x/", view, name="x")의 "x" 자체는 부르는 곳이 아니다 (path 호출이 여러 줄이어도 몇 줄 안에 있다)
    defined: dict[tuple[int, str], list[int]] = {}
    for f in facts:
        if f["k"] == "urlname":
            defined.setdefault((f["file_id"], f["name"]), []).append(f["line"])
    used = {r["value"] for r in conn.execute("SELECT DISTINCT value FROM refs WHERE kind = 'urlname'")}
    for r in conn.execute(
        """SELECT DISTINCT s.value, s.line, f.id AS file_id FROM string_literals s JOIN methods m ON m.id = s.method_id
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'python' AND length(s.value) < 80"""
    ):
        if not any(0 <= r["line"] - line <= 6 for line in defined.get((r["file_id"], r["value"]), [])):
            used.add(r["value"])
    used |= {f["value"] for f in facts if f["k"] in ("urlref", "str")}
    return json.loads(row["value"]), {u.rsplit(":", 1)[-1] for u in used}


# Python 메서드 중 프레임워크가 이름으로 부르는 것 (Django 모델·폼·뷰·관리자·필드·명령·DRF 훅)
_PY_HOOK = re.compile(
    r"^(get_|has_|clean|save|delete|render|to_python|from_db_value|value_|format_|process_|handle|add_arguments|"
    r"ready|setup|dispatch|validate|deconstruct|contribute_to_class|formfield|pre_save|post_|test_func|as_|run$|"
    r"execute|perform_|create|update|destroy|list$|retrieve|partial_update|filter_|paginate|natural_key|check|"
    r"items$|location$|lastmod$|changefreq$|priority$|form_|lookup|queryset|label_from_instance|decompress|"
    r"compress|widget_attrs|bound_data|has_changed|prepare_|index_queryset|should_update|full_clean|"
    r"serialize|parse|load|dump|emit|filter$|apply_|translate_url|on_|resolve|order_|read_queryset)")
# 프레임워크·도구가 이름이나 등록으로 부르는 모듈 (템플릿 태그·시그널·관리자·앱 설정·URL·설정·명령·테스트)
_PY_FRAMEWORK_MODULES = re.compile(
    r"(^|\.)(templatetags\..*|context_processors|receivers|signals|admin|apps|urls|settings\w*|conf|wsgi|asgi|manage|"
    r"checks|migrations\..*|management\.commands\..*|tests?(\..*)?|test_\w+|conftest|setup|tasks|middleware|"
    r"__main__|main)$")


def _unused_python_methods(conn: sqlite3.Connection) -> list[Finding]:
    """Python은 호출 대상을 이름으로만 알 수 있어, 확실히 안 쓰는 것만 후보로 둔다.
    이름이 코드 어디에서든 불리거나(obj.x(), 콜백 인자, 문자열 "x", 템플릿 {{ obj.x }}) 같은 이름이 여러 곳에 정의돼
    있거나(재정의·다형성), 데코레이터가 붙었거나(시그널·템플릿 태그·프로퍼티·작업), 프레임워크 훅 이름이거나,
    프레임워크가 부르는 모듈(templatetags·receivers·admin·management 등)이면 뺀다. 뷰(URL에 걸린 클래스)도 뺀다."""
    if conn.execute("SELECT 1 FROM files WHERE kind = 'python' LIMIT 1").fetchone() is None:
        return []
    rows = conn.execute(
        """SELECT m.id, m.name, m.line_start, m.line_end, m.annotations, c.name AS class_name, c.kind AS class_kind,
                  c.stereotype, c.superclass, c.interfaces, f.path, f.module
           FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'python' ORDER BY f.path, m.line_start"""
    ).fetchall()
    called = {r["dst_id"] for r in conn.execute("SELECT DISTINCT dst_id FROM edges WHERE dst_kind = 'method'")}
    names: set[str] = set()
    for r in conn.execute("SELECT callee, name_args, keyword_args FROM calls"):
        names.add(r["callee"])
        names.update(a.split(".")[-1] for a in json.loads(r["name_args"]))
        for v in json.loads(r["keyword_args"]).values():
            names.update(re.findall(r"[A-Za-z_]\w*", v))
    literals = {r["value"] for r in conn.execute("SELECT DISTINCT value FROM string_literals WHERE length(value) < 80")}
    names |= literals
    names |= {v.rsplit(".", 1)[-1] for v in literals if "." in v}  # "oscar.core.loading.default_class_loader"
    names |= {r["value"] for r in conn.execute("SELECT DISTINCT value FROM refs WHERE kind IN ('attr', 'method')")}
    for f in load_facts(conn):
        if f["k"] in ("str", "urlref"):
            names.add(f["value"])
        elif f["k"] == "attr":
            names.add(f["value"].split(".")[-1])
        elif f["k"] == "names":
            names.update(f["value"].split())
    for r in conn.execute("SELECT target FROM url_patterns"):
        names.update(re.findall(r"[A-Za-z_]\w*", r["target"]))
    # getattr(self, "apply_" + x), "_validate_%s" % type, f"handle_{x}": 이 앞부분으로 시작하는 이름은 동적으로 불린다
    heads = {re.split(r"%|\{", v, maxsplit=1)[0] if ("%" in v or "{" in v) else v for v in literals}
    prefixes = tuple(h for h in heads if h.endswith("_") and 2 < len(h) < 40 and re.fullmatch(r"\w+", h))
    defined: dict[str, int] = {}  # 클래스 메서드 이름별 정의 수 (2곳 이상이면 재정의·다형성이라 이름만으로 판단 못 함)
    for r in rows:
        if r["class_kind"] == "class":
            defined[r["name"]] = defined.get(r["name"], 0) + 1
    class_bases: dict[str, list[str]] = {}
    for r in conn.execute(
        """SELECT c.name, c.superclass, c.interfaces FROM classes c JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'python' AND c.kind = 'class'"""
    ):
        class_bases.setdefault(r["name"].split(".")[-1], [b for b in [r["superclass"], *json.loads(r["interfaces"] or "[]")] if b])

    def external_base(bases: list[str], depth: int = 0) -> bool:
        """조상 중에 저장소 밖 클래스(models.Model·forms.Form·tables.Table)가 있나."""
        return depth < 10 and any(b not in class_bases or external_base(class_bases[b], depth + 1) for b in bases)

    findings = []
    for r in rows:
        name = r["name"]
        if r["id"] in called or name in names or name.startswith("<") or (name.startswith("__") and name.endswith("__")):
            continue
        if defined.get(name, 0) > 1 or r["stereotype"] == "Controller" or r["path"].startswith(("docs/", "tests/")):
            continue
        if set(json.loads(r["annotations"])) - {"staticmethod", "classmethod"}:
            continue  # @receiver·@register.filter·@property·@shared_task 등은 프레임워크가 부른다
        if _PY_FRAMEWORK_MODULES.search(r["module"] or "") or (prefixes and name.startswith(prefixes)):
            continue
        if r["class_kind"] == "class":
            bases = [b for b in [r["superclass"], *json.loads(r["interfaces"] or "[]")] if b]
            if external_base(bases) and _PY_HOOK.match(name):
                continue  # 저장소 밖 부모(models.Model·forms.Form·View)가 부르는 훅
        findings.append(Finding(
            kind="method",
            name=f"{r['class_name']}.{name}",
            location=f"{r['path']}:{r['line_start']}",
            confidence="후보",
            reason=f"이 이름을 부르는 코드·템플릿·문자열이 없음 ({r['line_end'] - r['line_start'] + 1}줄)",
        ))
    return findings


def _unused_jsps(conn: sqlite3.Connection, literal_blob: str) -> list[Finding]:
    view_targets = {r["dst_id"] for r in conn.execute("SELECT dst_id FROM edges WHERE dst_kind = 'view'")}
    # include·경로 참조는 상대 경로라서 파일 이름 뒷부분으로 비교한다
    ref_values = [r["value"] for r in conn.execute("SELECT value FROM refs WHERE kind IN ('include', 'jsp')")]
    ref_names = {v.rsplit("/", 1)[-1] for v in ref_values}
    # 설정에 확장자 없이 적힌 뷰 경로 (에러 페이지 매핑 등)
    ref_names |= {r["value"].rsplit("/", 1)[-1] + ".jsp" for r in conn.execute("SELECT value FROM refs WHERE kind = 'path'")}
    findings = []
    for r in conn.execute("SELECT id, path FROM files WHERE kind = 'jsp' ORDER BY path"):
        if r["id"] in view_targets:
            continue
        name = r["path"].rsplit("/", 1)[-1]
        if name in ref_names:
            continue
        # 컨트롤러가 뷰 이름을 문자열 조합으로 만드는 경우: 확장자 없는 이름이 리터럴에 있음
        if _in_text(name.removesuffix(".jsp"), literal_blob):
            continue
        findings.append(
            Finding(
                kind="jsp",
                name=name,
                location=r["path"],
                confidence="후보",
                reason="컨트롤러가 반환하지 않고 다른 JSP·설정도 가리키지 않음",
            )
        )
    return findings


def _in_text(value: str, text: str) -> bool:
    """value가 text 안에 독립된 단어로 있는가. "Note.deleteOld"는 "Note.deleteOldNotes"에 없다고 본다."""
    return re.search(rf"(?<![\w.]){re.escape(value)}(?![\w])", text) is not None


def _mention(comment: str | None, word: str) -> str | None:
    """주석에서 word가 나오는 줄을 모두 돌려준다 (수정 이력에 남은 사람의 설명)."""
    if not comment:
        return None
    lines = [" ".join(line.split()) for line in comment.splitlines() if word in line]
    return " / ".join(lines) or None
