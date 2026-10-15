"""Claude API 공급자(anthropic)와 웹 묻기 색인 메모리 캐시. SDK 호출은 가짜로 바꿔 네트워크 없이 확인한다."""

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sasu import db
from sasu.config import Settings, load_settings
from sasu.indexer import index_repository
from sasu.rag import expand as expand_mod
from sasu.rag.answer import AnthropicClient, ClaudeCodeClient, LLMClient, LLMError, make_llm_client
from sasu.rag.pipeline import embedder_or_none
from sasu.web import server
from test_python import FASTAPI

anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402  (anthropic 1.x가 함께 설치한다)


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.calls: list[dict] = []
        self.response = response
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _response(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(**b) for b in blocks], stop_reason=stop_reason)


def _client(response=None, error=None, **kwargs) -> tuple[AnthropicClient, FakeMessages]:
    client = AnthropicClient("test-key", **kwargs)
    messages = FakeMessages(response, error)
    client._client = SimpleNamespace(messages=messages)  # SDK 대신
    return client, messages


def _settings(provider: str, model: str = "", key: str = "test-key") -> Settings:
    return Settings(None, "oracle", "", "", model, provider, anthropic_api_key=key)


def test_chat_sends_system_separately_without_temperature():
    client, fake = _client(_response({"type": "thinking", "thinking": ""}, {"type": "text", "text": "[\"login\"]"}),
                           model="haiku", effort="low")
    out = client.chat([{"role": "system", "content": "규칙"}, {"role": "user", "content": "질문"}])
    assert out == "[\"login\"]"  # thinking 블록은 건너뛰고 글만
    sent = fake.calls[0]
    assert sent["model"] == "claude-haiku-5-5"  # claude CLI 짧은 이름 → API 모델 ID
    assert sent["system"] == "규칙" and sent["messages"] == [{"role": "user", "content": "질문"}]
    assert sent["output_config"] == {"effort": "low"}
    # Haiku 5.5·Opus 5.5는 기본값이 아닌 temperature를 400으로 거절한다 → 보내지 않는다
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(sent)


def test_thinking_option_and_default_model():
    client, fake = _client(_response({"type": "text", "text": "ok"}), thinking="disabled")
    client.chat([{"role": "user", "content": "q"}])
    assert fake.calls[0]["thinking"] == {"type": "disabled"} and "system" not in fake.calls[0]
    assert AnthropicClient("k").model == "claude-opus-5-5"
    assert AnthropicClient("k", "claude-sonnet-5-5").model == "claude-sonnet-5-5"


