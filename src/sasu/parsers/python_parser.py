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
_DEFINITION_CONTAINERS = {
    "class_definition", "decorated_definition", "function_definition", "block", "if_statement", "elif_clause",
    "else_clause", "try_statement", "except_clause", "finally_clause",
}
# 문자열로 클래스·모델을 불러오는 함수 (django-oscar get_class("basket.views", "BasketView"), Django apps.get_model)
_LOADERS = {"get_class", "get_model", "get_app_config"}
# 클래스 본문의 상수 중 연결에 쓰는 것: 앱 설정 label·name, 클래스 기반 뷰의 model·queryset·template_name
_CLASS_ATTRS = {"label", "name", "model", "queryset", "template_name"}


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
        guarded = _guarded_classes(node) if t in ("if_statement", "try_statement") else []
        for cls_node in guarded:
            # Django 재사용 앱: if not is_model_registered("basket", "Basket"): class Basket(AbstractBasket)
            classes.extend(_parse_class(cls_node, source, package=module, module_name=module_name, prefix=""))
        if t not in ("function_definition", "class_definition", "decorated_definition", "import_statement",
                     "import_from_statement", "comment") and not (i == 0 and t == "expression_statement"):
            _scan_body(node, source, module_body, docstring=None,
                       skip={(c.start_byte, c.end_byte) for c in guarded})
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

    # 앱 설정 클래스의 def get_urls(self): return [path("add/", self.add_view.as_view()), ...] (django-oscar 등)
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "function_definition" and _text(n.child_by_field_name("name"), source) == "get_urls":
            url_patterns.extend(_path_calls(n.child_by_field_name("body"), source))
            continue
        stack.extend(c for c in n.named_children if c.type in _DEFINITION_CONTAINERS)

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
    facts = django_facts(root, source, module)
    if facts:
        module_class.py_info = {"facts": facts}  # 인수인계 질문·정리 후보가 쓴다 (analysis/django.py)
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
    meta: dict | None = None
    attrs: dict[str, str] = {}
    relations: dict[str, str] = {}  # 역방향 관계 이름 → 이 모델의 필드 (order = ForeignKey(..., related_name="notes"))
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
            if method.name in ("__init__", "ready"):  # ready: Django 앱 설정이 뷰·하위 앱을 불러 두는 곳
                fields.extend(_init_fields(target, source))
        elif target.type == "class_definition":
            inner_name = _text(target.child_by_field_name("name"), source)
            if inner_name == "Meta":
                meta = _meta_options(target, source)
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
                        value = _attr_value(left, right, source)
                        if value:
                            attrs[left] = value
                        related = _related_name(right, source)
                        if related:
                            relations[related] = left

    # SQLModel은 class User(UserBase, table=True)처럼 다른 부모를 거치는 경우가 많아 table=True도 모델로 본다
    is_model = (any(_MODEL_BASE.search(b) for b in raw_bases) or class_kwargs.get("table") == "True") \
        and not _VIEW_BASE.search(name)
    django_model = is_model and any(b.endswith("models.Model") or b == "Model" for b in raw_bases)
    meta = meta or {}
    if is_model and not meta.get("abstract"):  # abstract = True인 Django 모델은 테이블이 없다 (상속한 쪽이 갖는다)
        if table_name is None and meta.get("db_table"):
            table_name = meta["db_table"]
        if table_name is None and django_model:
            table_name = f"{meta.get('app_label') or django_app(package)}_{name.split('.')[-1].lower()}"
        if table_name is None and class_kwargs.get("table") == "True":
            table_name = name.split(".")[-1].lower()  # SQLModel(table=True) 기본 규칙
    stereotype = _class_stereotype(name, simple_bases, module_name, methods)
    has_settings = body is not None and any(
        c.type == "expression_statement" and c.named_children and c.named_children[0].type == "assignment"
        for c in body.named_children)
    if stereotype == "Controller" and not methods and has_settings:
        # class ProductListView(generic.ListView): model = Product 처럼 메서드 없이 설정만 있는 클래스 뷰.
        # URL을 걸 곳이 있게 클래스 본문을 가짜 메서드 <class>에 담는다 (queryset = ... 의 ORM 호출도 여기 잡힌다)
        view_body = JavaMethod(name="<class>", line_start=definition.start_point.row + 1,
                               line_end=definition.end_point.row + 1, annotations=[], parameters=[], doc=None)
        _scan_body(body, source, view_body, docstring=doc)
        methods.append(view_body)
    py_info: dict = {}
    if django_model or meta:
        # 다른 파일의 추상 모델을 상속한 class Basket(AbstractBasket)는 linker가 이 정보로 테이블을 정한다
        py_info["model"] = {**meta, "django": django_model, "app": django_app(package)}
    if attrs:
        py_info["attrs"] = attrs
    if relations and "model" in py_info:
        py_info["relations"] = relations
    loaded = {f.name: f.call_args["loader"] for f in fields if f.call_args.get("loader")}
    if loaded:
        py_info["loaded"] = loaded

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
        explicit_stereotype=stereotype,
        table_name=table_name,
        py_info=py_info,
    )
    return [cls, *inner]


