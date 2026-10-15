"""Python 프로젝트 연결: URL(라우터 접두어·include), 호출(import·self·이름 추정), ORM·SQL → 테이블, 템플릿.

Java처럼 타입과 DI 정보가 없어서, 확실히 알 수 있는 것(import, self, 주입한 필드 타입)부터 잇고
이름이 프로젝트에 하나뿐인 함수만 "이름으로 추정"해서 잇는다 (연결에 표시가 남는다).
"""

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from sasu.linker import normalize_url
from sasu.parsers.mybatis_parser import extract_tables
from sasu.parsers.python_parser import django_app

ROUTER_TYPES = {"APIRouter", "FastAPI", "Flask", "Blueprint", "Starlette", "Router"}
INCLUDE_CALLS = {"include_router": "prefix", "register_blueprint": "url_prefix"}
TEMPLATE_CALLS = {"render_template", "render", "TemplateResponse", "render_to_response", "get_template"}
TEMPLATE_SUFFIX = (".html", ".htm", ".jinja", ".jinja2", ".j2")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
# URL에 걸 클래스 기반 뷰의 메서드: HTTP 메서드 + Django 제네릭 뷰가 요청 처리 중에 부르는 훅
VIEW_HOOKS = (*HTTP_METHODS, "dispatch", "get_queryset", "get_object", "get_context_data", "get_form_kwargs",
              "get_initial", "form_valid", "form_invalid", "formset_valid", "get_success_url", "get_template_names",
              "get_redirect_url",
              "<class>")  # <class>: 메서드 없이 model·template_name만 둔 클래스 뷰 (python_parser)
_GENERIC_VIEW_KIND = {"CreateView": "insert", "UpdateView": "update", "DeleteView": "delete"}

ORM_KIND = {
    **dict.fromkeys(
        ["filter", "filter_by", "get", "all", "first", "query", "select", "exclude", "count", "exists", "one",
         "one_or_none", "scalars", "scalar", "scalar_one", "get_or_404", "first_or_404", "values", "values_list",
         "order_by", "paginate", "execute"], "select"),
    **dict.fromkeys(["create", "add", "add_all", "save", "bulk_create", "insert", "get_or_create"], "insert"),
    **dict.fromkeys(["update", "bulk_update", "update_or_create", "merge"], "update"),
    **dict.fromkeys(["delete", "remove"], "delete"),
}
# 모델 객체를 새로 만드는 호출: User(...), User.model_validate(...)
_MODEL_FACTORIES = {"model_validate", "model_construct", "from_orm", "parse_obj", "from_dict"}
_SESSION_WRITES = {"add": "insert", "add_all": "insert", "merge": "update", "delete": "delete"}
_SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|MERGE)\b", re.IGNORECASE)
# 첫 단어만 보면 로그 문구("insert BackupResult Data : {}")도 걸린다. 종류마다 있어야 할 말을 본다
_SQL_SHAPE = {
    "select": re.compile(r"\bFROM\b", re.IGNORECASE), "with": re.compile(r"\bSELECT\b", re.IGNORECASE),
    "insert": re.compile(r"\bINTO\b", re.IGNORECASE), "merge": re.compile(r"\bINTO\b", re.IGNORECASE),
    "update": re.compile(r"\bSET\b", re.IGNORECASE), "delete": re.compile(r"\bFROM\b", re.IGNORECASE),
}


def sql_kind(text: str) -> str | None:
    """코드 안 문자열이 SQL이면 종류(select·insert·update·delete·merge), 아니면 None."""
    match = _SQL_START.match(text)
    if not match or len(text) < 15:
        return None
    kind = match.group(1).lower()
    if not _SQL_SHAPE[kind].search(text):
        return None
    return "select" if kind == "with" else kind
# 너무 흔해서 이름만으로 추정하면 틀리기 쉬운 메서드 이름
_GENERIC_NAMES = {
    "get", "set", "post", "put", "delete", "save", "update", "create", "add", "remove", "run", "main", "init",
    "load", "read", "write", "close", "open", "send", "process", "handle", "execute", "query", "filter", "all",
    "first", "list", "items", "keys", "values", "copy", "clear", "append", "extend", "format", "join", "split",
    # Django 폼·뷰가 프레임워크에서 물려받는 이름 (form.is_valid()를 프로젝트의 엉뚱한 is_valid로 잇지 않게)
    "is_valid", "clean", "full_clean", "as_view", "dispatch", "render", "get_form", "get_object", "get_queryset",
    "get_context_data", "get_form_kwargs", "form_valid", "form_invalid", "get_success_url", "get_absolute_url",
}


@dataclass
class PyContext:
    files: dict[int, dict] = field(default_factory=dict)  # file_id → {path, module, aliases, package}
    module_file: dict[str, int] = field(default_factory=dict)  # 모듈 경로 → file_id
    module_class: dict[str, int] = field(default_factory=dict)  # 모듈 경로 → 모듈 가짜 클래스 id
    class_in_module: dict[tuple[str, str], int] = field(default_factory=dict)  # (모듈, 클래스 이름) → class_id


