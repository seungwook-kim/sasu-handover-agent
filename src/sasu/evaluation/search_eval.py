"""검색 평가: 버그 증상 설명(한국어 커밋 메시지)으로 고칠 메서드를 찾을 수 있나.

- 질문: 버그 수정 커밋 제목에서 "fix:" 접두어를 뗀 문장
  (예: "개인연차 미등록 사용자의 휴가 신청·수정이 NullPointerException으로 실패하는 문제")
- 정답: 그 커밋이 고친 메서드
- 색인: 수정 **전** 시점 코드. 커밋 메시지와 수정 뒤 주석이 검색 대상에 섞이지 않게 한다.
- 지표: 상위 K개 안에 정답 메서드가 하나라도 있는 비율(Recall@K), MRR.

기법을 하나씩 켜며 비교한다: BM25 → + 용어 사전 → + 그래프 전파.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sasu import db
from sasu.config import cache_dir, load_settings
from sasu.evaluation.history import _git, export_snapshot, fix_commit_methods
from sasu.indexer import index_repository
from sasu.rag.search import MethodIndex

_PREFIX = re.compile(r"^\s*(fix|hotfix|bug)(\([^)]*\))?\s*:\s*", re.IGNORECASE)
_HANGUL = re.compile(r"[가-힣]")
_RERANKED = "위 + 재순위"


@dataclass
class Variant:
    name: str
    recall: dict[int, float]
    mrr: float


@dataclass
class SearchEvalResult:
    snapshot: str
    queries: int
    variants: list[Variant]
    examples: list[tuple[str, str, int | None]]  # (질문, 정답 중 하나, 순위)


def evaluate_search(repo: Path, rev: str, ks: tuple[int, ...] = (1, 5, 10),
                    work_dir: Path | None = None, embedder=None, reranker=None) -> SearchEvalResult:
    """reranker가 있으면 마지막 검색 방법의 상위 reranker.pool개를 다시 세운 줄을 더한다."""
    work_dir = work_dir or cache_dir()
    short = _git(repo, "rev-parse", "--short", rev).strip()
    snapshot = export_snapshot(repo, rev, work_dir / f"snapshot-{short}")
    conn = db.connect(work_dir / f"eval-{short}.db")
    index_repository(conn, snapshot, load_settings().sql_dialect)
    index = MethodIndex(conn, embedder)
    if embedder is not None:
        from sasu.rag.embeddings import embed_with_code
        from sasu.rag.vectors import build_vector_index

        build_vector_index(conn, index.embedding_documents(embed_with_code(embedder)), embedder)
    labels = {label.split(".")[-2] + "." + label.split(".")[-1] for label in index.labels.values()}

    cases = []
    for sha, subject, keys in fix_commit_methods(repo, rev):
        query = _PREFIX.sub("", subject).strip()
        gold = {k for k in keys if k in labels}
        if gold and _HANGUL.search(query) and len(query) >= 8:
            cases.append((query, gold))

    def short_label(label: str) -> str:
        parts = label.split(".")
        return f"{parts[-2]}.{parts[-1]}"

    plans = [
        ("BM25 (글자 2-gram)", "bm25", False, False),
        ("BM25 + 용어 사전", "bm25", True, False),
        ("BM25 + 용어 사전 + 그래프 전파", "bm25", True, True),
    ]
    if embedder is not None:
        plans += [
            (f"벡터만 ({embedder.name.split('/')[-1]})", "vector", False, False),
            ("BM25 + 벡터 (RRF) + 그래프", "rrf", True, True),
            (f"BM25 + 벡터 ({index.fusion} {index.vector_weight:g}) + 그래프", "hybrid", True, True),
        ]
    if reranker is not None:
        _, mode_, expand_, graph_ = plans[-1]
        plans.append((_RERANKED, mode_, expand_, graph_))
    last = plans[-1][0]
    variants = []
    examples: list[tuple[str, str, int | None]] = []
    for name, mode, expand, graph in plans:
        hits = {k: 0 for k in ks}
        rr = 0.0
        for query, gold in cases:
            if name == _RERANKED:
                pool = index.search(query, top=max(reranker.pool, max(ks)), expand=expand, graph=graph, mode=mode)
                found = reranker.rerank(conn, query, pool, top=max(ks))
            else:
                found = index.search(query, top=max(ks), expand=expand, graph=graph, mode=mode)
            ranked = [short_label(h.label) for h in found]
            rank = next((i + 1 for i, label in enumerate(ranked) if label in gold), None)
            for k in ks:
                hits[k] += int(rank is not None and rank <= k)
            rr += 1 / rank if rank else 0.0
            if name == last and len(examples) < 10:
                examples.append((query, sorted(gold)[0], rank))
        n = len(cases) or 1
        variants.append(Variant(name, {k: hits[k] / n for k in ks}, rr / n))
    conn.close()
    return SearchEvalResult(snapshot=f"{rev} ({short})", queries=len(cases), variants=variants, examples=examples)
