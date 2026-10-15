"""일상어 평가 도구: split, 통계(Wilson 구간·안정성·짝 비교), eval-everyday 보고 (LLM 없이·가짜 LLM)."""

import json

import pytest
from typer.testing import CliRunner

from sasu.cli import app
from sasu.evaluation.everyday_eval import (
    CaseResult,
    VariantResult,
    majority,
    paired,
    report,
    select,
    split_of,
    stability,
    wilson,
)
from sasu.rag import expand as expand_mod
from test_expand import FakeLLM, conn  # noqa: F401  (conn: 분석 파일 fixture)

runner = CliRunner()


def _case(id_, rank, gold=True, split="dev", low=False):
    return CaseResult(id_, f"질문 {id_}", ["A.b"] if gold else [], [], rank, low, 0.0, [], 0.0, 0.0, split=split)


def test_split_defaults_to_dev_and_select():
    qs = [{"id": "E1"}, {"id": "T1", "split": "test"}, {"id": "E2", "split": "dev"}]
    assert [split_of(q) for q in qs] == ["dev", "test", "dev"]
    assert [q["id"] for q in select(qs, "dev")] == ["E1", "E2"]
    assert [q["id"] for q in select(qs, "test")] == ["T1"] and len(select(qs, "all")) == 3
    with pytest.raises(ValueError):
        select(qs, "train")
    with pytest.raises(ValueError):
        select(qs + [{"id": "X", "split": "train"}], "all")
    # holdout: 마지막 측정용 세트 (저장소 밖 파일)도 같은 도구로 잰다
    held = qs + [{"id": "H1", "split": "holdout"}]
    assert [q["id"] for q in select(held, "holdout")] == ["H1"] and len(select(held, "all")) == 4


def test_wilson_interval():
    lo, hi = wilson(12, 16)  # 75%: 대략 51~90%
    assert lo == pytest.approx(0.505, abs=0.005) and hi == pytest.approx(0.898, abs=0.005)
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == pytest.approx(1.0)
    assert wilson(0, 0) == (0.0, 1.0)
    narrow = wilson(60, 80)  # 같은 75%라도 표본이 크면 좁다
    assert narrow[1] - narrow[0] < hi - lo


def test_stability_majority_and_paired():
    hits = {"E1": [True] * 5, "E2": [False] * 5, "E3": [True, False, True, True, False], "E4": [False, True, False, False, False]}
    assert stability(hits) == {"always": 1, "never": 1, "flaky": 2, "distribution": {0: 1, 1: 1, 3: 1, 5: 1}}
    assert majority([True, True, False]) and not majority([True, False]) and not majority([])
    base = {"E1": [True], "E2": [True], "E3": [False], "E4": [False]}
    p = paired(base, hits)
    assert (p["wrong_to_right"], p["right_to_wrong"], p["both_right"], p["both_wrong"]) == (1, 1, 1, 1)
    assert p["gained"] == ["E3"] and p["lost"] == ["E2"]


def test_report_by_split_and_runs():
    base = [VariantResult("(a)", [_case("E1", None), _case("E2", 2), _case("T1", None, split="test"),
                                  _case("G1", None, gold=False, low=True)])]
    runs = [
        VariantResult("(b)", [_case("E1", 1), _case("E2", 3), _case("T1", 1, split="test"), _case("G1", None, gold=False, low=True)]),
        VariantResult("(b)", [_case("E1", None), _case("E2", 1), _case("T1", 4, split="test"), _case("G1", None, gold=False)]),
    ]
    out = report({"(a)": base, "(b)": runs}, ["dev", "test"], base="(a)")
    dev = out["dev"]["(b)"]
    assert dev["runs"] == 2 and dev["specific"] == 2 and dev["general"] == 1
    assert dev["top6"]["per_run"] == [1.0, 0.5] and dev["top6"]["mean"] == 0.75
    assert (dev["top6"]["min"], dev["top6"]["max"]) == (0.5, 1.0)
    assert dev["top6"]["pooled_ci"] == wilson(3, 4)  # 질문×실행을 합친 구간
    assert dev["top1"]["per_run"] == [0.5, 0.5]
    assert dev["stability"]["always"] == 1 and dev["stability"]["flaky"] == 1
    assert dev["paired_vs_base"]["wrong_to_right"] == 0  # E1은 1/2라 다수결로 틀림
    assert dev["low_conf_general"]["per_run"] == [1.0, 0.0]
    assert out["dev"]["(a)"]["paired_vs_base"] is None
    test = out["test"]["(b)"]
    assert test["specific"] == 1 and test["general"] == 0 and test["top6"]["mean"] == 1.0
    assert test["paired_vs_base"]["gained"] == ["T1"]


