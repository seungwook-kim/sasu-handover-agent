"""물어보기 앞부분: 대상 코드를 정하고 근거를 모은다. CLI·웹·MCP가 함께 쓴다.

대상을 정하는 순서: 고른 후보(method_id) → 직접 적은 대상(Class.method, URL, 파일:줄)
→ 질문 안에 적힌 대상 → 검색 1위.
검색 1위로 정했을 때만 auto=True다. 화면은 이때 답을 만들기 전에 대상이 맞는지 묻는다.

검색으로 정했을 때는 후보마다 고른 이유(why: 맞은 낱말·업무명)와, 1위가 질문과 약하게만
맞는지(low_confidence)를 함께 준다. LLM이 있으면 검색 전에 질문을 코드 말로 넓힌다 (expand.py).
"""

import re
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
    question_kind: str | None = None  # 넓히기 LLM이 본 질문 종류 code | general (LLM이 없거나 판정 없으면 None)


def choose_target(conn: sqlite3.Connection, question: str, target: str | None = None,
                  method_id: int | None = None, top: int = 6, **search_opts) -> Selection:
    """search_opts: search_question에 그대로 넘긴다 (웹 서버가 메모리에 둔 index·expander)."""
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
    r = search_question(conn, question, top, **search_opts)
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
                    expander=_DEFAULT, index=None, reranker=_DEFAULT) -> SearchResult:
    """검색 상위 pool개에서 같은 클래스는 per_class개까지 top개.
    같은 클래스 메서드만 다섯 개 나오면 1위가 엉뚱할 때 고쳐 고를 다른 코드가 없고,
    클래스당 하나만 두면 같은 서비스의 2위 메서드(진짜 답)가 밀려나서 둘까지 둔다.

    expander: 질문 넓히기 (기본: 설정대로 만든다, None이면 넓히지 않는다). index: 평가처럼 여러 번 물을 때 재사용.
    reranker: LLM 재순위 (rerank.py, 기본: SASU_RERANK대로). 있으면 같은 규칙으로 reranker.pool개를 모아
    LLM이 고른 순서로 top개."""
    from sasu.rag.expand import make_expander
    from sasu.rag.rerank import make_reranker
    from sasu.rag.search import MethodIndex

    if expander is _DEFAULT:
        expander = make_expander()
    if reranker is _DEFAULT:
        reranker = make_reranker()
    index = index or MethodIndex(conn, embedder_or_none())
    how = "BM25 + 벡터 하이브리드" if index.has_vectors else "BM25 (벡터 색인 없음: sasu embed)"
    extra = expander.expand(conn, question) if expander is not None else []
    weights = getattr(expander, "last_weights", None) if extra else None  # 넓히기 변형 union2의 말별 가중치
    kind = getattr(expander, "last_kind", None) if expander is not None else None
    if kind == "general":  # 일반 개념 질문은 어차피 경고하니 다시 고르지 않는다 (호출·지연 아낌)
        reranker = None
    # 다시 고를 때는 풀을 넓게 (reranker.pool개, 같은 클래스 reranker.per_class개까지) 모아 LLM이 top개를 고른다
    want = max(reranker.pool, top) if reranker is not None else top
    limit = max(per_class, getattr(reranker, "per_class", per_class)) if reranker is not None else per_class
    # 검색에 쓰는 말 = LLM이 넓힌 말 + 빠진 코드 동사 (expanded_terms로는 LLM 출력만 보여 준다)
    terms = extra + code_verbs(question, extra) if extra else []
    if extra:
        how += " + LLM 질문 넓히기"
    # 필드 주석 용어 사전으로 넓힌 말 (why에 보여 주려고). LLM이 넓혔으면 쓰지 않는다 (search의 expand=not extra)
    glossary = [] if extra else index.expand(question)
    names = Labels(conn)
    hits = index.search(question, top=max(pool, top, 3 * want), expand=not extra, extra_terms=terms, extra_weights=weights)
    chosen: list = []
    skipped: list = []
    per: Counter = Counter()
    for hit in hits:
        class_id = names.class_of(hit.method_id)
        if per[class_id] >= limit:
            skipped.append(hit)
            continue
        per[class_id] += 1
        chosen.append(hit)
        if len(chosen) >= want:
            break
    # 상위 pool개가 몇 클래스뿐이라 자리가 남으면, 클래스 제한으로 뺀 것을 점수 순으로 채운다
    # (당직처럼 한 업무의 컨트롤러·서비스가 상위를 다 차지하면 후보가 4개로 줄던 문제)
    if len(chosen) < want and skipped:
        order = {hit.method_id: i for i, hit in enumerate(hits)}
        chosen = sorted(chosen + skipped[:want - len(chosen)], key=lambda h: order[h.method_id])
    if reranker is not None:
        chosen = reranker.rerank(conn, question, chosen, top)[:top]
        if not getattr(reranker, "last_error", None):
            how += " + " + getattr(reranker, "label", "LLM 재순위")
    result: list[Candidate] = []
    for hit in chosen:
        t = target_of(conn, hit.method_id)
        why = _why(index.matched_words(question, hit.method_id, terms + glossary),
                   names.class_label(names.class_of(hit.method_id)))
        result.append(Candidate(hit.method_id, hit.label, hit.score, t.path, t.line_start,
                                names.full(hit.method_id), why))
    coverage = index.coverage(question, result[0].method_id) if result else 0.0
    # 넓힌 말 없이 질문 낱말만으로 가장 잘 맞는 코드의 BM25 점수 (LLM 출력에 흔들리지 않게)
    best = max(index.bm25(question).values(), default=0.0)
    first_words = index.matched_words(question, result[0].method_id) if result else []
    low = is_low_confidence(best, bool(first_words), kind) or not result
    return SearchResult(result, how, extra, low, round(coverage, 3), round(best, 2), kind)


