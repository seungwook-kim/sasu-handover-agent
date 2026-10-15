"""LLM으로 질문 넓히기: 한국어 일상어 질문을 코드에 나올 말로 넓힌다.

"로그인 5번 틀리면 잠겨?"의 답은 processLoginIncorrect·lockCnt에 있는데, 질문 낱말(로그인·틀리·잠겨)만으로는
BM25가 거기까지 닿지 못한다. 검색 전에 LLM에게 영어 식별자 조각(login, incorrect, lock),
이 프로젝트식 약어(업무명 ↔ 클래스 이름에서 뽑은 힌트), 한국어 동의어(잠김 → 잠금·제한)를 받아
BM25 질의에 낮은 가중치로 더한다 (search.MethodIndex.bm25의 extra_terms).

- 설정: SASU_QUERY_EXPAND = auto(기본: LLM이 있으면 켬) | on | off.
  claude-code면 빠른 모델(SASU_EXPAND_MODEL, 기본 haiku), OpenAI 호환이면 SASU_EXPAND_MODEL 또는 SASU_LLM_MODEL.
- LLM이 없거나 실패하면 빈 목록 (검색은 지금 그대로). 결과는 질문 해시로 SASU_HOME/cache에 둔다.
- 대상을 직접 적은 질문(Class.method · URL · 파일:줄)에는 쓰지 않는다.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

from sasu.analysis.labels import Labels
from sasu.config import Settings, cache_dir, load_settings

MAX_TERMS = 12
_HINT_PAIRS = 120  # 업무명=클래스 조각 힌트 수
_HINT_FRAGMENTS = 50  # 자주 나오는 식별자 조각 수

SYSTEM_PROMPT = """너는 코드 검색어를 넓히는 도구다. 개발자가 한국어 일상어로 한 질문을 받아,
그 답이 들어 있는 소스 코드(메서드 이름·변수·주석·SQL)에 나올 법한 검색어를 고른다.

고를 것:
- 영어 식별자 조각 (예: login, incorrect, lock, count)
- 질문의 동작을 코드 동사로: 신청·등록·저장·만들기 → insert, regist / 조회·목록·가져오기 → select, list /
  수정·바꾸기·갱신 → update / 삭제·지우기 → delete
- 이 프로젝트식 약어: 아래 힌트의 "업무명=클래스 조각"과 "자주 나오는 조각"에서 질문과 맞는 것만
- 코드 주석에 나올 한국어 동의어 (예: 잠겨 → 잠금, 제한 / 글 번호 → 게시물 아이디)

규칙:
- 질문에 이미 있는 낱말은 빼고 4~12개.
- 특정 업무가 아닌 일반 프로그래밍 개념 질문이면 []만 쓴다.
- 출력은 JSON 문자열 배열 하나만. 설명·코드 블록 없이.

