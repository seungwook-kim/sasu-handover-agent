"""프로젝트 지도: 빌드 정보, 모듈 요약, 모듈별 URL과 읽기·쓰기 테이블."""

from pathlib import Path

import pytest

from sasu import db
from sasu.analysis.overview import build_overview, module_urls
from sasu.indexer import index_repository
from sasu.parsers.build_info import read_build_info

POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>org.egovframe</groupId><artifactId>egovframe-web-config-parent</artifactId><version>5.0.1</version></parent>
  <artifactId>demo-portal</artifactId>
  <version>1.2.0</version>
  <packaging>war</packaging>
  <properties><java.version>17</java.version></properties>
  <dependencies>
    <dependency><groupId>org.egovframe.rte</groupId><artifactId>egovframe-rte-ptl-mvc</artifactId></dependency>
    <dependency><groupId>org.mybatis</groupId><artifactId>mybatis</artifactId></dependency>
    <dependency><groupId>com.oracle</groupId><artifactId>ojdbc8</artifactId></dependency>
    <dependency><groupId>org.hibernate</groupId><artifactId>hibernate-validator</artifactId></dependency>
    <dependency><groupId>jakarta.servlet</groupId><artifactId>jakarta.servlet-api</artifactId></dependency>
  </dependencies>
</project>
"""

FILES = {
    "pom.xml": POM,
    "src/main/java/demo/portal/bbs/web/BoardController.java": """
package demo.portal.bbs.web;
@Controller
public class BoardController {
    @Resource(name = "boardService") private BoardService boardService;
    @RequestMapping("/bbs/list.do")
    public String list() { boardService.list(); return "bbs/List"; }
    @RequestMapping("/bbs/save.do")
    public String save() { boardService.save(); return "redirect:/bbs/list.do"; }
}
""",
    "src/main/java/demo/portal/bbs/service/BoardServiceImpl.java": """
package demo.portal.bbs.service;
@Service("boardService")
public class BoardServiceImpl {
    @Resource(name = "boardDAO") private BoardDAO boardDAO;
    public void list() { boardDAO.list(); }
    public void save() { boardDAO.save(); }
}
""",
    "src/main/java/demo/portal/bbs/service/BoardDAO.java": """
package demo.portal.bbs.service;
@Repository("boardDAO")
public class BoardDAO {
    public void list() { selectList("Board.list"); }
    public void save() { insert("Board.save"); }
}
""",
    "src/main/java/demo/portal/uat/web/LoginController.java": """
package demo.portal.uat.web;
@Controller
public class LoginController {
    @RequestMapping("/uat/login.do")
    public String login() { return "uat/Login"; }
}
""",
    "src/main/resources/mapper/Board_SQL_oracle.xml": """<mapper namespace="Board">
  <select id="list">SELECT * FROM TB_BOARD b JOIN TB_USER u ON b.WRITER = u.ID</select>
  <insert id="save">INSERT INTO TB_BOARD (ID) VALUES (#{id})</insert>
</mapper>""",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def conn(repo: Path, tmp_path: Path):
    connection = db.connect(tmp_path / "sasu.db")
    index_repository(connection, repo, dialect="oracle")
    yield connection
    connection.close()


def test_build_info_from_pom(repo):
    info = read_build_info(repo)
    assert (info.tool, info.project, info.version, info.packaging) == ("maven", "demo-portal", "1.2.0", "war")
    assert info.parent == "egovframe-web-config-parent 5.0.1"
    assert info.java_version == "17"
    assert info.servlet_api == "jakarta"
    assert info.stack["데이터 접근"] == ["mybatis"]
    assert info.stack["DB 드라이버"] == ["ojdbc8"]
    assert info.stack["검증"] == ["hibernate-validator"]


def test_overview_modules_and_tables(conn):
    ov = build_overview(conn)
    assert ov.build["project"] == "demo-portal"
    assert ov.counts["URL"] == 3
    by_name = {m.name: m for m in ov.modules}
    assert by_name["bbs.web"].urls == 2
    assert by_name["bbs.service"].statements == 2
    assert by_name["uat.web"].urls == 1
    board = next(t for t in ov.tables if t.name == "TB_BOARD")
    assert board.kinds == {"select": 1, "insert": 1}
    assert board.modules == ["bbs.service"]


def test_module_urls_split_reads_and_writes(conn):
    rows = {r.url: r for r in module_urls(conn, "bbs")}
    assert rows["/bbs/list.do"].tables == ["TB_BOARD", "TB_USER"]
    assert rows["/bbs/list.do"].writes == []
    assert rows["/bbs/save.do"].writes == ["TB_BOARD"]
    assert "/uat/login.do" not in rows
