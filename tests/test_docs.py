"""인수인계 문서 넣기: 형식별 읽기, 비밀 가리기, 조각·날짜, 코드 연결, 문서–코드 불일치, [문서] 근거, 웹 API."""

import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from doc_samples import make_docx, make_hwp, make_hwpx, make_pdf, make_pptx
from sasu import db
from sasu.docs.chunker import MAX_CHARS, document_date, make_chunks
from sasu.docs.conflicts import all_conflicts, sync_doc_questions
from sasu.docs.readers import DocReadError, read_document
from sasu.docs.redact import redact
from sasu.docs.search import DocIndex
from sasu.docs.store import add_document, add_path, delete_document, list_documents
from sasu.handover import refresh_questions
from sasu.indexer import index_repository
from sasu.rag.answer import DOC_RULES, build_messages, validate
from sasu.rag.evidence import DOC, FACT, Evidence, resolve_target
from sasu.rag.pipeline import doc_conflicts_for, embedder_or_none, evidence_for, search_docs


@pytest.fixture(autouse=True)
def _no_embedding(monkeypatch):
    """.env의 로컬 임베딩(모델 내려받기)·LLM 판정(SASU_DOC_JUDGE)이 테스트에 섞이지 않게."""
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    monkeypatch.setenv("SASU_DOC_JUDGE", "off")
    embedder_or_none.cache_clear()
    yield
    embedder_or_none.cache_clear()


# ── 비밀 가리기 (가짜 값) ─────────────────────────────────

SECRETS = [  # (원문, 사라져야 할 값, 종류)
    ("운영 DB 비밀번호: Egov#2024fake 입니다.", "Egov#2024fake", "비밀번호"),
    ("password=fakeP@ss99", "fakeP@ss99", "비밀번호"),
    ("DB_PASSWORD = 'zz9-fake-secret'", "zz9-fake-secret", "비밀번호"),
    ("jdbc.password=FakeJdbc77", "FakeJdbc77", "비밀번호"),
    ("비번은 qwer1234!이다", "qwer1234!", "비밀번호"),
    ("PW : Fake!!pw01", "Fake!!pw01", "비밀번호"),
    ("계정 admin / Fake!Pass1", "Fake!Pass1", "계정/암호"),
    ("ID/PW: egovadmin/fake1234", "fake1234", "계정/암호"),
    ("계정/비밀번호: egov/egov1234!", "egov1234!", "계정/암호"),
    ("jdbc:oracle:thin:egovuser/FakeOraPw1@db.local:1521:ORCL", "FakeOraPw1", "접속 문자열"),
    ("postgres://app:FakePg99@db.local:5432/app", "FakePg99", "접속 문자열"),
    # 가짜 키는 이어 붙여 만든다 (저장소에 키 모양 문자열을 그대로 두지 않게)
    ("API 키: " + "sk-" + "ant-api03-FAKEFAKEFAKEFAKEFAKE1234", "sk-" + "ant-api03-FAKEFAKEFAKEFAKEFAKE1234", "API 키"),
    ("AKIAFAKEFAKEFAKE1234 를 쓴다", "AKIAFAKEFAKEFAKE1234", "API 키"),
    ("api_key=abcdef1234567890", "abcdef1234567890", "API 키"),
    ("client_secret: Fake-Client-Secret-99", "Fake-Client-Secret-99", "API 키"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.FAKEsignature123",
     "eyJhbGciOiJIUzI1NiJ9", "토큰"),
    ("token: fake_tok_1234567890abcd", "fake_tok_1234567890abcd", "토큰"),
    ("담당자 주민번호 900101-1234567", "900101-1234567", "주민번호"),
    ("법인카드 1234-5678-9012-3456 로 결제", "1234-5678-9012-3456", "카드번호"),
    ("카드번호 4111111111111111", "4111111111111111", "카드번호"),
    ("WAS2 211.45.67.89 에 배포", "211.45.67.89", "IP"),
    ("10.0.0.5  WAS1", "10.0.0.5", "IP"),
]


