"""인수인계 질문 생성, 저장(답변 보존), 엑셀 내보내기·가져오기."""

from pathlib import Path

import pytest
from openpyxl import load_workbook

from sasu import db
from sasu.analysis.questions import generate_questions, save_questions
from sasu.handover_sheet import export_sheet, import_sheet
from sasu.indexer import index_repository
from sasu.parsers.java_parser import parse_java

FILES = {
    "src/main/java/demo/web/UserController.java": """
package demo.web;
@Controller
public class UserController {
    @Resource(name = "userService") private UserService userService;
    @RequestMapping("/user/delete.do")
    public String delete(String userSe) { userService.delete(userSe); return "user/List"; }
    @RequestMapping("/user/lock.do")
    public String lock(String userSe) { userService.lock(userSe); return "user/List"; }
}
""",
    "src/main/java/demo/service/UserServiceImpl.java": """
package demo.service;
@Service("userService")
public class UserServiceImpl {
    @Resource(name = "userDAO") private UserDAO userDAO;
    @Resource(name = "userIdGnrService") private EgovIdGnrService idGen;

    public void delete(String userSe) {
        // TODO 탈퇴 회원 보관 기간 정책 확인 필요
        if ("USR03".equals(userSe)) {
            userDAO.deleteEmployee();
        } else if ("".equals(userSe) || "Y".equals(userSe)) {
            userDAO.deleteMember();
        }
        try {
            idGen.getNextStringId();
        } catch (Exception e) {
        }
    }

    public void lock(String userSe) {
        userDAO.updateLock(userSe);
    }
}
""",
    "src/main/java/demo/service/UserDAO.java": """
package demo.service;
@Repository("userDAO")
public class UserDAO {
    public void deleteEmployee() { delete("User.deleteEmployee"); }
    public void deleteMember() { delete("User.deleteMember"); }
    public void updateLock(String userSe) { update("User.updateLock" + userSe); }
}
""",
    "src/main/resources/mapper/User_SQL_oracle.xml": """<mapper namespace="User">
  <delete id="deleteEmployee">DELETE FROM TB_EMP</delete>
  <update id="updateLockGNR">UPDATE TB_MEMBER SET LOCK_AT = 'Y'</update>
  <update id="updateLockENT">UPDATE TB_ENTRPRS SET LOCK_AT = 'Y'</update>
</mapper>""",
}


@pytest.fixture
def setup(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = db.connect(tmp_path / "sasu.db")
    index_repository(conn, repo, dialect="oracle")
    yield conn, repo, tmp_path
    conn.close()


def test_generates_expected_kinds(setup):
    conn, _, _ = setup
    by_kind = {}
    for q in generate_questions(conn):
        by_kind.setdefault(q.kind, []).append(q)
    assert "User.deleteMember" in by_kind["broken_sql"][0].question
    assert '"User.updateLock"' in by_kind["dynamic_sql"][0].question
    assert "TODO 탈퇴 회원 보관 기간 정책 확인 필요" in by_kind["note"][0].question
    assert "'USR03'" in by_kind["magic_value"][0].question
    assert "'Y'" not in by_kind["magic_value"][0].question  # 흔한 값은 묻지 않는다
    assert "userIdGnrService" in by_kind["external_bean"][0].question
    assert by_kind["empty_catch"][0].target == "UserServiceImpl.delete"


def test_broken_sql_on_url_path_ranks_first(setup):
    conn, _, _ = setup
    assert generate_questions(conn)[0].kind == "broken_sql"


def test_answers_survive_regeneration_and_reindex(setup):
    conn, repo, tmp_path = setup
    save_questions(conn, generate_questions(conn), repo)
    key = conn.execute("SELECT key FROM questions WHERE kind = 'magic_value'").fetchone()["key"]
    conn.execute("UPDATE questions SET answer = 'USR03은 업무사용자', status = 'answered' WHERE key = ?", (key,))
    conn.commit()

    index_repository(conn, repo, dialect="oracle")  # 다시 색인해도
    new, updated, stale = save_questions(conn, generate_questions(conn), repo)
    assert new == 0 and stale == 0
    row = conn.execute("SELECT answer, status, code FROM questions WHERE key = ?", (key,)).fetchone()
    assert row["answer"] == "USR03은 업무사용자"
    assert row["status"] == "answered"
    assert 'if ("USR03".equals(userSe))' in row["code"]


def test_excel_round_trip(setup):
    conn, repo, tmp_path = setup
    save_questions(conn, generate_questions(conn), repo)
    sheet = tmp_path / "q.xlsx"
    count = export_sheet(conn, sheet)
    wb = load_workbook(sheet)
    ws = wb.active
    assert ws.max_row == count + 1
    ws.cell(row=2, column=9, value="Mapper에서 빠진 걸 몰랐음. 추가 예정")
    ws.cell(row=2, column=10, value="김선배")
    wb.save(sheet)

    saved, missing = import_sheet(conn, sheet)
    assert (saved, missing) == (1, 0)
    row = conn.execute("SELECT answer, answered_by, status FROM questions WHERE status = 'answered'").fetchone()
    assert row["answered_by"] == "김선배"


def test_excel_export_without_questions(setup):
    """질문이 하나도 없는 프로젝트(spring-petclinic 같은)도 빈 질문지를 내려받을 수 있다."""
    conn, _, tmp_path = setup
    sheet = tmp_path / "empty.xlsx"
    assert export_sheet(conn, sheet) == 0
    assert load_workbook(sheet).active.max_row == 1


def test_note_tag_ignores_business_terms():
    src = """class A { void m() {
        // 임시 비밀번호를 생성한다
        // 임시로 막아 둠
    } }"""
    notes = parse_java(src.encode("utf-8")).classes[0].methods[0].notes
    assert [n.text for n in notes] == ["임시로 막아 둠"]
