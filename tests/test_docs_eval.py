"""문서 기능 개선과 평가(sasu eval-docs): 띄어 쓴 비밀, 기호 소제목, 컬럼 이름, 설정 연결, LLM 판정(가짜 LLM), 평가 지표.
LLM·임베딩은 부르지 않는다."""

import json

import pytest

from sasu import db
from sasu.docs.chunker import make_chunks
from sasu.docs.configs import config_facts, scheduled_targets
from sasu.docs.conflicts import all_conflicts, sync_doc_questions
from sasu.docs.judge import ConfigIndex, ConflictJudge, merge, parse_judgement
from sasu.docs.linking import CodeNames
from sasu.docs.readers import Block, RawDoc
from sasu.docs.redact import redact
from sasu.docs.store import add_document
from sasu.indexer import index_repository
from sasu.rag.pipeline import embedder_or_none
from test_docs import HANDOVER, JAVA


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    monkeypatch.setenv("SASU_DOC_JUDGE", "off")
    embedder_or_none.cache_clear()
    yield
    embedder_or_none.cache_clear()


# ── 비밀: 구분 기호 없이 띄어 쓴 꼴 ─────────────────────────

@pytest.mark.parametrize("text,secret", [
    ("공공데이터 수신 서비스키: 7bQ2kXz9LmP4vT8rWc3NyH6jD1sF5gA0eU2%3D%3D", "7bQ2kXz9LmP4vT8rWc3NyH6jD1sF5gA0eU2%3D%3D"),
    ("비고: 연계 API 키 SMSAPI-7f3a9c21e4b84d0f9a6e (재개 시 사용)", "SMSAPI-7f3a9c21e4b84d0f9a6e"),
    ("ssh opsuser@host / 비밀번호 Nas#Mnt2024", "Nas#Mnt2024"),
])
def test_redact_spaced_secrets(text, secret):
    hidden, counts = redact(text)
    assert secret not in hidden and sum(counts.values()) >= 1


@pytest.mark.parametrize("text", [
    "비밀번호 변경 주기는 90일임",
    "API 키 발급 절차는 정보보안 담당 승인 후 진행",
    "비밀번호 SHA-256 해시로 저장",
    "인증키 재발급은 매년 1월",
])
def test_redact_spaced_leaves_ordinary_text(text):
    assert redact(text) == (text, {})


def test_redact_reports_masked_values_for_evaluation():
    found: list = []
    redact("계정 admin / Fake!Pass1, 서버 10.1.2.3", found)
    assert ("계정/암호", "Fake!Pass1") in found and ("IP", "10.1.2.3") in found


# ── 기호로 쓴 소제목 (한글 문서에 흔한 ■ 2023-05-17 장애) ────

def test_marker_lines_become_headings_under_nearest_heading():
    doc = RawDoc("장애이력.hwpx", "hwpx", [
        Block("para", "장애 이력 (2023 ~ 2025)"),
        Block("heading", "1. 장애 목록", 1),
        Block("para", "■ 2023-05-17 휴가 승인 오류"),
        Block("para", "- 현상: 승인 버튼에서 오류"),
        Block("para", "□ 조치"),
        Block("para", "- 대리 승인 처리함."),
        Block("para", "■ 2023-09-04 배치 멈춤"),
        Block("para", "- 현상: 수행중으로 남음"),
        Block("para", "■ 반드시 백업할 것."),  # 문장으로 끝나면 본문
    ])
    chunks = make_chunks(doc)
    assert [c.heading for c in chunks] == [
        "", "1. 장애 목록 > 2023-05-17 휴가 승인 오류", "1. 장애 목록 > 2023-05-17 휴가 승인 오류 > 조치",
        "1. 장애 목록 > 2023-09-04 배치 멈춤"]
    assert "■ 반드시 백업할 것." in chunks[-1].text


# ── 설정 파일과 잇기 ──────────────────────────────────────