@pytest.mark.parametrize("text,secret,kind", SECRETS)
def test_redact_hides_fake_secrets(text, secret, kind):
    hidden, counts = redact(text)
    assert secret not in hidden
    assert f"[가림:{kind}]" in hidden
    assert counts[kind] >= 1


def test_redact_jdbc_url_hides_password_and_ip():
    hidden, counts = redact("jdbc:mysql://192.168.0.15:3306/com?user=root&password=FakeMy1")
    assert "FakeMy1" not in hidden and "192.168.0.15" not in hidden
    assert counts["비밀번호"] == 1 and counts["IP"] == 1
    hidden, counts = redact("jdbc:oracle:thin:egovuser/FakeOraPw1@10.20.30.40:1521:ORCL")
    assert hidden == "jdbc:oracle:thin:egovuser/[가림:접속 문자열]@[가림:IP]:1521:ORCL"


@pytest.mark.parametrize("text", [
    "비밀번호는 90일마다 바꾼다. 암호화는 SHA-256을 쓴다.",
    "비밀번호 오류 5회면 계정이 잠긴다",
    "비밀번호 정책: 영문+숫자 9자 이상",
    "password: 변경 필요",
    "비밀번호변경일: 2025-01-01",
    "localhost 127.0.0.1 에서 확인",
    "버전 4.1.0.0 사용",
    "1.2.3.4 세부 절 제목",
    "계정 관리 화면은 /uss/umt/user/EgovUserManage.do 이다",
    "전화 02-1234-5678, 작성일 2025-03-10",
    "휴가 신청(/lv/apply.do)은 TB_LEAVE 테이블에 저장한다.",
])
def test_redact_leaves_ordinary_text(text):
    hidden, counts = redact(text)
    assert hidden == text and not counts


def test_redact_counts_by_kind():
    _, counts = redact("비밀번호: Fake#1234\n서버 10.1.2.30, 10.1.2.31\n계정 admin / FakeAdm1n")
    assert dict(counts) == {"비밀번호": 1, "IP": 2, "계정/암호": 1}


# ── 형식별 읽기 ───────────────────────────────────────────

ITEMS = [("h1", "배치 운영"), ("p", "작성일: 2025.03.02"), ("h2", "수동 실행"),
         ("p", "매월 1일 오전 9시에 수동으로 실행한다."), ("table", "항목|값\n서버|운영 WAS")]


def _texts(doc):
    return [(b.kind, b.level, b.text) for b in doc.blocks]


def test_read_docx_headings_numbering_tables_and_date(tmp_path):
    doc = read_document(make_docx(tmp_path / "인수인계서.docx", ITEMS, numbered_headings=True))
    assert doc.format == "docx" and doc.prop_date == "2025-03-02"
    assert ("heading", 1, "배치 운영") in _texts(doc) and ("para", 0, "서버 | 운영 WAS") in _texts(doc)
    chunks = make_chunks(doc)
    # Word 자동 번호(글자에 없음)를 흉내 내 절 번호를 붙인다
    assert [(c.section, c.heading) for c in chunks] == [("1", "1 배치 운영"), ("1.1", "1 배치 운영 > 1.1 수동 실행")]
    assert "항목 | 값" in chunks[1].text


def test_read_hwpx_outline_styles_and_table(tmp_path):
    doc = read_document(make_hwpx(tmp_path / "운영.hwpx", ITEMS))
    assert doc.format == "hwpx" and doc.prop_date == "2024-11-05"
    chunks = make_chunks(doc)
    assert chunks[1].section == "1.1" and chunks[1].heading.endswith("1.1 수동 실행")
    assert "서버 | 운영 WAS" in chunks[1].text
    # 본문의 작성일이 파일 속성보다 먼저
    assert document_date(doc) == ("2025-03-02", "본문")


