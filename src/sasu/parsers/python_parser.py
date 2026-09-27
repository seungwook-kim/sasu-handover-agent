"""tree-sitter로 Python 소스를 읽어 Java와 같은 공통 모델(JavaFile·JavaClass·JavaMethod)로 만든다.

Java의 애노테이션·DI 대신 Python 웹 프로젝트의 관례로 계층을 정한다.
- URL: Flask @app.route / @bp.route, FastAPI @router.get(...) + APIRouter(prefix=...), Django urls.py
- 계층: views·routes·api·endpoints → Controller, service → Service, repository·crud·dao → Repository
- 테이블: Django models.Model(Meta.db_table 또는 앱_모델), SQLAlchemy __tablename__, SQLModel(table=True)
- 모듈의 최상위 함수는 모듈 이름으로 된 가짜 클래스(kind="module")에 담는다.
"""

import hashlib
import re

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from sasu.parsers.java_parser import (
    _NOTE_TAG,
    Annotation,
    Call,
    ConditionLiteral,
    EmptyCatch,
    JavaClass,
    JavaField,
    JavaFile,
    JavaMethod,
    NoteComment,
    UrlPattern,
)

_PY = Language(tspython.language())
_PARSER = Parser(_PY)

ROUTE_DECORATORS = {"route", "get", "post", "put", "delete", "patch", "api_route", "websocket", "head", "options"}
_DECISION = {
    "if_statement", "elif_clause", "for_statement", "while_statement", "except_clause",
    "conditional_expression", "case_clause", "if_clause",
}
_LITERALS = {"string", "integer", "float", "true", "false", "none", "concatenated_string"}
_VIEW_MODULES = {"views", "view", "routes", "route", "api", "endpoints", "controllers", "controller", "handlers", "routers", "router"}
_VIEW_BASE = re.compile(r"(View|ViewSet|Resource|APIView|Handler)$")
_MODEL_BASE = re.compile(r"(^|\.)(Model|Base|DeclarativeBase|SQLModel|Document)$")


def parse_python(source: bytes, module: str) -> JavaFile:
    """module: 점으로 이은 모듈 경로 (예: app.api.routes.users)."""
    tree = _PARSER.parse(source)
    root = tree.root_node
    package, _, module_name = module.rpartition(".")
    imports: list[str] = []
    header: list[str] = []
    classes: list[JavaClass] = []
    functions: list[JavaMethod] = []
    fields: list[JavaField] = []
    url_patterns: list[UrlPattern] = []

    # 함수·클래스 밖의 문장 (app.include_router(...), if 설정: ...) 은 가짜 메서드 <module>에 담는다
    module_body = JavaMethod(
        name="<module>", line_start=1, line_end=root.end_point.row + 1, annotations=[], parameters=[], doc=None,
    )
    for i, node in enumerate(root.named_children):
        t = node.type
        if t not in ("function_definition", "class_definition", "decorated_definition", "import_statement",
                     "import_from_statement", "comment") and not (i == 0 and t == "expression_statement"):
            _scan_body(node, source, module_body, docstring=None)
        if t == "comment" and not classes and not functions:
            header.append(_text(node, source).lstrip("# "))
        elif t == "expression_statement" and i == 0 and node.named_children and node.named_children[0].type == "string":
            header.append(_string(node.named_children[0], source))
        elif t in ("import_statement", "import_from_statement"):
            imports.extend(_imports(node, source))
        elif t in ("function_definition", "decorated_definition") and _definition(node).type == "function_definition":
            functions.append(_parse_function(node, source))
        elif t in ("class_definition", "decorated_definition"):
            classes.extend(_parse_class(node, source, package=module, module_name=module_name, prefix=""))
        elif t == "expression_statement":
            for assign in node.named_children:
                if assign.type in ("assignment", "augmented_assignment"):
                    fields.extend(_module_assignment(assign, source))
                    url_patterns.extend(_url_patterns(assign, source))

    if module_body.calls or module_body.condition_literals:
        functions.append(module_body)
    module_class = JavaClass(
        name=module_name,
        package=package,
        kind="module",
        line_start=1,
        line_end=root.end_point.row + 1,
        annotations=[],
        superclass=None,
        interfaces=[],
        fields=fields,
        methods=functions,
        doc="\n".join(header) or None,
        explicit_stereotype=_module_stereotype(module, functions),
    )
    return JavaFile(
        package=package,
        imports=imports,
        header_comment="\n".join(header) or None,
        classes=[module_class, *classes],
        language="python",
        url_patterns=url_patterns,
    )


