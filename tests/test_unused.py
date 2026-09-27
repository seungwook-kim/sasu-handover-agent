"""불필요한 소스와 끊어진 쿼리 참조를 찾는지 확인한다."""

from pathlib import Path

import pytest

from sasu import db
from sasu.analysis.unused import find_unused
from sasu.parsers.web_refs import extract_refs

FILES = {
    "src/main/java/demo/web/NoteController.java": """
package demo.web;

@Controller
public class NoteController {
    @Resource(name = "noteService")
    private NoteService noteService;

    @RequestMapping("/note/list.do")
    public String list() {
        noteService.selectList();
        return "demo/NoteList";
    }

    @RequestMapping("/note/secret.do")
    public String secret() {
        return "demo/NoteList";
    }
}
""",
    "src/main/java/demo/service/NoteServiceImpl.java": """
package demo.service;

@Service("noteService")
public class NoteServiceImpl implements NoteService {
    @Resource(name = "noteDAO")
    private NoteDAO noteDAO;

    public List selectList() {
        return noteDAO.selectList();
    }

    public void cleanup() {
        noteDAO.deleteOld();
    }

    @PostConstruct
    public void init() {
        noteDAO.selectList();
    }

    public void runBatch() {
        noteDAO.selectList();
    }
}
""",
    "src/main/java/demo/service/NoteDAO.java": """
package demo.service;

@Repository("noteDAO")
public class NoteDAO {
    public List selectList() {
        String queryId = "Note.selectList";
        return selectList(queryId);
    }

    public void deleteOld() {
        delete("Note.deleteOldNotes");
    }
}
""",
    "src/main/resources/mapper/Note_SQL_oracle.xml": """<?xml version="1.0" encoding="UTF-8"?><!--
  2024.01.02  kim  selectLegacy는 더 이상 사용하지 않음
-->
<mapper namespace="Note">
    <select id="selectList">SELECT * FROM TB_NOTE</select>
    <select id="selectLegacy">SELECT * FROM TB_NOTE_OLD</select>
    <delete id="deleteOld">DELETE FROM TB_NOTE</delete>
</mapper>
""",
    "src/main/webapp/WEB-INF/jsp/demo/NoteList.jsp": "<a href=\"${ctx}/note/list.do\">list</a>",
    "src/main/webapp/WEB-INF/jsp/demo/Orphan.jsp": "<p>nobody uses me</p>",
    "src/main/webapp/WEB-INF/jsp/demo/error/NoteError.jsp": "<p>error</p>",
    "src/main/webapp/WEB-INF/config/servlet.xml": """
<beans>
  <prop key="demo.NoteException">demo/error/NoteError</prop>
  <task:scheduled ref="noteService" method="runBatch" cron="0 0 * * * *"/>
</beans>
""",
}


@pytest.fixture
def report(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = db.connect(tmp_path / "sasu.db")
    from sasu.indexer import index_repository

    index_repository(conn, repo, dialect="oracle")
    yield find_unused(conn)
    conn.close()


def test_query_id_through_variable_is_linked(report):
    # String queryId = "Note.selectList"; selectList(queryId) 는 쓰이는 쿼리다
    assert "Note.selectList" not in [f.name for f in report.sql]


def test_unused_sql_with_history_evidence(report):
    legacy = next(f for f in report.sql if f.name == "Note.selectLegacy")
    assert legacy.confidence == "높음"
    assert "더 이상 사용하지 않음" in legacy.evidence
    assert "Note.deleteOld" in [f.name for f in report.sql]


def test_broken_query_reference(report):
    broken = next(f for f in report.broken_sql if f.name == "Note.deleteOldNotes")
    assert "Mapper에 없음" in broken.reason
    # cleanup()은 어떤 URL에서도 이어지지 않는다
    assert "죽은 코드" in broken.reason


def test_unreferenced_url(report):
    assert [f.name for f in report.url] == ["/note/secret.do"]


def test_unused_methods_skip_framework_and_config_calls(report):
    names = [f.name for f in report.method]
    assert "NoteServiceImpl.cleanup" in names
    assert "NoteServiceImpl.init" not in names  # @PostConstruct
    assert "NoteServiceImpl.runBatch" not in names  # <task:scheduled method="runBatch">


def test_unused_jsp_respects_config_view_paths(report):
    assert [f.name for f in report.jsp] == ["Orphan.jsp"]


def test_extract_refs():
    text = """<a href="${pageContext.request.contextPath}/cop/bbs/list.do">x</a>
<%@ include file="/WEB-INF/jsp/common/header.jsp" %>
<script>fn_go('/cop/bbs/detail.do?id=1');</script>"""
    refs = {(r.kind, r.value) for r in extract_refs(text)}
    assert ("url", "/cop/bbs/list.do") in refs
    assert ("url", "/cop/bbs/detail.do") in refs
    assert ("include", "/WEB-INF/jsp/common/header.jsp") in refs


def test_extract_rest_paths_and_spel_calls():
    js = "$.ajax({ url: '/twitter/tweets', type: 'GET' }); fetch(\"/api/items/\")"
    refs = {(r.kind, r.value) for r in extract_refs(js)}
    assert ("url", "/twitter/tweets") in refs
    assert ("url", "/api/items") in refs
    xml = """<property name="password" value="#{egovPasswordResolver.resolvePassword('db.pw')}"/>"""
    refs = {(r.kind, r.value) for r in extract_refs(xml, methods=True)}
    assert ("method", "resolvePassword") in refs
