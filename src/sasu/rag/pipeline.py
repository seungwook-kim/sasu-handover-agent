"""물어보기 앞부분: 대상 코드를 정하고 근거를 모은다. CLI·웹·MCP가 함께 쓴다.

대상을 정하는 순서: 고른 후보(method_id) → 직접 적은 대상(Class.method, URL, 파일:줄)
→ 질문 안에 적힌 대상 → 검색 1위.
검색 1위로 정했을 때만 auto=True다. 화면은 이때 답을 만들기 전에 대상이 맞는지 묻는다.

검색으로 정했을 때는 후보마다 고른 이유(why: 맞은 낱말·업무명)와, 1위가 질문과 약하게만
맞는지(low_confidence)를 함께 준다. LLM이 있으면 검색 전에 질문을 코드 말로 넓힌다 (expand.py).
"""

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from sasu.analysis.labels import Labels
from sasu.config import cache_dir, load_settings
from sasu.handover import repo_of
from sasu.rag.evidence import Evidence, Target, gather, resolve_target, target_of


@dataclass
class Candidate:
    method_id: int
    label: str
    score: float
    path: str
    line: int
    label_ko: str | None = None  # 한글 설명 "휴가관리 · 휴가를 등록한다" (주석에 없으면 None)
    why: str = ""  # 고른 이유 "맞은 말: 로그인, incorrect · 업무명 일반로그인"


@dataclass
class Selection:
    target: Target | None
    candidates: list[Candidate] = field(default_factory=list)  # 검색으로 찾았을 때만
    how: str = ""  # 검색 방식 (BM25, 하이브리드)
    not_found: str | None = None  # 직접 적은 대상을 못 찾았을 때 그 대상
    auto: bool = False  # 사람이 정하지 않고 검색 1위로 골랐다 (답 전에 확인받을 것)
    low_confidence: bool = False  # 검색 1위가 질문과 약하게만 맞는다 (후보·URL로 직접 고르게)
    expanded_terms: list[str] = field(default_factory=list)  # LLM이 넓힌 말 (없으면 [])


@dataclass
class SearchResult:
    candidates: list[Candidate]
    how: str
    expanded_terms: list[str] = field(default_factory=list)
    low_confidence: bool = False
    coverage: float = 0.0  # 1위 문서에 있는 질문 낱말 비율 (idf 가중, 참고용)
    best_score: float = 0.0  # 질문 낱말만으로 가장 잘 맞는 코드의 BM25 점수


def choose_target(conn: sqlite3.Connection, question: str, target: str | None = None,
                  method_id: int | None = None, top: int = 6) -> Selection:
    if method_id is not None:
        return Selection(target_of(conn, method_id))
    if target and target.strip():
        found = resolve_target(conn, target)
        if not found:
            return Selection(None, not_found=target)
        # 같은 이름이 여러 곳에 있으면 (메서드 이름만 준 경우) 나머지를 다른 후보로 보여 준다
        if len(found) == 1:
            return Selection(found[0], [], "같은 이름")
        names = Labels(conn)
        same = [Candidate(t.method_id, t.label, 0.0, t.path, t.line_start, names.full(t.method_id)) for t in found]
        return Selection(found[0], same, "같은 이름")
    found = resolve_target(conn, question)
    if found:
        return Selection(found[0])
    r = search_question(conn, question, top)
    return Selection(target_of(conn, r.candidates[0].method_id) if r.candidates else None, r.candidates, r.how,
                     auto=bool(r.candidates), low_confidence=r.low_confidence or not r.candidates,
                     expanded_terms=r.expanded_terms)


def search_candidates(conn: sqlite3.Connection, question: str, top: int = 6,
                      pool: int = 30, per_class: int = 2) -> tuple[list[Candidate], str]:
    """search_question의 후보와 검색 방식만 (MCP 등 예전 호출용)."""
    r = search_question(conn, question, top, pool, per_class)
    return r.candidates, r.how


_DEFAULT = object()


