"""LLM 재순위: 검색 상위 후보(기본 40개)를 빠른 LLM에 질문과 함께 보여 주고 묻기 대상 6개를 고르게 한다.

BM25·벡터는 낱말이 겹치는 메서드를 올릴 뿐, "잠기는 규칙을 실제로 정하는 곳"인지는 모른다.
후보마다 클래스.메서드·한글 설명·URL·Javadoc/Docstring 앞부분·부르는 SQL과 메서드를 짧게 보여 주면
LLM이 질문의 답이 들어 있을 메서드를 앞으로 올린다. 고르지 않은 후보는 검색 순서대로 뒤를 채운다.

- 설정: SASU_RERANK = auto(기본: 넓히기 공급자가 Claude API일 때만 켬) | llm(켬) | off.
  claude CLI 공급자는 호출마다 몇 초가 더 걸려 auto에서는 켜지 않는다 (llm으로 직접 켤 수 있다).
  SASU_RERANK_POOL(기본 40): LLM에 보여 줄 후보 수. 풀은 같은 클래스 3개까지로 모은다 (POOL_PER_CLASS).
- 일상어 tune 세트(Claude API Haiku, 넓히기 default와 함께, 5번): Java 60문항 상위 6 76.7% → 97.3%,
  1위 41% → 90%, 흔들린 질문 15 → 4 / 파이썬(django-oscar) 20문항 72% → 100%, 1위 46% → 88%, 흔들림 2 → 0.
  풀 20(같은 클래스 2개)은 Java 91%·파이썬 87%로 풀 안 정답 비율에서 막혔다. 질문당 +약 1.9초, 호출 1번.
- 넓히기가 일반 개념 질문(kind=general)으로 본 질문은 다시 고르지 않는다 (어차피 경고, 호출 아낌).
- 넓히기와 같은 LLM 클라이언트(expand.make_expand_client: 빠른 모델)를 쓴다.
- LLM이 없거나 실패하면 검색 순서 그대로 (조용히). 결과는 질문·후보 해시로 SASU_HOME/cache에 둔다.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from sasu.analysis.labels import Labels
from sasu.config import Settings, cache_dir, load_settings

DEFAULT_POOL = 40
POOL_PER_CLASS = 3  # 풀을 모을 때 같은 클래스 상한 (최종 6개도 LLM이 고른 대로)
_DOC_CHARS = 160  # 후보마다 보여 줄 Javadoc/Docstring 앞부분 길이

SYSTEM_PROMPT = """너는 코드 검색 결과를 다시 고르는 도구다. 개발자가 한국어 일상어로 한 질문과,
검색이 찾은 메서드 후보 목록을 받는다. 후보 번호가 작을수록 검색 점수가 높다.

할 일: 질문의 답이 실제로 들어 있는 메서드(그 동작·조건·규칙을 처리하거나 그 데이터를 읽고 쓰는 코드)를
가능성이 높은 순서로 최대 6개 고른다.
- 질문의 업무(무엇에 대한 것인지)와 동작(등록·조회·수정·삭제·검사 등)이 둘 다 맞는 후보를 앞에
- 이름만 비슷하고 다른 업무인 후보는 뺀다
- 판단이 서지 않으면 검색 순서(작은 번호)를 따른다