def link_python(conn: sqlite3.Connection, edges: set, classes: dict, methods: dict,
                methods_by_class: dict, fields_by_class: dict, name_map: dict) -> int:
    """edges에 Python 연결을 더하고, url_mappings·statements를 채운다. 돌려주는 값: URL 수."""
    ctx = _context(conn)
    if not ctx.files:
        return 0
    py_classes = {cid for cid, c in classes.items() if c["file_id"] in ctx.files}
    class_module = {cid: ctx.files[classes[cid]["file_id"]]["module"] for cid in py_classes}

    # 프로젝트 안에서 이름이 하나뿐인 함수·메서드 (이름으로 추정할 때만 쓴다)
    by_name: dict[str, list[int]] = defaultdict(list)
    for mid, m in methods.items():
        if m["class_id"] in py_classes:
            by_name[m["name"]].append(mid)

    info = {int(r["key"].split(":", 1)[1]): json.loads(r["value"])
            for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'pyclass:%'")}
    hier = _Hierarchy(ctx, classes, py_classes, info)
    tables = _django_tables(conn, hier, classes)  # class_id → 테이블 (추상 모델 상속·앱 label·proxy 반영)
    loaded = _loaded_names(conn, ctx, hier, tables)  # file_id → {변수: class_id} (Basket = get_model("basket", "Basket"))

    by_model_name: dict[str, list[int]] = defaultdict(list)
    for cid in sorted(tables):
        by_model_name[classes[cid]["name"].split(".")[-1]].append(cid)
    models = {name: cids[0] for name, cids in by_model_name.items()}
    orm_statements: dict[tuple[int, str], int] = {}

    def model_id(name: str, file_id: int) -> int | None:
        """이 파일에서 name이 가리키는 모델. 같은 이름의 모델이 여러 앱에 있으면(basket.Line·order.Line) 파일 기준으로 고른다."""
        cid = loaded.get(file_id, {}).get(name)
        if cid in tables:
            return cid
        cands = by_model_name.get(name)
        if not cands:
            return None
        if len(cands) > 1:
            module = ctx.files[file_id]["module"]
            same = [c for c in cands if module.startswith(hier.module(c).rsplit(".", 1)[0] + ".")]
            imported = [c for c in cands if any(_module_target(full, ctx) == (hier.module(c), name)
                                                for full in ctx.files[file_id]["aliases"].values())]
            return (imported or same or cands)[0]
        return cands[0]

    # 역방향 관계 매니저: order.notes.create(...) → OrderNote (ForeignKey(related_name="notes")), basket.line_set
    related: dict[str, list[int]] = defaultdict(list)
    for cid in sorted(hier.py_classes):
        for rel in hier.info.get(cid, {}).get("relations", {}):
            related[rel].extend(m for m in hier.model_tables(cid, tables) if m not in related[rel])
    for cid in sorted(tables):
        related[classes[cid]["name"].split(".")[-1].lower() + "_set"].append(cid)

    def related_model(receiver: str, file_id: int) -> int | None:
        """받는 쪽 끝이 관계 이름이면 그 모델. 여러 앱에 같은 이름이 있으면 같은 앱 것만."""
        if "." not in receiver:
            return None
        cands = related.get(receiver.rsplit(".", 1)[1])
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        module = ctx.files[file_id]["module"]
        same = [c for c in cands if module.startswith(hier.module(c).rsplit(".", 1)[0] + ".")]
        return same[0] if len(same) == 1 else None

    by_lower: dict[str, list[int]] = defaultdict(list)
    for name, cids in by_model_name.items():
        by_lower[name.lower()].extend(cids)

    def model_by_attr(receiver: str, file_id: int) -> int | None:
        last = receiver.split(".")[-1]
        if not last.isidentifier() or not last.islower() or last in ("self", "cls"):
            return None
        cands = by_lower.get(last)
        if not cands:
            return None
        if len(cands) > 1:
            module = ctx.files[file_id]["module"]
            cands = [c for c in cands if module.startswith(hier.module(c).rsplit(".", 1)[0] + ".")]
        return cands[0] if len(cands) == 1 else None

    def orm_statement(model: int, kind: str) -> int:
        key = (model, kind)
        if key not in orm_statements:
            c = classes[model]
            name = c["name"].split(".")[-1]
            label = name if len(by_model_name[name]) == 1 else f"{tables[model].split('_')[0]}.{name}"
            sid = conn.execute(
                "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
                (c["file_id"], f"ORM {label}", kind, c["line_start"], f"{name} 모델 ({tables[model]}) ORM {kind}"),
            ).lastrowid
            conn.execute("INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
                         (sid, tables[model].upper()))
            orm_statements[key] = sid
        return orm_statements[key]

    # session.add(obj)처럼 모델 이름이 안 보이는 쓰기는 메서드가 만들거나 다루는 모델로 판단한다 (1-1)
    created: dict[int, set[int]] = defaultdict(set)  # 메서드 → 새로 만든 모델
    touched: dict[int, set[int]] = defaultdict(set)  # 메서드 → 조회·매개변수로 다루는 모델
    session_writes: list[tuple[int, str, int]] = []

    # 1. 호출
    for call in conn.execute("SELECT * FROM calls").fetchall():
        caller = call["method_id"]
        class_id = methods[caller]["class_id"]
        if class_id not in py_classes:
            continue
        file = ctx.files[classes[class_id]["file_id"]]
        callee = call["callee"]
        line = call["line"]
        receiver = call["receiver"]
        name_args = json.loads(call["name_args"])

        # ORM: User.objects.filter(...), db.query(User), select(User), session.get(User, 1)
        file_id = classes[class_id]["file_id"]
        candidates = [receiver.split(".")[0].split("(")[0]] if receiver else []
        candidates += [a.split(".")[-1] for a in name_args]
        kind = ORM_KIND.get(callee)
        matched = next((m for m in (model_id(cand, file_id) for cand in candidates if cand in models) if m), None)
        if matched and not kind and re.search(r"\.(objects|_default_manager)\b", receiver or ""):
            kind = "select"  # Product.objects.browsable() 같은 사용자 정의 매니저 메서드
        if matched and kind:
            edges.add(("method", caller, "statement", orm_statement(matched, kind), None, line, None))
        elif not matched and kind and receiver and related_model(receiver, file_id):
            edges.add(("method", caller, "statement", orm_statement(related_model(receiver, file_id), kind), None, line,
                       "관계 이름으로 추정"))
        if matched:
            touched[caller].add(matched)
        if not receiver and callee in models and model_id(callee, file_id):
            created[caller].add(model_id(callee, file_id))
        elif receiver in models and callee in _MODEL_FACTORIES and model_id(receiver, file_id):
            created[caller].add(model_id(receiver, file_id))
        elif not matched and callee in _SESSION_WRITES and receiver and _is_session(receiver):
            session_writes.append((caller, _SESSION_WRITES[callee], line))
        elif receiver == "self" and callee in ("save", "delete") and hier.model_tables(class_id, tables):
            # 모델 메서드 안의 self.save(): 추상 모델이면 그것을 상속한 실제 모델의 테이블
            for model in hier.model_tables(class_id, tables):
                edges.add(("method", caller, "statement", orm_statement(model, "update" if callee == "save" else "delete"),
                           None, line, None))

        targets, note = _resolve(receiver, callee, class_id, file, ctx, classes, methods_by_class,
                                 fields_by_class, name_map, py_classes)
        if not targets and receiver:
            # Applicator().apply(...) 처럼 get_class로 불러 둔 클래스
            root = receiver.split(".")[0].split("(")[0]
            if receiver in (root, root + "()") and root in loaded.get(file_id, {}):
                targets = hier.ancestors(loaded[file_id][root])
            else:
                # request.basket.add_product(...), order.set_status(...): 변수·속성 이름이 모델 이름과 같으면 그 모델
                model = model_by_attr(receiver, file_id)
                if model is not None:
                    targets, note = hier.ancestors(model), "이름으로 추정"
                    if callee in ("save", "delete") and not any(methods_by_class[c].get(callee) for c in targets):
                        edges.add(("method", caller, "statement",
                                   orm_statement(model, "update" if callee == "save" else "delete"), None, line, note))
        linked = False
        for target_class in targets:
            for mid in methods_by_class[target_class].get(callee, []):
                if mid != caller:
                    edges.add(("method", caller, "method", mid, None, line, note))
                    linked = True
        if not linked and callee not in _GENERIC_NAMES and not callee.startswith("__") and len(callee) > 3:
            same = by_name.get(callee, [])
            if len(same) == 1 and same[0] != caller and _looks_like_project_call(receiver, file, ctx):
                edges.add(("method", caller, "method", same[0], None, line, "이름으로 추정"))

        # 템플릿: render_template("x.html"), render(request, "blog/x.html")
        if callee in TEMPLATE_CALLS:
            for arg in json.loads(call["string_args"]):
                if arg.endswith(TEMPLATE_SUFFIX):
                    template = _template_file(conn, arg)
                    edges.add(("method", caller, "view", template, _template_path(conn, template) or f"templates/{arg}", None, None))

    # 1-1. session.add(user): 새로 만든 모델이면 INSERT, 받아 온 모델을 다시 넣으면 UPDATE
    for caller, kind, line in session_writes:
        params = {p.split(".")[-1] for p in json.loads(methods[caller]["params"] or "[]")}
        file_id = classes[methods[caller]["class_id"]]["file_id"]
        seen = touched[caller] | {model_id(p, file_id) for p in params if p in models} - {None}
        if kind == "insert" and not created[caller]:
            kind = "update"
        for model in sorted(created[caller] if kind == "insert" else seen):
            edges.add(("method", caller, "statement", orm_statement(model, kind), None, line, "모델 추정"))

    # 2. 코드 안의 SQL 문자열 (cursor.execute("SELECT ..."), text("UPDATE ..."))
    for lit in conn.execute(
        """SELECT s.method_id, s.value, s.line, f.path, c.id AS class_id FROM string_literals s
           JOIN methods m ON m.id = s.method_id JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id
           WHERE f.kind = 'python'"""
    ).fetchall():
        kind = sql_kind(lit["value"])
        if kind is None:
            continue
        sid = conn.execute(
            "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
            (classes[lit["class_id"]]["file_id"], f"SQL {lit['path']}:{lit['line']}", kind, lit["line"], lit["value"]),
        ).lastrowid
        conn.executemany("INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
                         [(sid, t) for t in extract_tables(lit["value"])])
        edges.add(("method", lit["method_id"], "statement", sid, None, lit["line"], None))

    # 3. URL
    urls = _python_urls(conn, ctx, classes, methods, class_module)
    url_names: dict[str, set[str]] = defaultdict(set)
    django_urls, view_classes = _django_urls(conn, ctx, classes, methods_by_class, hier, loaded, url_names)
    urls += django_urls
    conn.executemany("INSERT INTO url_mappings (url, method_id) VALUES (?, ?)", urls)
    if url_names:  # path(..., name="summary"): 템플릿 {% url %}·reverse()는 경로 대신 이름으로 부른다 (정리 후보)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('pyurlnames', ?)",
                     (json.dumps({u: sorted(n) for u, n in sorted(url_names.items())}, ensure_ascii=False),))

    # 4. 클래스 기반 뷰의 model = Product / queryset = Product.objects...: Django 제네릭 뷰가 대신 읽고 쓴다
    #    template_name = "basket/basket.html"도 같은 방식으로 화면에 잇는다
    mapped = {mid for _, mid in django_urls}
    for view in view_classes:
        # 이 뷰가 직접 정의한 요청 처리 메서드(HTTP 메서드·훅·<class>)에서 잇는다. 없으면 URL에 걸린 이 뷰의 메서드
        own = [mid for name in VIEW_HOOKS for mid in methods_by_class[view].get(name, [])] or             [mid for mids in methods_by_class[view].values() for mid in mids if mid in mapped]
        template = hier.attr(view, "template_name")
        if template and template.endswith(TEMPLATE_SUFFIX):
            template_id = _template_file(conn, template)
            for mid in own:
                edges.add(("method", mid, "view", template_id, _template_path(conn, template_id) or f"templates/{template}",
                           None, None))
        name = hier.attr(view, "model") or hier.attr(view, "queryset")
        model = model_id(name, classes[view]["file_id"]) if name in models else None
        if model is None:
            continue
        kind = next((k for base, k in _GENERIC_VIEW_KIND.items() if hier.has_base_named(view, base)), "select")
        for mid in own:
            edges.add(("method", mid, "statement", orm_statement(model, kind), None, None, "클래스 뷰 model"))
    return len(urls)