# ── 정의 ────────────────────────────────────────────────────────────────


def _definition(node: Node) -> Node:
    """decorated_definition이면 안의 함수·클래스 정의."""
    if node.type == "decorated_definition":
        return node.child_by_field_name("definition") or node.named_children[-1]
    return node


def _decorators(node: Node, source: bytes) -> list[Annotation]:
    if node.type != "decorated_definition":
        return []
    result = []
    for dec in node.named_children:
        if dec.type != "decorator":
            continue
        expr = dec.named_children[0] if dec.named_children else None
        if expr is None:
            continue
        func, args = (expr.child_by_field_name("function"), expr.child_by_field_name("arguments")) if expr.type == "call" else (expr, None)
        dotted = _text(func, source)
        ann = Annotation(name=dotted.split(".")[-1])
        if "." in dotted:
            ann.args["object"] = [dotted.rsplit(".", 1)[0]]
        positional, keywords = _call_arguments(args, source)
        if positional:
            ann.args["value"] = positional
        ann.args.update(keywords)
        result.append(ann)
    return result


def _parse_class(node: Node, source: bytes, package: str, module_name: str, prefix: str) -> list[JavaClass]:
    definition = _definition(node)
    name = prefix + _text(definition.child_by_field_name("name"), source)
    bases_node = definition.child_by_field_name("superclasses")
    raw_bases, class_kwargs = [], {}
    if bases_node is not None:
        for arg in bases_node.named_children:
            if arg.type == "keyword_argument":
                class_kwargs[_text(arg.child_by_field_name("name"), source)] = _text(arg.child_by_field_name("value"), source)
            else:
                raw_bases.append(_text(arg, source))
    simple_bases = [b.split(".")[-1].split("[")[0] for b in raw_bases]

    fields: list[JavaField] = []
    methods: list[JavaMethod] = []
    inner: list[JavaClass] = []
    table_name = None
    meta_table = None
    body = definition.child_by_field_name("body")
    doc = None
    for i, member in enumerate(body.named_children if body is not None else []):
        if i == 0 and member.type == "expression_statement" and member.named_children and member.named_children[0].type == "string":
            doc = _string(member.named_children[0], source)
            continue
        target = _definition(member)
        if target.type == "function_definition":
            method = _parse_function(member, source)
            methods.append(method)
            if method.name == "__init__":
                fields.extend(_init_fields(target, source))
        elif target.type == "class_definition":
            inner_name = _text(target.child_by_field_name("name"), source)
            if inner_name == "Meta":
                meta_table = _meta_db_table(target, source)
            else:
                inner.extend(_parse_class(member, source, package, module_name, prefix=name + "."))
        elif member.type == "expression_statement":
            for assign in member.named_children:
                if assign.type == "assignment":
                    left = _text(assign.child_by_field_name("left"), source)
                    right = assign.child_by_field_name("right")
                    if left == "__tablename__" and right is not None and right.type == "string":
                        table_name = _string(right, source)
                    else:
                        fields.extend(_class_field(assign, source))

    # SQLModel은 class User(UserBase, table=True)처럼 다른 부모를 거치는 경우가 많아 table=True도 모델로 본다
    is_model = (any(_MODEL_BASE.search(b) for b in raw_bases) or class_kwargs.get("table") == "True") \
        and not _VIEW_BASE.search(name)
    if is_model:
        if table_name is None and meta_table:
            table_name = meta_table
        if table_name is None and any(b.endswith("models.Model") or b == "Model" for b in raw_bases):
            app = package.rsplit(".", 2)[-2] if package.count(".") >= 1 else package  # blog.models → blog
            table_name = f"{app}_{name.split('.')[-1].lower()}"
        if table_name is None and class_kwargs.get("table") == "True":
            table_name = name.split(".")[-1].lower()  # SQLModel(table=True) 기본 규칙

    cls = JavaClass(
        name=name,
        package=package,
        kind="class",
        line_start=definition.start_point.row + 1,
        line_end=definition.end_point.row + 1,
        annotations=_decorators(node, source),
        superclass=simple_bases[0] if simple_bases else None,
        interfaces=simple_bases[1:],
        fields=fields,
        methods=methods,
        doc=doc,
        explicit_stereotype=_class_stereotype(name, simple_bases, module_name, methods),
        table_name=table_name,
    )
    return [cls, *inner]