def _set(tmp_path, conn):  # noqa: F811
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    data = {"project": "x", "questions": [
        {"id": "E1", "question": "남은 연차가 없으면 신청이 어떻게 돼?",
         "gold": ["VacationServiceImpl.apply", "VacationDAO.selectRemain"]},
        {"id": "T1", "question": "등록은 어디서 해?", "gold": ["VacationDAO.insert"], "split": "test"},
        {"id": "H1", "question": "Service와 ServiceImpl은 왜 나눠?", "gold": [], "split": "test"},
    ]}
    path = tmp_path / "set.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path, db_path


def test_eval_everyday_no_llm_reports_each_split(conn, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    set_path, db_path = _set(tmp_path, conn)
    out = tmp_path / "out.json"
    result = runner.invoke(app, ["eval-everyday", "--set", str(set_path), "--db", db_path, "--no-llm", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "[dev] 정답 있는 질문 1" in result.output and "[test] 정답 있는 질문 1" in result.output
    assert "[test] H1 Service와 ServiceImpl은 왜 나눠? → (a) low_conf" in result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["summary"]) == {"dev", "test"} and data["runs"] == 1
    assert data["questions"][0]["results"]["(a) 지금 검색"][0]["run"] == 1
    only_test = runner.invoke(app, ["eval-everyday", "--set", str(set_path), "--db", db_path, "--no-llm", "--split", "test"])
    assert only_test.exit_code == 0 and "[dev]" not in only_test.output and "[test] T1" in only_test.output
    bad = runner.invoke(app, ["eval-everyday", "--set", str(set_path), "--db", db_path, "--split", "train"])
    assert bad.exit_code == 1


def test_eval_everyday_runs_variants_without_cache(conn, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    llm = FakeLLM('["insert", "remain"]')
    monkeypatch.setattr(expand_mod, "make_expand_client", lambda settings: llm)
    set_path, db_path = _set(tmp_path, conn)
    out = tmp_path / "out.json"
    result = runner.invoke(app, ["eval-everyday", "--set", str(set_path), "--db", db_path, "--runs", "2",
                                 "--expand-variant", "default,union2", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert len(llm.calls) == 3 * 2 * (1 + 2)  # 질문 3 × 실행 2 × (default 1번 + union2 2번)
    assert not list((tmp_path / "sasu-home").rglob("*.json"))  # --runs면 캐시를 쓰지 않는다
    data = json.loads(out.read_text(encoding="utf-8"))
    union = data["summary"]["dev"]["(b) 넓히기 union2"]
    assert union["runs"] == 2 and union["stability"]["always"] + union["stability"]["never"] == 1
    assert "union2 실행별 상위 6" in result.output
    runs = data["questions"][0]["results"]["(b) 넓히기 union2"]
    assert [r["run"] for r in runs] == [1, 2] and runs[0]["expanded_terms"] == ["insert", "remain"]
    bad = runner.invoke(app, ["eval-everyday", "--set", str(set_path), "--db", db_path, "--expand-variant", "loose"])
    assert bad.exit_code == 1
