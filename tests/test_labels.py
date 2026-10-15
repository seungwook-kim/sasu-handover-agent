"""한글 이름표(클래스 업무명·메서드 설명·모듈 업무명)와 사용성 테스트 뒤 더한 웹 API (A1~A10)."""

import io
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from sasu import db
from sasu.analysis.labels import Labels, business_name, class_label, method_label
from sasu.handover import refresh_questions
from sasu.indexer import index_repository
from sasu.rag.pipeline import choose_target, embedder_or_none
from sasu.web import server

FILES = {
    "src/main/java/demo/uss/ion/vct/web/VacationController.java": """
package demo.uss.ion.vct.web;
/**
 * 개요
 * - 휴가관리에 대한 controller 클래스를 정의한다.
 *
 * 상세내용
 * - 휴가관리에 대한 등록, 수정, 삭제, 조회 기능을 제공한다.
 * @author 공통서비스 개발팀
 * <pre>
 * << 개정이력(Modification Information) >>
 *   수정일      수정자           수정내용
 *  2009.08.03  홍길동          최초 생성
 * </pre>
 */
@Controller
public class VacationController {
    @Resource(name = "vacationService") private VacationService vacationService;

    /**
     * 휴가를 등록한다.
     * @param vo 휴가 정보
     */
    @RequestMapping("/uss/ion/vct/insertVacation.do")
    public String insertVacation(String vo) {
        vacationService.insertVacation(vo);
        return "uss/ion/vct/List";
    }

    /** 휴가 목록을 조회한다. */
    @RequestMapping("/uss/ion/vct/listVacation.do")
    public String listVacation() { vacationService.listVacation(); return "uss/ion/vct/List"; }
}
""",
    "src/main/java/demo/uss/ion/vct/service/VacationService.java": """
package demo.uss.ion.vct.service;
/**
 * 휴가관리에 관한 비즈니스 구현 클래스
 * @author 공통서비스 개발팀
 */
@Service("vacationService")
public class VacationService {
    @Resource(name = "vacationDAO") private VacationDAO vacationDAO;

    /**
     * 휴가를 등록한다. 남은 연차가 없으면 등록하지 않는다.
     */
    public void insertVacation(String vo) {
        if (vacationDAO.remain(vo) < 1) { throw new IllegalStateException("연차 없음"); }
        vacationDAO.insertVacation(vo);
        vacationDAO.updateRemain(vo);
    }

    /** 휴가 목록을 조회한다. */
    public void listVacation() { vacationDAO.listVacation(); }
}
""",
    "src/main/java/demo/uss/ion/vct/service/VacationDAO.java": """
package demo.uss.ion.vct.service;
/** 휴가관리에 대한 데이터 접근 클래스 */
@Repository("vacationDAO")
public class VacationDAO {
    /** 남은 연차를 조회한다 */
    public int remain(String vo) { return selectOne("Vacation.selectRemain", vo); }
    /** 휴가를 등록한다 */
    public void insertVacation(String vo) { insert("Vacation.insertVacationMissing", vo); }
    /** 남은 연차를 줄인다 */
    public void updateRemain(String type) { update("Vacation.updateRemain" + type); }
    /** 휴가 목록 */
    public void listVacation() { selectList("Vacation.selectVacationList"); }
}
""",
    "src/main/java/demo/uss/ion/bnt/web/DutyController.java": """
package demo.uss.ion.bnt.web;
/**
 * 개요
 * - 당직관리에 대한 controller 클래스를 정의한다.
 */
@Controller
public class DutyController {
    /** 당직 목록을 조회한다. */
    @RequestMapping("/uss/ion/bnt/listDuty.do")
    public String listDuty() { return "uss/ion/bnt/List"; }
}
""",
    "src/main/java/demo/uat/uia/web/LoginController.java": """
package demo.uat.uia.web;
/**
 * 일반 로그인, 인증서 로그인을 처리하는 컨트롤러 클래스
 */
@Controller
public class LoginController {
    /**
     * 일반(세션) 로그인을 처리한다
     * @param id 아이디
     */
    @RequestMapping("/uat/uia/actionLogin.do")
    public String actionLogin(String id) { return "main"; }
}
""",
    "src/main/resources/mapper/Vacation_SQL_oracle.xml": """<mapper namespace="Vacation">
  <select id="selectRemain">SELECT REMAIN FROM TB_YRYC</select>
  <select id="selectVacationList">SELECT * FROM TB_VCATN</select>
  <update id="updateRemainA">UPDATE TB_YRYC SET REMAIN = REMAIN - 1</update>
</mapper>""",
}