def _parse_function(node: Node, source: bytes) -> JavaMethod:
    definition = _definition(node)
    params = []
    params_node = definition.child_by_field_name("parameters")
    for p in params_node.named_children if params_node is not None else []:
        type_node = p.child_by_field_name("type")
        name_node = p.child_by_field_name("name") or (p if p.type == "identifier" else (p.named_children[0] if p.named_children else None))
        pname = _text(name_node, source) if name_node is not None else ""
        if pname in ("self", "cls"):
            continue
        params.append(_text(type_node, source) if type_node is not None else pname)
    body = definition.child_by_field_name("body")
    doc = None
    if body is not None and body.named_children:
        first = body.named_children[0]
        if first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
            doc = _string(first.named_children[0], source)
    method = JavaMethod(
        name=_text(definition.child_by_field_name("name"), source),
        line_start=definition.start_point.row + 1,
        line_end=definition.end_point.row + 1,
        annotations=_decorators(node, source),
        parameters=params,
        doc=doc,
    )
    # def 바로 다음 줄의 주석은 tree-sitter가 본문(block) 밖, 정의 바로 아래에 둔다
    for child in definition.children:
        if child.type == "comment":
            _scan_body(child, source, method, docstring=doc)
    if body is not None:
        _scan_body(body, source, method, docstring=doc)
        tokens = _normalized_tokens(body, source)
        method.body_tokens = len(tokens)
        method.body_hash = hashlib.sha1(" ".join(tokens).encode()).hexdigest()
    return method


def route_paths(method: JavaMethod) -> list[tuple[str, str | None]]:
    """(경로, 데코레이터 객체 이름). @router.get("/{id}") → ("/{id}", "router")."""
    result = []
    for ann in method.annotations:
        if ann.name in ROUTE_DECORATORS and ann.args.get("value"):
            obj = ann.first("object")
            for path in ann.args.get("value", []) + ann.args.get("path", []) + ann.args.get("rule", []):
                result.append((path, obj))
    return result


# ── 본문 ────────────────────────────────────────────────────────────────