def django_app(module: str) -> str:
    """Django 앱 이름 = 모델 모듈의 바로 위 패키지 (blog.models → blog, oscar.apps.basket.abstract_models → basket)."""
    return module.rsplit(".", 2)[-2] if module.count(".") >= 1 else module


def _guarded_classes(node: Node) -> list[Node]:
    """최상위 if·try 블록 바로 안의 class 정의 (else·except 쪽 포함)."""
    found = []
    for clause in [node, *(c for c in node.named_children if c.type in ("else_clause", "elif_clause", "except_clause", "finally_clause"))]:
        for block in (c for c in clause.named_children if c.type == "block"):
            found.extend(s for s in block.named_children
                         if s.type in ("class_definition", "decorated_definition") and _definition(s).type == "class_definition")
    return found


def _related_name(right: Node | None, source: bytes) -> str | None:
    """models.ForeignKey("order.Order", related_name="notes") → notes ("+"는 역방향 없음)."""
    if right is None or right.type != "call":
        return None
    func = _text(right.child_by_field_name("function"), source).split(".")[-1]
    if func not in ("ForeignKey", "OneToOneField", "ManyToManyField", "ParentalKey"):
        return None
    _, keywords = _call_arguments(right.child_by_field_name("arguments"), source)
    name = (keywords.get("related_name") or [""])[0]
    return name if name.isidentifier() else None


def _attr_value(left: str, right: Node | None, source: bytes) -> str | None:
    """label = "basket" → basket, model = Product → Product, queryset = Product.objects.filter(...) → Product."""
    if left not in _CLASS_ATTRS or right is None:
        return None
    if right.type == "string":
        return _string(right, source) or None
    if left in ("model", "queryset") and right.type in ("identifier", "attribute", "call"):
        root = re.match(r"[A-Za-z_]\w*", _text(right, source))
        return root.group(0) if root else None
    return None


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


def _scan_body(body: Node, source: bytes, method: JavaMethod, docstring: str | None, skip: set | None = None) -> None:
    stack = [body]
    while stack:
        n = stack.pop()
        if skip and (n.start_byte, n.end_byte) in skip:
            continue  # 따로 클래스로 읽은 정의
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
                call_args = {}
                if right is not None and right.type == "call":
                    type_name = _text(right.child_by_field_name("function"), source).split(".")[-1]
                    if type_name in _LOADERS:
                        # self.add_view = get_class("basket.views", "BasketAddView") → BasketAddView
                        strings, _ = _call_arguments(right.child_by_field_name("arguments"), source)
                        call_args = {"loader": [type_name, *strings]}
                        if type_name == "get_app_config":
                            type_name = "AppConfig"
                        elif strings:
                            type_name = strings[-1]
                elif right is not None and right.type == "identifier":
                    type_name = param_types.get(_text(right, source))
                if type_name:
                    fields.append(JavaField(name=name, type_name=type_name.split(".")[-1].strip("'\""), annotations=[],
                                            line=n.start_point.row + 1, call_args=call_args))
            continue
        stack.extend(n.named_children)
    return fields


def _meta_options(meta: Node, source: bytes) -> dict:
    """class Meta: db_table·app_label (문자열), abstract·proxy (True)."""
    options: dict = {}
    body = meta.child_by_field_name("body")
    for stmt in body.named_children if body is not None else []:
        for assign in stmt.named_children if stmt.type == "expression_statement" else []:
            if assign.type != "assignment":
                continue
            key = _text(assign.child_by_field_name("left"), source)
            right = assign.child_by_field_name("right")
            if key in ("db_table", "app_label") and right is not None and right.type == "string":
                options[key] = _string(right, source)
            elif key in ("abstract", "proxy") and right is not None and right.type == "true":
                options[key] = True
    return options


def _url_patterns(assign: Node, source: bytes) -> list[UrlPattern]:
    """urlpatterns = [path("posts/", views.post_list), path("api/", include("api.urls"))]."""
    left = assign.child_by_field_name("left")
    right = assign.child_by_field_name("right")
    if left is None or right is None or _text(left, source) != "urlpatterns":
        return []
    return _path_calls(right, source)


