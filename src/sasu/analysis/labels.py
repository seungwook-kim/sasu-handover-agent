"""한글 이름표: 클래스 업무명, 메서드 한글 설명, 모듈 업무명.

코드는 영어 약어(EgovVcatnManageController, uss.ion)라서 "휴가"·"당직" 같은 업무어로는
모듈·URL·질문을 찾을 수 없다. 개발자가 남긴 주석에서 한글 이름을 뽑아 붙인다.

- 클래스 업무명: Javadoc·파일 머리말의 `@Description :` 줄, 없으면 설명 첫 문단에서
  "~를 위한 컨트롤러 클래스" 같은 꼬리를 떼고 업무 이름만 남긴다 (20자 이내).
- 메서드 한글 설명: Javadoc 첫 한글 줄 (40자 이내).
- 모듈 업무명: 모듈 안 Controller 업무명 중 가장 흔한 것.

지어내지 않는다. 주석에 한글 설명이 없으면 None이다.
"""

import html
import re
import sqlite3
from collections import Counter

CLASS_MAX = 20
METHOD_MAX = 40

_KOREAN = re.compile(r"[가-힣]")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_DESCRIPTION = re.compile(r"^@?\s*Description\s*:\s*(.+)$", re.IGNORECASE)
# 설명 문단이 끝나는 곳: Javadoc 태그, 수정 이력 표
_STOP = re.compile(r"^@|개정이력|Modification Information|^수정일|^상세내용|^-{3,}|^={3,}")
# 문단 머리에 붙는 소제목 (eGovFrame: "개요" / "- 휴가관리에 대한 …" / "상세내용")
_HEADING = re.compile(r"^(?:개요|요약)\s*(?:[:\-]\s*|\s+)")
_DATE = re.compile(r"^\d{4}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2}")
_FILE_NAME = re.compile(r"^[\w$]+\.(?:java|py)$|^Class Name\s*:", re.IGNORECASE)

# "권한그룹에 관한 controller", "업무사용자관련 요청을 …"
_ABOUT = re.compile(r"^(.{1,30}?)\s*(?:에\s*)?(?:관한|대한|관해|관련)(?=\s|$)")
# "~를 위한 컨트롤러 클래스", "~을 처리하는 Controller Class 구현", "~ 서비스 컨트롤러"
_TAIL = re.compile(
    r"\s*(?:(?:을|를|에)?\s*(?:(?:처리|담당)?\s*하기\s*위한|위한|(?:처리|담당)?\s*하는)\s*|용\s*)?"
    r"(?:(?:웹|화면|서비스|비즈니스|구현|데이터\s*접근|DAO|Service|VO)\s*)*"
    r"(?:controller|컨트롤러|class|클래스|인터페이스|interface|구현체|dao\b|vo\b)",
    re.IGNORECASE,
)


def _doc_lines(doc: str) -> list[str]:
    """주석 표시(/** * */ \"\"\")와 HTML 태그를 뗀 줄들."""
    text = html.unescape(doc)
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^(?:/\*+|\"\"\"|''')", "", line)
        line = re.sub(r"(?:\*+/|\"\"\"|''')$", "", line).strip()
        line = re.sub(r"^\*+", "", line).strip()
        line = re.sub(r"^//+", "", line).strip()
        line = _TAG.sub(" ", line).strip()
        lines.append(" ".join(line.split()))
    return lines


def _first_paragraph(lines: list[str]) -> str | None:
    """수정 이력·태그 앞의 설명 중 한글이 있는 첫 문단."""
    paragraph: list[str] = []
    for line in lines + [""]:
        if _STOP.search(line):
            break
        line = _HEADING.sub("", line).lstrip("-·• ").strip() if line not in ("개요", "상세내용") else ""
        if line and _FILE_NAME.match(line):
            continue
        if line:
            paragraph.append(line)
            continue
        if paragraph and _KOREAN.search(" ".join(paragraph)):
            return " ".join(paragraph)
        paragraph = []
    if paragraph and _KOREAN.search(" ".join(paragraph)):
        return " ".join(paragraph)
    return None