def _scan_body(body: Node, source: bytes, method: JavaMethod, docstring: str | None) -> None:
    stack = [body]
    while stack:
        n = stack.pop()
        t = n.type
        if t in _DECISION:
            method.complexity += 1
        elif t == "boolean_operator":
            method.complexity += 1
        if t in ("if_statement", "elif_clause"):
            cond = n.child_by_field_name("condition")
            if cond is not None:
                _condition_literals(cond, source, method)
        elif t == "case_clause":
            _condition_literals(n, source, method)
        if t == "comment":
            text = _text(n, source)
            m = _NOTE_TAG.search(text)
            if m:
                method.notes.append(NoteComment(
                    tag=(m.group(1) or m.group(2)).upper() if m.group(1) else m.group(2),
                    text=" ".join(text.lstrip("# ").split())[:200],
                    line=n.start_point.row + 1,
                ))
            continue
        if t == "call":
            method.calls.append(_call(n, source))
        elif t == "return_statement":
            value = next(iter(n.named_children), None)
            if value is not None and value.type == "string":
                method.returned_strings.append(_string(value, source))
        elif t == "except_clause":
            block = next((c for c in n.named_children if c.type == "block"), None)
            if block is not None and all(c.type in ("pass_statement", "comment") or _is_ellipsis(c) for c in block.named_children):
                types = [c for c in n.named_children if c.type not in ("block", "comment")]
                method.empty_catches.append(EmptyCatch(
                    exception_type=_text(types[0], source).split(" as ")[0] if types else "",
                    line=n.start_point.row + 1,
                ))
        elif t == "string":
            value = _string(n, source)
            if value != docstring:
                method.string_literals.append((value, n.start_point.row + 1))
            continue
        stack.extend(reversed(n.children))


def _call(node: Node, source: bytes) -> Call:
    func = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")
    if func is not None and func.type == "attribute":
        receiver = _text(func.child_by_field_name("object"), source)
        name = _text(func.child_by_field_name("attribute"), source)
    else:
        receiver, name = None, _text(func, source)
    positional, keywords = _call_arguments(args, source)
    name_args, prefixes = [], []
    for a in args.named_children if args is not None and args.type == "argument_list" else []:
        if a.type in ("identifier", "attribute"):
            name_args.append(_text(a, source))
        elif a.type == "binary_operator":
            left = a.child_by_field_name("left")
            if left is not None and left.type == "string" and _text(a.child_by_field_name("operator"), source) == "+":
                prefixes.append(_string(left, source))
        elif a.type == "string" and any(c.type == "interpolation" for c in a.named_children):
            head = _fstring_head(a, source)
            if head:
                prefixes.append(head)
    return Call(
        receiver=receiver,
        method=name,
        string_args=positional,
        line=node.start_point.row + 1,
        string_prefixes=prefixes,
        name_args=name_args,
        keyword_args={k: v[0] for k, v in keywords.items() if v},
    )


def _call_arguments(args: Node | None, source: bytes) -> tuple[list[str], dict[str, list[str]]]:
    positional: list[str] = []
    keywords: dict[str, list[str]] = {}
    if args is None:
        return positional, keywords
    for a in args.named_children:
        if a.type == "string" and not any(c.type == "interpolation" for c in a.named_children):
            positional.append(_string(a, source))
        elif a.type == "keyword_argument":
            key = _text(a.child_by_field_name("name"), source)
            value = a.child_by_field_name("value")
            if value is not None and value.type == "string":
                keywords[key] = [_string(value, source)]
            elif value is not None and value.type in ("list", "tuple"):
                keywords[key] = [_string(v, source) for v in value.named_children if v.type == "string"]
            elif value is not None:
                keywords[key] = [_text(value, source)]
    return positional, keywords


def _condition_literals(cond: Node, source: bytes, method: JavaMethod) -> None:
    text = " ".join(_text(cond, source).split())
    stack = [cond]
    while stack:
        n = stack.pop()
        if n.type in ("string", "integer"):
            value = _string(n, source) if n.type == "string" else _text(n, source)
            method.condition_literals.append(ConditionLiteral(value, n.start_point.row + 1, text[:160]))
            continue
        stack.extend(n.children)


def _normalized_tokens(node: Node, source: bytes) -> list[str]:
    tokens: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "comment":
            continue
        if n.type in _LITERALS:
            tokens.append("LIT")
            continue
        if n.child_count == 0:
            tokens.append("ID" if n.type == "identifier" else n.type)
            continue
        stack.extend(reversed(n.children))
    return tokens


# ── 모듈·클래스 수준 할당 ─────────────────────────────────────────────