@pytest.fixture
def repo_conn(tmp_path: Path):
    repo = tmp_path / "hr"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    db_path = tmp_path / "hr.db"
    conn = db.connect(db_path)
    index_repository(conn, repo, dialect="oracle")
    yield conn, db_path
    conn.close()


@pytest.fixture
def client(repo_conn, monkeypatch):
    _, db_path = repo_conn
    monkeypatch.setenv("SASU_DB_PATH", str(db_path))
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    embedder_or_none.cache_clear()
    return TestClient(server.create_app())


# ── 이름표 뽑기 ─────────────────────────────────────────────

@pytest.mark.parametrize("text, name", [
    ("주소록정보를 관리하기 위한 컨트롤러 클래스", "주소록정보 관리"),
    ("게시물 관리를 위한 컨트롤러 클래스", "게시물 관리"),
    ("권한그룹에 관한 controller 클래스를 정의한다.", "권한그룹"),
    ("전체일정을 처리하는 Controller Class 구현", "전체일정"),
    ("회의관리를 처리하기 위한 Controller 구현 Class", "회의관리"),
    ("공통코드에 관한 요청을 받아 서비스 클래스로 요청을 전달하고 서비스클래스에서 처리한 결과를", "공통코드"),
    ("업무사용자관련 요청을 비즈니스 클래스로 전달하고 처리된결과를 해당 웹 화면으로 전달하는 Controller", "업무사용자"),
    ("스크랩관리 서비스 컨트롤러 클래스", "스크랩관리"),
    ("발송메일을 상세 조회하는 컨트롤러 클래스", "발송메일 상세 조회"),
    ("Facebook을 처리하는 Controller Class 구현", None),  # 한글 업무명이 아니다
    ("Twitter(X) OAuth 인증 시작/콜백 처리와 인증키 입력 팝업을 담당하는 컨트롤러.", None),  # 20자 넘음: 자르지 않는다
])
def test_business_name(text, name):
    assert business_name(text) == name


def test_class_label_sources():
    assert class_label(FILES["src/main/java/demo/uss/ion/vct/web/VacationController.java"].split("@Controller")[0]
                       .split("package demo.uss.ion.vct.web;")[1]) == "휴가관리"
    # @Description 줄이 있으면 그것을 쓴다
    assert class_label("/**\n * @Class Name : EgovComUtlController.java\n"
                       " * @Description : 공통유틸리티성 작업을 위한 Controller\n * @Modification Information\n */"
                       ) == "공통유틸리티성 작업"
    # 클래스 주석이 없으면 파일 머리말에서
    assert class_label(None, "/**\n * 개요\n * - 배너에 대한 controller 클래스를 정의한다.\n */") == "배너"
    # 수정 이력의 한글(최초 생성)이나 작성자는 업무명이 아니다
    assert class_label("/**\n * EgovImageProcessController.java\n * @author 공통 서비스 개발팀 이삼섭\n"
                       " * == 개정이력(Modification Information) ==\n *  2009.04.02  이삼섭  최초생성\n */") is None
    assert class_label(None) is None


def test_method_label():
    assert method_label("/**\n\t * 휴가를 등록한다.\n\t * @param vo\n\t */") == "휴가를 등록한다"
    assert method_label("/**\n * Returns the user.\n * @return user\n */") is None
    assert method_label("/**\n * 2026.07.13 KISA 보안취약점 조치 - 관리자 또는 소유자\n */") is None
    assert method_label('"""주문을 만든다.\n\n    재고가 없으면 실패한다.\n    """') == "주문을 만든다"
    long = method_label("/** " + "가" * 60 + " */")
    assert len(long) == 40 and long.endswith("…")


