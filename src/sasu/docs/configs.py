"""설정 파일의 키=값을 읽는다: 문서–코드 연결(설정 키·설정 파일 이름)과 불일치 LLM 판정의 근거에 쓴다.

- *.properties (유니코드 이스케이프 주석을 풀고, 바로 위 주석을 설명으로), *.yml·*.yaml (들여쓰기 경로 a.b.c),
  XML의 <property name value>(bean id를 앞에 붙임)와 <x>값</x> (web.xml의 session-timeout 등).
- 테스트·빌드·숨김 폴더, 다국어 메시지 파일, MyBatis 매퍼·pom.xml은 뺀다. 비밀번호 같은 값은 빼고 쓰지 않는다.
- 스프링 스케줄 설정의 targetObject + targetMethod → 실행되는 메서드도 꺼낸다 (scheduled_targets).
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ConfigFact:
    path: str  # 저장소 기준 경로
    line: int
    key: str
    value: str
    comment: str

    def text(self) -> str:
        words = " ".join(re.findall(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+", self.key))
        return f"{Path(self.path).name} {self.key} {words} = {self.value} {self.comment}".strip()

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


_SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", ".venv", "venv", "test", "tests", "__pycache__"}
_I18N = re.compile(r"(?:^message|_[a-z]{2}(?:_[A-Z]{2})?\.properties$)", re.IGNORECASE)
_UNICODE = re.compile(r"\\u([0-9a-fA-F]{4})")
_XML_PROPERTY = re.compile(r"<property\s+name=\"([\w.\-]+)\"\s+value=\"([^\"]{1,200})\"")
_XML_TEXT = re.compile(r"<([\w\-]+)>\s*([^<\s][^<]{0,80}?)\s*</\1>")
_XML_COMMENT = re.compile(r"<!--(.*?)-->")
_BEAN_ID = re.compile(r"<bean\s+[^>]*id=\"([\w.\-]+)\"")
_XML_SKIP = ("<mapper", "<sqlMap", "<project ", "<Configuration")
_SUFFIXES = (".properties", ".yml", ".yaml", ".xml")


def config_texts(repo: Path | None, suffixes: tuple[str, ...] = _SUFFIXES):
    """(저장소 기준 경로, 글) — 설정 파일만."""
    if repo is None:
        return
    for f in sorted(Path(repo).rglob("*")):
        if not f.is_file() or f.suffix.lower() not in suffixes:
            continue
        rel = f.relative_to(repo)
        if (_SKIP_DIRS & {p.lower() for p in rel.parts[:-1]} or any(p.startswith(".") for p in rel.parts[:-1])
                or f.stat().st_size > 500_000):
            continue
        if f.suffix.lower() == ".properties" and _I18N.search(f.name):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f.suffix.lower() == ".xml" and (any(s in text[:3000] for s in _XML_SKIP) or f.name == "pom.xml"):
            continue
        yield rel.as_posix(), text


def config_facts(repo: Path | None, limit: int = 5000) -> list[ConfigFact]:
    """설정 파일의 키=값. 값이 비밀(비밀번호 등)이면 뺀다."""
    from sasu.docs.redact import redact

    facts: list[ConfigFact] = []
    for rel, text in config_texts(repo):
        if len(facts) >= limit:
            break
        suffix = Path(rel).suffix.lower()
        parse = _properties if suffix == ".properties" else _yaml if suffix in (".yml", ".yaml") else _xml
        for line, key, value, comment in parse(text):
            if "[가림:" in redact(f"{key}={value}")[0]:  # 비밀번호 같은 값은 쓰지 않는다
                continue
            facts.append(ConfigFact(rel, line, key, value, comment[:120]))
    return facts[:limit]


def _properties(text: str):
    comment = ""
    for i, raw in enumerate(text.splitlines(), 1):
        line = _UNICODE.sub(lambda m: chr(int(m.group(1), 16)), raw).strip()
        if not line:
            comment = ""
            continue
        if line.startswith(("#", "!")):
            comment = line.lstrip("#! ").strip()
            continue
        m = re.match(r"([^=:\s]+)\s*[=:\s]?\s*(.*)$", line)
        if m:  # 빈 값도 키로 둔다 (SHELL.UNIX.batchShellFiles = → "목록이 비어 있음"도 사실)
            yield i, m.group(1), m.group(2).strip(), comment
        comment = ""


def _yaml(text: str):
    stack: list[tuple[int, str]] = []
    for i, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([\w.\-]+)\s*:\s*(.*?)\s*(?:#\s*(.*))?$", raw)
        if not m:
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = ".".join([k for _, k in stack] + [m.group(2)])
        if m.group(3):
            yield i, key, m.group(3).strip("'\""), m.group(4) or ""
        else:
            stack.append((indent, m.group(2)))


def _xml(text: str):
    comment, bean = "", ""
    for i, line in enumerate(text.splitlines(), 1):
        if c := _XML_COMMENT.search(line):
            comment = c.group(1).strip()
        if b := _BEAN_ID.search(line):
            bean = b.group(1)
        for name, value in _XML_PROPERTY.findall(line):
            yield i, f"{bean}.{name}" if bean else name, value, comment
        for tag, value in _XML_TEXT.findall(line):
            if re.search(r"\d", value) or len(value) <= 40:
                yield i, tag, value.strip(), comment


_TARGET_OBJECT = re.compile(r"<property\s+name=\"targetObject\"\s+ref=\"([\w.\-]+)\"")
_TARGET_METHOD = re.compile(r"<property\s+name=\"targetMethod\"\s+value=\"(\w+)\"")


def scheduled_targets(repo: Path | None) -> dict[str, list[tuple[str, str]]]:
    """설정 파일 이름 → [(빈 이름, 메서드 이름)] (스프링 MethodInvokingJobDetailFactoryBean의 targetObject·targetMethod)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for rel, text in config_texts(repo, (".xml",)):
        for bean in re.split(r"<bean\s", text)[1:]:
            obj, method = _TARGET_OBJECT.search(bean), _TARGET_METHOD.search(bean)
            if obj and method:
                out.setdefault(Path(rel).name, []).append((obj.group(1), method.group(1)))
    return out