출력은 JSON 객체 하나만: {"pick": [후보 번호...]}. 설명·코드 블록 없이."""


def mode() -> str:
    value = os.environ.get("SASU_RERANK", "auto").strip().lower()
    return value if value in ("auto", "llm", "off") else "auto"


def pool_size() -> int:
    try:
        return max(6, min(60, int(os.environ.get("SASU_RERANK_POOL", DEFAULT_POOL))))
    except ValueError:
        return DEFAULT_POOL


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"[/*#\"']+", " ", text or "")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def describe(conn: sqlite3.Connection, method_id: int, names: Labels | None = None) -> str:
    """후보 하나를 한 줄로: Class.method · 한글 설명 · URL · 주석 앞부분 · SQL(테이블) · 부르는 메서드."""
    names = names or Labels(conn)
    r = conn.execute(
        """SELECT m.name, m.doc, c.name AS class_name, f.path FROM methods m
           JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id WHERE m.id = ?""",
        (method_id,),
    ).fetchone()
    if r is None:
        return str(method_id)
    parts = [f"{r['class_name']}.{r['name']}"]
    if label := names.full(method_id):
        parts.append(label)
    parts.append(Path(r["path"]).as_posix().rsplit("/", 2)[-2] if "/" in Path(r["path"]).as_posix() else r["path"])
    url = conn.execute("SELECT url FROM url_mappings WHERE method_id = ? LIMIT 1", (method_id,)).fetchone()
    if url:
        parts.append(f"URL {url[0]}")
    if r["doc"]:
        parts.append(f"주석: {_clean(r['doc'], _DOC_CHARS)}")
    sql = conn.execute(
        """SELECT s.full_id, group_concat(DISTINCT t.table_name) AS tables FROM edges e
           JOIN statements s ON s.id = e.dst_id LEFT JOIN statement_tables t ON t.statement_id = s.id
           WHERE e.src_id = ? AND e.src_kind = 'method' AND e.dst_kind = 'statement' GROUP BY s.id LIMIT 3""",
        (method_id,),
    ).fetchall()
    if sql:
        parts.append("SQL " + ", ".join(f"{s['full_id'].rsplit('.', 1)[-1]}({s['tables'] or ''})" for s in sql))
    callees = [c[0] for c in conn.execute(
        "SELECT DISTINCT callee FROM calls WHERE method_id = ? LIMIT 6", (method_id,))]
    if callees:
        parts.append("부름 " + ", ".join(callees))
    return " · ".join(parts)


def parse_pick(raw: str, n: int) -> list[int]:
    """LLM 출력에서 고른 후보 번호(1부터)를 0부터의 위치로. 틀린 번호·중복은 버린다. 형식이 틀리면 []."""
    start, end = raw.find("{"), raw.rfind("}")
    items = None
    if 0 <= start < end:
        try:
            data = json.loads(raw[start:end + 1])
            items = data.get("pick") if isinstance(data, dict) else None
        except json.JSONDecodeError:
            items = None
    if items is None:
        start, end = raw.find("["), raw.rfind("]")
        if 0 <= start < end:
            try:
                items = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                items = None
    if not isinstance(items, list):
        return []
    picked: list[int] = []
    for item in items:
        try:
            i = int(item) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in picked:
            picked.append(i)
    return picked


class LLMReranker:
    """use_cache=False면 캐시를 읽지도 쓰지도 않는다 (평가 반복 측정용)."""

    def __init__(self, client, pool: int | None = None, cache: Path | None = None, use_cache: bool = True):
        self.client = client
        self.pool = pool or pool_size()
        self.per_class = POOL_PER_CLASS
        self.cache = cache if cache is not None else cache_dir() / "rerank"
        self.use_cache = use_cache
        self.last_seconds: float = 0.0
        self.last_error: str | None = None
        self.last_cached: bool = False

    @property
    def available(self) -> bool:
        return bool(self.client is not None and getattr(self.client, "available", False))

    def _key(self, question: str, lines: list[str]) -> str:
        model = getattr(self.client, "model", "")
        text = f"{model}\n{SYSTEM_PROMPT}\n{question.strip()}\n" + "\n".join(lines)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def choose(self, question: str, lines: list[str], top: int = 6) -> list[int] | None:
        """후보 설명 줄(검색 순서)에서 LLM이 고른 위치(0부터, 최대 top개). 실패·형식 틀림이면 None.
        분석 파일을 쓰지 않아 여러 질문을 동시에 물을 수 있다 (평가)."""
        started = time.perf_counter()
        self.last_error, self.last_cached = None, False
        try:
            if len(lines) <= 1 or not self.available:
                return None
            path = self.cache / f"{self._key(question, lines)}.json"
            if self.use_cache and path.is_file():
                try:
                    picked = [int(i) for i in json.loads(path.read_text(encoding="utf-8"))["pick"]]
                    self.last_cached = True
                    return [i for i in picked if 0 <= i < len(lines)][:top]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            listing = "\n".join(f"[{i}] {line}" for i, line in enumerate(lines, 1))
            user = f"질문: {question.strip()}\n\n후보:\n{listing}\n\n최대 {top}개를 골라 JSON으로."
            raw = self.client.chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}])
            picked = parse_pick(raw, len(lines))[:top]
            if not picked:
                self.last_error = "재순위 출력 형식이 틀림"
                return None
            if self.use_cache:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"question": question, "pick": picked}), encoding="utf-8")
                except OSError:
                    pass
            return picked
        except Exception as e:  # LLM 오류·시간 초과: 검색 순서 그대로
            self.last_error = str(e)[:200]
            return None
        finally:
            self.last_seconds = time.perf_counter() - started

    def rerank(self, conn: sqlite3.Connection, question: str, hits: list, top: int = 6) -> list:
        """hits(검색 순서)를 LLM이 고른 순서로. 고른 것 뒤에 나머지를 검색 순서대로. 실패하면 hits 그대로."""
        if len(hits) <= 1 or not self.available:
            self.last_seconds, self.last_error, self.last_cached = 0.0, None, False
            return list(hits)
        names = Labels(conn)
        picked = self.choose(question, [describe(conn, h.method_id, names) for h in hits], top)
        return reorder(hits, picked)


def reorder(hits: list, picked: list[int] | None) -> list:
    """고른 위치를 앞에, 나머지는 원래 순서대로. picked가 None·빈 목록이면 그대로."""
    if not picked:
        return list(hits)
    picked = [i for i in dict.fromkeys(picked) if 0 <= i < len(hits)]
    return [hits[i] for i in picked] + [h for i, h in enumerate(hits) if i not in picked]


def make_reranker(settings: Settings | None = None) -> LLMReranker | None:
    """SASU_RERANK와 넓히기 공급자를 보고 만든다. 끄거나, auto인데 Claude API가 아니거나, LLM이 없으면 None."""
    from sasu.rag.expand import make_expand_client

    value = mode()
    if value == "off":
        return None
    try:
        settings = settings or load_settings()
        provider = os.environ.get("SASU_EXPAND_PROVIDER", "auto").strip().lower() or "auto"
        if provider == "auto":
            provider = settings.llm_provider
        if value == "auto" and provider != "anthropic":
            return None
        reranker = LLMReranker(make_expand_client(settings))
    except Exception:
        return None
    return reranker if reranker.available else None
