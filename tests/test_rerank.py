"""LLM 재순위: 후보 풀을 LLM이 고른 순서로, 실패하면 검색 순서 그대로, 설정 (가짜 LLM)."""

import json

from sasu.config import Settings
from sasu.evaluation.everyday_eval import run_variant
from sasu.rag import expand as expand_mod
from sasu.rag import rerank as rerank_mod
from sasu.rag.answer import LLMError
from sasu.rag.expand import QueryExpander
from sasu.rag.pipeline import search_question
from sasu.rag.rerank import LLMReranker, describe, make_reranker, parse_pick, reorder
from sasu.rag.search import MethodIndex
from test_expand import FakeLLM, conn  # noqa: F401  (conn: 분석 파일 fixture)

QUESTION = "남은 연차가 없으면 신청이 어떻게 돼?"


def test_parse_pick_and_reorder():
    assert parse_pick('고름: {"pick": [3, 1, 3, 9, "2", "x"]}', 4) == [2, 0, 1]  # 1부터 → 0부터, 범위 밖·중복 버림
    assert parse_pick("[2, 1]", 3) == [1, 0]  # 배열만 줘도
    assert parse_pick("모르겠어요", 3) == [] and parse_pick('{"pick": "1"}', 3) == []
    assert reorder(["a", "b", "c", "d"], [2, 0]) == ["c", "a", "b", "d"]  # 고른 것 뒤에 나머지를 검색 순서대로
    assert reorder(["a", "b"], None) == ["a", "b"] and reorder(["a", "b"], []) == ["a", "b"]


def test_rerank_puts_llm_pick_first_and_shows_candidates(conn, tmp_path):
    index = MethodIndex(conn)
    plain = search_question(conn, QUESTION, expander=None, index=index, reranker=None)
    pool = [c.label for c in search_question(conn, QUESTION, top=40, expander=None, index=index,
                                             reranker=None, per_class=3).candidates]
    last = len(pool)  # 풀의 마지막 후보를 1위로 고르게 한다
    llm = FakeLLM(json.dumps({"pick": [last, 1]}))
    rr = LLMReranker(llm, cache=tmp_path / "cache")
    r = search_question(conn, QUESTION, expander=None, index=index, reranker=rr)
    assert [c.label for c in r.candidates][:2] == [pool[-1], pool[0]]
    assert len(r.candidates) == len(plain.candidates) and "LLM 재순위" in r.how
    # LLM에는 질문과 후보 설명(Class.method · 한글 설명 · 주석 앞부분 …)을 번호와 함께 준다
    user = llm.calls[0][1]["content"]
    assert QUESTION in user and f"[{last}] {pool[-1]}" in user and "휴가를 신청한다" in user and "URL " in user
    assert describe(conn, r.candidates[0].method_id).startswith(r.candidates[0].label)
    # 같은 질문·후보는 캐시에서
    search_question(conn, QUESTION, expander=None, index=index, reranker=rr)
    assert len(llm.calls) == 1 and rr.last_cached


def test_rerank_failure_keeps_search_order(conn, tmp_path):
    index = MethodIndex(conn)
    # 실패해도 같은 풀(같은 클래스 3개까지)에서 검색 순서 그대로
    pool_order = [c.label for c in search_question(conn, QUESTION, expander=None, index=index, reranker=None,
                                                   per_class=3).candidates]
    for llm in (FakeLLM(error=LLMError("시간 초과")), FakeLLM("형식이 틀린 답")):
        rr = LLMReranker(llm, cache=tmp_path / "cache", use_cache=False)
        r = search_question(conn, QUESTION, expander=None, index=index, reranker=rr)
        assert [c.label for c in r.candidates] == pool_order and rr.last_error
        assert "LLM 재순위" not in r.how
    assert not list((tmp_path / "cache").glob("*.json"))  # use_cache=False면 쓰지 않는다


def test_general_question_skips_rerank(conn, tmp_path):
    index = MethodIndex(conn)
    ex = QueryExpander(FakeLLM('{"kind": "general", "terms": ["generic"]}'), cache=tmp_path / "x", use_cache=False)
    llm = FakeLLM('{"pick": [2]}')
    r = search_question(conn, "제네릭 와일드카드는 언제 써?", expander=ex, index=index,
                        reranker=LLMReranker(llm, use_cache=False))
    assert r.low_confidence and llm.calls == []  # 일반 개념 질문은 어차피 경고, 재순위 호출 없음


def test_rerank_setting(monkeypatch):
    api = Settings(None, "oracle", "", "", "", "anthropic")
    cli = Settings(None, "oracle", "", "", "sonnet", "claude-code")
    monkeypatch.setattr(expand_mod, "make_expand_client", lambda settings: FakeLLM())
    monkeypatch.setenv("SASU_RERANK", "auto")
    monkeypatch.setenv("SASU_EXPAND_PROVIDER", "auto")
    assert make_reranker(api) is not None  # 기본: Claude API(빠른 모델)일 때 켠다
    assert make_reranker(cli) is None  # claude CLI는 호출마다 몇 초라 auto에서는 끈다
    monkeypatch.setenv("SASU_EXPAND_PROVIDER", "anthropic")
    assert make_reranker(cli) is not None  # 넓히기 공급자를 API로 따로 정했으면 켠다
    monkeypatch.setenv("SASU_EXPAND_PROVIDER", "auto")
    monkeypatch.setenv("SASU_RERANK", "llm")
    assert make_reranker(cli) is not None
    monkeypatch.setenv("SASU_RERANK", "off")
    assert make_reranker(api) is None
    monkeypatch.setenv("SASU_RERANK", "auto")
    unavailable = FakeLLM()
    unavailable.available = False
    monkeypatch.setattr(expand_mod, "make_expand_client", lambda settings: unavailable)
    assert make_reranker(api) is None  # LLM이 없으면 검색 그대로
    monkeypatch.setenv("SASU_RERANK_POOL", "12")
    assert rerank_mod.pool_size() == 12
    monkeypatch.setenv("SASU_RERANK_POOL", "많이")
    assert rerank_mod.pool_size() == rerank_mod.DEFAULT_POOL


def test_eval_runner_records_rerank(conn):
    index = MethodIndex(conn)
    questions = [{"id": "Q1", "question": QUESTION, "gold": ["VacationController.apply"]}]
    rr = LLMReranker(FakeLLM('{"pick": [1]}'), use_cache=False)
    run = run_variant(conn, "r", questions, index, None, reranker=rr)
    assert run.cases[0].rank is not None and run.cases[0].rerank_error is None
    assert run.avg_rerank_seconds >= 0.0
    # 재순위를 넘기지 않으면 설정(SASU_RERANK)과 상관없이 재순위 없이 잰다
    assert run_variant(conn, "a", questions, index, None).cases[0].rerank_seconds == 0.0
