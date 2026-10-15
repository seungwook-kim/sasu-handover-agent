"""문서 조각과 코드를 잇는다.

조각 글에서 URL(.do 등), 테이블 이름, 클래스·메서드 이름, 한글 업무명(클래스 주석의 업무명)을 찾아
코드에 있는 이름과 맞춘다. 연결은 코드 id가 아니라 이름으로 저장한다 (다시 분석하면 id가 바뀌므로).
코드에 없는 URL·테이블처럼 보이는 이름도 found=0으로 남긴다 (문서–코드 불일치 후보).
"""

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from sasu.linker import normalize_url

URL, TABLE, CLASS, METHOD, LABEL, CONFIG = "url", "table", "class", "method", "label", "config"

_URL_TOKEN = re.compile(r"(?<![\w/:.@-])(/[A-Za-z0-9_\-./{}]*[A-Za-z0-9_}])")
_UPPER_TOKEN = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*_?[A-Z0-9_]{2,})(?![A-Za-z0-9_])")
_ANY_IDENT = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]{2,})(?![A-Za-z0-9_])")
_CLASS_METHOD = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9_]+)\s*[.#]\s*([a-z_][A-Za-z0-9_]+)(?:\(\))?")
_CAMEL = re.compile(r"^[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*$")
_DOTTED = re.compile(r"(?<![\w.\-])([A-Za-z_][\w\-]*(?:\.[\w\-]+)+)(?![\w\-])")
_TABLE_WORD = re.compile(r"테이블|table|TB\b", re.IGNORECASE)
_UPPER_STOP = {"API", "URL", "SQL", "DB", "WAS", "WEB", "HTTP", "HTTPS", "JSON", "XML", "HTML", "CSV", "PDF", "ID", "PW",
               "TODO", "FIXME", "NULL", "AND", "OR", "NOT", "SELECT", "INSERT", "UPDATE", "DELETE", "FROM", "WHERE",
               "JOIN", "INTO", "VALUES", "SET", "LIKE", "ORDER", "GROUP", "BY", "UTF", "EUC", "KST", "UTC", "AM", "PM",
               "CPU", "RAM", "OS", "IP", "PC", "SMS", "LMS", "MMS", "ERP", "SSO", "LDAP", "VPN", "DNS", "SSL", "TLS",
               "JDBC", "JPA", "JSP", "MVC", "CRUD", "ASAP", "FAQ", "QNA", "NAS", "SAN", "DBA", "PM", "PL", "QA", "SI",
               "SM", "OK", "NG", "HWP", "HWPX", "DOCX", "XLSX", "PPTX", "CRON", "BATCH", "LOG", "ERROR", "WARN", "INFO",
               "DEBUG", "TRUE", "FALSE", "YES", "NO", "EXCEL", "README", "MAX", "MIN", "COUNT", "SUM", "AVG"}


@dataclass(frozen=True)
class Link:
    kind: str
    value: str
    found: bool


class CodeNames:
    """코드 쪽 이름 사전. 조각 여러 개를 이을 때 한 번만 읽는다."""

    def __init__(self, conn: sqlite3.Connection):
        self.urls = {r[0] for r in conn.execute("SELECT DISTINCT url FROM url_mappings")}
        self.url_ext = Counter(u.rsplit(".", 1)[-1] for u in self.urls if "." in u.rsplit("/", 1)[-1])
        self.url_heads = Counter(u.strip("/").split("/", 1)[0] for u in self.urls if u.strip("/"))
        tables = [r[0] for r in conn.execute(
            "SELECT DISTINCT table_name FROM statement_tables UNION SELECT table_name FROM classes WHERE table_name IS NOT NULL")]
        self.tables = {t.upper(): t for t in tables if t}
        prefixes: Counter = Counter()
        for t in self.tables:
            if len(t) >= 6 and t.isupper():
                prefixes[t[:5]] += 1
        self.table_prefixes = {p for p, n in prefixes.items() if n >= 3}
        # SQL에 나오는 다른 대문자 이름(컬럼 LOCK_AT 등): '테이블' 옆에 있어도 없는 테이블로 보지 않는다
        self.sql_words: set[str] = set()
        for (sql,) in conn.execute("SELECT sql FROM statements"):
            self.sql_words.update(w.upper() for w in _ANY_IDENT.findall(sql or "") if "_" in w)
        self.classes: dict[str, int] = Counter(r[0] for r in conn.execute("SELECT name FROM classes WHERE kind != 'module'"))
        self.methods: dict[str, list[str]] = defaultdict(list)
        for r in conn.execute("SELECT m.name, c.name AS cls FROM methods m JOIN classes c ON c.id = m.class_id"):
            self.methods[r["name"]].append(f"{r['cls']}.{r['name']}")
        from sasu.analysis.labels import Labels

        # 업무명(클래스 주석) 세 글자 이상만. 띄어쓰기는 무시하고 맞춘다
        self._config(conn)
        self.labels = sorted({label for label in Labels(conn).classes().values()
                              if label and len(label.replace(" ", "")) >= 3 and re.search(r"[가-힣]", label)},
                             key=len, reverse=True)

    def _config(self, conn: sqlite3.Connection) -> None:
        """설정 키(Globals.DbType)·설정 값(/upload/allinone)·설정 파일 이름 → "경로:줄"/"경로",
        스케줄 설정 파일 → 실행 메서드."""
        from sasu.docs.configs import config_facts, scheduled_targets
        from sasu.handover import repo_of

        repo = repo_of(conn)
        facts = config_facts(repo)
        self.config_keys: dict[str, str] = {}
        self.config_values: dict[str, str] = {}
        self.config_files: dict[str, str] = {}
        for f in facts:
            if "." in f.key and len(f.key) >= 6:
                self.config_keys.setdefault(f.key, f.where)
            if f.value.startswith("/") and len(f.value) >= 6 and " " not in f.value:
                self.config_values.setdefault(f.value.rstrip("/"), f.where)
            self.config_files.setdefault(f.path.rsplit("/", 1)[-1], f.path)
        self.scheduled = scheduled_targets(repo)

    # ── 글에서 찾기 ───────────────────────────────────────

    def find(self, text: str) -> list[Link]:
        links: dict[tuple[str, str], bool] = {}

        def add(kind: str, value: str, found: bool):
            key = (kind, value)
            links[key] = links.get(key, False) or found

        for raw in _URL_TOKEN.findall(text):
            url = raw.rstrip(".")
            norm = normalize_url(url)
            if norm in self.urls:
                add(URL, norm, True)
            elif url.rstrip("/") in self.config_values:  # 설정 값 (/upload/allinone)
                add(CONFIG, self.config_values[url.rstrip("/")], True)
            elif self.looks_like_url(norm):
                add(URL, norm, False)
        for cls, method in _CLASS_METHOD.findall(text):
            label = f"{cls}.{method}"
            if any(m == label for m in self.methods.get(method, [])):
                add(METHOD, label, True)
        for token in set(_ANY_IDENT.findall(text)):
            upper = token.upper()
            # 소문자 낱말(user)이 테이블 이름과 우연히 같은 것은 뺀다: 대문자이거나 밑줄이 있을 때만
            if upper in self.tables and (token.isupper() or "_" in token):
                add(TABLE, self.tables[upper], True)
                continue
            if token in self.classes and re.search(r"[a-z]", token) and re.search(r"[A-Z]", token):
                add(CLASS, token, True)
            elif _CAMEL.match(token) and len(token) >= 8 and 0 < len(self.methods.get(token, [])) <= 3:
                for label in self.methods[token]:
                    add(METHOD, label, True)
        for token in set(_DOTTED.findall(text)):
            if token in self.config_keys:
                add(CONFIG, self.config_keys[token], True)
            elif token in self.config_files:
                add(CONFIG, self.config_files[token], True)
                for bean, name in self.scheduled.get(token, []):
                    label = f"{bean[:1].upper()}{bean[1:]}.{name}"
                    if label in self.methods.get(name, []):
                        add(METHOD, label, True)
        for token in set(_UPPER_TOKEN.findall(text)):
            if token.upper() not in self.tables and self.looks_like_table(token, text):
                add(TABLE, token, False)
        compact = re.sub(r"\s+", "", text)
        taken: list[str] = []
        for label in self.labels:
            key = label.replace(" ", "")
            if key in compact and not any(key in t for t in taken):  # "휴가관리"가 맞으면 "휴가" 같은 짧은 것은 빼기
                taken.append(key)
                add(LABEL, label, True)
        return [Link(k, v, f) for (k, v), f in links.items()]

    def looks_like_url(self, url: str) -> bool:
        """코드에는 없지만 이 프로젝트 URL처럼 생겼나 (확장자 .do가 같거나 첫 경로가 같은 URL이 많다)."""
        last = url.rsplit("/", 1)[-1]
        if "." in last:
            ext = last.rsplit(".", 1)[-1]
            return self.url_ext.get(ext, 0) >= 3 and ext not in ("jsp", "html", "js", "css", "png", "jpg", "xml")
        head = url.strip("/").split("/", 1)[0]
        return url.count("/") >= 2 and self.url_heads.get(head, 0) >= 3

    def looks_like_table(self, token: str, text: str) -> bool:
        """코드에 없는 대문자 낱말이 테이블 이름처럼 생겼나: 테이블 이름 앞부분(COMTN 등)이 같거나, 옆에 '테이블'."""
        if token in _UPPER_STOP or len(token) < 4 or token.isdigit() or token.upper() in self.sql_words:
            return False
        if token[:5] in self.table_prefixes and len(token) >= 6:
            return True
        for m in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text):
            around = text[max(0, m.start() - 12):m.end() + 12]
            if _TABLE_WORD.search(around) and ("_" in token or len(token) >= 6):
                return True
        return False