예: "주문 취소하면 재고 돌아와?" → ["order","cancel","stock","restore","Ordr","Stck","취소처리","재고수량"]"""

_TARGET = re.compile(
    r"[A-Za-z_]\w*\.[a-z_]\w*\s*\("  # Class.method(  /  obj.method(
    r"|\b[A-Z]\w+\.[a-z_]\w+\b"  # Class.method
    r"|(?:^|\s)/[\w\-./]+"  # /uss/ion/vct/insertVcatnManage.do
    r"|\b[\w\-/\\]+\.(?:java|py|xml|jsp):\d+"  # 파일:줄
)
_CLASS_SUFFIX = re.compile(r"(?:Controller|ServiceImpl|Service|Impl|DAO|Dao|Mapper|Repository|VO|Vo)$")
_GENERIC = {
    "select", "insert", "update", "delete", "list", "view", "info", "detail", "manage", "regist", "get", "set",
    "is", "egov", "impl", "service", "controller", "dao", "vo", "com", "search", "page", "do", "cnt", "tot",
    "to", "of", "and", "for", "by", "the", "with", "from", "map", "string", "object", "model", "result",
}


def is_targeted(question: str) -> bool:
    """대상을 직접 적은 질문인가 (Class.method · URL · 파일:줄). 이때는 넓히지 않는다."""
    return bool(_TARGET.search(question))


def mode() -> str:
    value = os.environ.get("SASU_QUERY_EXPAND", "auto").strip().lower()
    return value if value in ("auto", "on", "off") else "auto"


def make_expand_client(settings: Settings):
    """넓히기용 LLM. claude-code는 빠른 모델(기본 haiku)로, 나머지는 OpenAI 호환 주소로."""
    from sasu.rag.answer import ClaudeCodeClient, LLMClient

    model = os.environ.get("SASU_EXPAND_MODEL", "").strip()
    if settings.llm_provider == "claude-code":
        return ClaudeCodeClient(model or "haiku", timeout=60.0)
    client = LLMClient(settings, timeout=30.0)
    if model:
        client.model = model
    return client


def project_hints(conn: sqlite3.Connection) -> str:
    """프롬프트에 넣을 짧은 용어 힌트: 업무명=클래스 조각, 자주 나오는 식별자 조각. 코드에서 뽑는다."""
    names = Labels(conn)
    pairs: Counter = Counter()
    for r in conn.execute(
        "SELECT c.id, c.name, COUNT(m.id) AS n FROM classes c JOIN methods m ON m.class_id = c.id "
        "WHERE c.kind != 'interface' GROUP BY c.id ORDER BY n DESC"
    ):
        label = names.class_label(r["id"])
        core = _CLASS_SUFFIX.sub("", re.sub(r"^Egov", "", r["name"]))
        if label and core and len(core) >= 3:
            pairs[f"{label.replace(' ', '')}={core}"] += r["n"]
    fragments: Counter = Counter()
    for r in conn.execute("SELECT name FROM methods"):
        for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", r["name"]):
            if len(part) >= 3 and part.lower() not in _GENERIC:
                fragments[part] += 1
    pair_text = ", ".join(p for p, _ in pairs.most_common(_HINT_PAIRS))
    frag_text = ", ".join(f for f, _ in fragments.most_common(_HINT_FRAGMENTS))
    return f"업무명=클래스 조각: {pair_text}\n자주 나오는 조각: {frag_text}"


def parse_terms(raw: str, question: str = "") -> list[str]:
    """LLM 출력에서 JSON 배열을 꺼낸다. 형식이 틀리면 빈 목록."""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    terms: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        term = " ".join(item.split())
        if not term or len(term) > 40 or term in question or term in terms:
            continue
        terms.append(term)
    return terms[:MAX_TERMS]


class QueryExpander:
    def __init__(self, client, cache: Path | None = None, use_cache: bool = True):
        self.client = client
        self.cache = cache if cache is not None else cache_dir() / "query-expand"
        self.use_cache = use_cache
        self.last_seconds: float = 0.0  # 마지막 넓히기에 걸린 시간 (캐시면 0에 가깝다)
        self.last_cached: bool = False
        self.last_error: str | None = None
        self._hints: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return bool(self.client is not None and getattr(self.client, "available", False))

    def _key(self, conn: sqlite3.Connection, question: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = 'repo_path'").fetchone()
        project = row[0] if row else ""
        model = getattr(self.client, "model", "")
        text = f"{project}\n{model}\n{SYSTEM_PROMPT}\n{question.strip()}"  # 프롬프트가 바뀌면 다시 묻는다
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def hints(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = 'indexed_at'").fetchone()
        key = row[0] if row else ""
        if key not in self._hints:
            self._hints = {key: project_hints(conn)}
        return self._hints[key]

    def expand(self, conn: sqlite3.Connection, question: str) -> list[str]:
        """넓힌 말 목록. 대상이 적힌 질문, LLM 없음, 실패면 빈 목록 (조용히)."""
        started = time.perf_counter()
        self.last_cached, self.last_error = False, None
        try:
            if not question.strip() or is_targeted(question) or not self.available:
                return []
            path = self.cache / f"{self._key(conn, question)}.json"
            if self.use_cache and path.is_file():
                try:
                    self.last_cached = True
                    return list(json.loads(path.read_text(encoding="utf-8"))["terms"])
                except (OSError, ValueError, KeyError):
                    self.last_cached = False
            user = f"{self.hints(conn)}\n\n질문: {question.strip()}"
            raw = self.client.chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}])
            terms = parse_terms(raw, question)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"question": question, "model": getattr(self.client, "model", ""),
                                            "terms": terms}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            return terms
        except Exception as e:  # LLM 오류·시간 초과 등: 검색은 넓히기 없이 그대로
            self.last_error = str(e)[:200]
            return []
        finally:
            self.last_seconds = time.perf_counter() - started


def make_expander(settings: Settings | None = None) -> QueryExpander | None:
    """SASU_QUERY_EXPAND와 LLM 설정을 보고 만든다. 끄거나 LLM이 없으면 None."""
    if mode() == "off":
        return None
    try:
        expander = QueryExpander(make_expand_client(settings or load_settings()))
    except Exception:
        return None
    return expander if expander.available else None
