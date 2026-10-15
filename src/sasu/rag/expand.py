"""LLM으로 질문 넓히기: 한국어 일상어 질문을 코드에 나올 말로 넓힌다.

"로그인 5번 틀리면 잠겨?"의 답은 processLoginIncorrect·lockCnt에 있는데, 질문 낱말(로그인·틀리·잠겨)만으로는
BM25가 거기까지 닿지 못한다. 검색 전에 LLM에게 영어 식별자 조각(login, incorrect, lock),
이 프로젝트식 약어(업무명 ↔ 클래스 이름에서 뽑은 힌트), 한국어 동의어(잠김 → 잠금·제한)를 받아
BM25 질의에 낮은 가중치로 더한다 (search.MethodIndex.bm25의 extra_terms).
같은 호출에서 질문 종류(code: 이 프로젝트 코드 질문 / general: 일반 개념 질문, last_kind)도 받아
검색 쪽 low_confidence 판단에 쓴다 (pipeline.is_low_confidence). 프롬프트가 바뀌면 캐시 키도 바뀐다.

- 설정: SASU_QUERY_EXPAND = auto(기본: LLM이 있으면 켬) | on | off.
  SASU_EXPAND_VARIANT = default(기본) | strict | union2 (넓히기 출력 흔들림 줄이기 실험용, 아래 VARIANTS).
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
그 답이 들어 있는 소스 코드(메서드 이름·변수·주석·SQL)에 나올 법한 검색어를 고르고, 질문 종류를 판정한다.

고를 것:
- 영어 식별자 조각 (예: login, incorrect, lock, count)
- 질문의 동작을 코드 동사로: 신청·등록·저장·만들기 → insert, regist / 조회·목록·가져오기 → select, list /
  수정·바꾸기·갱신 → update / 삭제·지우기 → delete
- 이 프로젝트식 약어: 아래 힌트의 "업무명=클래스 조각"과 "자주 나오는 조각"에서 질문과 맞는 것만
- 코드 주석에 나올 한국어 동의어 (예: 잠겨 → 잠금, 제한 / 글 번호 → 게시물 아이디)

질문 종류 kind:
- "code": 이 프로젝트의 특정 기능·화면·데이터·처리 규칙을 묻는다. 일반 개념이 섞여도 이 프로젝트에서
  어떻게 하는지를 물으면 code (예: "우리는 트랜잭션을 어디서 걸어?")
- "general": 특정 업무와 상관없는 일반 프로그래밍·DB·서버·프레임워크 개념이나 비교를 묻는다
  (예: "제네릭에서 와일드카드는 언제 써?")

규칙:
- 질문에 이미 있는 낱말은 빼고 4~12개. kind가 general이어도 검색어는 고른다.
- 출력은 JSON 객체 하나만: {"kind": "code" 또는 "general", "terms": [검색어 문자열...]}. 설명·코드 블록 없이.

예: "주문 취소하면 재고 돌아와?" → {"kind":"code","terms":["order","cancel","stock","restore","Ordr","Stck","취소처리","재고수량"]}"""

# strict: 같은 질문에 같은 말이 나오게 순서·개수를 못박고, 힌트의 클래스 조각을 먼저 고르게 한다
STRICT_PROMPT = """너는 코드 검색어를 넓히는 도구다. 개발자가 한국어 일상어로 한 질문을 받아,
그 답이 들어 있는 소스 코드(메서드 이름·변수·주석·SQL)에 나올 검색어를 고른다.
같은 질문에는 언제나 같은 답이 나오도록 아래 순서와 개수를 그대로 지킨다.

순서 (합 6~10개):
1. 업무 조각 1~3개: 아래 힌트 "업무명=클래스 조각"에서 질문의 업무와 맞는 항목의 오른쪽(클래스 조각)을 힌트에 적힌 그대로. 맞는 항목이 없으면 건너뛴다.
2. 동작 1개: 신청·등록·저장·만들기 → insert / 조회·목록·가져오기 → select / 수정·바꾸기·갱신 → update / 삭제·지우기 → delete. 질문에 동작이 없으면 건너뛴다.
3. 영어 식별자 조각 2~4개: 질문의 핵심 명사·조건을 코드 이름에 흔한 영어 낱말로 (한 낱말씩). 힌트 "자주 나오는 조각"에 있으면 그 모양 그대로 먼저.
4. 한국어 동의어 1~2개: 코드 주석에 나올 말 (질문 낱말과 다른 말).

규칙:
- 질문에 이미 있는 낱말은 빼고, 확실하지 않은 추측은 넣지 않는다.
- 특정 업무가 아닌 일반 프로그래밍 개념 질문이면 []만 쓴다.
- 출력은 JSON 문자열 배열 하나만. 설명·코드 블록 없이.

예 1 (힌트에 주문관리=Ordr, 재고관리=Stck이 있을 때)
질문: 주문 취소하면 재고 돌아와?
출력: ["Ordr","Stck","update","cancel","stock","restore","취소처리","재고수량"]
예 2
질문: 제네릭에서 와일드카드는 언제 써?
출력: []"""