CONFIG = {
    "src/main/resources/props/globals.properties": (
        "# \\ube44\\ubc00\\ubc88\\ud638 \\uc720\\ud6a8\\uae30\\uac04\n"  # 비밀번호 유효기간
        "Globals.ExpirePwdDay = 180\n"
        "Globals.fileStorePath = /upload/files/\n"
        "Globals.mysql.Password = realLooking#Pw1\n"
        "SHELL.UNIX.batchShellFiles\t= \n"),
    "src/main/resources/spring/context-scheduling-leave.xml": """<beans>
  <bean id="leaveJob" class="org.springframework.scheduling.quartz.MethodInvokingJobDetailFactoryBean">
    <property name="targetObject" ref="leaveServiceImpl" />
    <property name="targetMethod" value="apply" />
  </bean>
  <!-- 매 12시간마다 -->
  <bean id="leaveTrigger" class="org.springframework.scheduling.quartz.SimpleTriggerFactoryBean">
    <property name="repeatInterval" value="43200000" />
  </bean>
</beans>""",
    "src/main/webapp/WEB-INF/web.xml": "<web-app><session-config><session-timeout>60</session-timeout></session-config></web-app>",
    "src/test/resources/globals.properties": "Globals.ExpirePwdDay = 1\n",
    "app.yml": "server:\n  port: 8080  # 포트\n",
}


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "leave"
    for rel, content in {**JAVA, **CONFIG}.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = db.connect(tmp_path / "leave.db")
    index_repository(conn, repo, dialect="oracle")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "휴가관리-인수인계.md").write_text(HANDOVER + """
## 5. 설정

휴가 첨부는 /upload/files 아래에 쌓인다.
비밀번호 유효기간은 Globals.ExpirePwdDay 로 정한다.
휴가 정산 배치 설정은 context-scheduling-leave.xml 에 있다.
배치 화이트리스트는 SHELL.UNIX.batchShellFiles 에 적는다.
TB_LEAVE 테이블의 USER_ID 컬럼은 사번이다.
""", encoding="utf-8")
    yield conn, repo, docs
    conn.close()


def test_config_facts_properties_xml_yaml(project):
    _, repo, _ = project
    facts = {f.key: f for f in config_facts(repo)}
    assert facts["Globals.ExpirePwdDay"].value == "180" and facts["Globals.ExpirePwdDay"].comment == "비밀번호 유효기간"
    assert facts["Globals.ExpirePwdDay"].path == "src/main/resources/props/globals.properties"  # test 폴더는 뺀다
    assert facts["SHELL.UNIX.batchShellFiles"].value == ""  # 빈 값도 키로 둔다
    assert "Globals.mysql.Password" not in facts  # 비밀번호 값은 쓰지 않는다
    assert facts["leaveTrigger.repeatInterval"].value == "43200000" and facts["leaveTrigger.repeatInterval"].comment == "매 12시간마다"
    assert facts["session-timeout"].value == "60" and facts["server.port"].value == "8080"
    assert scheduled_targets(repo) == {"context-scheduling-leave.xml": [("leaveServiceImpl", "apply")]}
    index = ConfigIndex(list(facts.values()))
    assert index.resolve("${Globals.ExpirePwdDay}") == "${Globals.ExpirePwdDay} (= 180)"
    assert [f.key for f in index.referenced(['getProperty("Globals.ExpirePwdDay")'])] == ["Globals.ExpirePwdDay"]
    assert {f.key for f in index.in_files("설정은 context-scheduling-leave.xml 에")} >= {"leaveTrigger.repeatInterval"}


def test_links_config_keys_values_files_and_skips_columns(project):
    conn, _, _ = project
    names = CodeNames(conn)
    found = {(l.kind, l.value, l.found) for l in names.find(
        "첨부는 /upload/files 아래. Globals.ExpirePwdDay 참고. 배치는 context-scheduling-leave.xml 에 있다.")}
    assert ("config", "src/main/resources/props/globals.properties:3", True) in found
    assert ("config", "src/main/resources/props/globals.properties:2", True) in found
    assert ("config", "src/main/resources/spring/context-scheduling-leave.xml", True) in found
    assert ("method", "LeaveServiceImpl.apply", True) in found  # 스케줄 설정이 부르는 메서드
    # SQL의 컬럼 이름은 '테이블' 옆에 있어도 없는 테이블로 보지 않는다
    assert not any(l.kind == "table" and l.value == "USER_ID" for l in names.find("TB_LEAVE 테이블의 USER_ID 컬럼"))


# ── LLM 판정 (가짜 LLM) ───────────────────────────────────

class FakeLLM:
    model = "fake-judge"
    available = True

    def __init__(self, reply):
        self.reply = reply
        self.calls: list = []

    def chat(self, messages):
        self.calls.append(messages)
        return self.reply(messages) if callable(self.reply) else self.reply