def _module_assignment(assign: Node, source: bytes) -> list[JavaField]:
    """router = APIRouter(prefix="/users"), bp = Blueprint("x", __name__, url_prefix="/x")."""
    left = assign.child_by_field_name("left")
    right = assign.child_by_field_name("right")
    if left is None or right is None or left.type != "identifier" or right.type != "call":
        return []
    positional, keywords = _call_arguments(right.child_by_field_name("arguments"), source)
    args = dict(keywords)
    if positional:
        args["value"] = positional
    return [JavaField(
        name=_text(left, source),
        type_name=_text(right.child_by_field_name("function"), source).split(".")[-1],
        annotations=[],
        line=assign.start_point.row + 1,
        call_args=args,
    )]


def _class_field(assign: Node, source: bytes) -> list[JavaField]:
    """title = models.CharField(verbose_name="제목"), title: str = Field(description="제목")."""
    left = assign.child_by_field_name("left")
    right = assign.child_by_field_name("right")
    type_node = assign.child_by_field_name("type")
    if left is None or left.type != "identifier":
        return []
    type_name, doc = "", None
    if right is not None and right.type == "call":
        type_name = _text(right.child_by_field_name("function"), source).split(".")[-1]
        positional, keywords = _call_arguments(right.child_by_field_name("arguments"), source)
        for key in ("verbose_name", "help_text", "description", "comment", "title", "doc"):
            if keywords.get(key) and keywords[key][0] and not keywords[key][0].startswith(("_", "gettext")):
                doc = keywords[key][0]
                break
        if doc is None and type_name.endswith("Field") and positional:
            doc = positional[0]  # models.CharField("제목", max_length=10)
    if type_node is not None:
        type_name = _text(type_node, source)
    comment = assign.parent.next_named_sibling if assign.parent is not None else None
    if doc is None and comment is not None and comment.type == "comment" and comment.start_point.row == assign.start_point.row:
        doc = _text(comment, source).lstrip("# ")
    return [JavaField(name=_text(left, source), type_name=type_name, annotations=[], line=assign.start_point.row + 1, doc=doc)]


def _init_fields(init: Node, source: bytes) -> list[JavaField]:
    """__init__의 self.repo = UserRepository() 또는 self.repo = repo (repo: UserRepository)."""
    param_types = {}
    params = init.child_by_field_name("parameters")
    for p in params.named_children if params is not None else []:
        type_node = p.child_by_field_name("type")
        name_node = p.child_by_field_name("name") or (p.named_children[0] if p.named_children else None)
        if type_node is not None and name_node is not None:
            param_types[_text(name_node, source)] = _text(type_node, source)
    fields = []
    stack = [init.child_by_field_name("body")]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        if n.type == "assignment":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and left.type == "attribute" and _text(left.child_by_field_name("object"), source) == "self":
                name = _text(left.child_by_field_name("attribute"), source)
                type_name = None
                if right is not None and right.type == "call":
                    type_name = _text(right.child_by_field_name("function"), source).split(".")[-1]
                elif right is not None and right.type == "identifier":
                    type_name = param_types.get(_text(right, source))
                if type_name:
                    fields.append(JavaField(name=name, type_name=type_name.split(".")[-1].strip("'\""), annotations=[],
                                            line=n.start_point.row + 1))
            continue
        stack.extend(n.named_children)
    return fields


def _meta_db_table(meta: Node, source: bytes) -> str | None:
    body = meta.child_by_field_name("body")
    for stmt in body.named_children if body is not None else []:
        for assign in stmt.named_children if stmt.type == "expression_statement" else []:
            if assign.type == "assignment" and _text(assign.child_by_field_name("left"), source) == "db_table":
                right = assign.child_by_field_name("right")
                if right is not None and right.type == "string":
                    return _string(right, source)
    return None


