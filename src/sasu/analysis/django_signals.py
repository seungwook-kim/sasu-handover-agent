"""Django(Python) 프로젝트에서 전임자에게 물어야 할 곳.

Java의 "설정에만 있는 빈"처럼, Django도 코드 흐름(URL → 뷰 → 호출)에 보이지 않게 동작을 바꾸는 장치가 많다.
- 시그널 receiver: 부르는 코드 없이 다른 곳의 저장·주문 완료에 붙어 실행된다
- 미들웨어·컨텍스트 프로세서: 모든 요청·모든 화면에서 실행된다
- settings 업무 상수(OSCAR_* 등)로 나뉘는 분기: 운영 값이 무엇인지 코드만 봐서는 모른다
- 커스텀 매니저·쿼리셋: Model.open.all()에 status="Open" 같은 조건이 숨어 있다
- get_class·get_model: 포크한 앱이 있으면 다른 클래스로 바뀐다
- 관리 명령·Celery 작업: 누가 언제 실행하는지 코드에 없다
- raw SQL·extra(), 트랜잭션(atomic·select_for_update) 경계

python_parser가 모듈 가짜 클래스의 pyclass 메타에 남긴 사실(facts)과 calls 표만 쓴다 (LLM 없음).
Java 프로젝트에는 Python 파일이 없어 아무것도 만들지 않는다.
"""

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

# Django 자체 설정 (업무 상수가 아니라 프레임워크 설정이라 묻지 않는다)
_DJANGO_SETTINGS = re.compile(
    r"^(DEBUG|SECRET_KEY|ALLOWED_HOSTS|INSTALLED_APPS|MIDDLEWARE|TEMPLATES|ROOT_URLCONF|WSGI_APPLICATION|"
    r"ASGI_APPLICATION|SITE_ID|APPEND_SLASH|PREPEND_WWW|ADMINS|MANAGERS|INTERNAL_IPS|LOCALE_PATHS|LOGGING\w*|"
    r"FIXTURE_DIRS|TEST_RUNNER|SERVER_EMAIL|SIGNING_BACKEND|FORMAT_MODULE_PATH|SILENCED_SYSTEM_CHECKS|"
    r"(AUTH|SESSION|CSRF|SECURE|EMAIL|STATIC|STATICFILES|MEDIA|LANGUAGE|LANGUAGES|USE|DATABASE|DATABASES|CACHE|"
    r"CACHES|CACHE_MIDDLEWARE|LOGIN|LOGOUT|FILE_UPLOAD|DATA_UPLOAD|DEFAULT|TIME|DATE|DATETIME|DECIMAL|NUMBER|"
    r"THOUSAND|MONTH_DAY|YEAR_MONTH|SHORT_DATE|SHORT_DATETIME|FIRST_DAY|X_FRAME|PASSWORD|MESSAGE|CELERY|"
    r"STORAGES|FORM_RENDERER|ABSOLUTE_URL|DISALLOWED|IGNORABLE|CORS|REST_FRAMEWORK|SIMPLE_JWT|"
    r"BASE_DIR|PROJECT_DIR|HAYSTACK|THUMBNAIL|COMPRESS|DEBUG_TOOLBAR)(_\w+)?)$"
)
# Django가 모델을 저장·삭제할 때 보내는 시그널 (보내는 코드가 프로젝트에 없다)
MODEL_SIGNALS = {"pre_save", "post_save", "pre_delete", "post_delete", "m2m_changed", "pre_init", "post_init"}
_BUILTIN_SIGNALS = MODEL_SIGNALS | {
    "request_started", "request_finished", "user_logged_in", "user_logged_out", "user_login_failed",
    "pre_migrate", "post_migrate", "setting_changed", "class_prepared", "got_request_exception",
}
_SIGNAL_WRITES = {"pre_save": ("insert", "update"), "post_save": ("insert", "update"),
                  "pre_delete": ("delete",), "post_delete": ("delete",)}
_LOADERS = ("get_class", "get_classes", "get_model", "get_profile_class")
_TASK_DECORATORS = {"shared_task", "task", "periodic_task", "db_task", "db_periodic_task", "job", "actor"}
# 예상한 "없음"을 잡는 예외 (조회했는데 없으면 넘어감). 모든 예외를 삼키는 것보다 덜 위험하다
NARROW_EXCEPTIONS = re.compile(r"(DoesNotExist|ObjectDoesNotExist|KeyError|IndexError|AttributeError|"
                               r"StopIteration|NoReverseMatch|Resolver404|ImportError|ModuleNotFoundError)$")


