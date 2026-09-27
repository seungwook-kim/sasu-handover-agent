"""근거 수집, 한국어 검색, LLM 답 검증 ("왜?" 질의응답)."""

import subprocess
from pathlib import Path

import pytest

from sasu import db
from sasu.analysis.questions import generate_questions, save_questions
from sasu.githistory import NOISE_COMMIT
from sasu.indexer import index_repository
from sasu.rag.answer import answer, validate
from sasu.rag.evidence import ANSWER, FACT, RECORD, Evidence, gather, resolve_target
from sasu.rag.search import MethodIndex, tokenize

CONTROLLER = """package demo.web;
@Controller
public class VacationController {
    @Resource(name = "vacationService") private VacationService vacationService;

    /** 휴가를 신청한다. */
    @RequestMapping("/vct/apply.do")
    public String apply(VacationVO vo) {
        vacationService.apply(vo);
        return "vct/List";
    }
}
"""

SERVICE_V1 = """package demo.service;
@Service("vacationService")
public class VacationServiceImpl {
    @Resource(name = "vacationDAO") private VacationDAO vacationDAO;

    public void apply(VacationVO vo) {
        int remain = vacationDAO.selectRemain(vo);
        if (remain < 1) {
            throw new IllegalStateException("no leave");
        }
        vacationDAO.insert(vo);
    }
}
"""

# 연차 정보가 없는 사용자도 신청할 수 있게 고친 버그 수정
SERVICE_V2 = SERVICE_V1.replace(
    "int remain = vacationDAO.selectRemain(vo);",
    "Integer remain = vacationDAO.selectRemain(vo);\n        if (remain == null) { remain = 0; }",
)
# 들여쓰기만 바꾼 포맷 정리 커밋
SERVICE_V3 = SERVICE_V2.replace("    ", "\t")