def _is_session(receiver: str) -> bool:
    last = receiver.split(".")[-1].lower()
    return last in ("db", "sess", "s") or last.endswith("session")


# ── 문맥 ───────────────────────────────────────────────────────────


def _context(conn: sqlite3.Connection) -> PyContext:
    ctx = PyContext()
    for f in conn.execute("SELECT id, path, module, imports FROM files WHERE kind = 'python'"):
        module = f["module"] or ""
        is_package = f["path"].endswith("__init__.py")
        package = module if is_package else module.rpartition(".")[0]
        aliases = {}
        for item in json.loads(f["imports"] or "[]"):
            full, _, alias = item.partition(" as ")
            full = resolve_relative(full.strip(), package)
            bound = alias.strip() or full.split(".")[-1]
            aliases[bound] = full
        ctx.files[f["id"]] = {"path": f["path"], "module": module, "aliases": aliases, "package": package}
        ctx.module_file[module] = f["id"]
    for c in conn.execute(
        "SELECT c.id, c.name, c.kind, f.id AS file_id FROM classes c JOIN files f ON f.id = c.file_id WHERE f.kind = 'python'"
    ):
        module = ctx.files[c["file_id"]]["module"]
        if c["kind"] == "module":
            ctx.module_class[module] = c["id"]
        else:
            ctx.class_in_module[(module, c["name"])] = c["id"]
    return ctx