def _url_patterns(assign: Node, source: bytes) -> list[UrlPattern]:
    """urlpatterns = [path("posts/", views.post_list), path("api/", include("api.urls"))]."""
    left = assign.child_by_field_name("left")
    right = assign.child_by_field_name("right")
    if left is None or right is None or _text(left, source) != "urlpatterns":
        return []
    result = []
    stack = [right]
    while stack:
        n = stack.pop()
        if n.type == "call":
            func = _text(n.child_by_field_name("function"), source).split(".")[-1]
            args = n.child_by_field_name("arguments")
            items = args.named_children if args is not None else []
            if func in ("path", "re_path", "url") and len(items) >= 2 and items[0].type == "string":
                route = _string(items[0], source).lstrip("^").rstrip("$")
                target = items[1]
                if target.type == "call" and _text(target.child_by_field_name("function"), source).endswith("include"):
                    inc_args = target.child_by_field_name("arguments")
                    first = inc_args.named_children[0] if inc_args is not None and inc_args.named_children else None
                    if first is not None and first.type == "string":
                        result.append(UrlPattern(route, _string(first, source), n.start_point.row + 1, is_include=True))
                else:
                    result.append(UrlPattern(route, _text(target, source), n.start_point.row + 1))
                continue
        stack.extend(n.named_children)
    return result


# ── 계층 정하기 ──────────────────────────────────────────────────────


def _module_stereotype(module: str, functions: list[JavaMethod]) -> str | None:
    parts = module.lower().split(".")
    last = parts[-1]
    if any(route_paths(f) for f in functions) or last in _VIEW_MODULES or any(p in ("api", "routes", "views") for p in parts[:-1]):
        return "Controller" if functions else None
    if "service" in last:
        return "Service" if functions else None
    if any(k in last for k in ("repository", "repo", "dao", "crud", "store")):
        return "Repository" if functions else None
    return None


def _class_stereotype(name: str, bases: list[str], module_name: str, methods: list[JavaMethod]) -> str | None:
    simple = name.split(".")[-1]
    if any(_VIEW_BASE.search(b) for b in bases) or any(route_paths(m) for m in methods) or simple.endswith(("Controller", "Handler")):
        return "Controller"
    if simple.endswith("Service"):
        return "Service"
    if simple.endswith(("Repository", "Repo", "DAO", "Dao", "Crud", "CRUD")):
        return "Repository"
    return None


# ── 도우미 ─────────────────────────────────────────────────────────


def _imports(node: Node, source: bytes) -> list[str]:
    """"full.path as alias" 형식. from a.b import c as d → a.b.c as d."""
    result = []
    if node.type == "import_statement":
        for name in node.named_children:
            if name.type == "aliased_import":
                result.append(f"{_text(name.child_by_field_name('name'), source)} as {_text(name.child_by_field_name('alias'), source)}")
            else:
                result.append(_text(name, source))
    else:
        module = _text(node.child_by_field_name("module_name"), source)
        # from . import views → .views, from .sub import x → .sub.x (앞의 점 개수가 상대 경로 단계)
        joiner = "" if module.endswith(".") else "."
        for name in node.children_by_field_name("name"):
            if name.type == "aliased_import":
                result.append(f"{module}{joiner}{_text(name.child_by_field_name('name'), source)} as {_text(name.child_by_field_name('alias'), source)}")
            else:
                result.append(f"{module}{joiner}{_text(name, source)}")
    return result


def _string(node: Node, source: bytes) -> str:
    return "".join(_text(c, source) for c in node.named_children if c.type in ("string_content", "escape_sequence"))


def _fstring_head(node: Node, source: bytes) -> str:
    head = []
    for c in node.named_children:
        if c.type == "interpolation":
            break
        if c.type in ("string_content", "escape_sequence"):
            head.append(_text(c, source))
    return "".join(head)


def _is_ellipsis(node: Node) -> bool:
    return node.type == "expression_statement" and bool(node.named_children) and node.named_children[0].type == "ellipsis"


def _text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