def _reply(messages):
    """'5일 미만' 문장을 근거 E1과 다르다고 답한다 (문장 번호는 프롬프트에서 찾는다)."""
    user = messages[1]["content"]
    for line in user.splitlines():
        if line.startswith("S") and "5일 미만" in line:
            s = line.split(":", 1)[0][1:]
            return '앞말 ```json\n{"conflicts": [{"s": %s, "e": "E1", "doc": "5일", "code": "remain < 3"}]}\n```' % s
    return '{"conflicts": []}'


def test_judge_finds_merges_caches_and_respects_cached_only(project, tmp_path):
    conn, _, docs = project
    add_document(conn, docs / "휴가관리-인수인계.md")
    llm = FakeLLM(_reply)
    judge = ConflictJudge(llm, tmp_path / "judge-cache")
    found = judge.conflicts(conn)
    assert [(c.kind, c.doc_says, c.code_says.split(" (")[0]) for c in found] == [("llm", "5일", "remain < 3")]
    prompt = llm.calls[0][1]["content"]
    assert "[E1]" in prompt and "Fake#Pw123" not in prompt  # 가린 비밀은 보내지 않는다
    linked = conn.execute("SELECT COUNT(DISTINCT chunk_id) FROM doc_links WHERE found = 1").fetchone()[0]
    calls = judge.stats()["calls"]
    assert len(llm.calls) == calls and 0 < calls <= linked  # 코드와 이어진 조각마다 많아야 한 번
    # 같은 조각·같은 근거면 캐시: 다시 묻지 않는다
    again = ConflictJudge(FakeLLM("never"), tmp_path / "judge-cache")
    assert len(again.conflicts(conn)) == 1 and again.stats()["cached"] == calls and not again.client.calls
    # 캐시만 읽기: 캐시에 없으면 LLM을 부르지 않는다
    offline = ConflictJudge(FakeLLM("never"), tmp_path / "other-cache", cached_only=True)
    assert offline.conflicts(conn) == [] and not offline.client.calls
    # 규칙이 이미 잡은 문장(5일)은 LLM 것을 빼고 합친다
    rule = all_conflicts(conn, judge=None)
    merged = merge(rule, found)
    assert len(merged) == len(rule) and sum(c.kind == "number" for c in merged) == 1


def test_judge_conflicts_become_doc_check_questions(project, tmp_path):
    conn, _, docs = project
    add_document(conn, docs / "휴가관리-인수인계.md")

    def reply(messages):
        user = messages[1]["content"]
        line = next((l for l in user.splitlines() if l.startswith("S") and "Globals.ExpirePwdDay" in l), None)
        if line:
            return json.dumps({"conflicts": [{"s": int(line.split(":")[0][1:]), "e": "E1", "doc": "90일", "code": "180"}]})
        return '{"conflicts": []}'

    judge = ConflictJudge(FakeLLM(reply), tmp_path / "c")
    new, _, _ = sync_doc_questions(conn, judge)
    rows = conn.execute("SELECT question, priority FROM questions WHERE kind = 'doc_check'").fetchall()
    assert new == len(rows) and any("'90일'" in r["question"] and r["priority"] == 70.0 for r in rows)
    # 판정기 없이(기본: SASU_DOC_JUDGE=off) 맞추면 LLM 질문은 답이 없어 사라진다
    sync_doc_questions(conn)
    assert not any("'90일'" in r[0] for r in conn.execute("SELECT question FROM questions WHERE kind = 'doc_check'"))


def test_parse_judgement_is_forgiving():
    assert parse_judgement('{"conflicts": [{"s": 1, "e": "E2"}]}') == [{"s": 1, "e": "E2"}]
    assert parse_judgement("설명\n```json\n{\"conflicts\": []}\n```") == []
    assert parse_judgement("모르겠어요") == [] and parse_judgement('{"conflicts": "x"}') == []


def test_judge_skips_chunks_without_code_links_and_llm_errors(project, tmp_path):
    conn, _, docs = project
    add_document(conn, docs / "휴가관리-인수인계.md")

    class Broken(FakeLLM):
        def chat(self, messages):
            raise RuntimeError("API down")

    judge = ConflictJudge(Broken(None), tmp_path / "c")
    assert judge.conflicts(conn) == [] and judge.stats()["errors"] >= 1  # 실패하면 규칙 판정만


# ── 평가 (sasu eval-docs) ─────────────────────────────────