def resolve_relative(path: str, package: str) -> str:
    """.views (package blog) → blog.views, ..core.x → 상위 패키지의 core.x"""
    if not path.startswith("."):
        return path
    dots = len(path) - len(path.lstrip("."))
    parts = package.split(".") if package else []
    base = parts[: len(parts) - (dots - 1)] if dots > 1 else parts
    rest = path.lstrip(".")
    return ".".join([*base, rest] if rest else base)


def _module_target(dotted: str, ctx: PyContext) -> tuple[str, str] | None:
    """app.crud.get_user → ("app.crud", "get_user") 처럼 가장 긴 모듈 경로를 찾는다. 모듈 자체면 ("app.crud", "")."""
    if dotted in ctx.module_class:
        return dotted, ""
    head, _, tail = dotted.rpartition(".")
    while head:
        if head in ctx.module_class or head in ctx.module_file:
            return head, tail
        head, _, more = head.rpartition(".")
        tail = f"{more}.{tail}"
    # 모듈 경로 뒷부분만 맞는 경우 (프로젝트 루트가 src/ 등으로 다를 때)
    for module in ctx.module_class:
        if module.endswith("." + dotted):
            return module, ""
    return None


def _resolve(receiver: str | None, callee: str, class_id: int, file: dict, ctx: PyContext, classes: dict,
             methods_by_class: dict, fields_by_class: dict, name_map: dict, py_classes: set) -> tuple[list[int], str | None]:
    module = file["module"]
    aliases = file["aliases"]
    if receiver in (None, ""):
        # 같은 모듈의 함수 → import한 함수
        own = ctx.module_class.get(module)
        if own is not None and callee in methods_by_class[own]:
            return [own], None
        if callee in aliases:
            target = _module_target(aliases[callee], ctx)
            if target and target[1] == "" and callee in methods_by_class.get(ctx.module_class.get(target[0], -1), {}):
                return [ctx.module_class[target[0]]], None
            if target and target[1]:
                mod_class = ctx.module_class.get(target[0])
                return ([mod_class] if mod_class is not None else []), None
        return [], None
    if receiver in ("self", "cls", "super()"):
        chain = [class_id]
        sup = classes[class_id]["superclass"]
        if sup:
            chain += [c for c in name_map.get(sup, []) if c in py_classes]
        return chain, None
    if receiver.startswith("self.") and receiver.count(".") == 1:
        f = fields_by_class[class_id].get(receiver[5:])
        if f is not None:
            return [c for c in name_map.get(f["type_name"], []) if c in py_classes], None
        return [], None
    root = receiver.split(".")[0]
    if root in aliases and "(" not in receiver:
        dotted = aliases[root] + receiver[len(root):]
        target = _module_target(dotted, ctx)
        if target:
            mod, rest = target
            if rest == "" and mod in ctx.module_class:
                return [ctx.module_class[mod]], None
            cls = ctx.class_in_module.get((mod, rest))
            if cls is not None:
                return [cls], None
    if receiver in ctx.module_class:
        return [ctx.module_class[receiver]], None
    # 클래스 이름으로 부르는 classmethod·staticmethod
    same_module = ctx.class_in_module.get((module, receiver))
    if same_module is not None:
        return [same_module], None
    if receiver in aliases:
        target = _module_target(aliases[receiver], ctx)
        if target and target[1]:
            cls = ctx.class_in_module.get(target)
            if cls is not None:
                return [cls], None
    return [], None