def test_labels_on_index(repo_conn):
    conn, _ = repo_conn
    names = Labels(conn)
    mid = conn.execute("SELECT m.id FROM methods m JOIN classes c ON c.id = m.class_id "
                       "WHERE c.name = 'VacationController' AND m.name = 'insertVacation'").fetchone()[0]
    assert names.full(mid) == "휴가관리 · 휴가를 등록한다"
    # 업무가 여럿 모인 모듈은 한 업무명으로 부르지 않는다
    assert names.module_label("uss.ion") == "휴가관리 외 1"
    assert set(names.module_labels("uss.ion")) == {"휴가관리", "당직관리"}
    assert names.module_label("uat.uia") == "일반 로그인, 인증서 로그인"
    assert names.search_urls("휴가 등록") == ["/uss/ion/vct/insertVacation.do"]
    assert names.search_urls("당직") == ["/uss/ion/bnt/listDuty.do"]


def test_search_candidates_two_per_class(repo_conn, monkeypatch):
    conn, _ = repo_conn
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    embedder_or_none.cache_clear()
    sel = choose_target(conn, "휴가 등록할 때 연차가 없으면 어떻게 돼?")
    assert sel.auto is True
    classes = Counter(c.label.split(".")[0] for c in sel.candidates)
    assert 2 <= sum(classes.values()) <= 6 and len(classes) >= 2 and max(classes.values()) <= 2
    assert all(c.label_ko for c in sel.candidates)
    # 사람이 대상을 정하면 auto가 아니다
    assert choose_target(conn, "왜?", "VacationService.insertVacation").auto is False


def test_search_candidates_cap(repo_conn, monkeypatch):
    """검색이 한 클래스 메서드만 줄줄이 내놓아도 후보는 클래스당 2개, 모두 6개까지."""
    from sasu.rag import search
    from sasu.rag.pipeline import search_candidates

    conn, _ = repo_conn
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    embedder_or_none.cache_clear()
    rows = conn.execute("SELECT m.id, c.name || '.' || m.name FROM methods m JOIN classes c ON c.id = m.class_id "
                        "ORDER BY c.name = 'VacationDAO' DESC, m.id").fetchall()
    hits = [search.Hit(mid, 1.0 - i / 100, label) for i, (mid, label) in enumerate(rows)]
    monkeypatch.setattr(search.MethodIndex, "search", lambda self, q, top=5, **kw: hits[:top])
    cands, _ = search_candidates(conn, "아무 질문")
    per = Counter(c.label.split(".")[0] for c in cands)
    assert per["VacationDAO"] == 2 and max(per.values()) <= 2
    assert len(cands) == min(6, sum(min(2, n) for n in Counter(l.split(".")[0] for _, l in rows).values()))
    assert [c.label for c in cands][:2] == [rows[0][1], rows[1][1]]  # 점수 순서 그대로
    assert len(search_candidates(conn, "아무 질문", top=3)[0]) == 3


# ── 웹 API (A1~A10) ───────────────────────────────────────

def test_overview_module_labels_and_broken_sql(client):  # A1
    ov = client.get("/api/overview").json()
    modules = {m["name"]: m for m in ov["modules"]}
    assert modules["uss.ion"]["label"] == "휴가관리 외 1"
    assert "당직관리" in modules["uss.ion"]["labels"]
    # Vacation.insertVacationMissing: /uss/ion/vct/insertVacation.do에서 이어진다
    assert ov["broken_sql"] == {"reachable": 1, "unreachable": 0}


def test_module_url_labels(client):  # A2
    data = client.get("/api/modules/uss.ion").json()
    labels = {u["url"]: u["label"] for u in data["urls"]}
    assert labels["/uss/ion/vct/insertVacation.do"] == "휴가를 등록한다"
    assert labels["/uss/ion/bnt/listDuty.do"] == "당직 목록을 조회한다"