KEY = {
    "문서": {"휴가관리-인수인계.md": {"문서 날짜": "2025-03-10", "형식": "md"}},
    "wrong": [
        {"id": "W1", "문서": "휴가관리-인수인계.md", "문장": "휴가 승인(/lv/approve.do)은 상태 코드 '04'일 때만 처리한다.",
         "anchor": "상태 코드 '04'일 때만", "종류": "상태값"},
        {"id": "W2", "문서": "휴가관리-인수인계.md", "문장": "휴가 취소는 /lv/cancel.do 에서 한다.",
         "anchor": "/lv/cancel.do 에서 한다", "종류": "URL"},
        {"id": "W3", "문서": "휴가관리-인수인계.md", "문장": "남은 연차가 3일 미만이면 신청할 수 없다.",
         "anchor": "남은 연차가 3일 미만이면", "종류": "숫자"},  # (평가 시험용으로 틀렸다고 친다) 규칙으로는 못 잡는다
    ],
    "secrets": [
        {"id": "S1", "문서": "휴가관리-인수인계.md", "종류": "비밀번호", "원문": "Fake#Pw123"},
        {"id": "S2", "문서": "휴가관리-인수인계.md", "종류": "사설 IP", "원문": "10.1.2.30"},
    ],
    "doc_questions": {
        "doc_only": [{"id": "Q1", "질문": "정산 배치는 언제 손으로 돌려?", "문서": "휴가관리-인수인계.md",
                      "anchor": "휴가 정산 배치를 수동으로"}],
        "doc_and_code": [{"id": "QC1", "질문": "휴가 승인은 어떤 상태일 때 돼?", "문서": "휴가관리-인수인계.md",
                          "anchor": "상태 코드 '04'"}],
    },
    "code_links": [
        {"id": "L1", "문서": "휴가관리-인수인계.md", "언급": "/lv/apply.do", "종류": "url", "이어져야 할 코드": ["LeaveController.apply"]},
        {"id": "L2", "문서": "휴가관리-인수인계.md", "언급": "TB_LEAVE_HISTORY", "종류": "table", "이어져야 할 코드": []},
        {"id": "L3", "문서": "휴가관리-인수인계.md", "언급": "Globals.ExpirePwdDay", "종류": "config",
         "이어져야 할 코드": ["src/main/resources/props/globals.properties:2"]},
        {"id": "L4", "문서": "휴가관리-인수인계.md", "언급": "휴가 승인(/lv/approve.do)", "종류": "url",
         "이어져야 할 코드": ["LeaveServiceImpl.approve"]},  # URL은 컨트롤러로 이어진다: 정확히는 못 맞힘
    ],
}


def test_eval_docs_metrics_on_copy(project, tmp_path):
    from sasu.evaluation.docs_eval import run, summary_rows

    conn, _, docs = project
    conn.close()
    (docs / "비교용.txt").write_text("같은 내용 사본", encoding="utf-8")  # 정답 목록에 없는 파일은 읽기만
    key = tmp_path / "key.json"
    key.write_text(json.dumps(KEY, ensure_ascii=False), encoding="utf-8")
    source = tmp_path / "leave.db"
    before = source.read_bytes()
    r = run(docs, key, source, tmp_path / "work", everyday=False)
    assert source.read_bytes() == before  # 원본 분석 파일은 그대로
    assert {f["file"]: f["ok"] for f in r.formats} == {"비교용.txt": True, "휴가관리-인수인계.md": True}
    assert next(f for f in r.formats if f["file"].endswith(".md"))["date_ok"] is True
    assert [a["name"] for a in r.meta["added"]] == ["휴가관리-인수인계.md"]
    assert r.secrets["hidden"] == 2 and r.secrets["recall"] == 1.0
    # 정답 목록에 없는데 가린 것은 과잉 가림 후보로 (사람이 표본을 본다): 여기서는 목록에서 일부러 뺀 FakeAdm1n
    assert [o["value"] for o in r.secrets["over_masked"]] == ["FakeAdm1n"]
    c = r.conflicts
    assert (c["caught"], c["wrong_total"]) == (2, 3)
    assert c["false_reports"] == 3 and c["reported"] == 5  # 정답 목록에 없는 규칙 판정 3건 (정밀도 낮음으로 보임)
    assert [w["id"] for w in c["per_wrong"] if not w["caught"]] == ["W3"]
    q = r.questions
    assert q["doc_only"]["top6"] == 1 and q["doc_and_code"]["top6"] == 1
    links = {x["id"]: x["status"] for x in r.links["cases"]}
    assert links == {"L1": "exact", "L2": "missing_marked", "L3": "config", "L4": "none"}
    assert (r.links["strict"], r.links["loose"]) == (3, 3)
    rows = dict(summary_rows(r))
    assert rows["비밀 가림 재현율"].startswith("2/2") and rows["불일치 재현율"] == "2/3"


