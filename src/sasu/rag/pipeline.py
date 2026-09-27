"""물어보기 앞부분: 대상 코드를 정하고 근거를 모은다. CLI·웹·MCP가 함께 쓴다.

대상을 정하는 순서: 고른 후보(method_id) → 직접 적은 대상(Class.method, URL, 파일:줄)
→ 질문 안에 적힌 대상 → 검색 1위.
"""

import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache

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


@dataclass
class Selection:
    target: Target | None
    candidates: list[Candidate] = field(default_factory=list)  # 검색으로 찾았을 때만
    how: str = ""  # 검색 방식 (BM25, 하이브리드)
    not_found: str | None = None  # 직접 적은 대상을 못 찾았을 때 그 대상


def choose_target(conn: sqlite3.Connection, question: str, target: str | None = None,
                  method_id: int | None = None, top: int = 5) -> Selection:
    if method_id is not None:
        return Selection(target_of(conn, method_id))
    if target and target.strip():
        found = resolve_target(conn, target)
        return Selection(found[0]) if found else Selection(None, not_found=target)
    found = resolve_target(conn, question)
    if found:
        return Selection(found[0])
    candidates, how = search_candidates(conn, question, top)
    return Selection(target_of(conn, candidates[0].method_id) if candidates else None, candidates, how)


def search_candidates(conn: sqlite3.Connection, question: str, top: int = 5) -> tuple[list[Candidate], str]:
    from sasu.rag.search import MethodIndex

    index = MethodIndex(conn, embedder_or_none())
    how = "BM25 + 벡터 하이브리드" if index.has_vectors else "BM25 (벡터 색인 없음: sasu embed)"
    result = []
    for hit in index.search(question, top=top):
        t = target_of(conn, hit.method_id)
        result.append(Candidate(hit.method_id, hit.label, hit.score, t.path, t.line_start))
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