FILES = {
    "src/main/java/demo/web/VacationController.java": CONTROLLER,
    "src/main/java/demo/service/VacationDAO.java": """package demo.service;
@Repository("vacationDAO")
public class VacationDAO {
    public Integer selectRemain(VacationVO vo) { return selectOne("Vacation.selectRemain", vo); }
    public void insert(VacationVO vo) { insert("Vacation.insert", vo); }
}
""",
    "src/main/java/demo/service/VacationVO.java": """package demo.service;
public class VacationVO {
    /** 남은 연차 일수 */
    private int remainDays;
    /** 휴가 신청자 아이디 */
    private String applicantId;
    public int getRemainDays() { return remainDays; }
}
""",
    "src/main/resources/mapper/Vacation_SQL_oracle.xml": """<?xml version="1.0" encoding="UTF-8"?><!--
  2026.08.27  kim  selectRemain: 연차 정보가 없으면 NULL을 돌려줌
-->
<mapper namespace="Vacation">
  <select id="selectRemain">SELECT REMAIN FROM TB_YRYC WHERE USER_ID = #{applicantId}</select>
  <insert id="insert">INSERT INTO TB_VCATN (USER_ID) VALUES (#{applicantId})</insert>
</mapper>""",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def setup(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    service = repo / "src/main/java/demo/service/VacationServiceImpl.java"
    service.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "kim")
    for content, message in [
        (SERVICE_V1, "휴가 신청 기능 추가"),
        (SERVICE_V2, "fix: 연차 미등록 사용자의 휴가 신청이 NullPointerException으로 실패하는 문제"),
        (SERVICE_V3, "이클립스 > Source > Format"),
    ]:
        service.write_text(content, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", message)
    conn = db.connect(tmp_path / "sasu.db")
    index_repository(conn, repo, dialect="oracle")
    yield conn, repo, tmp_path
    conn.close()


def test_resolve_target_forms(setup):
    conn, _, _ = setup
    assert resolve_target(conn, "VacationServiceImpl.apply")[0].label == "VacationServiceImpl.apply"
    assert resolve_target(conn, "/vct/apply.do")[0].label == "VacationController.apply"
    assert resolve_target(conn, "demo/service/VacationServiceImpl.java:8")[0].label == "VacationServiceImpl.apply"


def test_evidence_levels_and_git_history(setup):
    conn, repo, tmp = setup
    target = resolve_target(conn, "VacationServiceImpl.apply")[0]
    ev = {e.title: e for e in gather(conn, target, repo, tmp / "cache")}
    assert ev["VacationServiceImpl.apply 코드"].level == FACT
    assert "/vct/apply.do" in ev["이 메서드를 거치는 URL"].text
    assert "SQL SELECT Vacation.selectRemain (테이블: TB_YRYC)" in ev["이 메서드가 부르는 코드와 SQL"].text
    assert "연차 정보가 없으면 NULL" in ev["Mapper 수정 이력"].text
    commits = ev["이 코드를 마지막으로 바꾼 커밋"]
    assert commits.level == RECORD
    # 포맷 정리 커밋을 건너뛰고 실제 이유가 있는 커밋을 찾는다
    assert "NullPointerException" in commits.text
    assert "Format" not in commits.text


def test_handover_answer_becomes_evidence(setup):
    conn, repo, tmp = setup
    save_questions(conn, generate_questions(conn), repo)
    conn.execute(
        "INSERT INTO questions (key, kind, priority, question, why, file_path, line, target, status, answer, "
        "answered_by, answered_at, created_at, updated_at) VALUES ('k', 'note', 1, '연차가 없으면?', '-', "
        "'src/main/java/demo/service/VacationServiceImpl.java', 8, 'VacationServiceImpl.apply', 'answered', "
        "'연차 테이블에 행이 없는 신규 입사자가 있음', '김선배', '2026-09-01', 'x', 'x')"
    )
    target = resolve_target(conn, "VacationServiceImpl.apply")[0]
    answers = [e for e in gather(conn, target, repo, tmp / "cache", use_git=False) if e.level == ANSWER]
    assert "신규 입사자" in answers[0].text
    assert "김선배" in answers[0].title


def test_korean_search_uses_field_comments(setup):
    conn, _, _ = setup
    index = MethodIndex(conn)
    assert "remain" in index.expand("남은 연차가 없으면 어떻게 돼?")
    # 서비스는 잘 맞는 DAO(selectRemain)를 부르므로, 그래프 전파를 켜면 순위가 올라간다
    query = "남은 연차가 없으면 신청이 어떻게 돼?"

    def score(graph: bool) -> float:
        hits = {h.label: h.score for h in index.search(query, top=10, graph=graph)}
        return hits["VacationServiceImpl.apply"]

    assert score(graph=True) > score(graph=False)
    assert "VacationServiceImpl.apply" in [h.label for h in index.search(query, top=3)]
    assert "getRemainDays" not in [h.label.split(".")[1] for h in index.search("남은 연차", top=10)]


def test_tokenize_drops_question_words():
    tokens = tokenize("휴가 신청은 어떻게 만들어? insertVcatnManage")
    assert "어떻" not in tokens and "만들" not in tokens
    assert {"휴가", "신청", "insert", "vcatn", "manage"} <= set(tokens)


def test_noise_commit_pattern():
    assert NOISE_COMMIT.search("이클립스 > Source > Format")
    assert NOISE_COMMIT.search("v3.10.0 beta")
    assert NOISE_COMMIT.search("Initial commit")
    assert not NOISE_COMMIT.search("fix: 휴가 신청 오류")


EVIDENCE = [
    Evidence("E1", FACT, "코드", "if (remain < 1) throw"),
    Evidence("E2", RECORD, "커밋", "fix: 연차 미등록 사용자 NPE"),
    Evidence("E3", ANSWER, "인수인계 답변", "신규 입사자는 연차 행이 없음"),
]


def test_validate_keeps_well_grounded_sentences():
    raw = "[사실] 남은 연차가 1일 미만이면 예외를 던집니다. (E1)\n[답변] 신규 입사자는 연차 정보가 없습니다. (E3)"
    result = validate(raw, EVIDENCE)
    assert [(s.label, s.citations) for s in result.sentences] == [("사실", ["E1"]), ("답변", ["E3"])]
    assert result.issues == []


def test_validate_downgrades_and_flags():
    raw = (
        "[사실] 연차가 없으면 NPE가 났기 때문에 고쳤습니다. (E2)\n"  # 커밋 메시지는 '기록'이라 사실이 아님
        "[답변] 운영에서는 매년 초기화합니다. (E9)\n"  # 없는 근거
        "근거 없이 말하는 문장"
    )
    result = validate(raw, EVIDENCE)
    assert [s.label for s in result.sentences] == ["추정", "추정", "추정"]
    assert any("E9" in issue for issue in result.issues)
    assert len(result.issues) == 4  # 낮춤 3번 + 없는 근거 1번


def test_validate_unknown_and_follow_up_question():
    raw = "모름: 연차를 초기화하는 배치가 근거에 없습니다.\n물어볼 질문: 연차는 언제 누가 초기화하나요?"
    result = validate(raw, EVIDENCE)
    assert [s.label for s in result.sentences] == ["모름", "질문"]


class FakeClient:
    model = "fake-model"
    available = True

    def __init__(self, reply: str):
        self.reply = reply
        self.messages = None

    def chat(self, messages):
        self.messages = messages
        return self.reply


def test_answer_sends_evidence_and_validates():
    client = FakeClient("[사실] 1일 미만이면 예외입니다. (E1)")
    result = answer("연차가 없으면?", "VacationServiceImpl.apply", EVIDENCE, client)
    assert result.mode == "llm" and result.model == "fake-model"
    user = client.messages[1]["content"]
    assert "[E3] (인수인계 답변)" in user and "질문: 연차가 없으면?" in user


def test_answer_without_llm_returns_evidence_only():
    result = answer("연차가 없으면?", "X.y", EVIDENCE, None)
    assert result.mode == "evidence_only" and result.sentences == []
