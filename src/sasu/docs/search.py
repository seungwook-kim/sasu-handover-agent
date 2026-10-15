"""문서 조각 검색: BM25(코드 검색과 같은 한국어 2-gram 토큰) + 임베딩이 있으면 벡터.

조각은 많아야 수백~수천 개라 색인을 요청마다 만들어도 빠르다 (eGovFrame 메서드 9천 개의 수십분의 일).
벡터는 코드 조각과 같은 임베딩 캐시(cache/embeddings.db)를 쓴다: 넣을 때 한 번 임베딩해 두면 다시 하지 않는다.
"""

import hashlib
import math
import sqlite3
import struct
from collections import Counter
from dataclasses import dataclass

from sasu.rag.search import tokenize

_VECTOR_WEIGHT = 0.3  # 코드 검색과 같은 선형 결합 (BM25를 최고점 1로 맞추고 코사인 × 0.3)
MIN_COVERAGE = 0.34  # 연결 없이 질문만으로 근거에 넣을 조각: 질문 낱말(idf 가중)의 이 비율 이상이 조각에 있어야


@dataclass
class DocHit:
    chunk_id: int
    score: float
    coverage: float  # 질문 낱말(idf 가중) 중 이 조각에 있는 비율
    title: str


def chunk_search_text(name: str, heading: str | None, page: str | None, text: str) -> str:
    return " ".join(x for x in (name, heading or "", page or "", text) if x)


class DocIndex:
    def __init__(self, conn: sqlite3.Connection, embedder=None):
        from sasu.docs.store import chunk_title

        self.conn = conn
        self.embedder = embedder
        self.texts: dict[int, str] = {}
        self.titles: dict[int, str] = {}
        try:
            rows = conn.execute(
                """SELECT c.id, c.section, c.heading, c.page, c.text, d.name, d.doc_date
                   FROM doc_chunks c JOIN documents d ON d.id = c.doc_id""").fetchall()
        except sqlite3.OperationalError:  # 문서 테이블이 없는 예전 분석 파일 (읽기 전용)
            rows = []
        for r in rows:
            self.texts[r["id"]] = chunk_search_text(r["name"], r["heading"], r["page"], r["text"])
            self.titles[r["id"]] = chunk_title(r["name"], r["section"], r["heading"], r["doc_date"], r["page"])
        self.tf = {cid: Counter(tokenize(t)) for cid, t in self.texts.items()}
        self.lengths = {cid: sum(tf.values()) for cid, tf in self.tf.items()}
        self.avg_len = sum(self.lengths.values()) / max(len(self.lengths), 1)
        df: Counter = Counter()
        for tf in self.tf.values():
            df.update(tf.keys())
        n = len(self.tf)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._vectors: dict[int, list[float]] | None = None
        self._query_vectors: dict[str, list[float] | None] = {}  # 같은 질문을 두 번 임베딩하지 않게

    def __len__(self) -> int:
        return len(self.texts)

    def bm25(self, query: str, k1: float = 1.2, b: float = 0.75, extra_terms: list[str] | None = None) -> dict[int, float]:
        terms = Counter(tokenize(query))
        for t in dict.fromkeys(tokenize(" ".join(extra_terms or []))):
            if t not in terms:
                terms[t] = 0.7
        scores: dict[int, float] = {}
        for cid, tf in self.tf.items():
            score = 0.0
            for term, qf in terms.items():
                f = tf.get(term)
                if f:
                    score += self.idf.get(term, 0.0) * qf * f * (k1 + 1) / (
                        f + k1 * (1 - b + b * self.lengths[cid] / self.avg_len))
            if score > 0:
                scores[cid] = score
        return scores

    def coverage(self, query: str, chunk_id: int) -> float:
        terms = set(tokenize(query))
        unseen = math.log(1 + (len(self.tf) + 0.5) / 0.5)
        total = sum(self.idf.get(t, unseen) for t in terms)
        if not total:
            return 0.0
        tf = self.tf.get(chunk_id, {})
        return sum(self.idf.get(t, unseen) for t in terms if t in tf) / total

    def vectors(self) -> dict[int, list[float]]:
        """조각 벡터 (임베딩 캐시에서, 없는 것만 새로 임베딩해 캐시에 넣는다)."""
        if self._vectors is None:
            self._vectors = embed_chunks(self.texts, self.embedder) if self.embedder is not None else {}
        return self._vectors

    def search(self, query: str, top: int = 6, extra_terms: list[str] | None = None) -> list[DocHit]:
        if not self.texts or not query.strip():
            return []
        scores = self.bm25(query, extra_terms=extra_terms)
        vectors = self.vectors() if self.embedder is not None else {}
        if vectors:
            if query not in self._query_vectors:
                try:
                    self._query_vectors[query] = self.embedder.embed([query])[0]
                except Exception:  # 임베딩 서버 오류면 BM25만
                    self._query_vectors[query] = None
            q = self._query_vectors[query]
            if q is not None:
                peak = max(scores.values(), default=0.0) or 1.0
                fused = {cid: s / peak for cid, s in scores.items()}
                for cid, v in vectors.items():
                    fused[cid] = fused.get(cid, 0.0) + _VECTOR_WEIGHT * max(_cosine(q, v), 0.0)
                scores = fused
        best = sorted(scores.items(), key=lambda kv: -kv[1])[:top]
        return [DocHit(cid, round(s, 4), round(self.coverage(query, cid), 3), self.titles[cid]) for cid, s in best]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def embed_chunks(texts: dict[int, str], embedder) -> dict[int, list[float]]:
    from sasu.rag.vectors import _cache

    if not texts:
        return {}
    try:
        cache = _cache(None)
    except Exception:
        return {}
    hashes = {cid: hashlib.sha1(t[:1500].encode("utf-8")).hexdigest() for cid, t in texts.items()}
    wanted = list(set(hashes.values()))
    cached: dict[str, bytes] = {}
    for i in range(0, len(wanted), 500):  # 캐시에는 코드 조각 수천 개도 있어 필요한 해시만 읽는다
        part = wanted[i:i + 500]
        cached.update(cache.execute(
            f"SELECT text_hash, vector FROM embedding_cache WHERE model = ? AND text_hash IN ({','.join('?' * len(part))})",
            [embedder.name, *part]).fetchall())
    missing = [cid for cid, h in hashes.items() if h not in cached]
    try:
        for i in range(0, len(missing), 64):
            ids = missing[i:i + 64]
            for cid, vector in zip(ids, embedder.embed([texts[cid][:1500] for cid in ids])):
                blob = struct.pack(f"{len(vector)}f", *vector)
                cached[hashes[cid]] = blob
                cache.execute("INSERT OR REPLACE INTO embedding_cache VALUES (?, ?, ?)", (embedder.name, hashes[cid], blob))
            cache.commit()
    except Exception:  # 임베딩 실패: 있는 것만
        pass
    finally:
        cache.close()
    return {cid: list(struct.unpack(f"{len(cached[h]) // 4}f", cached[h])) for cid, h in hashes.items() if h in cached}