def _path_calls(node: Node | None, source: bytes) -> list[UrlPattern]:
    """path("posts/", views.post_list), path("api/", include("api.urls")), path("basket/", self.basket_app.urls)."""
    result = []
    stack = [node] if node is not None else []
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
                    elif first is not None:  # include(self.catalogue_app.urls[0]), include(apps.get_app_config("x").urls[0])
                        result.append(UrlPattern(route, _text(first, source), n.start_point.row + 1, is_include=True))
                elif re.search(r"\.urls(\[0\])?$", _text(target, source)):  # path("basket/", self.basket_app.urls)
                    result.append(UrlPattern(route, _text(target, source), n.start_point.row + 1, is_include=True))
                else:
                    result.append(UrlPattern(route, " ".join(_text(target, source).split()), n.start_point.row + 1))
                continue
        stack.extend(n.named_children)
    return result


# ── Django 관례 (질문·정리 후보용) ─────────────────────────────────────

_UPPER_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")
_SETTINGS_OBJECTS = {"settings", "conf.settings", "django.conf.settings"}
_SETTINGS_MODULES = re.compile(r"^(settings\w*|defaults|global_settings|app_settings|conf)$")
_URL_REVERSE = {"reverse", "reverse_lazy", "redirect", "resolve_url"}
_URL_CALLS = ("path", "re_path", "url")
_CONDITION_PARENTS = {"if_statement", "elif_clause", "while_statement"}


def django_facts(root: Node, source: bytes, module: str) -> list[dict]:
    """메서드 표(calls 등)에 남지 않는 Django 관례를 모은다. 줄 번호로 메서드를 찾는다.
    - receiver: @receiver(post_save, sender=Order) 붙은 함수 (시그널을 받으면 실행)
    - setting: settings.OSCAR_X·getattr(settings, "X") 읽기 (cond: 조건식 안에서 읽음)
    - setting_def: settings·defaults 모듈의 대문자 상수 정의
    - urlname: path("x/", view, name="x")의 이름 / urlref: reverse("app:x")처럼 이름으로 부르는 곳
    - str: 클래스 본문의 이름 같은 문자열 (admin list_display = ("get_title",) 처럼 메서드를 이름으로 가리킴)
    - attr: 클래스 본문의 add_signal = signals.basket_addition 같은 이름 대입 (self.add_signal.send의 정체)"""
    facts: list[dict] = []
    seen: set = set()

    def add(fact: dict) -> None:
        key = tuple(sorted(fact.items()))
        if key not in seen:
            seen.add(key)
            facts.append(fact)

    last = module.rsplit(".", 1)[-1]
    if _SETTINGS_MODULES.match(last):
        for node in root.named_children:
            assign = node.named_children[0] if node.type == "expression_statement" and node.named_children else None
            if assign is not None and assign.type == "assignment":
                left = _text(assign.child_by_field_name("left"), source)
                if _UPPER_NAME.match(left):
                    value = " ".join(_text(assign.child_by_field_name("right"), source).split())
                    add({"k": "setting_def", "name": left, "line": assign.start_point.row + 1, "value": value[:80]})
                    # MIDDLEWARE·context_processors·INSTALLED_APPS에 등록한 점 경로 (호출이 없는 settings는 <module>이 안 생긴다)
                    for s in _strings_in(assign.child_by_field_name("right"), source):
                        if re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", s):
                            add({"k": "registered", "value": s})

    stack: list[tuple[Node, bool, bool]] = [(root, False, False)]  # (노드, 조건식 안, 클래스 본문 바로 안)
    while stack:
        n, in_cond, class_level = stack.pop()
        t = n.type
        if t == "decorated_definition":
            definition = _definition(n)
            if definition.type == "function_definition":
                for dec in _decorators(n, source):
                    if dec.name == "receiver":
                        signals = [s.split(".")[-1] for s in _decorator_names(n, source, "receiver")]
                        fact = {"k": "receiver", "func": _text(definition.child_by_field_name("name"), source),
                                "line": definition.start_point.row + 1, "signals": ",".join(signals)}
                        if dec.first("sender"):
                            fact["sender"] = dec.first("sender").split(".")[-1]
                        add(fact)
        elif t == "attribute" and _text(n.child_by_field_name("object"), source) in _SETTINGS_OBJECTS:
            name = _text(n.child_by_field_name("attribute"), source)
            if _UPPER_NAME.match(name):
                add({"k": "setting", "name": name, "line": n.start_point.row + 1, "cond": in_cond})
            continue
        elif t == "call":
            func = _text(n.child_by_field_name("function"), source)
            args = n.child_by_field_name("arguments")
            items = args.named_children if args is not None else []
            short = func.split(".")[-1]
            if func == "getattr" and len(items) >= 2 and _text(items[0], source) in _SETTINGS_OBJECTS \
                    and items[1].type == "string" and _UPPER_NAME.match(_string(items[1], source)):
                add({"k": "setting", "name": _string(items[1], source), "line": n.start_point.row + 1, "cond": in_cond})
            elif short in _URL_CALLS:
                _, keywords = _call_arguments(args, source)
                name = (keywords.get("name") or [""])[0]
                if name and re.fullmatch(r"[\w-]+", name):
                    add({"k": "urlname", "name": name, "line": n.start_point.row + 1})
            elif short in _URL_REVERSE and items and items[0].type == "string":
                add({"k": "urlref", "value": _string(items[0], source)})
        elif t == "expression_statement" and class_level and n.named_children and n.named_children[0].type == "assignment":
            assign = n.named_children[0]
            left = _text(assign.child_by_field_name("left"), source)
            right = assign.child_by_field_name("right")
            if right is not None and right.type in ("identifier", "attribute") and left.isidentifier():
                add({"k": "attr", "name": left, "value": _text(right, source), "line": assign.start_point.row + 1})
            owner = n.parent.parent if n.parent is not None else None
            bases = owner.child_by_field_name("superclasses") if owner is not None else None
            if right is not None and _UPPER_NAME.match(left) and bases is not None and "Settings" in _text(bases, source):
                # pydantic class Settings(BaseSettings): SMTP_TLS: bool = True → settings.SMTP_TLS의 기본값
                value = " ".join(_text(right, source).split())
                add({"k": "setting_def", "name": left, "line": assign.start_point.row + 1, "value": value[:80]})
            for s in _strings_in(right, source):
                if re.fullmatch(r"[A-Za-z_]\w*", s):
                    add({"k": "str", "value": s})
        children = n.named_children
        if t in _CONDITION_PARENTS:
            cond = n.child_by_field_name("condition")
            for c in reversed(children):
                stack.append((c, in_cond or (cond is not None and c.start_byte == cond.start_byte
                                             and c.end_byte == cond.end_byte), False))
            continue
        if t == "conditional_expression" and len(children) >= 2:
            for i, c in reversed(list(enumerate(children))):
                stack.append((c, in_cond or i == 1, False))
            continue
        body_of_class = t == "block" and n.parent is not None and n.parent.type == "class_definition"
        for c in reversed(children):
            stack.append((c, in_cond, body_of_class))
    names = _used_names(root, source)
    if names:
        # 정의 말고 쓰인 이름들: property(_get, _set), {"int": _option_int_field}, upload_to=get_path (정리 후보가 쓴다)
        facts.append({"k": "names", "value": " ".join(sorted(names))})
    return facts