def test_urls_korean_search(client):  # A3
    data = client.get("/api/urls", params={"q": "당직"}).json()
    assert data["urls"] == ["/uss/ion/bnt/listDuty.do"]
    assert data["labels"] == {"/uss/ion/bnt/listDuty.do": "당직 목록을 조회한다"}
    english = client.get("/api/urls", params={"q": "Vacation"}).json()
    assert english["total"] == 2 and set(english["labels"]) == set(english["urls"])
    # 경로 추적도 한글로 찾는다
    assert client.get("/api/trace", params={"url": "당직"}).json()["url"] == "/uss/ion/bnt/listDuty.do"


def test_question_labels_skip_and_skipped_count(client):  # A4, A5
    data = client.get("/api/questions").json()
    assert data["skipped"] == 0
    q = next(x for x in data["items"] if x["target"] == "VacationDAO.insertVacation")
    assert q["label"] == "휴가관리 · 휴가를 등록한다"
    assert q["module"] == "uss.ion" and q["module_label"] == "휴가관리 외 1"

    skipped = client.post(f"/api/questions/{q['id']}/skip", json={"reason": "전임자도 모름", "by": "김신입"}).json()
    assert skipped["status"] == "skipped" and skipped["answer"] == "전임자도 모름"
    assert skipped["answered_by"] == "김신입" and skipped["answered_at"]
    assert client.get("/api/questions").json()["skipped"] == 1
    # 이유 없이 건너뛰면 answer는 null
    assert client.post(f"/api/questions/{q['id']}/skip", json={"reason": " "}).json()["answer"] is None
    # 되돌리기: 빈 답 → 다시 물어볼 질문
    assert client.post(f"/api/questions/{q['id']}/answer", json={"answer": ""}).json()["status"] == "open"
    assert client.post("/api/questions/99999/skip", json={"reason": "x"}).status_code == 404


def test_question_skip_reason_is_not_evidence(repo_conn):
    """건너뜀 이유는 인수인계 답변으로 근거·검색에 들어가지 않는다."""
    from sasu.handover import skip_question
    from sasu.rag.evidence import ANSWER, gather, target_of

    conn, _ = repo_conn
    refresh_questions(conn)
    qid, target = conn.execute(
        "SELECT id, target FROM questions WHERE target = 'VacationDAO.insertVacation'").fetchone()
    skip_question(conn, qid, "모름", None)
    mid = conn.execute("SELECT m.id FROM methods m JOIN classes c ON c.id = m.class_id "
                       "WHERE c.name = 'VacationDAO' AND m.name = 'insertVacation'").fetchone()[0]
    evidence = gather(conn, target_of(conn, mid), None, None, use_git=False)
    assert not any(e.level == ANSWER for e in evidence)


def _sheet(client, **params):
    res = client.get("/api/questions/export", params=params)
    assert res.status_code == 200
    return load_workbook(io.BytesIO(res.content)).active


def test_export_ids_and_q_numbers(client):  # A6
    items = client.get("/api/questions").json()["items"]
    ws = _sheet(client)
    assert ws.max_row == len(items) + 1
    assert {ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)} == {f"Q{q['id']}" for q in items}
    some = [items[0]["id"], items[-1]["id"]]
    ws = _sheet(client, ids=",".join(map(str, some)))
    assert ws.max_row == 3
    assert {ws.cell(row=r, column=1).value for r in (2, 3)} == {f"Q{i}" for i in some}
    assert client.get("/api/questions/export", params={"ids": "a,b"}).status_code == 400


