"""인수인계서 만들기: 업무별 URL → 코드 → SQL → 테이블, 받은 답변(답한 사람·날짜), 확인 필요 빈칸, 근거 등급."""

import re
import sqlite3

import pytest

from sasu.analysis.handover_doc import (
    ANSWER,
    BLANK,
    FACT,
    GRADES,
    UNKNOWN,
    build_handover_doc,
    outline,
    related_doc_chunks,
    render_markdown,
    write_docx,
    write_markdown,
)
from sasu.handover import refresh_questions, save_answer
from test_behaviors import _conn

_GRADE_MARK = re.compile(r"^- \[(%s)\] " % "|".join(re.escape(g) for g, _ in GRADES))


def _answered(tmp_path):
    conn = _conn(tmp_path)
    refresh_questions(conn)
    qid = conn.execute("SELECT id FROM questions WHERE kind = 'magic_value' AND target = 'UserServiceImpl.delete'").fetchone()[0]
    save_answer(conn, qid, "USR03은 직원 계정이라 사원 테이블에서 지운다", "김선임")
    return conn, qid


def test_sections_and_counts(tmp_path):
    conn, qid = _answered(tmp_path)
    doc = build_handover_doc(conn, project="demo")
    assert [g.name for g in doc.businesses] == ["web"]  # 한글 업무명이 없으면 모듈로 묶는다
    assert outline(doc)[3] == "1. web 모듈 (한글 업무명 없음)"
    assert outline(doc)[:3] == ["요약", "근거 등급 범례", "차례"]
    c = doc.counts
    assert (c["businesses"], c["urls"], c["answered"]) == (1, 3, 1)
    assert c["open"] == c["questions_total"] - 1 and c["open"] > 0

    user = doc.businesses[0]
    assert [p.url for p in user.urls] == ["/user/delete.do", "/user/lock.do", "/user/password.do"]
    delete = "\n".join(x.text for x in user.urls[0].lines)
    assert "`UserController.delete`" in delete and "DELETE User.deleteEmployee" in delete
    assert "쓰기 TB_EMP(삭제)" in delete
    assert any(x.grade == UNKNOWN and "userIdGnrService" in x.text for x in user.urls[0].lines)  # 확정 못 한 연결
    assert any(x.grade == ANSWER and f"Q{qid}" in x.text for x in user.urls[0].lines)  # 받은 답변을 가리킨다
    # 받은 답변: 답한 사람·날짜
    assert len(user.answers) == 1
    assert re.search(r"USR03은 직원 계정.*\(김선임, \d{4}-\d{2}-\d{2}\)$", user.answers[0].text)
    # 확인 필요: 미답변 질문 + 업무마다 운영 확인 칸
    assert user.open_items[-1].qid is None and user.open_items[-1].text.startswith("운영 중 알아야 할 것")
    assert all(i.qid != qid for i in user.open_items)
    assert any(x.grade == FACT and "TB_MEMBER(수정)" in x.text for x in user.overview)


def test_markdown_every_sentence_graded(tmp_path):
    conn, _ = _answered(tmp_path)
    md = render_markdown(build_handover_doc(conn, project="demo"))
    assert md.startswith("# demo 인수인계서")
    for head in ("## 요약", "## 근거 등급 범례", "## 차례", "### 받은 답변", "### 확인 필요 (전임자 기입)", "#### `/user/delete.do`"):
        assert head in md
    assert "관련 문서" not in md  # 문서를 넣지 않았으면 그 절이 없다
    body = md.split("## 차례", 1)[1]
    for line in body.splitlines():
        if line.startswith("- "):
            assert _GRADE_MARK.match(line), line
        elif line.startswith("  - "):
            assert line.startswith(f"  - [{FACT}] ") or line.startswith("  - 답: "), line
    assert md.count(f"답: {BLANK}") == md.count(f"- [{UNKNOWN}] Q") + 1  # 질문마다 + 업무마다 운영 칸
    summary = md.split("## 요약", 1)[1].split("##", 1)[0]
    assert "업무 1개" in summary and "화면(URL) 3개" in summary and "받은 답변은 1건" in summary


def test_scope_by_business_and_module(tmp_path):
    conn, _ = _answered(tmp_path)
    one = build_handover_doc(conn, business="없는업무")
    assert one.businesses == [] and one.counts["urls"] == 0
    web = build_handover_doc(conn, module="web")
    assert web.counts["urls"] == 3
    assert "범위 모듈 web" in render_markdown(web)


def test_no_questions_yet(tmp_path):
    conn = _conn(tmp_path)
    doc = build_handover_doc(conn)
    assert doc.counts["questions_total"] == 0
    assert any("sasu questions" in x.text for x in doc.summary)
    assert all(len(g.open_items) == 1 for g in doc.businesses)  # 운영 확인 칸만