def _looks_like_project_call(receiver: str | None, file: dict, ctx: PyContext) -> bool:
    """이름 추정은 프로젝트 코드를 부르는 모양일 때만: 받는 쪽이 없거나, 지역 변수·필드처럼 보일 때."""
    if receiver is None:
        return True
    root = receiver.split(".")[0].split("(")[0]
    target = file["aliases"].get(root)
    if target is not None:  # import한 외부 라이브러리(os, json, requests …)면 추정하지 않는다
        return _module_target(target, ctx) is not None
    return True


def _template_file(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM files WHERE kind = 'template' AND (path LIKE ? OR path = ?) ORDER BY length(path) LIMIT 1",
        (f"%templates/{name}", name),
    ).fetchone()
    return row["id"] if row else None


def _template_path(conn: sqlite3.Connection, file_id: int | None) -> str | None:
    if file_id is None:
        return None
    return conn.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()["path"]


# ── URL: FastAPI·Flask ─────────────────────────────────────────────


def _python_urls(conn, ctx: PyContext, classes: dict, methods: dict, class_module: dict) -> list[tuple[str, int]]:
    routers: dict[tuple[str, str], dict] = {}
    for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'pyassign:%'"):
        _, file_id, var = r["key"].split(":", 2)
        info = json.loads(r["value"])
        if info["type"] in ROUTER_TYPES:
            module = ctx.files[int(file_id)]["module"]
            args = info["args"]
            ctor = (args.get("prefix") or args.get("url_prefix") or [""])[0]
            routers[(module, var)] = {"prefix": ctor, "parents": []}

    def router_ref(text: str, file: dict) -> tuple[str, str] | None:
        """users.router / api_router / bp 를 (모듈, 변수)로."""
        text = text.strip()
        if "." in text:
            root, _, var = text.rpartition(".")
            dotted = file["aliases"].get(root.split(".")[0])
            if dotted:
                target = _module_target(dotted + root[len(root.split(".")[0]):], ctx)
                if target:
                    return (target[0] if not target[1] else f"{target[0]}.{target[1]}", var)
            return None
        if (file["module"], text) in routers:
            return file["module"], text
        dotted = file["aliases"].get(text)
        if dotted:
            mod, _, var = dotted.rpartition(".")
            return mod, var
        return None

    for call in conn.execute(
        """SELECT c.receiver, c.callee, c.name_args, c.keyword_args, m.class_id FROM calls c
           JOIN methods m ON m.id = c.method_id WHERE c.callee IN ('include_router', 'register_blueprint')"""
    ):
        file = ctx.files.get(classes[call["class_id"]]["file_id"])
        if file is None:
            continue
        name_args = json.loads(call["name_args"])
        if not name_args or not call["receiver"]:
            continue
        child = router_ref(name_args[0], file)
        parent = router_ref(call["receiver"], file)
        prefix = json.loads(call["keyword_args"]).get(INCLUDE_CALLS[call["callee"]], "")
        if child in routers:
            routers[child]["parents"].append((parent, _prefix_text(prefix)))

    def prefixes(key, depth=0) -> list[str]:
        info = routers.get(key)
        if info is None or depth > 8:
            return [""]
        if not info["parents"]:
            return [info["prefix"]]
        result = []
        for parent, include_prefix in info["parents"]:
            for p in prefixes(parent, depth + 1) if parent in routers else [""]:
                result.append(p + include_prefix + info["prefix"])
        return result

    urls = []
    for mid, m in methods.items():
        routes = json.loads(m["routes"]) if m["routes"] else []
        if not routes:
            continue
        module = class_module.get(m["class_id"])
        for path, obj in routes:
            base = prefixes((module, obj)) if obj else [""]
            for prefix in base:
                urls.append((normalize_url(prefix + "/" + path), mid))
    return urls