def test_errors_become_llm_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.APIStatusError("overloaded", response=httpx2.Response(529, request=request), body=None)
    for error, text in ((overloaded, "529"), (anthropic.APIConnectionError(request=request), "연결")):
        client, _ = _client(error=error)
        with pytest.raises(LLMError, match=text):
            client.chat([{"role": "user", "content": "q"}])
    client, _ = _client(_response(stop_reason="refusal"))
    with pytest.raises(LLMError, match="refusal"):
        client.chat([{"role": "user", "content": "q"}])
    client, _ = _client(_response({"type": "thinking", "thinking": ""}, stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="max_tokens"):
        client.chat([{"role": "user", "content": "q"}])


def test_available_needs_key_and_key_stays_out_of_repr(monkeypatch):
    assert AnthropicClient("").available is False
    assert AnthropicClient("k").available is True
    monkeypatch.setenv("SASU_ANTHROPIC_API_KEY", "  secret-value  ")
    settings = load_settings()
    assert settings.anthropic_api_key == "secret-value"
    assert "secret-value" not in repr(settings)
    assert "ANTHROPIC_API_KEY" not in os.environ  # SDK 기본 이름으로 내보내지 않는다 (claude CLI 과금 방식)


def test_make_llm_client_providers():
    client = make_llm_client(_settings("anthropic", "sonnet"))
    assert isinstance(client, AnthropicClient) and client.model == "claude-sonnet-5-5"
    assert isinstance(make_llm_client(_settings("claude-code")), ClaudeCodeClient)
    assert isinstance(make_llm_client(_settings("openai")), LLMClient)


def test_expand_provider_switch(monkeypatch):
    monkeypatch.delenv("SASU_EXPAND_MODEL", raising=False)
    # auto: 답변 공급자를 따른다
    client = expand_mod.make_expand_client(_settings("anthropic", "sonnet"))
    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-haiku-5-5" and client.effort == "low"
    assert isinstance(expand_mod.make_expand_client(_settings("claude-code")), ClaudeCodeClient)
    # 답변은 claude CLI 그대로, 넓히기만 API로
    monkeypatch.setenv("SASU_EXPAND_PROVIDER", "anthropic")
    client = expand_mod.make_expand_client(_settings("claude-code", "sonnet"))
    assert isinstance(client, AnthropicClient) and client.model == "claude-haiku-5-5"
    monkeypatch.setenv("SASU_EXPAND_MODEL", "haiku")
    assert expand_mod.make_expand_client(_settings("claude-code")).model == "claude-haiku-5-5"
    monkeypatch.setenv("SASU_EXPAND_PROVIDER", "claude-code")
    assert isinstance(expand_mod.make_expand_client(_settings("anthropic")), ClaudeCodeClient)


def test_expander_works_through_api_client(tmp_path):
    conn = db.connect(tmp_path / "e.db")
    client, fake = _client(_response({"type": "text", "text": "[\"login\",\"lock\"]"}), model="claude-haiku-5-5")
    expander = expand_mod.QueryExpander(client, cache=tmp_path / "cache")
    terms = expander.expand(conn, "로그인 5번 틀리면 잠겨?")
    assert "login" in terms and "lock" in terms
    assert fake.calls[0]["model"] == "claude-haiku-5-5"
    assert len(fake.calls) == 1


# ── 웹 묻기: 검색 색인을 메모리에 둔다 ─────────────────────


@pytest.fixture
def web(tmp_path, monkeypatch):
    repo = tmp_path / "shop"
    for rel, content in FASTAPI.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    db_path = tmp_path / "shop.db"
    conn = db.connect(db_path)
    index_repository(conn, repo, dialect="oracle")
    conn.close()
    monkeypatch.setenv("SASU_DB_PATH", str(db_path))
    embedder_or_none.cache_clear()
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    app = server.create_app()
    return TestClient(app), app.state.ask_cache, db_path


def test_ask_index_is_built_once_and_rebuilt_when_db_changes(web):
    client, cache, db_path = web
    body = {"question": "주문 조회는 어떻게 해?", "use_git": False}
    first = client.post("/api/ask/evidence", json=body).json()
    second = client.post("/api/ask/evidence", json=body).json()
    assert cache.builds == 1  # 두 번째 요청은 메모리의 색인을 쓴다
    assert first["candidates"] == second["candidates"] and first["candidates"]
    # 다시 분석하거나 답변을 저장하면 분석 파일이 바뀐다 → 다음 요청에서 다시 만든다
    stat = db_path.stat()
    os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    third = client.post("/api/ask/evidence", json=body).json()
    assert cache.builds == 2 and third["candidates"] == first["candidates"]


def test_ask_cache_follows_project_switch(web, tmp_path, monkeypatch):
    client, cache, db_path = web
    body = {"question": "주문 조회는 어떻게 해?", "use_git": False}
    client.post("/api/ask/evidence", json=body)
    other = tmp_path / "other.db"
    other.write_bytes(db_path.read_bytes())
    monkeypatch.setenv("SASU_DB_PATH", str(other))  # 다른 분석 파일 (프로젝트 전환과 같다)
    assert client.post("/api/ask/evidence", json=body).json()["candidates"]
    assert cache.builds == 2