def test_eval_docs_with_fake_judge_merges(project, tmp_path):
    from sasu.evaluation.docs_eval import run

    conn, _, docs = project
    conn.close()
    key = tmp_path / "key.json"
    key.write_text(json.dumps(KEY, ensure_ascii=False), encoding="utf-8")

    def reply(messages):
        line = next((l for l in messages[1]["content"].splitlines() if l.startswith("S") and "3일 미만" in l), None)
        if line:
            return json.dumps({"conflicts": [{"s": int(line.split(":")[0][1:]), "e": "E1", "doc": "3일", "code": "2"}]})
        return '{"conflicts": []}'

    judge = ConflictJudge(FakeLLM(reply), tmp_path / "jc")
    r = run(docs, key, tmp_path / "leave.db", tmp_path / "work", everyday=False, judge=judge)
    assert r.conflicts["caught"] == 3 and r.conflicts["rule"]["caught"] == 2 and r.conflicts["llm"]["caught"] == 1
    assert r.conflicts["judge"]["model"] == "fake-judge"


def test_cli_eval_docs(project, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sasu.cli import app

    conn, _, docs = project
    conn.close()
    key = tmp_path / "key.json"
    key.write_text(json.dumps(KEY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("COLUMNS", "200")
    out = tmp_path / "out.json"
    result = CliRunner().invoke(app, ["eval-docs", "--docs", str(docs), "--key", str(key), "--db", str(tmp_path / "leave.db"),
                                      "--work", str(tmp_path / "work"), "--no-everyday", "--no-embed", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "불일치 재현율" in result.output and "2/3" in result.output and "놓친 틀린 문장: W3" in result.output
    assert json.loads(out.read_text(encoding="utf-8"))["secrets"]["hidden"] == 2


def test_cli_docs_add_and_conflicts_with_llm_judge(project, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sasu.cli import app
    from sasu.docs import judge as judge_module

    conn, _, docs = project
    conn.close()
    def reply(messages):  # 규칙이 못 잡는 문장 하나 ("3일 미만"은 코드에 3이 있어 규칙은 같다고 본다)
        line = next((l for l in messages[1]["content"].splitlines() if l.startswith("S") and "3일 미만" in l), None)
        if line:
            return json.dumps({"conflicts": [{"s": int(line.split(":")[0][1:]), "e": "E1", "doc": "3일", "code": "2"}]})
        return '{"conflicts": []}'

    fake = FakeLLM(reply)
    monkeypatch.setattr(judge_module.ConflictJudge, "from_settings",
                        classmethod(lambda cls, settings=None, cached_only=False: cls(fake, tmp_path / "jc")))
    monkeypatch.setenv("SASU_DB_PATH", str(tmp_path / "leave.db"))
    monkeypatch.setenv("COLUMNS", "200")
    runner = CliRunner()
    added = runner.invoke(app, ["docs", "add", str(docs), "--llm-judge"])
    assert added.exit_code == 0, added.output
    assert "문서와 코드가 다른 곳: 6건" in added.output  # 규칙 5 + LLM 1
    calls = len(fake.calls)
    assert calls
    found = runner.invoke(app, ["docs", "conflicts", "--llm-judge"])
    assert found.exit_code == 0 and len(fake.calls) == calls  # 같은 조각은 캐시
    # 기본(SASU_DOC_JUDGE=off)이면 규칙만, 켜면 LLM 판정도 (캐시에서)
    plain = runner.invoke(app, ["docs", "conflicts"]).output
    monkeypatch.setenv("SASU_DOC_JUDGE", "on")
    judged = runner.invoke(app, ["docs", "conflicts"]).output
    assert plain.count("문서와 코드가 달라요") + 1 == judged.count("문서와 코드가 달라요")
    assert "LLM 판정" in judged and "LLM 판정" not in plain
    assert len(fake.calls) == calls
