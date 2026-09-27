"""인수인계 한 바퀴를 명령어로 그대로 따라간다.

떠나는 개발자: 질문 목록을 받고 답을 적는다 → 새 담당자: 코드 이름을 모른 채 말로 묻는다 →
사수가 그 답을 근거로 찾아 [답변]으로 답한다. 답이 없던 곳은 [추정]으로 낮춘다.
"""

import re

from typer.testing import CliRunner

from sasu.cli import app
from test_python import FASTAPI

runner = CliRunner()


class RecordingClient:
    """근거를 그대로 보고 답하는 가짜 LLM. 받은 근거를 기록해 둔다."""

    available = True
    model = "fake"

    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, messages):
        user = messages[-1]["content"]
        self.prompts.append(user)
        answer = re.search(r"\[(E\d+)\] \(인수인계 답변\)", user)
        if answer:
            return f"[답변] 키오스크 주문은 결제 단말이 달라 따로 처리한다 ({answer.group(1)})"
        return "[답변] 이유는 담당자만 안다 (E1)"


def _repo(tmp_path):
    repo = tmp_path / "shop"
    for rel, content in FASTAPI.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


def test_handover_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("SASU_DB_PATH", raising=False)
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    client = RecordingClient()
    monkeypatch.setattr("sasu.rag.answer.make_llm_client", lambda settings: client)

    # 1. 분석 → 지금 프로젝트가 된다
    result = runner.invoke(app, ["index", str(_repo(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "* shop" in runner.invoke(app, ["projects"]).output

    # 2. 떠나는 개발자에게 줄 질문 목록
    result = runner.invoke(app, ["questions", "--top", "20"])
    assert result.exit_code == 0
    qid = re.search(r"Q(\d+) 값이 'KIOSK'", result.output).group(1)

    # 3. 떠나는 개발자의 답
    result = runner.invoke(app, ["answer", f"Q{qid}", "키오스크 주문은 결제 단말(PG)이 달라서 따로 처리합니다", "--by", "김선임"])
    assert result.exit_code == 0 and "저장" in result.output

    # 4. 새 담당자가 코드 이름을 모른 채 묻는다 → 답이 검색과 근거에 들어간다
    result = runner.invoke(app, ["ask", "키오스크 주문은 왜 따로 처리해?", "--no-git"])
    assert result.exit_code == 0, result.output
    assert "대상 orders.create_order" in result.output
    assert "인수인계 답변 (김선임" in result.output
    assert "[답변] 키오스크 주문은 결제 단말이 달라" in result.output

    # 5. 답이 없는 곳에서 [답변]이라고 하면 [추정]으로 낮춘다
    result = runner.invoke(app, ["ask", "주문 조회는 왜 이렇게 해?", "-t", "orders.read_order", "--no-git"])
    assert result.exit_code == 0
    assert "[추정] 이유는 담당자만 안다" in result.output and "추정으로 낮춤" in result.output

    # 6. 다시 분석해도 답은 남는다
    runner.invoke(app, ["index", str(tmp_path / "shop")])
    result = runner.invoke(app, ["questions", "--top", "20"])
    assert "답: 키오스크 주문은 결제 단말" in result.output