def business_name(text: str) -> str | None:
    """설명 한 문장에서 업무 이름만 남긴다. 20자를 넘으면 None (억지로 자르지 않는다)."""
    text = " ".join(text.split()).strip(" .:-")
    if m := _ABOUT.match(text):
        name = m.group(1)
    elif m := _TAIL.search(text):
        name = text[:m.start()]
    else:
        name = text
    name = re.sub(r"(?<=[가-힣A-Za-z0-9)])(?:을|를)\s+", " ", name)  # "주소록정보를 관리" → "주소록정보 관리"
    name = re.sub(r"(?:을|를|의|에|에서)$", "", name.strip())
    name = re.sub(r"하기$", "", name)
    name = " ".join(name.split()).strip(" .,:-")
    if len(name) > CLASS_MAX and ", " in name:
        name = name.split(", ")[0]  # "커뮤니티 사용자관리, 커뮤니티 게시판 관리" → 앞부분
    if not name or not _KOREAN.search(name) or len(name) > CLASS_MAX:
        return None
    return name


def class_label(doc: str | None, header: str | None = None) -> str | None:
    """클래스 Javadoc, 없으면 파일 머리말에서 업무명을 찾는다."""
    for source in (doc, header):
        if not source:
            continue
        lines = _doc_lines(source)
        for line in lines:
            if m := _DESCRIPTION.match(line):
                if name := business_name(m.group(1)):
                    return name
        paragraph = _first_paragraph(lines)
        if paragraph and (name := business_name(paragraph)):
            return name
    return None


def method_label(doc: str | None) -> str | None:
    """Javadoc(docstring) 첫 한글 줄. 태그(@param 등) 앞까지만 본다."""
    if not doc:
        return None
    for line in _doc_lines(doc):
        if not line:
            continue
        if line.startswith("@"):
            break
        if _DATE.match(line) or not _KOREAN.search(line):
            continue  # "2026.07.13 KISA 보안취약점 조치 - …" 같은 수정 메모
        line = line.strip(" .")
        return line if len(line) <= METHOD_MAX else line[:METHOD_MAX - 1] + "…"
    return None


def join_labels(*parts: str | None) -> str | None:
    """"휴가관리 · 휴가를 등록한다". 둘 다 없으면 None."""
    kept = [p for p in parts if p]
    return " · ".join(dict.fromkeys(kept)) or None


