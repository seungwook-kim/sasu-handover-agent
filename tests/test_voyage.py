"""Voyage 임베딩·재순위, 섞는 방식(minmax), 임베딩 조각의 원문 코드, 긴 경로·읽기 실패, Python 평가 정답.
네트워크는 가짜 urlopen으로 막는다."""

import io
import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest
from test_vectors import FILES, FakeEmbedder, conn  # noqa: F401  (conn: 분석 파일 fixture)

from sasu import db, indexer
from sasu.config import Settings
from sasu.evaluation.history import fix_commit_methods
from sasu.indexer import _long_path, index_repository
from sasu.rag import rerank as rerank_mod
from sasu.rag.embeddings import (
    VOYAGE_URL,
    ApiEmbedder,
    EmbeddingError,
    embed_with_code,
    hybrid_defaults,
    make_embedder,
)
from sasu.rag.rerank import LLMReranker, VoyageReranker, make_reranker
from sasu.rag.search import MethodIndex, _minmax
from sasu.rag.source import SourceReader


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_http(monkeypatch):
    """보낸 요청을 모으고, 경로별로 정한 응답을 준다."""
    sent: list[tuple[str, dict]] = []
    replies: dict[str, object] = {}

    def urlopen(request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        sent.append((request.full_url, body))
        reply = replies[request.full_url.rsplit("/", 1)[-1]]
        if isinstance(reply, Exception):
            raise reply
        data = reply(body) if callable(reply) else reply
        return FakeResponse(json.dumps(data).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent, replies


def _settings() -> Settings:
    return Settings(llm_provider="openai", llm_base_url="http://llm.local/v1", llm_api_key="", llm_model="",
                    db_path=Path("x.db"), sql_dialect="oracle")


def test_minmax_fusion_puts_both_on_same_scale():
    bm25 = {1: 20.0, 2: 10.0, 3: 0.0}
    vector = {2: 0.9, 3: 0.5, 4: 0.7}
    fused = _minmax(bm25, vector, 0.8)
    assert fused[1] == pytest.approx(0.2)  # BM25 1위지만 벡터에 없음
    assert fused[2] == pytest.approx(0.2 * 0.5 + 0.8 * 1.0)  # 벡터 1위
    assert fused[4] == pytest.approx(0.8 * 0.5)  # BM25에 없음
    assert max(fused, key=fused.get) == 2
    assert _minmax({}, {}, 0.5) == {}


def test_hybrid_defaults_follow_embedder_and_env(monkeypatch):
    monkeypatch.delenv("SASU_HYBRID_FUSION", raising=False)
    monkeypatch.delenv("SASU_VECTOR_WEIGHT", raising=False)
    assert hybrid_defaults(FakeEmbedder()) == ("linear", 0.3)  # 로컬·가짜: 예전 그대로
    api = ApiEmbedder(VOYAGE_URL, "k", "voyage-code-3")
    assert hybrid_defaults(api) == ("minmax", 0.8)
    monkeypatch.setenv("SASU_VECTOR_WEIGHT", "0.6")
    assert hybrid_defaults(api) == ("minmax", 0.6)
    monkeypatch.setenv("SASU_HYBRID_FUSION", "linear")
    monkeypatch.setenv("SASU_VECTOR_WEIGHT", "abc")  # 틀린 값은 기본값
    assert hybrid_defaults(api) == ("linear", 0.3)


def test_embed_with_code_setting(monkeypatch):
    api, local = ApiEmbedder(VOYAGE_URL, "k", "m"), FakeEmbedder()
    monkeypatch.delenv("SASU_EMBED_CODE", raising=False)
    assert embed_with_code(api) and not embed_with_code(local)
    monkeypatch.setenv("SASU_EMBED_CODE", "on")
    assert embed_with_code(local)
    monkeypatch.setenv("SASU_EMBED_CODE", "off")
    assert not embed_with_code(api)


def test_query_embedding_adds_input_type_only_for_voyage(fake_http):
    sent, replies = fake_http
    replies["embeddings"] = lambda body: {"data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(body["input"])]}
    ApiEmbedder(VOYAGE_URL, "k", "voyage-code-3").embed_query("게시판 목록")
    ApiEmbedder(VOYAGE_URL, "k", "voyage-code-3").embed(["조각"])
    ApiEmbedder("http://intra.local/v1", "", "bge").embed_query("질문")
    assert sent[0][1]["input_type"] == "query"
    assert "input_type" not in sent[1][1]  # 조각은 그대로 (캐시와 같은 벡터)
    assert "input_type" not in sent[2][1]  # 다른 OpenAI 호환 서버는 모르는 필드를 거부할 수 있다


def test_make_embedder_voyage(monkeypatch):
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "voyage")
    monkeypatch.delenv("SASU_EMBED_MODEL", raising=False)
    monkeypatch.delenv("SASU_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("SASU_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(EmbeddingError):
        make_embedder(_settings())
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    e = make_embedder(_settings())
    assert isinstance(e, ApiEmbedder) and e.base_url == VOYAGE_URL and e.model == "voyage-code-3"


def test_embedding_documents_append_source_code(conn):  # noqa: F811
    index = MethodIndex(conn)
    plain = index.embedding_documents(False)
    with_code = index.embedding_documents(True)
    assert plain == index.documents
    mid = next(m for m, label in index.labels.items() if label.endswith("HolidayService.registerHoliday"))
    assert with_code[mid].startswith(plain[mid])
    assert "public void registerHoliday() { save(); }" in with_code[mid]


def test_source_reader_without_repo_returns_empty(conn, tmp_path):  # noqa: F811
    conn.execute("UPDATE meta SET value = ? WHERE key = 'repo_path'", (str(tmp_path / "moved"),))
    read = SourceReader(conn)
    assert read.root is None and read(1) == ""
    assert MethodIndex(conn).embedding_documents(True) == MethodIndex(conn).documents  # 못 읽으면 요약만


def test_voyage_reranker_orders_by_score_caches_and_falls_back(conn, fake_http, tmp_path):  # noqa: F811
    sent, replies = fake_http
    index = MethodIndex(conn)
    hits = index.search("공휴일 메일", top=4, mode="bm25")
    assert len(hits) >= 3
    replies["rerank"] = lambda body: {"data": [{"index": i, "relevance_score": 0.1 * i} for i in range(len(body["documents"]))]}
    rr = VoyageReranker("pa-test", cache=tmp_path / "rr")
    out = rr.rerank(conn, "공휴일 메일", hits)
    assert [h.method_id for h in out] == [h.method_id for h in reversed(hits)]  # 점수가 큰 마지막 후보가 1위
    body = sent[-1][1]
    assert body["model"] == "rerank-2.5" and body["query"] == "공휴일 메일"
    assert any("save();" in d for d in body["documents"])  # 후보 글에 원문 코드
    rr.rerank(conn, "공휴일 메일", hits)
    assert len(sent) == 1 and rr.last_cached  # 같은 질문·후보는 캐시
    replies["rerank"] = OSError("network down")
    fresh = VoyageReranker("pa-test", cache=tmp_path / "rr2")
    assert fresh.rerank(conn, "공휴일 메일", hits) == hits and "network down" in fresh.last_error


def test_make_reranker_picks_voyage(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.delenv("SASU_EMBED_API_KEY", raising=False)
    monkeypatch.setenv("SASU_RERANK", "voyage")
    assert isinstance(make_reranker(_settings()), VoyageReranker)
    monkeypatch.setenv("SASU_RERANK", "auto")  # 임베딩이 Voyage여도 auto로는 켜지 않는다 (본체 평가에서 손해)
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "voyage")
    assert make_reranker(_settings()) is None
    monkeypatch.setenv("SASU_RERANK", "off")
    assert make_reranker(_settings()) is None
    assert rerank_mod.mode() == "off" and LLMReranker  # 기존 LLM 재순위는 그대로 있다


def test_index_skips_unreadable_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    real = indexer._index_java

    def flaky(conn, path, rel, stats):  # noqa: F811
        if rel.endswith("MailService.java"):
            raise OSError("경로가 너무 김")
        return real(conn, path, rel, stats)

    monkeypatch.setattr(indexer, "_index_java", flaky)
    c = db.connect(tmp_path / "s.db")
    stats = index_repository(c, repo, dialect="oracle")
    assert stats.skipped == ["src/demo/MailService.java"]
    names = {r[0] for r in c.execute("SELECT name FROM classes")}
    assert "HolidayService" in names and "MailService" not in names  # 나머지는 분석됨
    assert c.execute("SELECT value FROM meta WHERE key = 'repo_path'").fetchone()[0] == str(repo.resolve())
    c.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows 긴 경로 접두어")
def test_long_path_prefix_on_windows():
    p = _long_path(Path("C:/work/repo"))
    assert str(p).startswith("\\\\?\\") and str(_long_path(p)) == str(p)  # 두 번 붙이지 않는다


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def test_fix_commit_methods_reads_python(tmp_path):
    repo = tmp_path / "py"
    (repo / "shop").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    views = repo / "shop" / "views.py"
    views.write_text("def list_items(request):\n    return 1\n\n\nclass Cart:\n    def add(self, x):\n        return x\n",
                     encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD").strip()
    views.write_text("def list_items(request):\n    return 2\n\n\nclass Cart:\n    def add(self, x):\n        return x + 1\n",
                     encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: 장바구니 담기 오류 수정")
    [(_, subject, keys)] = fix_commit_methods(repo, base)
    assert subject.startswith("fix:") and keys == {"views.list_items", "Cart.add"}
