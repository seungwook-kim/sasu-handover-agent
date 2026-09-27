"""tree-sitter로 Java 소스를 읽어 클래스·필드·메서드·호출 정보를 뽑는다.

SI 프로젝트(Spring MVC + MyBatis)에서 계층을 잇는 데 필요한 정보만 뽑는다.
- 클래스 애노테이션: @Controller, @Service("빈이름"), @Repository("빈이름"), @RequestMapping
- 필드 주입: @Resource(name="빈이름"), @Autowired + @Qualifier
- 메서드: URL 매핑, 다른 객체의 메서드 호출, 문자열 인자("네임스페이스.쿼리ID"), 반환하는 뷰 이름
- 위험 신호 재료: 복잡도, 비어 있는 catch 블록
"""

import hashlib
from dataclasses import dataclass, field

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

_JAVA = Language(tsjava.language())
_PARSER = Parser(_JAVA)

MAPPING_ANNOTATIONS = {
    "RequestMapping",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
}

# 순환 복잡도에 1씩 더하는 노드
_DECISION_NODES = {
    "if_statement",
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "catch_clause",
    "switch_label",
    "ternary_expression",
}


@dataclass
class Annotation:
    name: str
    # 키 → 값 목록. 괄호 안에 값만 있으면 키는 "value"
    args: dict[str, list[str]] = field(default_factory=dict)

    def first(self, key: str = "value") -> str | None:
        values = self.args.get(key)
        return values[0] if values else None


@dataclass
class JavaField:
    name: str
    type_name: str
    annotations: list[Annotation]
    line: int

    @property
    def bean_name(self) -> str | None:
        """주입 대상 빈 이름. @Resource(name=...) 또는 @Qualifier("...")."""
        for ann in self.annotations:
            if ann.name == "Resource":
                return ann.first("name")
            if ann.name == "Qualifier":
                return ann.first()
        return None


@dataclass
class Call:
    receiver: str | None  # 호출 대상 객체 표현식. 같은 클래스 메서드면 None
    method: str
    string_args: list[str]
    line: int
    # "LoginUsr.updateLoginIncorrect" + userSe 처럼 문자열로 시작하는 더하기 인자의 앞부분.
    # 실행 때 결정되는 동적 쿼리 ID를 찾는 데 쓴다.
    string_prefixes: list[str] = field(default_factory=list)


@dataclass
class EmptyCatch:
    exception_type: str
    line: int


@dataclass
class JavaMethod:
    name: str
    line_start: int
    line_end: int
    annotations: list[Annotation]
    parameters: list[str]  # 파라미터 타입
    doc: str | None
    calls: list[Call] = field(default_factory=list)
    returned_strings: list[str] = field(default_factory=list)
    complexity: int = 1
    empty_catches: list[EmptyCatch] = field(default_factory=list)
    string_literals: list[tuple[str, int]] = field(default_factory=list)
    # 식별자·리터럴을 지운 본문 토큰의 해시. 변수 이름만 다른 복사-붙여넣기 코드를 찾는 데 쓴다.
    body_hash: str | None = None
    body_tokens: int = 0

    @property
    def mapping_paths(self) -> list[str]:
        return _mapping_paths(self.annotations)


@dataclass
class JavaClass:
    name: str  # 중첩 클래스는 Outer.Inner
    package: str
    kind: str  # class, interface, enum
    line_start: int
    line_end: int
    annotations: list[Annotation]
    superclass: str | None
    interfaces: list[str]
    fields: list[JavaField]
    methods: list[JavaMethod]
    doc: str | None

    @property
    def fqcn(self) -> str:
        return f"{self.package}.{self.name}" if self.package else self.name

    def annotation(self, name: str) -> Annotation | None:
        return next((a for a in self.annotations if a.name == name), None)

    @property
    def stereotype(self) -> str | None:
        for name in ("Controller", "RestController", "Service", "Repository", "Component"):
            if self.annotation(name):
                return name
        return None

    @property
    def bean_name(self) -> str | None:
        """@Service("이름") 같은 명시적 빈 이름. 없으면 Spring 기본 규칙(첫 글자 소문자)."""
        stereotype = self.stereotype
        if stereotype is None:
            return None
        explicit = self.annotation(stereotype).first()
        if explicit:
            return explicit
        simple = self.name.split(".")[-1]
        return simple[:1].lower() + simple[1:]

    @property
    def mapping_paths(self) -> list[str]:
        return _mapping_paths(self.annotations)


@dataclass
class JavaFile:
    package: str
    imports: list[str]
    header_comment: str | None  # package 선언 앞의 주석 (수정 이력이 있는 경우가 많음)
    classes: list[JavaClass]