@pytest.mark.parametrize("compressed", [True, False])
def test_read_hwp5_body_text(tmp_path, compressed):
    items = [("h1", "배치 운영"), ("p", "매월 1일 수동으로 실행한다."), ("h1", "장애 대응"), ("p", "운영팀장에게 알린다.")]
    doc = read_document(make_hwp(tmp_path / "옛문서.hwp", items, compressed=compressed))
    chunks = make_chunks(doc)
    assert [(c.section, c.heading, c.text) for c in chunks] == [
        ("1", "1 배치 운영", "매월 1일 수동으로 실행한다."), ("2", "2 장애 대응", "운영팀장에게 알린다.")]


def test_read_hwp_failures_have_reasons(tmp_path):
    with pytest.raises(DocReadError, match="암호"):
        read_document(make_hwp(tmp_path / "잠김.hwp", [("p", "x")], encrypted=True))
    fake = tmp_path / "옛날.hwp"
    fake.write_bytes(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05")
    with pytest.raises(DocReadError, match="HWP 5.0 형식이 아니에요"):
        read_document(fake)


def test_read_pptx_slides_are_pages(tmp_path):
    doc = read_document(make_pptx(tmp_path / "교육.pptx", [("휴가 처리", ["신청은 TB_LEAVE에 저장"]), ("로그인", ["잠금"])]))
    chunks = make_chunks(doc)
    assert [(c.heading, c.page, c.text) for c in chunks] == [
        ("휴가 처리", "1", "신청은 TB_LEAVE에 저장"), ("로그인", "2", "잠금")]


def test_read_xlsx_rows_keep_headers(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "배치목록"
    ws.append(["작업", "주기", "담당"])
    ws.append(["정산", "매월 1일", "운영팀"])
    wb.save(tmp_path / "운영.xlsx")
    chunks = make_chunks(read_document(tmp_path / "운영.xlsx"))
    assert chunks[0].page == "배치목록" and chunks[0].heading == "배치목록"
    assert "작업: 정산 · 주기: 매월 1일 · 담당: 운영팀" in chunks[0].text


def test_read_pdf_pages_and_numbered_headings(tmp_path):
    chunks = make_chunks(read_document(make_pdf(tmp_path / "guide.pdf", ["1. Batch", "Run manually on day 1."])))
    assert chunks[0].section == "1" and chunks[0].page == "1" and "Run manually" in chunks[0].text


def test_read_markdown_and_cp949_text(tmp_path):
    (tmp_path / "a.md").write_text("# 운영\n\n## 3.2 배치\n수동 실행\n\n```\n# 주석은 제목 아님\n```\n", encoding="utf-8")
    chunks = make_chunks(read_document(tmp_path / "a.md"))
    assert chunks[0].section == "3.2" and chunks[0].heading == "운영 > 3.2 배치"
    assert "# 주석은 제목 아님" in chunks[0].text
    (tmp_path / "b.txt").write_bytes("1. 개요\n휴가 업무를 설명한다.\n2. 운영\n배치는 매월 실행한다.\n".encode("cp949"))
    chunks = make_chunks(read_document(tmp_path / "b.txt"))
    assert [(c.section, c.text) for c in chunks] == [("1", "휴가 업무를 설명한다."), ("2", "배치는 매월 실행한다.")]


def test_read_rejects_unsupported_and_empty(tmp_path):
    (tmp_path / "x.doc").write_bytes(b"\xd0\xcf")
    with pytest.raises(DocReadError, match="지원하지 않는"):
        read_document(tmp_path / "x.doc")
    (tmp_path / "empty.md").write_text("\n\n", encoding="utf-8")
    with pytest.raises(DocReadError, match="글자를 찾지 못했어요"):
        read_document(tmp_path / "empty.md")
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    with pytest.raises(DocReadError, match="zip"):
        read_document(tmp_path / "broken.docx")


def test_long_section_is_split_by_paragraph(tmp_path):
    body = "\n\n".join(f"{i}번째 문단입니다. " + "가나다라마바사 " * 40 for i in range(8))
    (tmp_path / "long.md").write_text(f"# 긴 절\n\n{body}\n", encoding="utf-8")
    chunks = make_chunks(read_document(tmp_path / "long.md"))
    assert len(chunks) > 1 and all(len(c.text) <= MAX_CHARS * 1.2 for c in chunks)
    assert {c.heading for c in chunks} == {"긴 절"}


# ── 코드와 잇기·불일치·근거 (작은 Java 프로젝트) ──────────

JAVA = {
    "src/main/java/demo/web/LeaveController.java": """package demo.web;
/** 휴가관리 컨트롤러 */
@Controller
public class LeaveController {
    @Resource(name = "leaveService") private LeaveService leaveService;

    /** 휴가를 신청한다. */
    @RequestMapping("/lv/apply.do")
    public String apply(LeaveVO vo) { leaveService.apply(vo); return "lv/List"; }

    /** 휴가를 승인한다. */
    @RequestMapping("/lv/approve.do")
    public String approve(LeaveVO vo) { leaveService.approve(vo); return "lv/List"; }

    /** 휴가 목록 */
    @RequestMapping("/lv/list.do")
    public String list(LeaveVO vo) { return "lv/List"; }
}
""",
    "src/main/java/demo/service/LeaveServiceImpl.java": """package demo.service;
/** 휴가관리 서비스 */
@Service("leaveService")
public class LeaveServiceImpl {
    @Resource(name = "leaveDAO") private LeaveDAO leaveDAO;

    public void apply(LeaveVO vo) {
        int remain = leaveDAO.selectRemain(vo);
        if (remain < 3) {
            throw new IllegalStateException("no leave");
        }
        leaveDAO.insert(vo);
    }

    public void approve(LeaveVO vo) {
        if ("02".equals(vo.getStatus())) {
            leaveDAO.updateStatus(vo);
        }
    }
}
""",
    "src/main/java/demo/service/LeaveDAO.java": """package demo.service;
@Repository("leaveDAO")
public class LeaveDAO {
    public Integer selectRemain(LeaveVO vo) { return selectOne("Leave.selectRemain", vo); }
    public void insert(LeaveVO vo) { insert("Leave.insert", vo); }
    public void updateStatus(LeaveVO vo) { update("Leave.updateStatus", vo); }
}
""",
    "src/main/resources/mapper/Leave_SQL_oracle.xml": """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="Leave">
  <select id="selectRemain">SELECT REMAIN FROM TB_LEAVE_REMAIN WHERE USER_ID = #{userId}</select>
  <insert id="insert">INSERT INTO TB_LEAVE (USER_ID) VALUES (#{userId})</insert>
  <update id="updateStatus">UPDATE TB_LEAVE SET STATUS = '03' WHERE ID = #{id}</update>
  <select id="selectLog">SELECT * FROM TB_LEAVE_LOG</select>
</mapper>""",
}

# 맞는 문장과 일부러 틀린 문장 (틀린 것: 숫자 5, 코드 값 '04', 없는 테이블, 안 쓰는 테이블, 없는 URL)
HANDOVER = """# 휴가관리 인수인계서

작성일: 2025-03-10

## 1. 휴가 신청

휴가 신청(/lv/apply.do)은 TB_LEAVE 테이블에 저장한다.
남은 연차가 3일 미만이면 신청할 수 없다.

## 2. 휴가 승인

휴가 승인(/lv/approve.do)은 상태 코드 '04'일 때만 처리한다.
승인 결과는 TB_LEAVE_HISTORY 테이블에 남긴다.
휴가 승인(/lv/approve.do)은 TB_LEAVE_REMAIN 테이블을 수정한다.

## 3. 휴가 취소

휴가 취소는 /lv/cancel.do 에서 한다.
LeaveServiceImpl.apply 는 남은 연차가 5일 미만이면 막는다.

## 4. 운영 정보

운영 DB 비밀번호: Fake#Pw123
운영 서버 10.1.2.30, 계정 admin / FakeAdm1n
매월 1일 새벽 2시에 휴가 정산 배치를 수동으로 실행한다.
"""


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "leave"
    for rel, content in JAVA.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = db.connect(tmp_path / "leave.db")
    index_repository(conn, repo, dialect="oracle")
    doc = tmp_path / "docs" / "휴가관리-인수인계.md"
    doc.parent.mkdir()
    doc.write_text(HANDOVER, encoding="utf-8")
    yield conn, repo, doc
    conn.close()


def test_add_document_redacts_links_and_keeps_only_chunks(project):
    conn, _, doc = project
    r = add_document(conn, doc, "tester")
    assert r.status == "added" and r.chunks == 5 and r.doc_date == "2025-03-10" and r.date_source == "본문"
    assert r.redactions == {"비밀번호": 1, "IP": 1, "계정/암호": 1}
    stored = "\n".join(t for (t,) in conn.execute("SELECT text FROM doc_chunks"))
    assert "Fake#Pw123" not in stored and "FakeAdm1n" not in stored and "10.1.2.30" not in stored
    assert "[가림:비밀번호]" in stored
    links = {(k, v, f) for k, v, f in conn.execute("SELECT kind, value, found FROM doc_links")}
    assert {("url", "/lv/apply.do", 1), ("url", "/lv/approve.do", 1), ("table", "TB_LEAVE", 1),
            ("table", "TB_LEAVE_REMAIN", 1), ("method", "LeaveServiceImpl.apply", 1),
            ("label", "휴가관리", 1)} <= links
    # 코드에 없는 이름은 불일치 후보로 남긴다
    assert ("url", "/lv/cancel.do", 0) in links and ("table", "TB_LEAVE_HISTORY", 0) in links
    assert set(r.missing) == {"table TB_LEAVE_HISTORY", "url /lv/cancel.do"}
    # 같은 파일을 다시 넣으면 건너뛰고, 내용이 바뀌면 새 판으로 바꾼다
    assert add_document(conn, doc).status == "unchanged"
    doc.write_text(HANDOVER + "\n## 5. 추가\n내용\n", encoding="utf-8")
    assert add_document(conn, doc).status == "replaced"
    assert [d["chunks"] for d in list_documents(conn)] == [6]


def test_conflicts_catch_wrong_sentences_only(project):
    conn, _, doc = project
    add_document(conn, doc)
    found = {(c.kind, c.doc_says) for c in all_conflicts(conn)}
    assert found == {
        ("code_value", "'04'"),
        ("table_missing", "TB_LEAVE_HISTORY"),
        ("table", "/lv/approve.do → TB_LEAVE_REMAIN"),
        ("url_missing", "/lv/cancel.do"),
        ("number", "5일"),
    }
    c = next(c for c in all_conflicts(conn) if c.kind == "number")
    assert c.target == "LeaveServiceImpl.apply" and "3" in c.code_says
    assert c.message.startswith("문서와 코드가 달라요")
    assert c.doc_title == "휴가관리-인수인계.md 3 휴가 취소 (2025-03)"


def test_conflicts_become_doc_check_questions_and_survive_refresh(project):
    conn, repo, doc = project
    add_document(conn, doc)
    new, _, _ = sync_doc_questions(conn)
    assert new == 5
    rows = conn.execute("SELECT * FROM questions WHERE kind = 'doc_check'").fetchall()
    approve = next(r for r in rows if "TB_LEAVE_REMAIN" in r["question"])
    assert approve["target"] == "LeaveController.approve" and approve["file_path"].endswith("LeaveController.java")
    # 코드 질문을 다시 만들어도 '문서 확인' 질문은 남는다 (답도 지킨다)
    conn.execute("UPDATE questions SET status='answered', answer='TB_LEAVE만 씀' WHERE id = ?", (approve["id"],))
    conn.commit()
    refresh_questions(conn)
    kept = conn.execute("SELECT status, answer, stale FROM questions WHERE id = ?", (approve["id"],)).fetchone()
    assert tuple(kept) == ("answered", "TB_LEAVE만 씀", 0)
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE kind = 'doc_check'").fetchone()[0] == 5
    # 문서를 지우면 답 없는 '문서 확인' 질문은 사라지고, 답한 것은 기록으로 남는다
    delete_document(conn, list_documents(conn)[0]["id"])
    sync_doc_questions(conn)
    left = conn.execute("SELECT stale, answer FROM questions WHERE kind = 'doc_check'").fetchall()
    assert [tuple(r) for r in left] == [(1, "TB_LEAVE만 씀")]


def test_documents_survive_reindex_and_relink(project):
    conn, repo, doc = project
    add_document(conn, doc)
    index_repository(conn, repo, dialect="oracle")  # db.reset: 분석 테이블만 비운다
    from sasu.docs.linking import relink_all

    assert relink_all(conn) > 0
    assert conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0] == 5
    # 스키마 버전이 바뀌어 분석 테이블을 지워도 문서는 남는다
    assert {"documents", "doc_chunks", "doc_links"} <= db.PRESERVED_TABLES


def test_doc_evidence_has_doc_level_and_title(project):
    conn, repo, doc = project
    add_document(conn, doc)
    target = resolve_target(conn, "LeaveServiceImpl.apply")[0]
    evidence = evidence_for(conn, target, use_git=False, question="연차가 모자라면 신청이 돼?")
    docs = [e for e in evidence if e.level == DOC]
    assert docs and docs[0].title == "휴가관리-인수인계.md 1 휴가 신청 (2025-03)"
    assert docs[0].chunk_id is not None and docs[0].location == "휴가관리-인수인계.md 1"
    assert {e.id for e in evidence} == {f"E{i}" for i in range(1, len(evidence) + 1)}
    # 문서에만 있는 운영 지식은 연결이 없어도 질문으로 찾는다
    hits = search_docs(conn, "정산 배치는 언제 수동으로 돌려?")
    assert hits[0].title == "휴가관리-인수인계.md 4 운영 정보 (2025-03)" and hits[0].coverage >= 0.34
    # 근거로 넣은 조각의 불일치 (대상 apply: 문서 5일 vs 코드 3)
    kinds = {c.kind for c in doc_conflicts_for(conn, evidence)}
    assert "number" in kinds


def test_no_docs_means_no_change(project):
    conn, _, _ = project
    target = resolve_target(conn, "LeaveServiceImpl.apply")[0]
    evidence = evidence_for(conn, target, use_git=False, question="연차")
    assert all(e.level != DOC for e in evidence)
    assert DOC_RULES not in build_messages("q", "t", evidence)[0]["content"]
    assert search_docs(conn, "연차") == [] and len(DocIndex(conn)) == 0


def test_answer_doc_label_validation_and_conflict_sentence():
    evidence = [Evidence("E1", FACT, "코드", "if (remain < 3)"), Evidence("E2", DOC, "인수인계서.md 1 신청 (2025-03)", "5일")]
    assert DOC_RULES in build_messages("q", "t", evidence)[0]["content"]
    raw = ("[문서] 남은 연차가 5일 미만이면 막는다고 적혀 있다 (E2)\n"
           "[사실] 문서에 따르면 5일이다 (E2)\n"
           "[문서] 코드는 3을 쓴다 (E1)\n"
           "[추정] 문서와 코드가 달라요: 문서는 5일, 코드는 3이다 (E1, E2)\n"
           "물어볼 질문: 지금 기준은 3일인가요, 5일인가요?")
    result = validate(raw, evidence)
    labels = [(s.label, s.text) for s in result.sentences]
    assert labels[0] == ("문서", "문서에 따르면 남은 연차가 5일 미만이면 막는다고 적혀 있다")
    assert labels[1][0] == "추정"  # 문서만으로 [사실]은 안 된다
    assert labels[2][0] == "추정"  # [문서]인데 문서 근거가 없음
    assert result.doc_conflicts == ["문서와 코드가 달라요: 문서는 5일, 코드는 3이다"]


def test_add_folder_reports_failures(project, tmp_path):
    conn, _, doc = project
    (doc.parent / "잠김.hwp").write_bytes(b"not ole")
    (doc.parent / "무시.bin").write_bytes(b"x")
    results = {r.name: r for r in add_path(conn, doc.parent)}
    assert set(results) == {"휴가관리-인수인계.md", "잠김.hwp"}
    assert results["잠김.hwp"].status == "failed" and "HWP 5.0" in results["잠김.hwp"].reason


# ── 웹 API ────────────────────────────────────────────────

@pytest.fixture
def web(project, tmp_path, monkeypatch):
    conn, _, doc = project
    monkeypatch.setenv("SASU_DB_PATH", str(tmp_path / "leave.db"))
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    embedder_or_none.cache_clear()
    from sasu.web import server

    return TestClient(server.create_app()), doc


def test_web_upload_list_search_linked_delete(web, tmp_path):
    client, doc = web
    docx = make_docx(tmp_path / "운영.docx", [("h1", "장애 대응"), ("p", "휴가 승인(/lv/approve.do) 오류는 운영팀장에게 알린다. 비밀번호: FakeWeb#1")])
    files = [("files", (doc.name, doc.read_bytes(), "text/markdown")),
             ("files", ("운영.docx", docx.read_bytes(), "application/octet-stream")),
             ("files", ("x.exe", b"MZ", "application/octet-stream"))]
    data = client.post("/api/docs", files=files, data={"by": "kim"}).json()
    status = {r["name"]: r["status"] for r in data["results"]}
    assert status == {"휴가관리-인수인계.md": "added", "운영.docx": "added", "x.exe": "failed"}
    assert data["doc_conflicts"] == 5
    docx_result = next(r for r in data["results"] if r["name"] == "운영.docx")
    assert docx_result["redactions"] == {"비밀번호": 1}
    listed = client.get("/api/docs/list").json()["documents"]
    assert [d["name"] for d in listed] == ["운영.docx", "휴가관리-인수인계.md"] and listed[0]["added_by"] == "kim"
    hits = client.get("/api/docs/search", params={"q": "정산 배치 수동 실행"}).json()["hits"]
    assert hits[0]["title"].startswith("휴가관리-인수인계.md 4 운영 정보") and "text" in hits[0]
    linked = client.get("/api/docs/linked", params={"kind": "url", "value": "/lv/approve.do"}).json()["chunks"]
    assert {c["doc"] for c in linked} == {"운영.docx", "휴가관리-인수인계.md"}
    detail = client.get(f"/api/docs/{listed[0]['id']}").json()
    assert detail["document"]["name"] == "운영.docx" and "FakeWeb#1" not in json.dumps(detail, ensure_ascii=False)
    chunk = client.get(f"/api/docs/chunks/{linked[0]['id']}").json()
    assert chunk["links"]
    assert len(client.get("/api/docs/conflicts").json()["conflicts"]) == 5
    assert client.delete(f"/api/docs/{listed[0]['id']}").json() == {"deleted": listed[0]["id"]}
    assert client.delete(f"/api/docs/{listed[0]['id']}").status_code == 404
    assert client.get("/api/docs").status_code == 200  # Swagger는 그대로


def test_web_ask_trace_questions_show_docs(web):
    client, doc = web
    client.post("/api/docs", files=[("files", (doc.name, doc.read_bytes(), "text/markdown"))])
    ev = client.post("/api/ask/evidence", json={"question": "연차가 모자라면?", "target": "LeaveServiceImpl.apply",
                                                "use_git": False}).json()
    docs = [e for e in ev["evidence"] if e["level"] == "문서"]
    assert docs and docs[0]["title"].endswith("(2025-03)") and docs[0]["chunk_id"]
    assert any(c["kind"] == "number" for c in ev["doc_conflicts"])
    assert "docs" in ev
    tree = client.get("/api/trace", params={"url": "/lv/approve.do"}).json()["tree"]
    assert tree["docs"] >= 1  # URL에 이어진 조각
    qs = client.get("/api/questions").json()
    doc_q = [q for q in qs["items"] if q["kind"] == "doc_check"]
    assert len(doc_q) == 5 and qs["kinds"]["doc_check"] == "문서 확인"
    assert any(q["docs"] >= 1 for q in qs["items"])


def test_web_answer_reports_rule_and_llm_conflicts(web, monkeypatch):
    client, doc = web

    class FakeLLM:
        available = True
        model = "fake"

        def chat(self, messages):
            assert "'문서' 근거는" in messages[0]["content"]  # 문서 근거가 있으면 [문서] 규칙을 더한다
            doc_id = next(line.split("]")[0][1:] for line in messages[1]["content"].splitlines() if "(문서)" in line)
            return (f"[문서] 남은 연차가 5일 미만이면 막는다 ({doc_id})\n"
                    f"[추정] 문서와 코드가 달라요: 문서는 5일, 코드는 3이다 (E1, {doc_id})\n"
                    "물어볼 질문: 지금 기준은 3일인가요?")

    monkeypatch.setattr("sasu.rag.answer.make_llm_client", lambda settings: FakeLLM())
    client.post("/api/docs", files=[("files", (doc.name, doc.read_bytes(), "text/markdown"))])
    data = client.post("/api/ask/answer", json={"question": "연차가 모자라면?", "target": "LeaveServiceImpl.apply",
                                                "use_git": False}).json()
    assert data["sentences"][0]["label"] == "문서" and data["sentences"][0]["text"].startswith("문서에 따르면")
    assert data["doc_conflicts"]["llm"] == ["문서와 코드가 달라요: 문서는 5일, 코드는 3이다"]
    assert any(c["kind"] == "number" for c in data["doc_conflicts"]["rule"])


def test_mcp_gather_evidence_includes_doc_grade(project, tmp_path, monkeypatch):
    conn, _, doc = project
    add_document(conn, doc)
    monkeypatch.setenv("SASU_DB_PATH", str(tmp_path / "leave.db"))
    from sasu.mcp_server import gather_evidence

    text = gather_evidence("연차가 모자라면?", target="LeaveServiceImpl.apply", use_git=False)
    assert "(문서) 휴가관리-인수인계.md 1 휴가 신청 (2025-03)" in text
    assert "[문서]" in text and "※ 문서와 코드가 달라요" in text


def test_cli_docs_add_list_conflicts_remove(project, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sasu.cli import app

    conn, _, doc = project
    conn.close()
    monkeypatch.setenv("SASU_DB_PATH", str(tmp_path / "leave.db"))
    monkeypatch.setenv("COLUMNS", "200")
    runner = CliRunner()
    added = runner.invoke(app, ["docs", "add", str(doc.parent), "--by", "kim"])
    assert added.exit_code == 0, added.output
    assert "넣음" in added.output and "비밀번호 1" in added.output and "문서와 코드가 다른 곳: 5건" in added.output
    listed = runner.invoke(app, ["docs", "list"])
    assert "휴가관리-인수인계.md" in listed.output and "2025-03-10 (본문)" in listed.output
    found = runner.invoke(app, ["docs", "conflicts"])
    assert found.output.count("문서와 코드가 달라요") == 5
    searched = runner.invoke(app, ["docs", "search", "정산 배치"])
    assert "[문서] 휴가관리-인수인계.md 4 운영 정보 (2025-03)" in searched.output
    assert runner.invoke(app, ["docs", "remove", "1"]).exit_code == 0
    assert "넣은 문서가 없어요" in runner.invoke(app, ["docs", "list"]).output