def _used_names(root: Node, source: bytes) -> set[str]:
    """파일에서 쓰인 소문자·_ 시작 식별자 (def·class 이름 자리와 매개변수는 뺀다)."""
    skip: set[tuple[int, int]] = set()
    names: set[str] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if t in ("function_definition", "class_definition"):
            name = n.child_by_field_name("name")
            if name is not None:
                skip.add((name.start_byte, name.end_byte))
            params = n.child_by_field_name("parameters")
            if params is not None:
                skip.update((p.start_byte, p.end_byte) for p in params.named_children)
                for p in params.named_children:  # def f(x=default_fn): 기본값은 쓰인 이름
                    value = p.child_by_field_name("value")
                    if value is not None:
                        stack.append(value)
                n_children = [c for c in n.named_children if c is not params]
                stack.extend(n_children)
                continue
        elif t == "identifier":
            if (n.start_byte, n.end_byte) not in skip:
                text = _text(n, source)
                if len(text) > 2 and (text[0].islower() or text[0] == "_"):
                    names.add(text)
            continue
        elif t in ("comment", "string"):
            continue
        stack.extend(n.named_children)
    return names


def _decorator_names(node: Node, source: bytes, name: str) -> list[str]:
    """@receiver(post_save, ...) / @receiver([post_save, post_delete], ...)의 첫 인자 이름들."""
    for dec in node.named_children:
        expr = dec.named_children[0] if dec.type == "decorator" and dec.named_children else None
        if expr is None or expr.type != "call" or _text(expr.child_by_field_name("function"), source).split(".")[-1] != name:
            continue
        args = expr.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if first is None:
            return []
        if first.type in ("list", "tuple"):
            return [_text(c, source) for c in first.named_children if c.type in ("identifier", "attribute")]
        return [_text(first, source)] if first.type in ("identifier", "attribute") else []
    return []


def _strings_in(node: Node | None, source: bytes) -> list[str]:
    result, stack = [], [node] if node is not None else []
    while stack:
        n = stack.pop()
        if n.type == "string":
            result.append(_string(n, source))
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
