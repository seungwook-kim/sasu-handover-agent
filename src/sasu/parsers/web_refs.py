"""JSP·JS·설정 파일에서 다른 코드를 가리키는 참조를 뽑는다.

불필요한 소스를 찾을 때 "아무도 부르지 않는가"를 판단하는 재료다.
- url: 화면이나 스크립트가 부르는 URL (/cop/bbs/selectArticleList.do)
- jsp: 경로로 직접 가리키는 JSP (/WEB-INF/jsp/.../X.jsp)
- include: JSP가 포함하는 다른 JSP (<%@ include %>, <jsp:include>, <c:import>)
- method: 설정 XML이 이름으로 부르는 메서드 (<task:scheduled method="x">, init-method 등)
- path: 설정 XML에 확장자 없이 적힌 뷰 경로 (에러 페이지 매핑 등)
"""

import bisect
import re
from dataclasses import dataclass

# ${pageContext.request.contextPath}/a/b.do, '/a/b.do', "<c:url value='/a/b.do'/>" 모두 /a/b.do로 잡힌다
_URL = re.compile(r"(/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-{}]+)*\.do)\b")
# .do가 없는 REST 스타일 경로: url: '/twitter/tweets', fetch("/api/x") 처럼 따옴표 안의 절대 경로
_QUOTED_PATH = re.compile(r"""["'`](/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*)/?["'`?]""")
# 설정 XML의 SpEL: #{egovPasswordResolver.resolvePassword('...')}
_SPEL_CALL = re.compile(r"#\{\s*[A-Za-z_][\w.]*\.([A-Za-z_]\w*)\s*\(")
_JSP_PATH = re.compile(r"([A-Za-z0-9_\-/.]*?/?[A-Za-z0-9_\-]+\.jsp)\b")
_INCLUDE = re.compile(
    r"""<%@\s*include\s+file\s*=\s*["']([^"']+)["']"""
    r"""|<jsp:include\s+page\s*=\s*["']([^"']+)["']"""
    r"""|<c:import\s+url\s*=\s*["']([^"']+\.jsp)["']""",
    re.IGNORECASE,
)
_METHOD_ATTR = re.compile(
    r"""\b(?:method|init-method|destroy-method|targetMethod)\s*=\s*["']([A-Za-z_]\w*)["']"""
    r"""|<property\s+name\s*=\s*["']targetMethod["']\s+value\s*=\s*["']([A-Za-z_]\w*)["']""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Ref:
    kind: str  # url, jsp, include, method
    value: str
    line: int


# 설정 XML에 확장자 없이 적힌 뷰 경로: <prop key="...">egovframework/com/cmm/error/egovBizException</prop>
_VIEW_PATH = re.compile(r"""(?:>\s*|value\s*=\s*["'])([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+){2,})\s*(?:<|["'])""")


def extract_refs(text: str, *, methods: bool = False) -> list[Ref]:
    refs: list[Ref] = []
    line_starts = _line_starts(text)
    if methods:  # 설정 파일일 때만
        for m in _VIEW_PATH.finditer(text):
            refs.append(Ref("path", m.group(1), _line_of(line_starts, m.start())))
    for m in _URL.finditer(text):
        refs.append(Ref("url", m.group(1), _line_of(line_starts, m.start())))
    for m in _QUOTED_PATH.finditer(text):
        if not m.group(1).endswith(".do"):
            refs.append(Ref("url", m.group(1), _line_of(line_starts, m.start())))
    for m in _INCLUDE.finditer(text):
        target = next(g for g in m.groups() if g)
        refs.append(Ref("include", target, _line_of(line_starts, m.start())))
    for m in _JSP_PATH.finditer(text):
        refs.append(Ref("jsp", m.group(1), _line_of(line_starts, m.start())))
    if methods:
        for m in _METHOD_ATTR.finditer(text):
            name = m.group(1) or m.group(2)
            if name.lower() not in ("get", "post", "put", "delete", "patch"):  # HTTP method 속성 제외
                refs.append(Ref("method", name, _line_of(line_starts, m.start())))
        for m in _SPEL_CALL.finditer(text):
            refs.append(Ref("method", m.group(1), _line_of(line_starts, m.start())))
    return refs


def _line_starts(text: str) -> list[int]:
    return [0] + [m.end() for m in re.finditer("\n", text)]


def _line_of(starts: list[int], pos: int) -> int:
    return bisect.bisect_right(starts, pos)
