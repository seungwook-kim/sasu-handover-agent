"""기존 동작 목록: URL마다 화면·읽고 쓰는 테이블·업무 코드 분기·주석·인수인계 답변."""

from pathlib import Path

from openpyxl import load_workbook

from sasu import db
from sasu.analysis.behaviors import _branch_label, export_behaviors, list_behaviors
from sasu.handover import refresh_questions, save_answer
from sasu.indexer import index_repository
from test_questions import FILES

EXTRA = {
    "src/main/java/demo/web/PasswordController.java": """
package demo.web;
@Controller
public class PasswordController {
    @RequestMapping("/user/password.do")
    public String change(String pw) {
        if (pw.length() < 8 || pw.length() > 20) { return "user/PasswordError"; }
        return "redirect:/user/lock.do";
    }
}
""",
}


def _conn(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in {**FILES, **EXTRA}.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = db.connect(tmp_path / "sasu.db")
    index_repository(conn, repo, dialect="oracle")
    return conn


def test_behavior_rows(tmp_path):
    conn = _conn(tmp_path)
    rows = {b.url: b for b in list_behaviors(conn)}
    assert set(rows) == {"/user/delete.do", "/user/lock.do", "/user/password.do"}

    delete = rows["/user/delete.do"]
    assert delete.handler == "UserController.delete"
    assert delete.writes == ["TB_EMP(삭제)"]  # deleteMember는 Mapper에 없어 테이블이 없다
    assert any(x.startswith("'USR03' ←") for x in delete.branches)
    assert not any("'Y'" in x for x in delete.branches)  # Y/N 같은 뻔한 값은 뺀다
    assert delete.notes == ["TODO 탈퇴 회원 보관 기간 정책 확인 필요"]
    assert any("userIdGnrService" in x for x in delete.external)
    assert delete.screens == ["WEB-INF/jsp/user/List.jsp (파일 없음)"]

    lock = rows["/user/lock.do"]
    assert "TB_MEMBER(수정)" in lock.writes and "TB_ENTRPRS(수정)" in lock.writes

    password = rows["/user/password.do"]
    assert [x.split(" ←")[0] for x in password.branches] == ["기준 20"]  # 한 자리 수는 너무 흔해서 뺀다
    assert password.redirects == ["/user/lock.do"]


def test_answers_and_module_filter(tmp_path):
    conn = _conn(tmp_path)
    refresh_questions(conn)
    qid = conn.execute("SELECT id FROM questions WHERE kind = 'magic_value' AND target = 'UserServiceImpl.delete'").fetchone()[0]
    save_answer(conn, qid, "USR03은 직원 계정이라 사원 테이블에서 지운다", "김선임")
    delete = next(b for b in list_behaviors(conn) if b.url == "/user/delete.do")
    assert delete.answers == ["UserServiceImpl.delete: USR03은 직원 계정이라 사원 테이블에서 지운다 (김선임)"]
    assert list_behaviors(conn, module="없는모듈") == []


def test_excel_checklist(tmp_path):
    conn = _conn(tmp_path)
    path = tmp_path / "out" / "기존동작목록.xlsx"
    assert export_behaviors(list_behaviors(conn), path) == 3
    ws = load_workbook(path).active
    header = [c.value for c in ws[1]]
    assert header[2] == "URL" and "고도화 후 확인" in header
    assert ws.data_validations.dataValidation[0].formula1.startswith('"같음')


def test_branch_label():
    assert _branch_label("09", 'sTemp.equals("09")') == "'09'"
    assert _branch_label("20", "pw.length() > 20") == "기준 20"
    assert _branch_label("180", "a > 18") == "'180'"