def _prefix_text(prefix: str) -> str:
    """키워드 인자 원문: "/api" → /api, settings.API_V1_STR → /{settings.API_V1_STR}"""
    if not prefix:
        return ""
    if prefix[0] in "\"'":
        return prefix.strip("\"'")
    if prefix.startswith("/"):
        return prefix
    return "/{" + prefix + "}"


# ── URL: Django ────────────────────────────────────────────────────


def _django_urls(conn, ctx: PyContext, classes: dict, methods_by_class: dict, hier: "_Hierarchy",
                 loaded: dict, url_names: dict | None = None) -> tuple[list[tuple[str, int]], set[int]]:
    """urls.py의 urlpatterns와 앱 설정 클래스의 get_urls()를 include를 따라 이어 URL → 뷰 메서드를 만든다.
    돌려주는 값: (URL, 메서드) 목록, URL에 걸린 클래스 기반 뷰들."""
    rows = conn.execute("SELECT u.*, f.module FROM url_patterns u JOIN files f ON f.id = u.file_id").fetchall()
    if not rows:
        return [], set()
    pattern_names = {  # (파일, 줄) → URL 이름 (python_parser의 urlname 사실)
        (classes[cid]["file_id"], f["line"]): f["name"]
        for cid, info in hier.info.items() if cid in classes
        for f in info.get("facts", []) if f.get("k") == "urlname"
    }
    by_file: dict[int, list[int]] = defaultdict(list)
    for cid in hier.py_classes:
        if classes[cid]["kind"] != "module":
            by_file[classes[cid]["file_id"]].append(cid)
    # 묶음: urls 모듈 이름(str) 또는 get_urls를 가진 앱 설정 클래스 id(int)
    units: dict[str | int, list] = defaultdict(list)
    for r in rows:
        owners = [c for c in by_file[r["file_id"]] if classes[c]["line_start"] <= r["line"] <= classes[c]["line_end"]]
        owner = max(owners, key=lambda c: classes[c]["line_start"]) if owners else None
        units[owner if owner is not None else r["module"]].append(r)
    app_units: dict[str, int] = {}  # 앱 label → 앱 설정 클래스 (label이 없으면 name의 끝)
    for key in units:
        if isinstance(key, int):
            label = hier.attr(key, "label") or (hier.attr(key, "name") or "").rsplit(".", 1)[-1]
            if label:
                app_units.setdefault(label, key)

    def include_unit(p, unit) -> str | int | None:
        target = p["target"]
        m = re.search(r"get_app_config\(\s*[\"']([\w.]+)[\"']", target)
        if m:
            return app_units.get(m.group(1))
        m = re.match(r"self\.(\w+)\.urls\b", target)
        if m:
            loader = hier.loader(unit, m.group(1)) if isinstance(unit, int) else None
            return app_units.get(loader[1]) if loader and loader[0] == "get_app_config" and len(loader) > 1 else None
        found = _module_target(target, ctx)
        if found and found[0] in units:
            return found[0]
        # 모듈 경로 앞부분이 다른 경우 (include("blog.urls") ↔ 모듈 src.blog.urls). "urls" 하나만 맞는 것은 제외
        return next((u for u in units if isinstance(u, str) and "." in u
                     and (u.endswith("." + target) or target.endswith("." + u))), None)

    included = {include_unit(p, unit) for unit, ps in units.items() for p in ps if p["is_include"]} - {None}
    roots = [u for u in units if u not in included] or list(units)
    urls: list[tuple[str, int]] = []
    views: set[int] = set()

    def walk(unit, prefix: str, depth: int) -> None:
        if depth > 8:
            return
        for p in units[unit]:
            route = prefix + "/" + p["route"]
            if p["is_include"]:
                child = include_unit(p, unit)
                if child is not None:
                    walk(child, route, depth + 1)
                continue
            mids, view = _django_target(p, unit, ctx, methods_by_class, hier, loaded)
            if view is not None:
                views.add(view)
            for mid in mids:
                urls.append((normalize_url(route), mid))
            name = pattern_names.get((p["file_id"], p["line"]))
            if name and mids and url_names is not None:
                url_names[normalize_url(route)].add(name)

    for root in roots:
        walk(root, "", 0)
    return urls, views


