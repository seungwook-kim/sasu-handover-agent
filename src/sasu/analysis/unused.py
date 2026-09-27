"""부르는 곳을 찾을 수 없는 SQL·URL·메서드·JSP를 찾는다.

"안 쓴다"고 단정하지 않는다. 정적 분석으로 못 찾는 호출(외부 링크, 리플렉션,
다른 시스템)이 있을 수 있어서, 종류마다 확신 정도를 붙인다.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field

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
    report.method = _unused_methods(conn)
    report.jsp = _unused_jsps(conn, literal_blob)
    return report


def _broken_sql(conn: sqlite3.Connection) -> list[Finding]:
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
    findings = []
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
        caller = f"{r['class_name']}.{r['method_name']}"
        if r["method_id"] in reachable:
            reason = f"{caller}가 부르는 쿼리가 Mapper에 없음. URL에서 이어지는 경로가 있어 실행하면 오류가 날 수 있음"
        else:
            reason = f"{caller}가 부르는 쿼리가 Mapper에 없음. 어떤 URL에서도 이어지지 않음 (죽은 코드)"
        findings.append(
            Finding(
                kind="broken_sql",
                name=r["value"],
                location=f"{r['path']}:{r['line']}",
                confidence="높음",
                reason=reason,
                evidence=hint,
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
        short_id = r["full_id"].split(".", 1)[1]
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
        """SELECT DISTINCT u.url, m.line_start, f.path, c.name AS class_name, m.name AS method_name
           FROM url_mappings u JOIN methods m ON m.id = u.method_id
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           ORDER BY u.url"""
    ).fetchall()
    findings = []
    for r in rows:
        url = r["url"]
        if url in referenced or _in_text(url, literal_blob) or "{" in url:
            continue
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
            WHERE c.kind = 'class' AND c.stereotype IN ({",".join("?" * len(STEREOTYPES))})
            ORDER BY f.path, m.line_start""",
        STEREOTYPES,
    ).fetchall()
    findings = []
    for r in rows:
        if r["id"] in called or r["name"] in config_methods:
            continue
        simple_class = r["class_name"].split(".")[-1]
        if r["name"] == simple_class:  # 생성자
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