def link_chunks(conn: sqlite3.Connection, chunk_ids: list[int] | None = None, names: CodeNames | None = None) -> int:
    """조각의 연결을 (다시) 만든다. chunk_ids가 없으면 전부. 돌려주는 값: 코드에 있는 연결 수."""
    names = names or CodeNames(conn)
    if chunk_ids is None:
        rows = conn.execute("SELECT id, heading, text FROM doc_chunks").fetchall()
        conn.execute("DELETE FROM doc_links")
    else:
        marks = ",".join("?" * len(chunk_ids)) or "NULL"
        rows = conn.execute(f"SELECT id, heading, text FROM doc_chunks WHERE id IN ({marks})", chunk_ids).fetchall()
        conn.execute(f"DELETE FROM doc_links WHERE chunk_id IN ({marks})", chunk_ids)
    found = 0
    for r in rows:
        # 제목 경로 중 가장 가까운 제목만 (문서 제목 "휴가관리 인수인계서"가 모든 조각을 휴가관리에 잇지 않게)
        links = names.find(f"{last_heading(r['heading'])}\n{r['text']}")
        conn.executemany("INSERT INTO doc_links (chunk_id, kind, value, found) VALUES (?, ?, ?, ?)",
                         [(r["id"], l.kind, l.value, int(l.found)) for l in links])
        found += sum(l.found for l in links)
    conn.commit()
    return found


def last_heading(heading: str | None) -> str:
    return (heading or "").split(" > ")[-1]


def relink_all(conn: sqlite3.Connection) -> int:
    """다시 분석한 뒤 부른다. 문서가 없으면 아무것도 하지 않는다."""
    if not has_docs(conn):
        return 0
    return link_chunks(conn)


def has_docs(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT 1 FROM doc_chunks LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:  # 읽기 전용으로 연 예전 분석 파일 (테이블이 없음)
        return False