def _django_target(p, unit, ctx: PyContext, methods_by_class: dict, hier: "_Hierarchy",
                   loaded: dict) -> tuple[list[int], int | None]:
    """views.post_list → 그 함수, views.PostView.as_view() → PostView의 get·post·get_queryset 등,
    self.add_view.as_view() → 앱 설정의 ready()에서 get_class로 불러 둔 뷰."""
    target = p["target"]
    file = ctx.files[p["file_id"]]
    m = re.search(r"([A-Za-z_][\w.]*)\.as_view\(", target)
    if m:
        target = m.group(1)
    else:  # login_required(views.profile) → views.profile
        names = [n.group(0) for n in re.finditer(r"[A-Za-z_][\w.]*", target) if target[n.end():n.end() + 1] != "("]
        if not names:
            return [], None
        target = names[-1]
    cls = None
    if target.startswith("self.") and isinstance(unit, int):
        loader = hier.loader(unit, target[5:])
        if loader and loader[0] == "get_class" and len(loader) >= 3:
            cls = hier.loaded_class(loader[1], loader[2])
    elif target in loaded.get(p["file_id"], {}):
        cls = loaded[p["file_id"]][target]
    if cls is None:
        root = target.split(".")[0]
        dotted = file["aliases"].get(root)
        dotted = dotted + target[len(root):] if dotted else f"{file['package']}.{target}"
        found = _module_target(dotted, ctx)
        if not found:
            return [], None
        module, rest = found
        if "." not in rest and module in ctx.module_class and rest in methods_by_class[ctx.module_class[module]]:
            return methods_by_class[ctx.module_class[module]][rest], None
        cls = ctx.class_in_module.get((module, rest))
    if cls is None:
        return [], None
    # 클래스 뷰: 이 클래스와 프로젝트 안 부모 클래스의 HTTP 메서드·제네릭 뷰 훅 (가까운 정의만)
    found_methods: dict[str, list[int]] = {}
    for c in hier.ancestors(cls):
        for name in VIEW_HOOKS:
            if name not in found_methods and methods_by_class[c].get(name):
                found_methods[name] = methods_by_class[c][name]
    if not found_methods:  # 훅이 아닌 이름만 있는 뷰: 그 클래스의 메서드 전부
        return [mid for name, mids in methods_by_class[cls].items() if not name.startswith("__") for mid in mids], cls
    return [mid for name in VIEW_HOOKS for mid in found_methods.get(name, [])], cls


# ── 클래스 상속·Django 모델 ───────────────────────────────────────


