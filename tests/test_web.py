"""웹 화면 API: 작은 FastAPI 프로젝트를 분석해 두고 화면이 부르는 API를 차례로 부른다."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from sasu import db
from sasu.indexer import index_repository
from sasu.rag.pipeline import embedder_or_none
from sasu.web import server
from test_python import FASTAPI


class FakeClient:
    available = True
    model = "fake"

    def chat(self, messages):
        return "[사실] 주문은 crud.get_order로 조회한다 (E1)"


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    monkeypatch.setattr("sasu.rag.answer.make_llm_client", lambda settings: FakeClient())
    embedder_or_none.cache_clear()
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    return TestClient(server.create_app())


def test_index_page_and_static_files(client):
    page = client.get("/")
    assert page.status_code == 200 and "사수" in page.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    # 번들한 글꼴은 인터넷 없이 이 서버에서 나가고, 글꼴 MIME으로 나간다
    font = client.get("/fonts/Pretendard-Regular.subset.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
    assert client.get("/favicon.svg").status_code == 200


def test_overview_and_trace(client):
    ov = client.get("/api/overview").json()
    assert ov["build"]["project"] == "shop"
    assert ov["counts"]["URL"] == 2
    urls = client.get("/api/urls", params={"q": "orders"}).json()
    assert urls["total"] == 2
    # 일부만 적으면 후보를 돌려준다
    assert client.get("/api/trace", params={"url": "orders"}).json()["url"] is None
    data = client.get("/api/trace", params={"url": "/{settings.API_V1_STR}/orders/{order_id}"}).json()
    labels = []

    def walk(node):
        labels.append(node["label"])
        for child in node["children"]:
            walk(child)

    walk(data["tree"])
    assert "crud.get_order" in labels and "SELECT ORM Order" in labels


def test_code_view_stays_inside_repo(client):
    code = client.get("/api/code", params={"path": "backend/app/crud.py", "start": 5}).json()
    assert any("def get_order" in line for line in code["lines"])
    assert client.get("/api/code", params={"path": "../shop.db"}).status_code == 404


def test_questions_answer_draft_and_excel_round_trip(client):
    data = client.get("/api/questions").json()
    assert data["total"] >= 2 and data["answered"] == 0
    q = next(x for x in data["items"] if x["kind"] == "magic_value")

    saved = client.post(f"/api/questions/{q['id']}/answer", json={"answer": "키오스크 주문은 결제 방식이 달라서", "by": "김개발"})
    assert saved.json()["status"] == "answered"
    assert client.get("/api/questions").json()["answered"] == 1
    assert "■ 질문" in client.get(f"/api/questions/{q['id']}/draft").json()["message"]

    # 엑셀로 내보내고, 다른 답을 적어 다시 가져온다
    sheet = client.get("/api/questions/export")
    assert sheet.status_code == 200
    wb = load_workbook(io.BytesIO(sheet.content))
    ws = wb.active
    header = [c.value for c in ws[1]]
    answer_col = next(i for i, v in enumerate(header, 1) if v and "답" in str(v) and "사람" not in str(v))
    ws.cell(row=2, column=answer_col, value="엑셀로 받은 답")
    buf = io.BytesIO()
    wb.save(buf)
    imported = client.post("/api/questions/import", files={"file": ("q.xlsx", buf.getvalue())}).json()
    assert imported["saved"] >= 1

    # 빈 답을 저장하면 다시 물어볼 질문이 된다
    cleared = client.post(f"/api/questions/{q['id']}/answer", json={"answer": " "}).json()
    assert cleared["status"] == "open"


def test_ask_evidence_then_answer(client):
    body = {"question": "주문 조회는 어떻게 해?", "target": "orders.read_order", "use_git": False}
    ev = client.post("/api/ask/evidence", json=body).json()
    assert ev["target"]["label"] == "orders.read_order"
    assert ev["evidence"][0]["id"] == "E1"
    # 대상을 직접 정하면 검색하지 않으니 확신 경고·넓힌 말이 없다
    assert ev["low_confidence"] is False and ev["expanded_terms"] == []
    ans = client.post("/api/ask/answer", json=body).json()
    assert ans["sentences"][0]["label"] == "사실" and ans["sentences"][0]["citations"] == ["E1"]


def test_cleanup_and_projects(client):
    unused = client.get("/api/unused").json()
    assert [s["kind"] for s in unused["sections"]] == ["broken_sql", "sql", "url", "method", "jsp"]
    common = client.get("/api/common").json()
    assert set(common) == {"shared", "clones", "clone_stats", "duplicate_sql"}
    assert client.post("/api/projects/current", json={"name": "없는-프로젝트"}).status_code == 404


def test_no_project_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SASU_DB_PATH", str(tmp_path / "missing.db"))
    res = TestClient(server.create_app()).get("/api/overview")
    assert res.status_code == 409 and res.json()["code"] == "no_project"


def test_behaviors_export(client):
    res = client.get("/api/behaviors/export")
    assert res.status_code == 200
    ws = load_workbook(io.BytesIO(res.content)).active
    assert ws.max_row == 3 and ws.cell(row=1, column=3).value == "URL"  # FastAPI 예제의 URL 2개


def test_ask_evidence_free_question_has_why_and_confidence(client, monkeypatch):
    class FakeExpander:
        last_seconds = 0.0

        def expand(self, conn, question):
            return ["order", "read"]

    monkeypatch.setattr("sasu.rag.expand.make_expander", lambda settings=None: FakeExpander())
    ev = client.post("/api/ask/evidence", json={"question": "주문 조회는 어떻게 해?", "use_git": False}).json()
    assert ev["auto"] is True and ev["candidates"]
    assert isinstance(ev["low_confidence"], bool)
    assert ev["expanded_terms"] == ["order", "read"]
    assert all(c["why"] for c in ev["candidates"])
