"""공통화 진단: 공통 코드 사용 현황, 복사해 붙인 코드, 중복 SQL."""

from pathlib import Path

import pytest

from sasu import db
from sasu.analysis.common import analyze_common, module_of, package_root
from sasu.indexer import index_repository

CHECK_BODY = """
        LoginVO user = Helper.currentUser();
        if (user == null || user.getId() == null || "".equals(user.getId())) {
            throw new IllegalStateException("no login");
        }
        if (!Helper.isAdmin(user) && !user.getId().equals(ownerId)) {
            throw new IllegalStateException("not owner");
        }
        Helper.audit(user.getId(), ownerId);
"""

FILES = {
    "src/demo/app/cmm/util/Helper.java": """
package demo.app.cmm.util;
public class Helper {
    public static LoginVO currentUser() { return null; }
    public static boolean isAdmin(LoginVO u) { return false; }
    public static void audit(String a, String b) {}
}
""",
    "src/demo/app/cmm/web/BaseController.java": f"""
package demo.app.cmm.web;
public abstract class BaseController {{
    protected void assertOwner(String ownerId) {{{CHECK_BODY}    }}
}}
""",
    "src/demo/app/bbs/web/BoardController.java": f"""
package demo.app.bbs.web;
@Controller
public class BoardController extends BaseController {{
    @RequestMapping("/bbs/list.do")
    public String list() {{ Helper.currentUser(); return "bbs/List"; }}
}}
""",
    # 부모를 상속하지 않고 같은 코드를 복사한 컨트롤러 두 개 (변수 이름만 다름)
    "src/demo/app/memo/web/MemoController.java": f"""
package demo.app.memo.web;
@Controller
public class MemoController {{
    @RequestMapping("/memo/list.do")
    public String list() {{ Helper.currentUser(); return "memo/List"; }}
    private void assertOwner(String ownerId) {{{CHECK_BODY}    }}
}}
""",
    "src/demo/app/todo/web/TodoController.java": f"""
package demo.app.todo.web;
@Controller
public class TodoController {{
    @RequestMapping("/todo/list.do")
    public String list() {{ Helper.isAdmin(null); return "todo/List"; }}
    private void checkWriter(String writerId) {{{CHECK_BODY.replace("ownerId", "writerId")}    }}
}}
""",
    "src/main/resources/mapper/Memo_SQL_oracle.xml": """<mapper namespace="Memo">
  <select id="selectUsers">SELECT a.USER_ID, a.USER_NM, b.ORG_NM FROM TB_USER a JOIN TB_ORG b ON a.ORG_ID = b.ORG_ID WHERE a.USE_AT = 'Y'</select>
</mapper>""",
    "src/main/resources/mapper/Todo_SQL_oracle.xml": """<mapper namespace="Todo">
  <select id="selectWriters">
    SELECT a.USER_ID, a.USER_NM, b.ORG_NM
      FROM TB_USER a JOIN TB_ORG b ON a.ORG_ID = b.ORG_ID
     WHERE a.USE_AT = 'Y'
  </select>
</mapper>""",
}


@pytest.fixture
def conn(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    connection = db.connect(tmp_path / "sasu.db")
    index_repository(connection, repo, dialect="oracle")
    yield connection
    connection.close()


def test_package_root_and_module():
    root = package_root(["demo.app.bbs.web", "demo.app.cmm.util", "demo.app.memo.web"])
    assert root == "demo.app"
    assert module_of("demo.app.cmm.util", root) == "cmm.util"


def test_shared_helper_used_by_many_modules(conn):
    report = analyze_common(conn)
    top = report.shared[0]
    assert top.class_name == "Helper"
    # BaseController(cmm.web)도 Helper를 부른다. 같은 모듈(cmm.util) 안의 호출은 세지 않는다.
    assert top.used_by_modules == ["bbs.web", "cmm.web", "memo.web", "todo.web"]


def test_clone_group_notes_parent_class(conn):
    report = analyze_common(conn)
    group = report.clones[0]
    names = sorted(m[0] for m in group.methods)
    assert names == ["BaseController.assertOwner", "MemoController.assertOwner", "TodoController.checkWriter"]
    assert group.note == "공통 부모 클래스 BaseController에 같은 코드가 있음, 상속하지 않고 복사한 곳 2"


def test_duplicate_sql_ignores_whitespace(conn):
    report = analyze_common(conn)
    assert [sorted(s[0] for s in d.statements) for d in report.duplicate_sql] == [
        ["Memo.selectUsers", "Todo.selectWriters"]
    ]