VARIANTS = ("default", "strict", "union2")  # default=지금 그대로, strict=고정 프롬프트, union2=두 번 넓혀 합집합
# union2 가중치: (두 번 다 나온 말, 한 번만 나온 말). 0.7은 search._EXTRA_WEIGHT(dev 세트로 정함)와 같게,
# 한 번만 나온 말은 그보다 낮게 미리 정한 값. 바꿔 볼 때는 dev 질문으로만 고른다
UNION_WEIGHTS = (0.7, 0.5)

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


def variant() -> str:
    value = os.environ.get("SASU_EXPAND_VARIANT", "default").strip().lower()
    return value if value in VARIANTS else "default"


def union_terms(runs: list[list[str]], weights: tuple[float, float] = UNION_WEIGHTS) -> tuple[list[str], dict[str, float]]:
    """여러 번 넓힌 말의 합집합과 말별 가중치. 매번 나온 말(대소문자 무시)이 앞, 가중치 weights[0].
    일부 실행에만 나온 말은 뒤, weights[1]. 처음 나온 모양·순서를 지킨다."""
    first: dict[str, str] = {}
    count: Counter = Counter()
    for terms in runs:
        for term in terms:
            first.setdefault(term.lower(), term)
        count.update({t.lower() for t in terms})  # 한 실행 안의 중복은 한 번으로
    every = len(runs)
    keys = sorted(first, key=lambda k: count[k] < every)  # 안정 정렬: 매번 나온 말이 앞
    merged = [first[k] for k in keys]
    return merged, {first[k]: weights[0] if count[k] >= every else weights[1] for k in keys}


def make_expand_client(settings: Settings):
    """넓히기용 LLM. 공급자는 SASU_EXPAND_PROVIDER(auto면 SASU_LLM_PROVIDER와 같게: anthropic | claude-code | openai).
    anthropic은 Claude API 빠른 모델(기본 claude-haiku-5-5, effort low)로 — claude CLI를 띄우는 것보다 3~4초 빠르다.
    claude-code는 빠른 모델(기본 haiku)로, 나머지는 OpenAI 호환 주소로. 모델은 SASU_EXPAND_MODEL로 바꾼다."""
    from sasu.rag.answer import AnthropicClient, ClaudeCodeClient, LLMClient

    model = os.environ.get("SASU_EXPAND_MODEL", "").strip()
    provider = os.environ.get("SASU_EXPAND_PROVIDER", "auto").strip().lower() or "auto"
    if provider == "auto":
        provider = settings.llm_provider
    if provider == "anthropic":
        # temperature는 못 넣는다 (Haiku 5.5는 기본값 1 외에는 400). thinking은 모델 기본(adaptive)에 effort low:
        # 생각을 끈 것과 지연이 같았다 (앞 10문항 평균 1.42초 vs 1.46초)
        return AnthropicClient(settings.anthropic_api_key, model or "claude-haiku-5-5", effort="low",
                               max_tokens=4096, timeout=30.0)
    if provider == "claude-code":
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


KINDS = ("code", "general")  # 넓히기 LLM이 함께 내는 질문 종류 (이 프로젝트 코드 질문 / 일반 개념 질문)


def parse_output(raw: str, question: str = "") -> tuple[list[str], str | None]:
    """LLM 출력에서 (넓힌 말, 질문 종류)를 꺼낸다. {"kind": ..., "terms": [...]} 객체나 예전 형식인 배열.
    종류가 없거나 code·general이 아니면 None (검색 쪽은 BM25 규칙으로 판단). 형식이 틀리면 ([], None)."""
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            kind = str(data.get("kind", "")).strip().lower()
            items = data.get("terms")
            return _clean_terms(items if isinstance(items, list) else [], question), kind if kind in KINDS else None
    return parse_terms(raw, question), None


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
    return _clean_terms(items, question)