class Labels:
    """한 분석 파일의 이름표. 요청 하나에서 여러 번 찾으므로 한 번 읽어 둔다."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._classes: dict[int, str | None] | None = None
        self._class_of: dict[int, int] = {}
        self._method_docs: dict[int, str] | None = None
        self._methods: dict[int, str | None] = {}
        self._modules: dict[str, str | None] | None = None
        self._module_all: dict[str, list[str]] = {}
        self._module_of: dict[int, str] | None = None

    # 클래스 ───────────────────────────────────────────────

    def classes(self) -> dict[int, str | None]:
        if self._classes is None:
            self._classes = {
                r["id"]: class_label(r["doc"], r["header_comment"])
                for r in self.conn.execute(
                    "SELECT c.id, c.doc, f.header_comment FROM classes c JOIN files f ON f.id = c.file_id")
            }
        return self._classes

    def class_label(self, class_id: int | None) -> str | None:
        return self.classes().get(class_id) if class_id is not None else None

    # 메서드 ───────────────────────────────────────────────

    def method_label(self, method_id: int | None) -> str | None:
        if method_id is None:
            return None
        if self._method_docs is None:
            self._method_docs = {
                r["id"]: r["doc"] for r in self.conn.execute(
                    "SELECT id, doc FROM methods WHERE doc GLOB '*[가-힣]*'")
            }
        if method_id not in self._methods:
            self._methods[method_id] = method_label(self._method_docs.get(method_id))
        return self._methods[method_id]

    def class_of(self, method_id: int) -> int | None:
        if method_id not in self._class_of:
            row = self.conn.execute("SELECT class_id FROM methods WHERE id = ?", (method_id,)).fetchone()
            self._class_of[method_id] = row["class_id"] if row else None
        return self._class_of[method_id]

    def full(self, method_id: int | None) -> str | None:
        """클래스 업무명 · 메서드 설명."""
        if method_id is None:
            return None
        return join_labels(self.class_label(self.class_of(method_id)), self.method_label(method_id))

    # 모듈 ─────────────────────────────────────────────────

    def module_of_class(self, class_id: int | None) -> str | None:
        if self._module_of is None:
            from sasu.analysis.overview import class_modules

            self._module_of = class_modules(self.conn)
        return self._module_of.get(class_id) if class_id is not None else None

    def modules(self) -> dict[str, str | None]:
        """모듈 → 업무명. Controller(또는 URL을 받는 클래스) 업무명 중 가장 흔한 것.
        가장 흔한 것도 절반이 안 되면 (업무가 여럿 모인 모듈) URL이 가장 많은 Controller의
        업무명에 "외 n"을 붙인다. 한 업무명으로 모듈 전체를 부르면 오해하기 때문이다."""
        if self._modules is not None:
            return self._modules
        urls = Counter({r["class_id"]: r["n"] for r in self.conn.execute(
            "SELECT m.class_id, COUNT(DISTINCT u.url) AS n FROM url_mappings u JOIN methods m ON m.id = u.method_id "
            "GROUP BY m.class_id")})
        per_module: dict[str, list[tuple[int, str]]] = {}
        for r in self.conn.execute(
            "SELECT id, name, stereotype FROM classes ORDER BY name"
        ):
            if r["stereotype"] not in ("Controller", "RestController") and not urls.get(r["id"]):
                continue
            label = self.class_label(r["id"])
            if label:
                per_module.setdefault(self.module_of_class(r["id"]) or "", []).append((urls.get(r["id"], 0), label))
        self._modules = {}
        for module, items in per_module.items():
            counts = Counter(label for _, label in items)
            self._module_all[module] = list(counts)
            top, n = counts.most_common(1)[0]
            if len(counts) > 1 and (n == 1 or n * 2 < len(items)):  # 절반도 안 되면 모듈 이름으로 삼지 않는다
                busiest = max(items, key=lambda x: x[0])[1]  # 같은 URL 수면 이름순 첫째 (max는 앞의 것을 고른다)
                self._modules[module] = f"{busiest} 외 {len(counts) - 1}"
            else:
                self._modules[module] = top
        return self._modules

    def module_label(self, module: str | None) -> str | None:
        return self.modules().get(module) if module else None

    def module_labels(self, module: str) -> list[str]:
        """모듈 안 Controller 업무명 전부 (화면에서 업무어로 모듈을 찾을 때)."""
        self.modules()
        return self._module_all.get(module, [])

    # URL ──────────────────────────────────────────────────

    def url_handlers(self) -> dict[str, int]:
        """URL → 처리 메서드 (여러 개면 id가 가장 작은 것)."""
        return {r["url"]: r["mid"] for r in self.conn.execute(
            "SELECT url, MIN(method_id) AS mid FROM url_mappings GROUP BY url")}

    def url_label(self, url: str, handlers: dict[str, int] | None = None) -> str | None:
        mid = (handlers or self.url_handlers()).get(url)
        return self.method_label(mid)

    def search_urls(self, query: str) -> list[str]:
        """한글 낱말이 모두 처리 메서드 설명·클래스 업무명에 들어 있는 URL (띄어쓰기 무시)."""
        words = [w for w in re.findall(r"[가-힣A-Za-z0-9]+", query) if _KOREAN.search(w)]
        if not words:
            return []
        scored = []
        for url, mid in self.url_handlers().items():
            method = (self.method_label(mid) or "").replace(" ", "")
            klass = (self.class_label(self.class_of(mid)) or "").replace(" ", "")
            stems = [_stem(w) for w in words]
            if not all(s in method + klass for s in stems):
                continue
            # 처리 메서드 설명에 든 낱말이 많을수록, 업무명이 정확히 그 말일수록 앞에 둔다
            # ("로그인" → 로그인 처리 URL이 로그인이 우연히 들어간 팝업 URL보다 먼저)
            score = 2 * sum(s in method for s in stems) + sum(s in klass for s in stems)
            score += 3 * any(klass == s for s in stems)
            scored.append((-score, url))
        return [url for _, url in sorted(scored)]


def _stem(word: str) -> str:
    """"휴가를" → "휴가": 끝의 조사 한 글자를 뗀다 (두 글자 이상 남을 때만)."""
    if len(word) > 2 and word[-1] in "을를이가은는의에도":
        return word[:-1]
    return word