def parse_java(source: bytes) -> JavaFile:
    tree = _PARSER.parse(source)
    root = tree.root_node
    package = ""
    imports: list[str] = []
    header_parts: list[str] = []
    classes: list[JavaClass] = []
    seen_code = False
    for child in root.named_children:
        if child.type in ("block_comment", "line_comment"):
            if not seen_code:
                header_parts.append(_text(child, source))
            continue
        seen_code = True
        if child.type == "package_declaration":
            name_node = next((c for c in child.named_children if c.type != "annotation"), None)
            package = _text(name_node, source) if name_node else ""
        elif child.type == "import_declaration":
            imports.append(_text(child, source).removeprefix("import").strip(" ;"))
    for child in root.named_children:
        if child.type in ("class_declaration", "interface_declaration", "enum_declaration"):
            classes.extend(_parse_class(child, source, package, prefix=""))
    return JavaFile(
        package=package,
        imports=imports,
        header_comment="\n".join(header_parts) or None,
        classes=classes,
    )


def _parse_class(node: Node, source: bytes, package: str, prefix: str) -> list[JavaClass]:
    name = prefix + _text(node.child_by_field_name("name"), source)
    kind = node.type.removesuffix("_declaration")
    superclass = None
    interfaces: list[str] = []
    sup = node.child_by_field_name("superclass")
    if sup is not None:
        superclass = _type_name(sup.named_children[-1], source)
    for child in node.children:
        # 클래스는 super_interfaces, 인터페이스는 extends_interfaces
        if child.type in ("super_interfaces", "extends_interfaces"):
            type_list = next((c for c in child.named_children if c.type == "type_list"), None)
            if type_list is not None:
                interfaces = [_type_name(t, source) for t in type_list.named_children]

    fields: list[JavaField] = []
    methods: list[JavaMethod] = []
    inner: list[JavaClass] = []
    body = node.child_by_field_name("body")
    if body is not None:
        for member in body.named_children:
            if member.type == "field_declaration":
                fields.extend(_parse_field(member, source))
            elif member.type in ("method_declaration", "constructor_declaration"):
                methods.append(_parse_method(member, source))
            elif member.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                inner.extend(_parse_class(member, source, package, prefix=name + "."))
            elif member.type == "enum_body_declarations":
                for sub in member.named_children:
                    if sub.type in ("method_declaration", "constructor_declaration"):
                        methods.append(_parse_method(sub, source))
                    elif sub.type == "field_declaration":
                        fields.extend(_parse_field(sub, source))

    cls = JavaClass(
        name=name,
        package=package,
        kind=kind,
        line_start=node.start_point.row + 1,
        line_end=node.end_point.row + 1,
        annotations=_annotations(node, source),
        superclass=superclass,
        interfaces=interfaces,
        fields=fields,
        methods=methods,
        doc=_preceding_doc(node, source),
    )
    return [cls, *inner]


def _parse_field(node: Node, source: bytes) -> list[JavaField]:
    type_node = node.child_by_field_name("type")
    type_name = _type_name(type_node, source) if type_node is not None else ""
    annotations = _annotations(node, source)
    result = []
    for child in node.named_children:
        if child.type == "variable_declarator":
            result.append(
                JavaField(
                    name=_text(child.child_by_field_name("name"), source),
                    type_name=type_name,
                    annotations=annotations,
                    line=node.start_point.row + 1,
                )
            )
    return result


def _parse_method(node: Node, source: bytes) -> JavaMethod:
    params: list[str] = []
    params_node = node.child_by_field_name("parameters")
    if params_node is not None:
        for p in params_node.named_children:
            t = p.child_by_field_name("type")
            if t is not None:
                params.append(_type_name(t, source))
    method = JavaMethod(
        name=_text(node.child_by_field_name("name"), source),
        line_start=node.start_point.row + 1,
        line_end=node.end_point.row + 1,
        annotations=_annotations(node, source),
        parameters=params,
        doc=_preceding_doc(node, source),
    )
    body = node.child_by_field_name("body")
    if body is not None:
        _scan_body(body, source, method)
        tokens = _normalized_tokens(body, source)
        method.body_tokens = len(tokens)
        method.body_hash = hashlib.sha1(" ".join(tokens).encode()).hexdigest()
    return method


_IDENTIFIER_NODES = {"identifier", "type_identifier", "field_identifier"}
_LITERAL_NODES = {
    "string_literal", "decimal_integer_literal", "hex_integer_literal", "decimal_floating_point_literal",
    "character_literal", "true", "false", "null_literal",
}


def _normalized_tokens(node: Node, source: bytes) -> list[str]:
    """본문을 토큰으로 펼치되 식별자는 ID, 리터럴은 LIT로 바꾼다. 주석은 뺀다.

    변수·메서드 이름만 바꿔 복사한 코드(Type-2 클론)가 같은 토큰열이 된다.
    """
    tokens: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("line_comment", "block_comment"):
            continue
        if n.type in _LITERAL_NODES:
            tokens.append("LIT")
            continue
        if n.child_count == 0:
            tokens.append("ID" if n.type in _IDENTIFIER_NODES else n.type)
            continue
        stack.extend(reversed(n.children))
    return tokens