def _clean_terms(items: list, question: str) -> list[str]:
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
    """use_cache=False면 캐시를 읽지도 쓰지도 않는다 (평가 반복 측정이 실제 캐시를 바꾸지 않게).
    variant_name: VARIANTS 중 하나 (비우면 SASU_EXPAND_VARIANT)."""

    def __init__(self, client, cache: Path | None = None, use_cache: bool = True, variant_name: str | None = None):
        if variant_name is not None and variant_name not in VARIANTS:
            raise ValueError(f"넓히기 변형은 {', '.join(VARIANTS)} 중 하나: {variant_name}")
        self.client = client
        self.cache = cache if cache is not None else cache_dir() / "query-expand"
        self.use_cache = use_cache
        self.variant = variant_name or variant()
        self.last_seconds: float = 0.0  # 마지막 넓히기에 걸린 시간 (캐시면 0에 가깝다)
        self.last_cached: bool = False
        self.last_error: str | None = None
        self.last_weights: dict[str, float] | None = None  # 말별 가중치 (union2만. None이면 모두 같은 가중치)
        # 마지막 질문을 LLM이 본 종류: code | general | None (판정 없음·실패·strict). 넓히기와 같은 호출에서 받는다
        self.last_kind: str | None = None
        self._hints: dict[str, str] = {}

    @property
    def prompt(self) -> str:
        return STRICT_PROMPT if self.variant == "strict" else SYSTEM_PROMPT

    @property
    def repeats(self) -> int:
        return 2 if self.variant == "union2" else 1

    @property
    def available(self) -> bool:
        return bool(self.client is not None and getattr(self.client, "available", False))

    def _key(self, conn: sqlite3.Connection, question: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = 'repo_path'").fetchone()
        project = row[0] if row else ""
        model = getattr(self.client, "model", "")
        text = f"{project}\n{model}\n{self.prompt}\n{question.strip()}"  # 프롬프트가 바뀌면 다시 묻는다
        if self.variant != "default":  # default의 키는 예전 그대로, 변형마다 따로
            text = f"{self.variant}\n{text}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def hints(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = 'indexed_at'").fetchone()
        key = row[0] if row else ""
        if key not in self._hints:
            self._hints = {key: project_hints(conn)}
        return self._hints[key]

    def _read(self, path: Path) -> tuple[list[list[str]], list[str | None]] | None:
        """캐시에 둔 넓히기 결과 (실행별 목록, 실행별 질문 종류). 없거나 깨졌으면 None."""
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            runs = data["runs"] if self.repeats > 1 else [data["terms"]]
            kinds = data.get("kinds") if self.repeats > 1 else [data.get("kind")]
            if not isinstance(kinds, list) or len(kinds) != len(runs):
                kinds = [None] * len(runs)
            return [list(terms) for terms in runs], [k if k in KINDS else None for k in kinds]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def expand(self, conn: sqlite3.Connection, question: str) -> list[str]:
        """넓힌 말 목록. 대상이 적힌 질문, LLM 없음, 실패면 빈 목록 (조용히)."""
        started = time.perf_counter()
        self.last_cached, self.last_error, self.last_weights, self.last_kind = False, None, None, None
        try:
            if not question.strip() or is_targeted(question) or not self.available:
                return []
            path = self.cache / f"{self._key(conn, question)}.json"
            cached = self._read(path) if self.use_cache else None
            self.last_cached = cached is not None
            if cached is None:
                user = f"{self.hints(conn)}\n\n질문: {question.strip()}"
                messages = [{"role": "system", "content": self.prompt}, {"role": "user", "content": user}]
                outputs = [parse_output(self.client.chat(messages), question) for _ in range(self.repeats)]
                runs, kinds = [o[0] for o in outputs], [o[1] for o in outputs]
            else:
                runs, kinds = cached
            # 여러 번 물었으면(union2) 모두 general일 때만 general, 하나라도 code면 code
            self.last_kind = ("general" if all(k == "general" for k in kinds) else
                              "code" if "code" in kinds else None)
            if self.repeats > 1:
                terms, self.last_weights = union_terms(runs)
            else:
                terms = runs[0]
            if self.use_cache and not self.last_cached:
                record = {"question": question, "model": getattr(self.client, "model", ""), "terms": terms,
                          "kind": self.last_kind}
                if self.variant != "default":
                    record["variant"] = self.variant
                if self.repeats > 1:
                    record["runs"], record["kinds"] = runs, kinds
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            return terms
        except Exception as e:  # LLM 오류·시간 초과 등: 검색은 넓히기 없이 그대로
            self.last_error, self.last_kind = str(e)[:200], None
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