def test_related_docs_without_tables(tmp_path):
    """문서 테이블이 없는 예전 분석 파일이면 조용히 빈 목록 (관련 문서 절도 없음)."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    assert related_doc_chunks(conn, [("url", "/user/delete.do", 3.0)]) == []


def test_files(tmp_path):
    conn, _ = _answered(tmp_path)
    doc = build_handover_doc(conn, project="demo")
    md = write_markdown(doc, tmp_path / "out" / "demo.md")
    assert md.read_text(encoding="utf-8").startswith("# demo")
    pytest.importorskip("docx")
    from docx import Document

    path = write_docx(doc, tmp_path / "out" / "demo.docx")
    text = "\n".join(p.text for p in Document(path).paragraphs)
    assert "demo 인수인계서" in text and "[확인 필요] 운영 중 알아야 할 것" in text
    assert "(김선임, " in text and "[사실] 처리 코드: UserController.delete" in text


def test_web_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from sasu.web import server

    conn, _ = _answered(tmp_path)
    conn.close()
    monkeypatch.setenv("SASU_DB_PATH", str(tmp_path / "sasu.db"))
    client = TestClient(server.create_app())
    md = client.get("/api/handover-doc", params={"format": "md", "business": "", "module": "web"})
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "attachment" in md.headers["content-disposition"]
    assert "## 근거 등급 범례" in md.text and "(김선임, " in md.text
    assert client.get("/api/handover-doc", params={"format": "pdf"}).status_code == 400
    assert client.get("/api/handover-doc", params={"format": "md", "module": "없는모듈"}).status_code == 404
    pytest.importorskip("docx")
    res = client.get("/api/handover-doc")  # 기본 docx
    assert res.status_code == 200 and res.content[:2] == b"PK"


def test_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sasu.cli import app

    conn, _ = _answered(tmp_path)
    conn.close()
    monkeypatch.setenv("SASU_DB_PATH", str(tmp_path / "sasu.db"))
    out = tmp_path / "out" / "demo-u.ser"  # 점이 있어도 확장자로 보지 않는다
    result = CliRunner().invoke(app, ["handover-doc", "--format", "md", "--module", "web", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "업무 1개" in result.output and "받은 답변 1건" in result.output
    assert (tmp_path / "out" / "demo-u.ser.md").exists()
    bad = CliRunner().invoke(app, ["handover-doc", "--format", "pdf"])
    assert bad.exit_code == 1


def test_doc_conflicts_in_related_docs_and_open_items(tmp_path, monkeypatch):
    """문서와 코드가 다른 곳: '관련 문서'에 ⚠ 줄, 그 업무의 '확인 필요'에 문서 확인 질문 (질문 목록과 겹치지 않게)."""
    from sasu.docs.conflicts import all_conflicts, sync_doc_questions
    from sasu.docs.store import add_document
    from test_docs import HANDOVER, JAVA

    from sasu import db
    from sasu.indexer import index_repository

    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    repo = tmp_path / "leave"
    for rel, content in JAVA.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    conn = db.connect(tmp_path / "leave.db")
    index_repository(conn, repo, dialect="oracle")
    doc_file = tmp_path / "휴가관리-인수인계.md"
    doc_file.write_text(HANDOVER, encoding="utf-8")
    add_document(conn, doc_file)
    conflicts = all_conflicts(conn)
    assert conflicts

    doc = build_handover_doc(conn, project="leave")
    leave = next(g for g in doc.businesses if g.name == "휴가관리")
    warns = [x.text for x in leave.docs if x.text.startswith("⚠ 문서와 코드가 달라요")]
    assert any("5일" in w for w in warns) and any("TB_LEAVE_REMAIN" in w for w in warns)
    asked = [i for i in leave.open_items if i.kind == "문서 확인"]
    assert asked and all(i.qid is None for i in asked)  # 아직 질문 목록에 없으면 빈칸만
    assert doc.counts["doc_conflicts"] >= len(warns) > 0
    md = render_markdown(doc)
    assert "- [확인 필요] ⚠ 문서와 코드가 달라요" in md and "(문서 확인)" in md

    # 질문 목록에 '문서 확인' 질문이 생긴 뒤에는 그 질문(Q번호)으로 한 번만
    sync_doc_questions(conn)
    doc = build_handover_doc(conn, project="leave")
    leave = next(g for g in doc.businesses if g.name == "휴가관리")
    texts = [i.text for i in leave.open_items if i.kind == "문서 확인"]
    assert len(texts) == len(set(texts)) and all(i.qid for i in leave.open_items if i.kind == "문서 확인")
    all_texts = [i.text for g in doc.businesses for i in g.open_items] + [i.text for i in doc.loose_open]
    assert len([t for t in all_texts if "TB_LEAVE_REMAIN" in t]) == 1
    conn.close()
