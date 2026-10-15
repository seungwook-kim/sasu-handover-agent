"""물어보기 앞부분: 대상 코드를 정하고 근거를 모은다. CLI·웹·MCP가 함께 쓴다.

대상을 정하는 순서: 고른 후보(method_id) → 직접 적은 대상(Class.method, URL, 파일:줄)
→ 질문 안에 적힌 대상 → 검색 1위.
검색 1위로 정했을 때만 auto=True다. 화면은 이때 답을 만들기 전에 대상이 맞는지 묻는다.
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


@dataclass
class Selection:
    target: Target | None
    candidates: list[Candidate] = field(default_factory=list)  # 검색으로 찾았을 때만
    how: str = ""  # 검색 방식 (BM25, 하이브리드)
    not_found: str | None = None  # 직접 적은 대상을 못 찾았을 때 그 대상
    auto: bool = False  # 사람이 정하지 않고 검색 1위로 골랐다 (답 전에 확인받을 것)


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
    candidates, how = search_candidates(conn, question, top)
    return Selection(target_of(conn, candidates[0].method_id) if candidates else None, candidates, how,
                     auto=bool(candidates))


def search_candidates(conn: sqlite3.Connection, question: str, top: int = 6,
                      pool: int = 30, per_class: int = 2) -> tuple[list[Candidate], str]:
    """검색 상위 pool개에서 같은 클래스는 per_class개까지 top개.
    같은 클래스 메서드만 다섯 개 나오면 1위가 엉뚱할 때 고쳐 고를 다른 코드가 없고,
    클래스당 하나만 두면 같은 서비스의 2위 메서드(진짜 답)가 밀려나서 둘까지 둔다."""
    from sasu.rag.search import MethodIndex

    index = MethodIndex(conn, embedder_or_none())
    how = "BM25 + 벡터 하이브리드" if index.has_vectors else "BM25 (벡터 색인 없음: sasu embed)"
    names = Labels(conn)
    result: list[Candidate] = []
    per: Counter = Counter()
    for hit in index.search(question, top=max(pool, top)):
        class_id = names.class_of(hit.method_id)
        if per[class_id] >= per_class:
            continue
        per[class_id] += 1
        t = target_of(conn, hit.method_id)
        result.append(Candidate(hit.method_id, hit.label, hit.score, t.path, t.line_start, names.full(hit.method_id)))
        if len(result) >= top:
            break
    return result, how


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
