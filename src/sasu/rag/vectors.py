"""벡터 저장소: sqlite-vec로 메서드 조각의 임베딩을 같은 SQLite 파일에 넣는다.

- 임베딩 캐시는 따로 둔 파일(data/cache/embeddings.db)에 (모델, 조각 해시)로 저장한다.
  코드가 안 바뀐 조각은 다시 분석해도, 평가용 과거 시점 색인에서도 다시 임베딩하지 않는다.
- 벡터 색인(vec_methods)은 분석 결과에서 파생된 것이라 필요할 때 캐시로 다시 만든다.
"""

import hashlib
import sqlite3
import struct
from collections.abc import Callable
from pathlib import Path

from sasu.rag.embeddings import Embedder



def load_extension(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except (ImportError, AttributeError, sqlite3.OperationalError):
        return False


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cache(path: Path | None) -> sqlite3.Connection:
    """임베딩 캐시는 프로젝트와 상관없이 하나 (같은 조각은 다시 임베딩하지 않는다)."""
    if path is None:
        from sasu.config import cache_dir

        path = cache_dir() / "embeddings.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS embedding_cache (
               model TEXT NOT NULL, text_hash TEXT NOT NULL, vector BLOB NOT NULL,
               PRIMARY KEY (model, text_hash))"""
    )
    return conn


def build_vector_index(
    conn: sqlite3.Connection,
    documents: dict[int, str],
    embedder: Embedder,
    cache_path: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """메서드 조각을 임베딩해 vec_methods에 넣는다. 돌려주는 값: (전체 조각 수, 새로 임베딩한 수)."""
    if not load_extension(conn):
        raise RuntimeError("sqlite-vec를 불러올 수 없습니다 (uv sync).")
    cache = _cache(cache_path)
    hashes = {mid: hashlib.sha1(text.encode("utf-8")).hexdigest() for mid, text in documents.items()}
    cached = {
        h: v for h, v in cache.execute(
            "SELECT text_hash, vector FROM embedding_cache WHERE model = ?", (embedder.name,)
        )
    }
    missing = [mid for mid, h in hashes.items() if h not in cached]
    batch = 256
    for i in range(0, len(missing), batch):
        ids = missing[i:i + batch]
        vectors = embedder.embed([documents[mid] for mid in ids])
        rows = [(embedder.name, hashes[mid], _pack(v)) for mid, v in zip(ids, vectors)]
        cache.executemany("INSERT OR REPLACE INTO embedding_cache VALUES (?, ?, ?)", rows)
        cache.commit()
        cached.update({hashes[mid]: _pack(v) for mid, v in zip(ids, vectors)})
        if progress:
            progress(min(i + batch, len(missing)), len(missing))
    cache.close()

    dim = len(_unpack(next(iter(cached.values())))) if cached else 0
    conn.execute("DROP TABLE IF EXISTS vec_methods")
    conn.execute(f"CREATE VIRTUAL TABLE vec_methods USING vec0(method_id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)")
    conn.executemany(
        "INSERT INTO vec_methods (method_id, embedding) VALUES (?, ?)",
        [(mid, cached[h]) for mid, h in hashes.items()],
    )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('embed_model', ?)", (embedder.name,))
    conn.commit()
    return len(documents), len(missing)


def has_vector_index(conn: sqlite3.Connection) -> str | None:
    """벡터 색인이 있으면 만든 임베딩 모델 이름."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_methods'").fetchone()
    row = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
    return row[0] if exists and row else None


def vector_search(conn: sqlite3.Connection, query_vector: list[float], top: int = 50) -> list[tuple[int, float]]:
    """(method_id, 코사인 거리) 가까운 순."""
    load_extension(conn)
    rows = conn.execute(
        "SELECT method_id, distance FROM vec_methods WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (_pack(query_vector), top),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