class _Hierarchy:
    """Python 클래스의 부모 찾기 (import·같은 모듈·프로젝트에 하나뿐인 이름 순)와 pyclass 정보."""

    def __init__(self, ctx: PyContext, classes: dict, py_classes: set, info: dict):
        self.ctx, self.classes, self.py_classes, self.info = ctx, classes, py_classes, info
        self.by_name: dict[str, list[int]] = defaultdict(list)
        for cid in sorted(py_classes):
            if classes[cid]["kind"] != "module":
                self.by_name[classes[cid]["name"].split(".")[-1]].append(cid)
        self._bases: dict[int, list[int]] = {}

    def module(self, cid: int) -> str:
        return self.ctx.files[self.classes[cid]["file_id"]]["module"]

    def bases(self, cid: int) -> list[int]:
        if cid not in self._bases:
            c = self.classes[cid]
            names = [n for n in [c["superclass"], *json.loads(c["interfaces"] or "[]")] if n]
            file = self.ctx.files[c["file_id"]]
            found = []
            for name in names:
                parent = None
                if name in file["aliases"]:
                    target = _module_target(file["aliases"][name], self.ctx)
                    parent = self.ctx.class_in_module.get(target) if target else None
                    if parent is None and target is None:
                        continue  # 저장소 밖 (django.views.generic.DetailView 등)
                parent = parent or self.ctx.class_in_module.get((file["module"], name))
                if parent is None and len(self.by_name.get(name, [])) == 1:
                    parent = self.by_name[name][0]
                if parent is not None and parent != cid:
                    found.append(parent)
            self._bases[cid] = found
        return self._bases[cid]

    def ancestors(self, cid: int) -> list[int]:
        """자기 자신부터 부모 쪽으로 (너비 우선, 중복 없이)."""
        order, queue = [], [cid]
        while queue and len(order) < 30:
            c = queue.pop(0)
            if c in order:
                continue
            order.append(c)
            queue.extend(self.bases(c))
        return order

    def attr(self, cid: int, name: str) -> str | None:
        for c in self.ancestors(cid):
            value = self.info.get(c, {}).get("attrs", {}).get(name)
            if value:
                return value
        return None

    def loader(self, cid: int, field: str) -> list[str] | None:
        """self.field = get_class("basket.views", "BasketView") → ["get_class", "basket.views", "BasketView"]."""
        for c in self.ancestors(cid):
            value = self.info.get(c, {}).get("loaded", {}).get(field)
            if value:
                return value
        return None

    def has_base_named(self, cid: int, name: str) -> bool:
        for c in self.ancestors(cid):
            row = self.classes[c]
            if name in [row["superclass"], *json.loads(row["interfaces"] or "[]")]:
                return True
        return False

    def loaded_class(self, module_label: str, name: str) -> int | None:
        """get_class("basket.views", "BasketView") → oscar/apps/basket/views.py의 BasketView."""
        cands = self.by_name.get(name, [])
        exact = [c for c in cands if self.module(c) == module_label or self.module(c).endswith("." + module_label)]
        if exact:
            return min(exact, key=lambda c: len(self.module(c)))
        return cands[0] if len(cands) == 1 else None

    def model_tables(self, cid: int, tables: dict) -> list[int]:
        """모델 클래스 cid의 실제 테이블을 가진 모델들: 자신, 추상 모델이면 그것을 상속한 모델."""
        if cid in tables:
            return [cid]
        if not self.info.get(cid, {}).get("model", {}).get("abstract"):
            return []
        return [c for c in tables if cid in self.ancestors(c)]


def _django_tables(conn, hier: _Hierarchy, classes: dict) -> dict[int, str]:
    """모델 class_id → 테이블. Django: 다른 파일의 추상 모델을 상속한 class Basket(AbstractBasket)도 모델이고,
    Meta를 쓰지 않으면 추상 부모의 Meta(app_label·db_table)를 물려받는다. proxy 모델은 부모 테이블을 쓴다."""
    memo: dict[int, dict | None] = {}

    def resolve(cid: int, depth: int = 0) -> dict | None:
        if cid in memo:
            return memo[cid]
        memo[cid] = None  # 순환 방지
        own = hier.info.get(cid, {}).get("model")
        parent = None
        if depth < 12:
            parent = next((r for r in (resolve(b, depth + 1) for b in hier.bases(cid)) if r), None)
        if not (own and own.get("django")) and parent is None:
            return None
        own = own or {}
        name = classes[cid]["name"].split(".")[-1].lower()
        inherited = parent if parent and parent["abstract"] else {}
        result = {
            "abstract": bool(own.get("abstract")),
            "app_label": own.get("app_label") or inherited.get("app_label") or django_app(hier.module(cid)),
            "db_table": own.get("db_table") or inherited.get("db_table"),
        }
        if result["abstract"]:
            result["table"] = None
        elif own.get("proxy") and parent:
            result["table"] = parent["table"]
        else:
            result["table"] = result["db_table"] or f"{result['app_label']}_{name}"
        memo[cid] = result
        return result

    tables = {cid: classes[cid]["table_name"] for cid in hier.py_classes if classes[cid]["table_name"]}
    for cid in sorted(hier.py_classes):
        if classes[cid]["kind"] == "module":
            continue
        r = resolve(cid)
        if r is None:
            continue
        if r["table"] != classes[cid]["table_name"]:
            conn.execute("UPDATE classes SET table_name = ? WHERE id = ?", (r["table"], cid))
        if r["table"]:
            tables[cid] = r["table"]
        else:
            tables.pop(cid, None)
    return tables


def _loaded_names(conn, ctx: PyContext, hier: _Hierarchy, tables: dict) -> dict[int, dict[str, int]]:
    """모듈 수준 Basket = get_model("basket", "Basket"), Applicator = get_class("offer.applicator", "Applicator")."""
    result: dict[int, dict[str, int]] = defaultdict(dict)
    for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'pyassign:%'"):
        _, file_id, var = r["key"].split(":", 2)
        value = json.loads(r["value"])
        args = value["args"].get("value", [])
        if value["type"] not in ("get_class", "get_model") or len(args) < 2:
            continue
        if value["type"] == "get_class":
            cid = hier.loaded_class(args[0], args[1])
        else:  # get_model("basket", "Basket"): 앱 label이 맞는 모델
            cands = [c for c in hier.by_name.get(args[1], []) if c in tables]
            cid = next((c for c in cands if tables[c].lower().startswith(args[0].lower() + "_")), None) or \
                (cands[0] if len(cands) == 1 else None)
        if cid is not None:
            result[int(file_id)][var] = cid
    return result