def _scan_body(body: Node, source: bytes, method: JavaMethod) -> None:
    stack = [body]
    while stack:
        n = stack.pop()
        t = n.type
        if t in _DECISION_NODES:
            method.complexity += 1
        elif t == "binary_expression":
            op = n.child_by_field_name("operator")
            if op is not None and op.type in ("&&", "||"):
                method.complexity += 1
        if t == "method_invocation":
            obj = n.child_by_field_name("object")
            args = n.child_by_field_name("arguments")
            arg_nodes = args.named_children if args is not None else []
            method.calls.append(
                Call(
                    receiver=_text(obj, source) if obj is not None else None,
                    method=_text(n.child_by_field_name("name"), source),
                    string_args=[_string_value(a, source) for a in arg_nodes if a.type == "string_literal"],
                    line=n.start_point.row + 1,
                    string_prefixes=[
                        prefix
                        for a in arg_nodes
                        if a.type == "binary_expression" and (prefix := _leading_string(a, source))
                    ],
                )
            )
        elif t == "return_statement":
            value = next(iter(n.named_children), None)
            if value is not None and value.type == "string_literal":
                method.returned_strings.append(_string_value(value, source))
        elif t == "catch_clause":
            block = n.child_by_field_name("body")
            if block is not None and not any(
                c.type not in ("line_comment", "block_comment") for c in block.named_children
            ):
                param = next((c for c in n.named_children if c.type == "catch_formal_parameter"), None)
                ctype = next((c for c in param.named_children if c.type == "catch_type"), None) if param else None
                method.empty_catches.append(
                    EmptyCatch(
                        exception_type=_text(ctype, source) if ctype is not None else "",
                        line=n.start_point.row + 1,
                    )
                )
        elif t == "string_literal":
            method.string_literals.append((_string_value(n, source), n.start_point.row + 1))
            continue
        stack.extend(reversed(n.children))


def _annotations(node: Node, source: bytes) -> list[Annotation]:
    modifiers = next((c for c in node.children if c.type == "modifiers"), None)
    if modifiers is None:
        return []
    result = []
    for m in modifiers.named_children:
        if m.type == "marker_annotation":
            result.append(Annotation(name=_simple_name(m.child_by_field_name("name"), source)))
        elif m.type == "annotation":
            ann = Annotation(name=_simple_name(m.child_by_field_name("name"), source))
            arg_list = m.child_by_field_name("arguments")
            for arg in arg_list.named_children if arg_list is not None else []:
                if arg.type == "element_value_pair":
                    key = _text(arg.child_by_field_name("key"), source)
                    ann.args[key] = _element_values(arg.child_by_field_name("value"), source)
                elif arg.type not in ("line_comment", "block_comment"):
                    ann.args["value"] = _element_values(arg, source)
            result.append(ann)
    return result


def _element_values(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    if node.type == "string_literal":
        return [_string_value(node, source)]
    if node.type == "element_value_array_initializer":
        values: list[str] = []
        for c in node.named_children:
            values.extend(_element_values(c, source))
        return values
    return [_text(node, source)]


def _mapping_paths(annotations: list[Annotation]) -> list[str]:
    paths: list[str] = []
    for ann in annotations:
        if ann.name in MAPPING_ANNOTATIONS:
            paths.extend(ann.args.get("value", []) + ann.args.get("path", []))
    return paths


def _preceding_doc(node: Node, source: bytes) -> str | None:
    prev = node.prev_named_sibling
    if prev is not None and prev.type == "block_comment" and _text(prev, source).startswith("/**"):
        return _text(prev, source)
    return None


def _type_name(node: Node, source: bytes) -> str:
    """List<BoardVO> → List, egovframework.X → X."""
    if node.type == "generic_type":
        base = next((c for c in node.named_children if c.type != "type_arguments"), node)
        return _type_name(base, source)
    return _text(node, source).split(".")[-1]


def _simple_name(node: Node | None, source: bytes) -> str:
    return _text(node, source).split(".")[-1] if node is not None else ""


def _leading_string(node: Node, source: bytes) -> str | None:
    """"A" + x + "B" 에서 맨 앞 문자열 "A"를 돌려준다. 문자열로 시작하지 않으면 None."""
    if node.type == "string_literal":
        return _string_value(node, source)
    if node.type == "binary_expression":
        op = node.child_by_field_name("operator")
        if op is not None and op.type == "+":
            return _leading_string(node.child_by_field_name("left"), source)
    return None


def _string_value(node: Node, source: bytes) -> str:
    return "".join(
        _text(c, source) for c in node.named_children if c.type in ("string_fragment", "escape_sequence")
    )


def _text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
