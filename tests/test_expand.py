"""LLM 질문 넓히기·후보 이유(why)·low_confidence (가짜 LLM)."""

from pathlib import Path

import pytest

from sasu import db
from sasu.evaluation.everyday_eval import load_set
from sasu.indexer import index_repository
from sasu.rag import expand as expand_mod
from sasu.rag.answer import LLMError
from sasu.rag.expand import QueryExpander, is_targeted, make_expander, parse_terms
from sasu.rag.pipeline import choose_target, is_low_confidence, search_question
from sasu.rag.search import MethodIndex
from test_rag import FILES


class FakeLLM:
    available = True
    model = "fake-haiku"

    def __init__(self, reply="", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[list[dict]] = []

    def chat(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture
def conn(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    c = db.connect(tmp_path / "sasu.db")
    index_repository(c, repo, dialect="oracle")
    yield c
    c.close()


def test_expand_parses_json_and_caches(conn, tmp_path):
    llm = FakeLLM('넓힌 말: ["insert", "Vacation", "insert", "휴가", 3]')
    ex = QueryExpander(llm, cache=tmp_path / "cache")
    question = "휴가 넣는 곳은 어디야?"
    assert ex.expand(conn, question) == ["insert", "Vacation"]  # 중복·질문에 있는 말·문자열 아닌 것은 뺀다
    # 프롬프트에 프로젝트 용어 힌트(업무명=클래스 조각, 자주 나오는 조각)가 들어간다
    user = llm.calls[0][1]["content"]
    assert "자주 나오는 조각" in user and question in user
    assert ex.expand(conn, question) == ["insert", "Vacation"]
    assert len(llm.calls) == 1 and ex.last_cached  # 두 번째는 캐시
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1


def test_expand_failure_or_target_falls_back_quietly(conn, tmp_path):
    failing = QueryExpander(FakeLLM(error=LLMError("시간 초과")), cache=tmp_path / "cache")
    assert failing.expand(conn, "휴가 신청은?") == [] and "시간 초과" in failing.last_error
    assert QueryExpander(FakeLLM("설명만 하고 배열이 없음"), cache=tmp_path / "c2").expand(conn, "휴가?") == []
    # 실패해도 검색은 넓히기 없이 그대로
    index = MethodIndex(conn)
    plain = search_question(conn, "남은 연차가 없으면 신청이 어떻게 돼?", expander=None, index=index)
    fallback = search_question(conn, "남은 연차가 없으면 신청이 어떻게 돼?", expander=failing, index=index)
    assert [c.label for c in fallback.candidates] == [c.label for c in plain.candidates]
    assert fallback.expanded_terms == [] and "넓히기" not in fallback.how
    # 대상을 직접 적은 질문에는 LLM을 부르지 않는다
    llm = FakeLLM('["x"]')
    ex = QueryExpander(llm, cache=tmp_path / "c3")
    for q in ("VacationServiceImpl.apply는 왜 이래?", "/vct/apply.do 는 뭐 해?", "VacationController.java:9 왜?"):
        assert is_targeted(q) and ex.expand(conn, q) == []
    assert llm.calls == [] and not is_targeted("로그인 5번 틀리면 잠겨?")


def test_expanded_terms_raise_candidate_and_show_in_why(conn, tmp_path):
    index = MethodIndex(conn)
    question = "등록은 어디서 해?"
    before = index.bm25(question).get(_mid(index, "VacationDAO.insert"), 0.0)
    after = index.bm25(question, extra_terms=["insert"])[_mid(index, "VacationDAO.insert")]
    assert after > before
    ex = QueryExpander(FakeLLM('["insert", "vacation"]'), cache=tmp_path / "cache")
    r = search_question(conn, question, expander=ex, index=index)
    assert r.expanded_terms == ["insert", "vacation"] and "LLM 질문 넓히기" in r.how
    dao = next(c for c in r.candidates if c.label == "VacationDAO.insert")
    assert dao.why.startswith("맞은 말: ") and "insert" in dao.why


def test_why_and_low_confidence(conn):
    index = MethodIndex(conn)
    r = search_question(conn, "남은 연차가 없으면 신청이 어떻게 돼?", expander=None, index=index)
    assert all(c.why for c in r.candidates)
    why = {c.label: c.why for c in r.candidates}
    assert why["VacationController.apply"].startswith("맞은 말: 신청")
    assert "remain" in why["VacationDAO.selectRemain"]  # 필드 주석 사전(남은 연차 → remain)으로 맞은 말
    # 코드에 답이 없는 일반 개념 질문: 질문 낱말이 코드와 약하게만 맞는다
    general = search_question(conn, "Service와 ServiceImpl은 왜 나눠?", expander=None, index=index)
    assert general.low_confidence
    assert is_low_confidence(5.0, True) and is_low_confidence(30.0, False) and not is_low_confidence(30.0, True)
    # 대상을 직접 고르면 확신 경고·넓힌 말이 없다
    chosen = choose_target(conn, "왜?", target="VacationServiceImpl.apply")
    assert not chosen.low_confidence and chosen.expanded_terms == []


def test_expand_setting(monkeypatch):
    monkeypatch.setattr(expand_mod, "make_expand_client", lambda settings: FakeLLM())
    monkeypatch.setenv("SASU_QUERY_EXPAND", "off")
    assert make_expander() is None
    monkeypatch.setenv("SASU_QUERY_EXPAND", "auto")
    assert make_expander() is not None
    unavailable = FakeLLM()
    unavailable.available = False
    monkeypatch.setattr(expand_mod, "make_expand_client", lambda settings: unavailable)
    assert make_expander() is None  # auto인데 LLM이 없으면 지금 검색 그대로


def test_claude_code_uses_fast_model(monkeypatch):
    from sasu.config import Settings

    settings = Settings(None, "oracle", "", "", "sonnet", "claude-code")
    monkeypatch.delenv("SASU_EXPAND_MODEL", raising=False)
    assert expand_mod.make_expand_client(settings).model == "haiku"
    monkeypatch.setenv("SASU_EXPAND_MODEL", "sonnet")
    assert expand_mod.make_expand_client(settings).model == "sonnet"


def test_parse_terms_and_everyday_set():
    assert parse_terms("```json\n[\"lock\", \"잠금\"]\n```") == ["lock", "잠금"]
    assert parse_terms("[]") == [] and parse_terms("{}") == []
    data = load_set()
    gold = [q for q in data["questions"] if q["gold"]]
    general = [q for q in data["questions"] if not q["gold"]]
    assert 12 <= len(gold) + len(general) <= 20 and 2 <= len(general) <= 3
    assert all("." in g for q in gold for g in q["gold"])


def _mid(index: MethodIndex, label: str) -> int:
    return next(mid for mid, lab in index.labels.items() if lab == label)