# 질문의 동작 말 → 코드 동사. 넓히기 프롬프트에도 있지만 LLM이 change·remove처럼 다른 말을 고르는 일이 잦아,
# LLM이 넓혔을 때 빠진 동사만 더한다 (넓힌 말과 같은 가중치)
_VERBS = (
    (re.compile(r"바꾸|바꿀|바꾼|바꿔|변경|수정|갱신|고치|고칠"), "update"),
    (re.compile(r"지우|지운|지워|삭제|없애"), "delete"),
    (re.compile(r"등록|저장|신청|만들|쌓"), "insert"),
    (re.compile(r"목록|조회|가져|꺼내|보여|읽어"), "select"),
)


def code_verbs(question: str, terms: list[str]) -> list[str]:
    have = {t.lower() for t in terms}
    return [verb for pattern, verb in _VERBS if pattern.search(question) and verb not in have]


LOW_SCORE = 12.0  # 질문 낱말로 가장 잘 맞는 코드의 BM25 점수가 이보다 낮으면 확신 낮음


def is_low_confidence(best_score: float, first_has_question_words: bool, kind: str | None = None) -> bool:
    """1위 후보가 질문과 약하게만 맞는가 (코드에 답이 없는 일반 개념 질문이거나 후보가 엉뚱하다).
    kind: 넓히기 LLM이 같은 호출에서 본 질문 종류. 있으면 그것만 본다 (general이면 경고).
    없으면 (LLM 없음·실패·strict 변형) BM25 규칙:
    - 질문 낱말이 코드 어디와도 약하게만 맞는다 (흔한 말 service·목록만 맞음): 최고 BM25 점수 < LOW_SCORE
    - 1위가 질문 낱말을 하나도 갖지 않는다 (넓힌 말·벡터·그래프로만 올라옴)
    일상어 평가 tune 세트(일반 11·정답 있는 60)에서 일반 경고 55% → 100%, 정답 있는데 경고 7% → 3%.
    LLM 판정에 BM25 규칙을 더하면(OR) 정답 있는데 경고가 9%로 늘어 뺐다.
    BM25 문턱은 eGovFrame 9천 메서드로 정해서 아주 작거나 큰 프로젝트에서는 다시 재 봐야 한다."""
    if kind in ("code", "general"):
        return kind == "general"
    return best_score < LOW_SCORE or not first_has_question_words


def _why(words: list[str], business: str | None) -> str:
    parts = [f"맞은 말: {', '.join(words)}" if words else "맞은 말 없음 (부르는 코드·비슷한 뜻으로 올라옴)"]
    if business:
        parts.append(f"업무명 {business}")
    return " · ".join(parts)


def evidence_for(conn: sqlite3.Connection, target: Target, use_git: bool = True,
                 question: str | None = None, doc_index=None) -> list[Evidence]:
    """코드 근거 뒤에 인수인계 문서 조각을 [문서] 등급으로 붙인다 (문서를 넣지 않았으면 그대로).
    question이 있으면 대상과 이어지지 않았어도 질문과 잘 맞는 조각을 넣는다."""
    from sasu.docs.evidence import doc_evidence

    items = gather(conn, target, repo_of(conn), cache_dir(), use_git=use_git)
    return items + doc_evidence(conn, target, question, len(items), doc_index)


def doc_conflicts_for(conn: sqlite3.Connection, evidence: list[Evidence]) -> list:
    """근거로 넣은 문서 조각이 코드와 다른 값을 말하는 곳 (규칙 기반, docs.conflicts.Conflict)."""
    from sasu.docs.conflicts import CodeFacts, chunk_conflicts

    chunk_ids = [e.chunk_id for e in evidence if e.chunk_id is not None]
    if not chunk_ids:
        return []
    facts = CodeFacts(conn)
    return [c for cid in chunk_ids for c in chunk_conflicts(conn, cid, facts)]


def search_docs(conn: sqlite3.Connection, question: str, top: int = 6, index=None) -> list:
    """질문과 맞는 문서 조각 (docs.search.DocHit). 문서를 넣지 않았으면 []."""
    from sasu.docs.search import DocIndex

    index = index or DocIndex(conn, embedder_or_none())
    return index.search(question, top=top)


@lru_cache(maxsize=1)
def embedder_or_none():
    """설정에 임베딩이 있으면 만든다 (모델은 한 번만 읽는다). 실패하면 BM25만 쓴다."""
    from sasu.rag.embeddings import EmbeddingError, make_embedder

    try:
        return make_embedder(load_settings())
    except EmbeddingError:
        return None