@dataclass
class DjangoSignal:
    kind: str
    key_detail: str
    method_id: int | None
    question: str
    why: str
    file_path: str | None = None
    line: int | None = None
    target: str | None = None
    reach: int | None = None  # 영향 범위(닿는 URL 수)를 메서드 자신 대신 이 값으로 (시그널은 보내는 쪽, 미들웨어는 전체)


def load_facts(conn: sqlite3.Connection) -> list[dict]:
    """python_parser가 남긴 사실들 + 파일 정보 (file_id, path, module)."""
    result = []
    for r in conn.execute(
        """SELECT m.value, c.file_id, f.path, f.module FROM meta m
           JOIN classes c ON c.id = CAST(substr(m.key, 9) AS INTEGER) JOIN files f ON f.id = c.file_id
           WHERE m.key LIKE 'pyclass:%' AND c.kind = 'module' AND f.kind = 'python'"""
    ):
        for fact in json.loads(r["value"]).get("facts", []):
            result.append({**fact, "file_id": r["file_id"], "path": r["path"], "module": r["module"] or ""})
    return result


def has_python(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM files WHERE kind = 'python' LIMIT 1").fetchone() is not None


class _Methods:
    """Python 메서드 찾기: 파일·줄, 파일·이름."""

    def __init__(self, conn: sqlite3.Connection):
        self.rows = {
            r["id"]: r for r in conn.execute(
                """SELECT m.id, m.name, m.line_start, m.line_end, m.annotations, c.id AS class_id, c.name AS class_name,
                          c.kind AS class_kind, c.superclass, c.interfaces, f.id AS file_id, f.path, f.module
                   FROM methods m JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
                   WHERE f.kind = 'python'""")
        }
        self.by_file: dict[int, list] = defaultdict(list)
        self.by_name: dict[str, list[int]] = defaultdict(list)
        for mid, r in self.rows.items():
            self.by_file[r["file_id"]].append(r)
            self.by_name[r["name"]].append(mid)

    def at(self, file_id: int, line: int, exact: bool = False) -> int | None:
        rows = [r for r in self.by_file.get(file_id, []) if r["name"] != "<module>"
                and (r["line_start"] == line if exact else r["line_start"] <= line <= r["line_end"])]
        if not rows:
            module = [r for r in self.by_file.get(file_id, []) if r["name"] == "<module>"]
            return None if exact or not module else module[0]["id"]
        return max(rows, key=lambda r: r["line_start"])["id"]

    def named(self, file_id: int, name: str) -> int | None:
        rows = [r for r in self.by_file.get(file_id, []) if r["name"] == name]
        if rows:
            return rows[0]["id"]
        same = self.by_name.get(name, [])
        return same[0] if len(same) == 1 else None

    def label(self, mid: int) -> str:
        r = self.rows[mid]
        return f"{r['class_name']}.{r['name']}"


def django_signals(conn: sqlite3.Connection, reach: dict[int, int]) -> list[DjangoSignal]:
    if not has_python(conn):
        return []
    facts = load_facts(conn)
    methods = _Methods(conn)
    calls = conn.execute(
        """SELECT c.method_id, c.receiver, c.callee, c.string_args, c.name_args, c.keyword_args, c.line
           FROM calls c JOIN methods m ON m.id = c.method_id JOIN classes k ON k.id = m.class_id
           JOIN files f ON f.id = k.file_id WHERE f.kind = 'python'"""
    ).fetchall()
    total_urls = conn.execute("SELECT COUNT(DISTINCT url) FROM url_mappings").fetchone()[0]
    found: list[DjangoSignal] = []
    found += _signals(conn, facts, calls, methods, reach)
    found += _request_hooks(conn, methods, total_urls, {f["value"] for f in facts if f["k"] == "registered"})
    found += _setting_branches(facts, methods, reach)
    found += _hidden_filters(conn, calls, methods)
    found += _swappable(calls, methods)
    found += _batch_jobs(methods)
    found += _raw_sql(conn, calls, methods)
    found += _transactions(calls, methods)
    return found


# ── 시그널 ─────────────────────────────────────────────────────


def _signal_graph(conn, facts, calls, methods: _Methods):
    """(받는 쪽 [(메서드, 시그널들, sender, 연결 방식)], 시그널 → 보내는 메서드, (모델, 쓰기 종류) → 쓰는 메서드)."""
    handlers: list[tuple[int, list[str], str | None, str]] = []  # (메서드, 시그널들, sender, 어떻게 연결)
    for f in facts:
        if f["k"] == "receiver":
            mid = methods.at(f["file_id"], f["line"], exact=True)
            if mid is not None:
                handlers.append((mid, [s for s in f["signals"].split(",") if s], f.get("sender"),
                                 f"@receiver({f['signals']}{', sender=' + f['sender'] if f.get('sender') else ''})"))
    # 프로젝트가 정의한 시그널 (basket_addition = Signal()). connect는 Sphinx app.connect 같은 다른 것도 있어 아는 시그널만
    known = _BUILTIN_SIGNALS | {r["key"].rsplit(":", 1)[-1] for r in conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'pyassign:%'") if json.loads(r["value"]).get("type") == "Signal"}
    for c in calls:
        if c["callee"] != "connect" or not c["receiver"]:
            continue
        names = json.loads(c["name_args"])
        signal = c["receiver"].split(".")[-1]
        if not names or signal not in known:
            continue
        handler = methods.named(methods.rows[c["method_id"]]["file_id"], names[0].split(".")[-1])
        if handler is None:
            continue
        sender = json.loads(c["keyword_args"]).get("sender")
        sender = sender.split(".")[-1] if sender else None
        handlers.append((handler, [signal], sender,
                         f"{c['receiver']}.connect({names[0]}{', sender=' + sender if sender else ''})"))
    if not handlers:
        return [], {}, {}

    # 보내는 곳: order_placed.send(...), signals.post_payment.send_robust(...), self.add_signal.send(...)
    # self.add_signal은 그 클래스 본문의 add_signal = signals.basket_addition (같은 파일에 여러 클래스가 있어 클래스별로)
    class_ranges: dict[int, list] = defaultdict(list)
    for r in conn.execute(
        """SELECT c.id, c.file_id, c.line_start, c.line_end FROM classes c JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'python' AND c.kind = 'class'"""
    ):
        class_ranges[r["file_id"]].append(r)
    class_attrs: dict[tuple[int, str], str] = {}
    for f in facts:
        if f["k"] == "attr":
            owners = [r for r in class_ranges.get(f["file_id"], []) if r["line_start"] <= f["line"] <= r["line_end"]]
            if owners:
                owner = max(owners, key=lambda r: r["line_start"])["id"]
                class_attrs[(owner, f["name"])] = f["value"].split(".")[-1]
    senders: dict[str, set[int]] = defaultdict(set)
    for c in calls:
        if c["callee"] not in ("send", "send_robust") or not c["receiver"]:
            continue
        name = c["receiver"].split(".")[-1]
        if c["receiver"].startswith("self."):
            name = class_attrs.get((methods.rows[c["method_id"]]["class_id"], name), name)
        senders[name].add(c["method_id"])
    # Django 모델 시그널: sender 모델을 쓰는(INSERT·UPDATE·DELETE) 메서드
    writers: dict[tuple[str, str], set[int]] = defaultdict(set)
    for r in conn.execute(
        """SELECT e.src_id, s.full_id, s.kind FROM edges e JOIN statements s ON s.id = e.dst_id
           WHERE e.dst_kind = 'statement' AND s.kind != 'select' AND s.full_id LIKE 'ORM %'"""
    ):
        writers[(r["full_id"].split(".")[-1].split(" ")[-1], r["kind"])].add(r["src_id"])
    return handlers, senders, writers


def _sites(signals: list[str], sender: str | None, senders: dict, writers: dict) -> set[int]:
    """이 시그널을 보내는(모델 시그널이면 sender 모델을 저장·삭제하는) 메서드들."""
    sites: set[int] = set()
    for s in signals:
        if s in MODEL_SIGNALS and sender:  # post_save(sender=Category): Category를 저장하는 곳만
            for kind in _SIGNAL_WRITES.get(s, ("insert", "update", "delete")):
                sites |= writers.get((sender, kind), set())
        else:
            sites |= senders.get(s, set())
    return sites


def _signals(conn, facts, calls, methods: _Methods, reach) -> list[DjangoSignal]:
    handlers, senders, writers = _signal_graph(conn, facts, calls, methods)
    result = []
    for mid, signals, sender, how in handlers:
        sites = _sites(signals, sender, senders, writers)
        sites.discard(mid)
        site_names = sorted({methods.label(m) for m in sites if m in methods.rows})
        signal_text = ", ".join(signals)
        if sites:
            where = f"보내는 곳 {len(site_names)}곳: {', '.join(site_names[:3])}{' 외' if len(site_names) > 3 else ''}"
            effect = f"이 처리가 실패하면 원래 기능({site_names[0]})도 실패하나요?"
        elif any(s in _BUILTIN_SIGNALS for s in signals):
            what = f"{sender} 저장·삭제" if sender and any(s in MODEL_SIGNALS for s in signals) else "Django"
            where = f"Django가 보내는 시그널 ({what} 때마다)"
            effect = "저장할 때마다 실행돼도 괜찮은가요 (대량 수정·관리자 화면 포함)?"
        else:
            where = "보내는 곳을 저장소에서 못 찾음 (저장소 밖이거나 쓰지 않는 시그널)"
            effect = "지금도 이 시그널이 발생하나요?"
        handler = methods.label(mid)
        result.append(DjangoSignal(
            kind="signal", key_detail=signal_text, method_id=mid,
            question=(f"시그널 {signal_text}{f'(sender={sender})' if sender else ''}이 발생하면 {handler}가 자동으로 "
                      f"실행됩니다. 무엇을 바꾸나요? {effect}"),
            why=f"{how}. 부르는 코드 없이 시그널로만 실행돼 경로 추적에 보이지 않음. {where}",
            line=methods.rows[mid]["line_start"],
            reach=max((reach.get(m, 0) for m in sites), default=0),
        ))
    return result


def behavior_hints(conn: sqlite3.Connection) -> dict[int, list[tuple[str, str]]]:
    """기존 동작 목록용: 메서드마다 ("branch", 설정값 분기) · ("external", 이 처리가 일으키는 시그널과 받는 쪽).
    고도화 때 URL 하나를 옮기면서 뒤에 붙은 시그널 처리(통계·재고 알림)를 빠뜨리기 쉬워서 함께 적는다."""
    if not has_python(conn):
        return {}
    facts = load_facts(conn)
    methods = _Methods(conn)
    calls = conn.execute(
        """SELECT c.method_id, c.receiver, c.callee, c.string_args, c.name_args, c.keyword_args, c.line
           FROM calls c JOIN methods m ON m.id = c.method_id JOIN classes k ON k.id = m.class_id
           JOIN files f ON f.id = k.file_id WHERE f.kind = 'python'"""
    ).fetchall()
    hints: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for f in facts:
        if f["k"] == "setting" and f.get("cond") and not _DJANGO_SETTINGS.match(f["name"]):
            mid = methods.at(f["file_id"], f["line"])
            if mid is not None and ("branch", f"설정 {f['name']}") not in hints[mid]:
                hints[mid].append(("branch", f"설정 {f['name']}"))
    handlers, senders, writers = _signal_graph(conn, facts, calls, methods)
    for handler, signals, sender, _ in handlers:
        for site in _sites(signals, sender, senders, writers) - {handler}:
            text = f"시그널 {', '.join(signals)}{f'({sender})' if sender else ''} → {methods.label(handler)}"
            if ("external", text) not in hints[site]:
                hints[site].append(("external", text))
    return hints


# ── 미들웨어·컨텍스트 프로세서 ──────────────────────────────────


def _request_hooks(conn, methods: _Methods, total_urls: int, in_settings: set[str]) -> list[DjangoSignal]:
    registered = in_settings | {r["value"] for r in conn.execute(
        """SELECT DISTINCT s.value FROM string_literals s JOIN methods m ON m.id = s.method_id
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'python' AND (s.value LIKE '%Middleware%' OR s.value LIKE '%context_processors.%'
                                        OR s.value LIKE '%middleware.%')""")}
    called = {r["dst_id"] for r in conn.execute("SELECT DISTINCT dst_id FROM edges WHERE dst_kind = 'method'")}
    result = []
    processors: list[tuple] = []  # (메서드 행, settings에 등록됐나)
    by_class: dict[int, list] = defaultdict(list)
    for r in methods.rows.values():
        by_class[r["class_id"]].append(r)
    for rows in by_class.values():
        first = rows[0]
        module_last = (first["module"] or "").rsplit(".", 1)[-1]
        if first["class_kind"] == "module":
            if module_last != "context_processors":
                continue
            for r in rows:
                setting = f"{first['module']}.{r['name']}" in registered
                if r["name"].startswith("_") or r["name"] == "<module>" or (r["id"] in called and not setting):
                    continue  # 다른 컨텍스트 프로세서가 부르는 도우미 함수
                processors.append((r, setting))
            continue
        name = first["class_name"].split(".")[-1]
        dotted = f"{first['module']}.{name}"
        if not (name.endswith("Middleware") or module_last == "middleware"):
            continue
        hooks = [r for r in rows if r["name"] in ("__call__", "process_request", "process_view", "process_response",
                                                   "process_exception", "process_template_response")]
        if not hooks:
            continue
        setting = dotted in registered
        result.append(DjangoSignal(
            kind="request_hook", key_detail=dotted, method_id=hooks[0]["id"],
            question=(f"모든 요청에서 {name}가 먼저 실행됩니다 (미들웨어). 요청·응답에 무엇을 넣거나 바꾸고, "
                      "순서를 바꾸거나 빼면 어떤 기능이 깨지나요?"),
            why=("settings MIDDLEWARE에 등록됨" if setting else "미들웨어 모양의 클래스 (settings 등록 여부 확인 필요)") +
                f". 뷰 코드에 보이지 않고 모든 요청마다 실행 ({', '.join(r['name'] for r in hooks)})",
            line=hooks[0]["line_start"], reach=total_urls,
        ))
    if processors:
        # 컨텍스트 프로세서는 하나하나보다 "모든 화면에 무엇이 들어가나"를 한 번에 묻는 게 낫다
        processors.sort(key=lambda p: (not p[1], p[0]["path"], p[0]["line_start"]))
        names = [p[0]["name"] for p in processors]
        first = processors[0][0]
        registered_n = sum(1 for p in processors if p[1])
        result.append(DjangoSignal(
            kind="request_hook", key_detail="context_processors", method_id=first["id"],
            question=(f"모든 화면 템플릿에 값을 넣는 컨텍스트 프로세서가 {len(names)}개 있습니다 ({', '.join(names[:5])}"
                      f"{' 외' if len(names) > 5 else ''}). 각각 어느 화면이 쓰고, 요청마다 DB를 읽는 것이 있나요? "
                      "빼면 어떤 화면이 깨지나요?"),
            why=(f"settings TEMPLATES에 등록된 것 {registered_n}개" if registered_n else
                 "context_processors 모듈의 함수 (settings 등록 여부 확인 필요)") +
                ". 뷰 코드에 보이지 않고 모든 템플릿 렌더링마다 실행",
            line=first["line_start"], reach=total_urls,
        ))
    return result


# ── settings 업무 상수 ─────────────────────────────────────────


def _setting_branches(facts, methods: _Methods, reach) -> list[DjangoSignal]:
    defaults: dict[str, dict] = {}
    for f in facts:
        if f["k"] == "setting_def" and not f["module"].startswith(("docs.", "tests.")):
            # 라이브러리 기본값(defaults.py)이 프로젝트 settings보다 앞서 보이게: 먼저 나온 정의를 쓴다
            defaults.setdefault(f["name"], f)
    reads: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for f in facts:
        if f["k"] != "setting" or _DJANGO_SETTINGS.match(f["name"]):
            continue
        mid = methods.at(f["file_id"], f["line"])
        if mid is not None:
            reads[f["name"]].append((mid, f))
    # 설정마다 영향이 가장 큰 분기 하나를 고르고, 같은 메서드에서 분기하는 설정은 한 질문으로 묶는다 (SMTP_TLS·SMTP_SSL…)
    by_method: dict[int, list[tuple[str, dict, list]]] = defaultdict(list)
    for name, sites in reads.items():
        branching = [(mid, f) for mid, f in sites if f.get("cond")]
        if not branching:
            continue  # 값을 그대로 쓰는 곳(쿠키 이름 등)은 묻지 않고, 값으로 분기하는 설정만
        mid, f = max(branching, key=lambda s: reach.get(s[0], 0))
        by_method[mid].append((name, f, sites))
    result = []
    for mid, items in by_method.items():
        items.sort(key=lambda i: i[1]["line"])
        names = [name for name, _, _ in items]
        shown = []
        for name, _, _ in items[:4]:
            d = defaults.get(name)
            shown.append(f"{name}(기본값 {d['value']}, {d['path']}:{d['line']})" if d else f"{name}(저장소에 기본값 없음)")
        users = sorted({methods.label(m) for _, _, sites in items for m, _ in sites})
        result.append(DjangoSignal(
            kind="setting_branch", key_detail=",".join(names), method_id=mid,
            question=(f"설정 {', '.join(shown)}{' 외' if len(items) > 4 else ''} 값에 따라 동작이 달라집니다. "
                      "운영 환경에서는 어떤 값이고, 왜 그렇게 정했나요? 바꾸면 어떤 화면·처리가 달라지나요?"),
            why=(f"settings 값으로 분기. 이 설정{'들' if len(items) > 1 else ''}을 읽는 곳 {len(users)}곳: "
                 f"{', '.join(users[:4])}{' 외' if len(users) > 4 else ''}"),
            line=items[0][1]["line"],
        ))
    return result


# ── 커스텀 매니저·쿼리셋 ────────────────────────────────────────


def _hidden_filters(conn, calls, methods: _Methods) -> list[DjangoSignal]:
    by_class: dict[int, list] = defaultdict(list)
    for r in methods.rows.values():
        by_class[r["class_id"]].append(r)
    # 키워드 인자 원문에서 문자열은 따옴표가 빠져 있어(status="Open" → Open) 같은 줄의 문자열 상수로 알아본다
    literals = {(r["method_id"], r["line"], r["value"]) for r in conn.execute(
        """SELECT s.method_id, s.line, s.value FROM string_literals s JOIN methods m ON m.id = s.method_id
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'python' AND (m.name = 'get_queryset' OR c.superclass LIKE '%Manager' OR c.superclass LIKE '%QuerySet')""")}

    def fixed(c, value: str) -> str | None:
        """고정된 조건 값을 보여 줄 모양으로 (status="Open", is_public=True, status=self.status_filter). 아니면 None."""
        if any((c["method_id"], line, value) in literals for line in range(c["line"], c["line"] + 4)):
            return f'"{value}"'
        return value if _FIXED_VALUE.match(value) else None

    filters: dict[int, list] = defaultdict(list)
    for c in calls:
        # 고정된 조건만. 매개변수로 거르는 것(owner=owner)은 숨은 조건이 아니다
        if c["callee"] in ("filter", "exclude") and any(fixed(c, v) for v in json.loads(c["keyword_args"]).values()):
            filters[c["method_id"]].append(c)
    managers: dict[str, list[str]] = defaultdict(list)  # 매니저 클래스 → 그것을 쓰는 모델 필드 (Basket.open)
    for r in conn.execute(
        """SELECT fl.name, fl.type_name, c.name AS model FROM fields fl JOIN classes c ON c.id = fl.class_id
           JOIN files f ON f.id = c.file_id WHERE f.kind = 'python' AND c.kind = 'class'"""
    ):
        if r["type_name"]:
            managers[r["type_name"]].append(f"{r['model'].split('.')[-1]}.{r['name']}")
    callee_count: dict[str, int] = defaultdict(int)
    for c in calls:
        callee_count[c["callee"]] += 1
    result = []
    for rows in by_class.values():
        first = rows[0]
        if first["class_kind"] != "class":
            continue
        bases = [first["superclass"] or "", *json.loads(first["interfaces"] or "[]")]
        name = first["class_name"].split(".")[-1]
        if not any(re.search(r"(Manager|QuerySet)$", b) for b in bases if b):
            continue
        hidden = [(r, filters[r["id"]]) for r in rows
                  if filters.get(r["id"]) and r["name"] != "<module>" and not r["name"].startswith("_")]
        if not hidden:
            continue
        hidden.sort(key=lambda h: h[0]["name"] != "get_queryset")  # 기본 조회를 바꾸는 get_queryset이 가장 숨어 있다
        parts = []
        for r, fs in hidden[:4]:
            kw = {k: fixed(fs[0], v) for k, v in json.loads(fs[0]["keyword_args"]).items() if fixed(fs[0], v)}
            cond = ", ".join(f"{k}={v}" for k, v in list(kw.items())[:3])
            parts.append(f"{r['name']} → {fs[0]['callee']}({cond})")
        used_by = managers.get(name, [])
        uses = sum(callee_count.get(r["name"], 0) for r, _ in hidden if r["name"] != "get_queryset")
        kind = "매니저" if any(b.endswith("Manager") for b in bases) else "쿼리셋"
        if hidden[0][0]["name"] == "get_queryset":
            via = f"{', '.join(used_by[:3])}로" if used_by else f"{name}로"
            question = (f"{via} 조회하면 조건이 자동으로 붙습니다: {'; '.join(parts)}. 이 조건은 어떤 업무 규칙이고, "
                        "조건 없이 전체를 봐야 하는 화면(관리자·통계 등)은 어떻게 조회하나요?")
            why = f"커스텀 {kind}의 get_queryset. 뷰에서는 .all()·.get()만 보여 조건이 눈에 띄지 않음"
        else:
            question = (f"{name}의 조회 메서드가 정해진 조건을 붙입니다: {'; '.join(parts)}. 각각 어떤 업무 규칙이고, "
                        "화면마다 같은 규칙을 써야 하나요?")
            why = f"커스텀 {kind}. 뷰에서는 메서드 이름만 보여 실제 조건이 드러나지 않음 (이름으로 부르는 곳 {uses}곳)"
        result.append(DjangoSignal(
            kind="hidden_filter", key_detail=name, method_id=hidden[0][0]["id"], question=question, why=why,
            line=hidden[0][0]["line_start"], reach=max(uses, len(used_by)),
        ))
    return result


# 고정된 조회 조건 값: True·None·숫자·문자열, self.status_filter·self.model.OPEN, 대문자 상수
_FIXED_VALUE = re.compile(r"""^(True|False|None|-?\d+(\.\d+)?|["'].*["']|self\.[\w.]+|(?:[\w.]*\.)?[A-Z][A-Z0-9_]+)$""")


# ── get_class·get_model (포크한 앱이 바꿀 수 있는 클래스) ────────


def _swappable(calls, methods: _Methods) -> list[DjangoSignal]:
    loads: dict[str, set[str]] = defaultdict(set)  # 앱 → 불러오는 클래스
    sites = 0
    first_site = None
    for c in calls:
        if c["callee"] not in _LOADERS:
            continue
        args = json.loads(c["string_args"])
        if len(args) < 2:
            continue
        sites += 1
        first_site = first_site or c
        loads[args[0].split(".")[0]].add(args[1] if c["callee"] != "get_classes" else args[0])
    if sites < 5:  # 몇 곳뿐이면 일반 import와 다르지 않다
        return []
    top = sorted(loads, key=lambda a: -len(loads[a]))
    loader = next((m for name in ("get_class", "get_model") for m in methods.by_name.get(name, [])), None)
    mid = loader or first_site["method_id"]
    return [DjangoSignal(
        kind="swappable_class", key_detail="get_class", method_id=mid,
        question=(f"클래스를 이름으로 불러오는 곳(get_class·get_model)이 {sites}곳, 앱 {len(loads)}개입니다. "
                  "포크해서(INSTALLED_APPS에 프로젝트 앱으로 바꿔 넣어) 바꾼 앱·클래스가 있나요? 있으면 무엇을 왜 바꿨나요?"),
        why=(f"불러오는 앱 예: {', '.join(f'{a}({len(loads[a])})' for a in top[:5])}. 포크한 앱이 있으면 "
             "같은 이름의 다른 클래스가 쓰여 코드에서 본 것과 다르게 동작"),
        line=methods.rows[mid]["line_start"], reach=sites,
    )]


# ── 관리 명령·Celery 작업 ──────────────────────────────────────


def _batch_jobs(methods: _Methods) -> list[DjangoSignal]:
    result = []
    for r in methods.rows.values():
        path = r["path"]
        if "/management/commands/" in f"/{path}" and r["name"] == "handle" and r["class_name"].split(".")[-1] == "Command":
            command = path.rsplit("/", 1)[-1].removesuffix(".py")
            result.append(DjangoSignal(
                kind="batch_job", key_detail=command, method_id=r["id"],
                question=(f"관리 명령 {command}(python manage.py {command})은 언제·누가 실행하나요 (cron·배포 스크립트·수동)? "
                          "실패하면 어떻게 알 수 있고, 다시 실행해도 안전한가요?"),
                why="management/commands 아래 명령. 코드 안에서 부르는 곳이 없고 운영자가 직접·주기적으로 실행",
                line=r["line_start"], reach=1,
            ))
            continue
        annotations = set(json.loads(r["annotations"] or "[]"))
        task = annotations & _TASK_DECORATORS
        if task:
            result.append(DjangoSignal(
                kind="batch_job", key_detail=r["name"], method_id=r["id"],
                question=(f"비동기·주기 작업 {r['name']}(@{sorted(task)[0]})은 어디서 큐에 넣고 언제 실행되나요? "
                          "실패하면 다시 시도하나요, 두 번 실행돼도 안전한가요?"),
                why="작업 큐(Celery 등)가 실행하는 함수. 부르는 곳(.delay·주기 설정)이 코드 흐름에 잘 안 보임",
                line=r["line_start"], reach=1,
            ))
    return result


# ── raw SQL·extra() ────────────────────────────────────────────


def _raw_sql(conn, calls, methods: _Methods) -> list[DjangoSignal]:
    hits: dict[int, list[str]] = defaultdict(list)
    for c in calls:
        receiver = c["receiver"] or ""
        callee = c["callee"]
        if callee == "extra" and receiver:
            hits[c["method_id"]].append(f"{receiver}.extra()")
        elif callee == "raw" and re.search(r"objects|manager|queryset|qs\b", receiver, re.IGNORECASE):
            hits[c["method_id"]].append(f"{receiver}.raw()")
        elif callee in ("execute", "executemany") and "cursor" in receiver.lower():
            hits[c["method_id"]].append(f"{receiver}.{callee}()")
        elif callee == "RawSQL":
            hits[c["method_id"]].append("RawSQL()")
    for r in conn.execute(
        """SELECT e.src_id, s.kind FROM edges e JOIN statements s ON s.id = e.dst_id
           WHERE e.dst_kind = 'statement' AND s.full_id LIKE 'SQL %'"""
    ):
        if r["src_id"] in methods.rows:
            hits[r["src_id"]].append(f"SQL 문자열({r['kind'].upper()})")
    result = []
    for mid, how in hits.items():
        if mid not in methods.rows:
            continue
        how = list(dict.fromkeys(how))
        result.append(DjangoSignal(
            kind="raw_sql", key_detail=how[0], method_id=mid,
            question=(f"ORM 대신 SQL을 직접 씁니다 ({', '.join(how[:3])}). 왜 ORM으로 하지 않았나요? "
                      "DB를 바꾸거나 테이블·컬럼 이름이 바뀌면 같이 고쳐야 하나요?"),
            why="ORM 밖의 SQL은 모델을 바꿔도 함께 바뀌지 않고, 특정 DB 문법에 묶일 수 있음",
        ))
    return result


# ── 트랜잭션 경계 ──────────────────────────────────────────────


def _transactions(calls, methods: _Methods) -> list[DjangoSignal]:
    per: dict[int, set[str]] = defaultdict(set)
    for r in methods.rows.values():
        if "atomic" in json.loads(r["annotations"] or "[]"):
            per[r["id"]].add("atomic")
    for c in calls:
        if c["callee"] == "atomic" or (c["callee"] == "select_for_update") or c["callee"] == "on_commit":
            per[c["method_id"]].add(c["callee"])
    result = []
    for mid, marks in per.items():
        if mid not in methods.rows or methods.rows[mid]["name"] == "<module>" or marks == {"on_commit"}:
            continue
        if "select_for_update" in marks:
            question = ("행 잠금(select_for_update)을 겁니다. 동시에 같은 데이터를 처리하면 어떻게 되고, "
                        "잠금 대기·교착 문제는 없었나요?")
            why = "동시 처리 규칙이 코드에 드러나지 않음"
        else:
            question = ("transaction.atomic으로 묶은 범위입니다. 중간에 실패하면 무엇까지 되돌려지나요? "
                        "그 안의 메일·시그널·외부 결제 같은 바깥 작업도 함께 되돌려지나요?")
            why = "트랜잭션 경계는 실패했을 때의 데이터 상태를 정함" + \
                  (" (on_commit으로 커밋 뒤 작업을 미룸)" if "on_commit" in marks else "")
        result.append(DjangoSignal(kind="transaction", key_detail=",".join(sorted(marks)), method_id=mid,
                                   question=question, why=why))
    return result
