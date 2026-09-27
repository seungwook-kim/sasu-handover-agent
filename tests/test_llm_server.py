"""사내 LLM 서버처럼 키 없이 여는 OpenAI 호환 주소에 실제 HTTP로 붙는다."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sasu.config import Settings
from sasu.rag.answer import LLMClient, LLMError
from sasu.rag.embeddings import ApiEmbedder


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.path.endswith("/chat/completions"):
            reply = {"choices": [{"message": {"content": "[사실] 확인 (E1)"}}]}
        elif self.path.endswith("/embeddings"):
            reply = {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in body["input"]]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


def _settings(base_url: str, api_key: str = "") -> Settings:
    return Settings(db_path=None, sql_dialect="oracle", llm_base_url=base_url, llm_api_key=api_key, llm_model="qwen")


def test_keyless_server(server):
    client = LLMClient(_settings(server))
    assert client.available  # 키가 없어도 주소와 모델이 있으면 쓴다
    assert client.chat([{"role": "user", "content": "안녕"}]) == "[사실] 확인 (E1)"
    request = _Handler.seen[-1]
    assert request["auth"] is None and request["body"]["model"] == "qwen" and request["body"]["temperature"] == 0


def test_key_is_sent_when_given(server):
    LLMClient(_settings(server, api_key="secret")).chat([{"role": "user", "content": "x"}])
    assert _Handler.seen[-1]["auth"] == "Bearer secret"


def test_embeddings_endpoint(server):
    vectors = ApiEmbedder(server, "", "bge-m3").embed(["가", "나"])
    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]] and _Handler.seen[-1]["auth"] is None


def test_unreachable_server_gives_clear_error():
    with pytest.raises(LLMError, match="연결할 수 없음"):
        LLMClient(_settings("http://127.0.0.1:9/v1"), timeout=2).chat([{"role": "user", "content": "x"}])
