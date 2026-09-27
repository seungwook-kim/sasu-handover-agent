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

ROUTER_TYPES = {"APIRouter", "FastAPI", "Flask", "Blueprint", "Starlette", "Router"}
INCLUDE_CALLS = {"include_router": "prefix", "register_blueprint": "url_prefix"}
TEMPLATE_CALLS = {"render_template", "render", "TemplateResponse", "render_to_response", "get_template"}
TEMPLATE_SUFFIX = (".html", ".htm", ".jinja", ".jinja2", ".j2")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

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
# 너무 흔해서 이름만으로 추정하면 틀리기 쉬운 메서드 이름
_GENERIC_NAMES = {
    "get", "set", "post", "put", "delete", "save", "update", "create", "add", "remove", "run", "main", "init",
    "load", "read", "write", "close", "open", "send", "process", "handle", "execute", "query", "filter", "all",
    "first", "list", "items", "keys", "values", "copy", "clear", "append", "extend", "format", "join", "split",
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

    models = {
        classes[cid]["name"].split(".")[-1]: cid
        for cid in py_classes if classes[cid]["table_name"]
    }
    orm_statements: dict[tuple[str, str], int] = {}

    def orm_statement(model: str, kind: str) -> int:
        key = (model, kind)
        if key not in orm_statements:
            c = classes[models[model]]
            sid = conn.execute(
                "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
                (c["file_id"], f"ORM {model}", kind, c["line_start"], f"{model} 모델 ({c['table_name']}) ORM {kind}"),
            ).lastrowid
            conn.execute("INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
                         (sid, c["table_name"].upper()))
            orm_statements[key] = sid
        return orm_statements[key]

    # session.add(obj)처럼 모델 이름이 안 보이는 쓰기는 메서드가 만들거나 다루는 모델로 판단한다 (1-1)
    created: dict[int, set[str]] = defaultdict(set)  # 메서드 → 새로 만든 모델
    touched: dict[int, set[str]] = defaultdict(set)  # 메서드 → 조회·매개변수로 다루는 모델
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
        candidates = [receiver.split(".")[0].split("(")[0]] if receiver else []
        candidates += [a.split(".")[-1] for a in name_args]
        kind = ORM_KIND.get(callee)
        matched = next((cand for cand in candidates if cand in models), None)
        if matched and kind:
            edges.add(("method", caller, "statement", orm_statement(matched, kind), None, line, None))
        if matched:
            touched[caller].add(matched)
        if (not receiver and callee in models) or (receiver in models and callee in _MODEL_FACTORIES):
            created[caller].add(callee if callee in models else receiver)
        elif not matched and callee in _SESSION_WRITES and receiver and _is_session(receiver):
            session_writes.append((caller, _SESSION_WRITES[callee], line))

        targets, note = _resolve(receiver, callee, class_id, file, ctx, classes, methods_by_class,
                                 fields_by_class, name_map, py_classes)
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
        seen = touched[caller] | {p for p in params if p in models}
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
        match = _SQL_START.match(lit["value"])
        if not match or len(lit["value"]) < 15:
            continue
        kind = match.group(1).lower()
        kind = "select" if kind == "with" else kind
        sid = conn.execute(
            "INSERT INTO statements (file_id, full_id, kind, line, sql) VALUES (?, ?, ?, ?, ?)",
            (classes[lit["class_id"]]["file_id"], f"SQL {lit['path']}:{lit['line']}", kind, lit["line"], lit["value"]),
        ).lastrowid
        conn.executemany("INSERT INTO statement_tables (statement_id, table_name) VALUES (?, ?)",
                         [(sid, t) for t in extract_tables(lit["value"])])
        edges.add(("method", lit["method_id"], "statement", sid, None, lit["line"], None))

    # 3. URL
    urls = _python_urls(conn, ctx, classes, methods, class_module)
    urls += _django_urls(conn, ctx, methods_by_class)
    conn.executemany("INSERT INTO url_mappings (url, method_id) VALUES (?, ?)", urls)
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


def _django_urls(conn, ctx: PyContext, methods_by_class: dict) -> list[tuple[str, int]]:
    patterns: dict[str, list] = defaultdict(list)  # urls 모듈 → 패턴
    for r in conn.execute("SELECT u.*, f.module FROM url_patterns u JOIN files f ON f.id = u.file_id"):
        patterns[r["module"]].append(r)
    if not patterns:
        return []
    included = {p["target"] for rows in patterns.values() for p in rows if p["is_include"]}
    roots = [m for m in patterns if m not in included and not any(m.endswith("." + i) or i.endswith(m) for i in included)]
    urls = []

    def walk(module: str, prefix: str, depth: int) -> None:
        if depth > 6:
            return
        file = ctx.files[ctx.module_file[module]]
        for p in patterns[module]:
            route = prefix + "/" + p["route"]
            if p["is_include"]:
                target = _module_target(p["target"], ctx)
                if target and target[0] in patterns:
                    walk(target[0], route, depth + 1)
                continue
            for mid in _django_target(p["target"], file, ctx, methods_by_class):
                urls.append((normalize_url(route), mid))

    for root in roots or list(patterns):
        walk(root, "", 0)
    return urls


def _django_target(target: str, file: dict, ctx: PyContext, methods_by_class: dict) -> list[int]:
    """views.post_list → 그 함수, views.PostView.as_view() → PostView의 get·post 등."""
    target = target.replace(".as_view()", "").split("(")[0]
    root = target.split(".")[0]
    dotted = file["aliases"].get(root)
    dotted = dotted + target[len(root):] if dotted else f"{file['package']}.{target}"
    found = _module_target(dotted, ctx)
    if not found:
        return []
    module, rest = found
    if "." not in rest and module in ctx.module_class and rest in methods_by_class[ctx.module_class[module]]:
        return methods_by_class[ctx.module_class[module]][rest]
    cls = ctx.class_in_module.get((module, rest))
    if cls is None:
        return []
    return [mid for name in HTTP_METHODS for mid in methods_by_class[cls].get(name, [])]
