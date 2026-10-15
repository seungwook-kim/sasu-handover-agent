"""LLM 질문 넓히기·후보 이유(why)·low_confidence (가짜 LLM)."""

import hashlib
from pathlib import Path

import pytest

from sasu import db
from sasu.evaluation.everyday_eval import load_set, select
from sasu.indexer import index_repository
from sasu.rag import expand as expand_mod
from sasu.rag.answer import LLMError
from sasu.rag.expand import VARIANTS, QueryExpander, is_targeted, make_expander, parse_terms, union_terms
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
    dev = select(data["questions"], "dev")  # split이 없으면 dev (가중치·문턱을 정한 질문)
    gold = [q for q in dev if q["gold"]]
    general = [q for q in dev if not q["gold"]]
    assert 12 <= len(gold) + len(general) <= 20 and 2 <= len(general) <= 3
    assert all("." in g for q in data["questions"] for g in q["gold"])
    assert len({q["id"] for q in data["questions"]}) == len(data["questions"])


class CountingLLM(FakeLLM):
    """부를 때마다 다른 답 (넓히기 흔들림 흉내)."""

    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    def chat(self, messages):
        self.calls.append(messages)
        return self.replies[(len(self.calls) - 1) % len(self.replies)]


def test_default_variant_keeps_old_cache_key(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("SASU_EXPAND_VARIANT", raising=False)
    question = "휴가 넣는 곳은 어디야?"
    ex = QueryExpander(FakeLLM('["insert"]'), cache=tmp_path / "cache")
    assert ex.variant == "default" and ex.prompt == expand_mod.SYSTEM_PROMPT
    project = conn.execute("SELECT value FROM meta WHERE key = 'repo_path'").fetchone()[0]
    old = hashlib.sha256(f"{project}\nfake-haiku\n{expand_mod.SYSTEM_PROMPT}\n{question}".encode("utf-8"))
    assert ex._key(conn, question) == old.hexdigest()[:32]  # 예전 캐시를 그대로 쓴다
    keys = {QueryExpander(FakeLLM(), cache=tmp_path, variant_name=v)._key(conn, question) for v in VARIANTS}
    assert len(keys) == 3  # 변형마다 캐시 키가 다르다
    monkeypatch.setenv("SASU_EXPAND_VARIANT", "strict")
    assert expand_mod.variant() == "strict" and QueryExpander(FakeLLM()).variant == "strict"
    monkeypatch.setenv("SASU_EXPAND_VARIANT", "모름")
    assert expand_mod.variant() == "default"
    with pytest.raises(ValueError):
        QueryExpander(FakeLLM(), variant_name="모름")


def test_strict_variant_uses_fixed_prompt(conn, tmp_path):
    llm = FakeLLM('["Vcatn", "insert", "vacation", "휴가신청"]')
    ex = QueryExpander(llm, cache=tmp_path / "cache", variant_name="strict")
    assert ex.expand(conn, "휴가 넣는 곳은 어디야?") == ["Vcatn", "insert", "vacation", "휴가신청"]
    system = llm.calls[0][0]["content"]
    assert system == expand_mod.STRICT_PROMPT and "6~10개" in system and "예 2" in system
    assert ex.last_weights is None  # strict는 말마다 같은 가중치


def test_union2_merges_two_runs_with_weights(conn, tmp_path):
    assert union_terms([["Login", "lock", "잠금"], ["login", "count"]], (0.7, 0.5)) == (
        ["Login", "lock", "잠금", "count"], {"Login": 0.7, "lock": 0.5, "잠금": 0.5, "count": 0.5})
    assert union_terms([["a"], ["a"]])[1] == {"a": expand_mod.UNION_WEIGHTS[0]}
    llm = CountingLLM(['["insert", "vacation"]', '["insert", "remain"]'])
    ex = QueryExpander(llm, cache=tmp_path / "cache", variant_name="union2")
    question = "휴가 넣는 곳은 어디야?"
    assert ex.expand(conn, question) == ["insert", "vacation", "remain"] and len(llm.calls) == 2
    assert ex.last_weights == {"insert": 0.7, "vacation": 0.5, "remain": 0.5}
    # 캐시에는 두 번의 원래 출력을 두고, 다시 읽어도 같은 합집합·가중치
    assert ex.expand(conn, question) == ["insert", "vacation", "remain"] and ex.last_cached and len(llm.calls) == 2
    assert ex.last_weights == {"insert": 0.7, "vacation": 0.5, "remain": 0.5}
    # 검색은 말별 가중치를 쓴다: 한 번만 나온 말은 기본(0.7)보다 낮게
    index = MethodIndex(conn)
    dao = _mid(index, "VacationDAO.insert")
    q = "등록은 어디서 해?"
    same = index.bm25(q, extra_terms=["insert"])
    assert index.bm25(q, extra_terms=["insert"], extra_weights={}) == same  # 가중치가 없는 말은 기본값
    assert index.bm25(q, extra_terms=["insert"], extra_weights={"insert": 0.5})[dao] < same[dao]
    r = search_question(conn, q, expander=QueryExpander(CountingLLM(['["insert"]', '["delete"]']),
                                                        cache=tmp_path / "c2", variant_name="union2"), index=index)
    assert r.expanded_terms == ["insert", "delete"]


def test_no_cache_neither_reads_nor_writes(conn, tmp_path):
    cache = tmp_path / "cache"
    QueryExpander(FakeLLM('["old"]'), cache=cache).expand(conn, "휴가 넣는 곳은 어디야?")
    before = {p: p.read_text(encoding="utf-8") for p in cache.glob("*.json")}
    llm = FakeLLM('["new"]')
    ex = QueryExpander(llm, cache=cache, use_cache=False)
    assert ex.expand(conn, "휴가 넣는 곳은 어디야?") == ["new"] and not ex.last_cached and len(llm.calls) == 1
    ex.expand(conn, "연차는 어디서 봐?")
    assert {p: p.read_text(encoding="utf-8") for p in cache.glob("*.json")} == before


def _mid(index: MethodIndex, label: str) -> int:
    return next(mid for mid, lab in index.labels.items() if lab == label)


def test_expand_returns_question_kind_in_same_call(conn, tmp_path):
    from sasu.rag.expand import parse_output

    assert parse_output('{"kind": "general", "terms": ["pool", "커넥션"]}', "커넥션 풀은 왜 써?") == (["pool"], "general")
    assert parse_output('```json\n{"kind":"CODE","terms":["lock"]}\n```') == (["lock"], "code")
    assert parse_output('["lock"]') == (["lock"], None)  # 예전 형식(배열)은 종류 없음
    assert parse_output('{"kind": "몰라", "terms": "lock"}') == ([], None)
    llm = FakeLLM('{"kind": "general", "terms": ["Session", "Cookie"]}')
    ex = QueryExpander(llm, cache=tmp_path / "cache")
    assert ex.expand(conn, "세션이랑 쿠키는 뭐가 달라?") == ["Session", "Cookie"] and ex.last_kind == "general"
    assert "general" in llm.calls[0][0]["content"]  # 한 번의 호출에서 종류도 받는다
    # 캐시에도 종류를 둔다
    ex.expand(conn, "세션이랑 쿠키는 뭐가 달라?")
    assert ex.last_cached and ex.last_kind == "general" and len(llm.calls) == 1
    failing = QueryExpander(FakeLLM(error=LLMError("시간 초과")), cache=tmp_path / "c2")
    assert failing.expand(conn, "휴가 신청은?") == [] and failing.last_kind is None
    # union2: 둘 다 general일 때만 general
    two = QueryExpander(CountingLLM(['{"kind":"general","terms":["a"]}', '{"kind":"code","terms":["b"]}']),
                        cache=tmp_path / "c3", variant_name="union2")
    two.expand(conn, "휴가 넣는 곳은 어디야?")
    assert two.last_kind == "code"


def test_low_confidence_uses_llm_kind_then_bm25_rule(conn, tmp_path):
    from sasu.rag.pipeline import code_verbs

    assert is_low_confidence(30.0, True, "general")
    assert not is_low_confidence(30.0, True, "code") and not is_low_confidence(30.0, True, None)
    assert not is_low_confidence(5.0, False, "code") and is_low_confidence(5.0, True, None)  # 판정이 있으면 그것만
    index = MethodIndex(conn)
    question = "남은 연차가 없으면 신청이 어떻게 돼?"
    plain = search_question(conn, question, expander=None, index=index)
    assert plain.question_kind is None  # LLM이 없으면 BM25 규칙 (작은 예제라 점수는 문턱 아래)
    general = QueryExpander(FakeLLM('{"kind":"general","terms":["vacation"]}'), cache=tmp_path / "cache")
    r = search_question(conn, question, expander=general, index=index)
    assert r.question_kind == "general" and r.low_confidence
    # 질문의 동작 말 중 LLM이 빠뜨린 코드 동사만 더한다 (expanded_terms에는 LLM 출력만)
    assert code_verbs("비밀번호 바꿀 때 틀리면?", ["password", "change"]) == ["update"]
    assert code_verbs("글 지우면 진짜 지워져?", ["Delete"]) == []
    assert code_verbs("휴가 신청 목록", ["x"]) == ["insert", "select"]
    assert r.expanded_terms == ["vacation"]


def test_candidates_fill_up_when_few_classes(conn):
    index = MethodIndex(conn)
    one = search_question(conn, "남은 연차가 없으면 신청이 어떻게 돼?", expander=None, index=index, per_class=1)
    two = search_question(conn, "남은 연차가 없으면 신청이 어떻게 돼?", expander=None, index=index, per_class=2)
    # 클래스 제한으로 자리가 남으면 뺀 후보로 채운다 (같은 클래스가 더 나와도 후보 수는 줄지 않는다)
    pool = len(index.search("남은 연차가 없으면 신청이 어떻게 돼?", top=30))
    assert len(one.candidates) == len(two.candidates) == min(6, pool)
