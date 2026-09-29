"""MCP 서버: 에이전트가 도구로 부르는 흐름 (메모리 연결 + 실제 stdio 하위 프로세스)."""

import os
import sys

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from sasu import db
from sasu.config import set_current_project
from sasu.indexer import index_repository
from test_python import FASTAPI


@pytest.fixture
def project(tmp_path, monkeypatch):
    """FastAPI 예제를 SASU_HOME/projects/shop.db로 분석해 지금 프로젝트로 둔다."""
    monkeypatch.delenv("SASU_DB_PATH", raising=False)
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    repo = tmp_path / "shop"
    for rel, content in FASTAPI.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    home = tmp_path / "sasu-home"
    conn = db.connect(home / "projects" / "shop.db")
    index_repository(conn, repo, dialect="oracle")
    conn.close()
    set_current_project("shop")
    return home


def _text(result) -> str:
    assert not result.is_error, result.content
    return "\n".join(c.text for c in result.content)


def test_tools_in_memory(project):
    from sasu.mcp_server import server

    async def run():
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"gather_evidence", "trace_url", "handover_questions", "record_answer"} <= names

            assert "* shop" in _text(await client.call_tool("list_projects", {}))
            assert "fastapi" in _text(await client.call_tool("project_overview", {}))

            trace = _text(await client.call_tool("trace_url", {"url": "/{settings.API_V1_STR}/orders/{order_id}"}))
            assert "crud.get_order" in trace and "TB_ORDER" in trace

            questions = _text(await client.call_tool("handover_questions", {"kind": "magic_value"}))
            qid = int(questions.split("\nQ", 1)[1].split(" ", 1)[0])
            saved = _text(await client.call_tool("record_answer", {"question_id": qid, "answer": "키오스크는 결제 단말이 다름"}))
            assert "저장" in saved

            evidence = _text(await client.call_tool("gather_evidence", {"question": "키오스크 주문은 왜 따로 처리해?", "use_git": False}))
            assert "대상: orders.create_order" in evidence
            assert "(인수인계 답변)" in evidence and "키오스크는 결제 단말이 다름" in evidence

            code = _text(await client.call_tool("read_code", {"path": "backend/app/crud.py", "start": 1, "end": 10}))
            assert "def get_order" in code
            outside = await client.call_tool("read_code", {"path": "../../etc/passwd"})
            assert outside.is_error

            missing = await client.call_tool("project_overview", {"project": "없음"})
            assert missing.is_error

    anyio.run(run)


def test_stdio_subprocess(project):
    """에이전트가 실제로 하는 것처럼 `python -m sasu mcp`를 띄워 stdio로 대화한다."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "sasu", "mcp"],
        env={"SASU_HOME": str(project), "SASU_EMBED_PROVIDER": "none", "PYTHONIOENCODING": "utf-8",
             "PATH": os.environ.get("PATH", "")},
    )

    async def run():
        async with Client(params) as client:
            result = await client.call_tool("search_code", {"question": "주문 조회"})
            assert "orders.read_order" in _text(result) or "crud.get_order" in _text(result)

    anyio.run(run)


def test_existing_behaviors_tool(project):
    from sasu.mcp_server import server

    async def run():
        async with Client(server) as client:
            text = _text(await client.call_tool("existing_behaviors", {"url": "orders"}))
            assert "URL 2개" in text and "쓰기: ORDER_HISTORY(등록)" in text and "분기: 'KIOSK'" in text

    anyio.run(run)


def test_bare_method_name_target(project):
    """에이전트가 메서드 이름만 넘겨도 찾는다. 같은 이름이 여럿이면 나머지를 후보로 알려 준다."""
    from sasu.mcp_server import server

    async def run():
        async with Client(server) as client:
            one = _text(await client.call_tool("gather_evidence", {"question": "왜?", "target": "read_order", "use_git": False}))
            assert "대상: orders.read_order" in one and "다른 후보" not in one
            many = _text(await client.call_tool("gather_evidence", {"question": "왜?", "target": "insert", "use_git": False}))
            assert "대상: OrderRepository.insert" in many

    anyio.run(run)


def test_read_code_by_method_name(project):
    from sasu.mcp_server import server

    async def run():
        async with Client(server) as client:
            text = _text(await client.call_tool("read_code", {"target": "crud.get_order"}))
            assert text.startswith("backend/app/crud.py") and "def get_order" in text
            assert (await client.call_tool("read_code", {})).is_error

    anyio.run(run)