def test_import_counts_only_changed_rows(client):  # A7
    items = client.get("/api/questions").json()["items"]
    first = items[0]["id"]
    client.post(f"/api/questions/{first}/answer", json={"answer": "이미 받은 답", "by": "김선배"})
    ws = _sheet(client)
    header = [c.value for c in ws[1]]
    answer_col = header.index("답변") + 1
    numbers = {ws.cell(row=r, column=1).value: r for r in range(2, ws.max_row + 1)}
    other = next(q["id"] for q in items if q["id"] != first)
    ws.cell(row=numbers[f"Q{other}"], column=answer_col, value="엑셀로 받은 새 답")
    buf = io.BytesIO()
    ws.parent.save(buf)

    res = client.post("/api/questions/import", files={"file": ("q.xlsx", buf.getvalue())}).json()
    assert res == {"saved": 1, "unchanged": 1, "missing": 0}
    # 같은 파일을 다시 올리면 바뀐 것이 없다
    again = client.post("/api/questions/import", files={"file": ("q.xlsx", buf.getvalue())}).json()
    assert again == {"saved": 0, "unchanged": 2, "missing": 0}


def test_behaviors_export_usage_sheet_and_korean_column(client):  # A8
    res = client.get("/api/behaviors/export", params={"module": "uss.ion"})
    assert res.status_code == 200
    assert "uss.ion" in res.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(res.content))
    ws = wb.worksheets[0]
    header = [c.value for c in ws[1]]
    assert header[2] == "URL" and header[3] == "기능(한글)"
    rows = {ws.cell(row=r, column=3).value: ws.cell(row=r, column=4).value for r in range(2, ws.max_row + 1)}
    assert rows["/uss/ion/vct/insertVacation.do"] == "휴가관리 · 휴가를 등록한다"
    check_col = header.index("고도화 후 확인") + 1
    assert {r.min_col for r in ws.data_validations.dataValidation[0].sqref.ranges} == {check_col}
    usage = wb["사용법"]
    text = "\n".join(str(c.value) for row in usage.iter_rows() for c in row if c.value)
    assert "1. " in text and "3. " in text and "달라짐(확인 필요)" in text and "없어짐" in text
    # 모듈 없이 받으면 파일 이름에 모듈이 없다
    assert "uss.ion" not in client.get("/api/behaviors/export").headers["content-disposition"]


def test_trace_broken_estimated_and_korean_label(client):  # A9
    data = client.get("/api/trace", params={"url": "/uss/ion/vct/insertVacation.do"}).json()
    nodes = []

    def walk(node):
        nodes.append(node)
        for child in node["children"]:
            walk(child)

    walk(data["tree"])
    by_label = {n["label"]: n for n in nodes if n["kind"] == "method"}
    assert by_label["VacationDAO.insertVacation"]["broken"] == ["Vacation.insertVacationMissing"]
    assert by_label["VacationService.insertVacation"]["broken"] == []
    assert by_label["VacationController.insertVacation"]["label_ko"] == "휴가를 등록한다"
    statements = {n["label"]: n for n in nodes if n["kind"] == "statement"}
    assert statements["UPDATE Vacation.updateRemainA"]["estimated"] is True  # "Vacation.updateRemain" + type
    assert statements["SELECT Vacation.selectRemain"]["estimated"] is False


def test_ask_evidence_auto_and_candidates(client):  # A10
    searched = client.post("/api/ask/evidence", json={"question": "휴가 등록할 때 연차가 없으면?", "use_git": False}).json()
    assert searched["auto"] is True
    classes = [c["label"].split(".")[0] for c in searched["candidates"]]
    assert 2 <= len(classes) <= 6 and max(Counter(classes).values()) <= 2
    assert all("label_ko" in c for c in searched["candidates"])
    assert searched["target"]["label_ko"]
    chosen = client.post("/api/ask/evidence", json={
        "question": "왜?", "target": "VacationController.insertVacation", "use_git": False}).json()
    assert chosen["auto"] is False
    assert chosen["target"]["label_ko"] == "휴가관리 · 휴가를 등록한다"
    picked = client.post("/api/ask/evidence", json={
        "question": "왜?", "method_id": searched["candidates"][-1]["method_id"], "use_git": False}).json()
    assert picked["auto"] is False