def search_question(conn: sqlite3.Connection, question: str, top: int = 6, pool: int = 30, per_class: int = 2,
                    expander=_DEFAULT, index=None) -> SearchResult:
    """검색 상위 pool개에서 같은 클래스는 per_class개까지 top개.
    같은 클래스 메서드만 다섯 개 나오면 1위가 엉뚱할 때 고쳐 고를 다른 코드가 없고,
    클래스당 하나만 두면 같은 서비스의 2위 메서드(진짜 답)가 밀려나서 둘까지 둔다.

    expander: 질문 넓히기 (기본: 설정대로 만든다, None이면 넓히지 않는다). index: 평가처럼 여러 번 물을 때 재사용."""
    from sasu.rag.expand import make_expander
    from sasu.rag.search import MethodIndex

    if expander is _DEFAULT:
        expander = make_expander()
    index = index or MethodIndex(conn, embedder_or_none())
    how = "BM25 + 벡터 하이브리드" if index.has_vectors else "BM25 (벡터 색인 없음: sasu embed)"
    extra = expander.expand(conn, question) if expander is not None else []
    if extra:
        how += " + LLM 질문 넓히기"
    glossary = index.expand(question)  # 필드 주석 용어 사전으로 넓힌 말 (why에 보여 주려고)
    names = Labels(conn)
    result: list[Candidate] = []
    per: Counter = Counter()
    for hit in index.search(question, top=max(pool, top), extra_terms=extra):
        class_id = names.class_of(hit.method_id)
        if per[class_id] >= per_class:
            continue
        per[class_id] += 1
        t = target_of(conn, hit.method_id)
        why = _why(index.matched_words(question, hit.method_id, extra + glossary), names.class_label(class_id))
        result.append(Candidate(hit.method_id, hit.label, hit.score, t.path, t.line_start,
                                names.full(hit.method_id), why))
        if len(result) >= top:
            break
    coverage = index.coverage(question, result[0].method_id) if result else 0.0
    # 넓힌 말 없이 질문 낱말만으로 가장 잘 맞는 코드의 BM25 점수 (LLM 출력에 흔들리지 않게)
    best = max(index.bm25(question).values(), default=0.0)
    first_words = index.matched_words(question, result[0].method_id) if result else []
    low = is_low_confidence(best, bool(first_words)) or not result
    return SearchResult(result, how, extra, low, round(coverage, 3), round(best, 2))


LOW_SCORE = 12.0  # 질문 낱말로 가장 잘 맞는 코드의 BM25 점수가 이보다 낮으면 확신 낮음


def is_low_confidence(best_score: float, first_has_question_words: bool) -> bool:
    """1위 후보가 질문과 약하게만 맞는가.
    - 질문 낱말이 코드 어디와도 약하게만 맞는다 (흔한 말 service·목록만 맞음): 최고 BM25 점수 < LOW_SCORE
    - 1위가 질문 낱말을 하나도 갖지 않는다 (넓힌 말·벡터·그래프로만 올라옴)
    문턱은 일상어 평가 세트(evaluation/everyday.json, eGovFrame 9천 메서드)로 정했다.
    점수는 문서 수에 따라 조금 달라지므로 아주 작거나 큰 프로젝트에서는 다시 재 봐야 한다."""
    return best_score < LOW_SCORE or not first_has_question_words


def _why(words: list[str], business: str | None) -> str:
    parts = [f"맞은 말: {', '.join(words)}" if words else "맞은 말 없음 (부르는 코드·비슷한 뜻으로 올라옴)"]
    if business:
        parts.append(f"업무명 {business}")
    return " · ".join(parts)


def evidence_for(conn: sqlite3.Connection, target: Target, use_git: bool = True) -> list[Evidence]:
    return gather(conn, target, repo_of(conn), cache_dir(), use_git=use_git)


@lru_cache(maxsize=1)
def embedder_or_none():
    """설정에 임베딩이 있으면 만든다 (모델은 한 번만 읽는다). 실패하면 BM25만 쓴다."""
    from sasu.rag.embeddings import EmbeddingError, make_embedder

    try:
        return make_embedder(load_settings())
    except EmbeddingError:
        return None
