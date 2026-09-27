"""MyBatis Mapper XML에서 네임스페이스, 쿼리 ID, 사용 테이블을 뽑는다.

XML 파서 대신 정규식을 쓴다. 줄 번호가 필요하고, 동적 SQL 태그(<if>, <choose>)가
섞인 본문에서 테이블 이름만 뽑으면 되기 때문이다.
"""

import re
from dataclasses import dataclass, field

_NAMESPACE = re.compile(r'<mapper\s+namespace\s*=\s*"([^"]+)"')
_STATEMENT = re.compile(
    r'<(select|insert|update|delete)\b([^>]*)>(.*?)</\1\s*>',
    re.DOTALL | re.IGNORECASE,
)
_ID_ATTR = re.compile(r'\bid\s*=\s*"([^"]+)"')
_SQL_FRAGMENT = re.compile(r'<sql\b[^>]*\bid\s*=\s*"([^"]+)"[^>]*>(.*?)</sql\s*>', re.DOTALL | re.IGNORECASE)
_INCLUDE = re.compile(r'<include\b[^>]*?\brefid\s*=\s*"([^"]+)"[^>]*>', re.IGNORECASE)
_XML_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|MERGE\s+INTO)\s+([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)",
    re.IGNORECASE,
)
# FROM A a, B b 처럼 쉼표로 이어진 테이블
_FROM_LIST = re.compile(
    r"\bFROM\s+(.+?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bHAVING\b|\bJOIN\b|\bUNION\b|\)|$)",
    re.IGNORECASE | re.DOTALL,
)
_NOT_TABLES = {"DUAL", "SELECT", "SET", "VALUES", "TABLE", "WHERE", "LATERAL"}


@dataclass
class SqlStatement:
    kind: str  # select, insert, update, delete
    id: str
    line: int
    sql: str  # 태그를 걷어낸 본문
    tables: list[str] = field(default_factory=list)


@dataclass
class MapperFile:
    namespace: str
    header_comment: str | None
    statements: list[SqlStatement]


def parse_mapper(text: str) -> MapperFile | None:
    ns = _NAMESPACE.search(text)
    if ns is None:
        return None
    header = _XML_COMMENT.search(text[: ns.start()])
    fragments = {m.group(1): m.group(2) for m in _SQL_FRAGMENT.finditer(text)}
    statements = []
    for m in _STATEMENT.finditer(text):
        id_match = _ID_ATTR.search(m.group(2))
        if id_match is None:
            continue
        body = _expand_includes(m.group(3), fragments)
        sql = _clean_sql(body)
        statements.append(
            SqlStatement(
                kind=m.group(1).lower(),
                id=id_match.group(1),
                line=text.count("\n", 0, m.start()) + 1,
                sql=sql,
                tables=extract_tables(sql),
            )
        )
    return MapperFile(
        namespace=ns.group(1),
        header_comment=header.group(1).strip() if header else None,
        statements=statements,
    )


def extract_tables(sql: str) -> list[str]:
    found: list[str] = []
    for m in _TABLE_REF.finditer(sql):
        found.append(m.group(1))
    for m in _FROM_LIST.finditer(sql):
        # FROM ( SELECT a.X, a.Y ... ) 처럼 서브쿼리면 쉼표 뒤가 테이블이 아니라 컬럼이다
        if m.group(1).lstrip().startswith("("):
            continue
        for part in m.group(1).split(",")[1:]:
            token = part.strip().split()
            if token and re.fullmatch(r"[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?", token[0]):
                found.append(token[0])
    result: list[str] = []
    for name in found:
        upper = name.upper()
        if upper in _NOT_TABLES or upper in result:
            continue
        result.append(upper)
    return result


def _expand_includes(body: str, fragments: dict[str, str], depth: int = 0) -> str:
    if depth > 3:
        return body

    def replace(m: re.Match) -> str:
        refid = m.group(1).split(".")[-1]
        return _expand_includes(fragments.get(refid, ""), fragments, depth + 1)

    return _INCLUDE.sub(replace, body)


def _clean_sql(body: str) -> str:
    body = _XML_COMMENT.sub(" ", body)
    body = body.replace("<![CDATA[", " ").replace("]]>", " ")
    body = _TAG.sub(" ", body)
    body = body.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return re.sub(r"[ \t]+", " ", body).strip()
