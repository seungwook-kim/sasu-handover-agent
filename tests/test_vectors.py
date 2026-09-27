"""벡터 저장소(sqlite-vec)와 하이브리드 검색. 실제 모델 대신 결정적인 가짜 임베딩을 쓴다."""

import hashlib
import math
from pathlib import Path

import pytest

from sasu import db
from sasu.indexer import index_repository
from sasu.rag.search import MethodIndex, _linear, _rrf
from sasu.rag.vectors import build_vector_index, has_vector_index, vector_search

FILES = {
    "src/demo/HolidayService.java": """package demo;
@Service("holidayService")
public class HolidayService {
    /** 공휴일을 등록한다 */
    public void registerHoliday() { save(); }
    /** 공휴일 목록을 조회한다 */
    public void listHoliday() { load(); }
    private void save() {}
    private void load() {}
}
""",
    "src/demo/MailService.java": """package demo;
@Service("mailService")
public class MailService {
    /** 메일을 발송한다 */
    public void sendMail() { deliver(); }
    private void deliver() {}
}
""",
}


class FakeEmbedder:
    """글자 2-gram을 16차원에 해시해서 만든 벡터. 같은 글이면 같은 벡터."""

    name = "fake:bigram16"

    def __init__(self):
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        vectors = []
        for text in texts:
            v = [0.0] * 16
            for i in range(len(text) - 1):
                v[int(hashlib.md5(text[i:i + 2].encode()).hexdigest(), 16) % 16] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        return vectors


@pytest.fixture
def conn(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    connection = db.connect(tmp_path / "sasu.db")
    index_repository(connection, repo, dialect="oracle")
    yield connection
    connection.close()


def test_build_index_and_reuse_cache(conn, tmp_path):
    embedder = FakeEmbedder()
    index = MethodIndex(conn)
    cache = tmp_path / "cache" / "emb.db"
    total, new = build_vector_index(conn, index.documents, embedder, cache_path=cache)
    assert total == new == len(index.documents) > 0
    assert has_vector_index(conn) == "fake:bigram16"

    calls = embedder.calls
    total2, new2 = build_vector_index(conn, index.documents, embedder, cache_path=cache)
    assert (total2, new2) == (total, 0)  # 바뀐 조각이 없으면 다시 임베딩하지 않는다
    assert embedder.calls == calls


def test_vector_search_returns_nearest(conn, tmp_path):
    embedder = FakeEmbedder()
    index = MethodIndex(conn, embedder)
    build_vector_index(conn, index.documents, embedder, cache_path=tmp_path / "emb.db")
    target = next(mid for mid, label in index.labels.items() if label == "MailService.sendMail")
    query_vector = embedder.embed([index.documents[target]])[0]
    nearest, distance = vector_search(conn, query_vector, top=3)[0]
    assert nearest == target and distance < 1e-5


def test_hybrid_mode_is_used_when_vectors_exist(conn, tmp_path):
    embedder = FakeEmbedder()
    index = MethodIndex(conn, embedder)
    assert not index.has_vectors
    build_vector_index(conn, index.documents, embedder, cache_path=tmp_path / "emb.db")
    assert index.has_vectors
    hits = index.search("공휴일 등록", top=2)
    assert hits[0].label == "HolidayService.registerHoliday"
    assert index.search("메일 발송", top=1, mode="vector")  # 벡터만으로도 결과가 나온다


def test_reindex_drops_vector_index(conn, tmp_path):
    embedder = FakeEmbedder()
    build_vector_index(conn, MethodIndex(conn).documents, embedder, cache_path=tmp_path / "emb.db")
    repo = Path(conn.execute("SELECT value FROM meta WHERE key = 'repo_path'").fetchone()[0])
    index_repository(conn, repo, dialect="oracle")  # 메서드 id가 바뀌므로 벡터 색인은 다시 만들어야 한다
    assert has_vector_index(conn) is None


def test_fusion_functions():
    bm25 = {1: 10.0, 2: 5.0}
    vector = {2: 0.9, 3: 0.8}
    assert _linear(bm25, vector, 0.3) == pytest.approx({1: 1.0, 2: 0.5 + 0.27, 3: 0.24})
    fused = _rrf([bm25, vector])
    assert max(fused, key=fused.get) == 2  # 두 검색 모두에 있는 항목이 앞선다
