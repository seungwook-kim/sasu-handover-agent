"""LLM 답변 평가: 커밋 메시지를 숨기고 "이 줄은 왜 이렇게 돼 있나?"에 답하게 한다.

1. 기준 시점 이후 버그 수정 커밋에서, 그 커밋이 넣은 줄이 지금(HEAD)도 남아 있는 곳을 고른다.
2. 그 줄에 대해 묻는다. 근거에서는 그 커밋의 메시지를 뺀다 (정답을 숨김).
3. 두 가지로 답한다.
   - 사수 (RAG): 코드·호출 관계·주석·다른 커밋 기록을 근거로, [사실]/[답변]/[추정] 형식
   - LLM만: 질문과 그 한 줄만 주고 자유롭게
4. 채점 LLM이 숨긴 커밋 메시지(실제 이유)와 비교해 0/1/2점을 주고,
   틀린 이유를 단정했는지(환각) 따로 표시한다.
"""

import json
import random
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sasu.evaluation.history import FIX_PATTERN, _git
from sasu.rag.answer import answer
from sasu.rag.evidence import gather, resolve_target

_HANGUL = re.compile(r"[가-힣]")
_TRIVIAL_LINE = re.compile(r"^\s*([{}();]*|import .*|package .*|//.*|/?\*.*|@\w+.*)\s*$")

BASELINE_SYSTEM = "너는 Java 개발자다. 질문한 코드 줄이 왜 이렇게 되어 있는지 한국어로 3문장 이내로 설명한다."

JUDGE_SYSTEM = """너는 코드 설명을 채점한다. '실제 이유'는 그 코드를 넣은 커밋의 메시지다.
'답변'이 실제 이유를 얼마나 맞혔는지 JSON 한 줄로만 답한다:
{"score": 0|1|2, "confident_wrong": true|false, "note": "한 문장"}
- score 2: 실제 이유와 같은 이유(무엇을 막거나 고치려는지)를 말함
- score 1: 관련은 있지만 일부만 맞거나 모호함
- score 0: 다른 이유를 말하거나 이유를 말하지 못함
- confident_wrong: 실제 이유와 다른 이유를 추정·불확실 표시 없이 단정했으면 true
  ("추정", "~로 보인다", "모름" 같은 표시가 있으면 단정이 아니다)"""


@dataclass
class Case:
    sha: str
    subject: str
    path: str
    line: int
    code: str
    target: str


@dataclass
class Outcome:
    case: Case
    rag_answer: str
    rag_score: int
    rag_confident_wrong: bool
    rag_downgraded: int
    baseline_answer: str
    baseline_score: int
    baseline_confident_wrong: bool
    notes: dict = field(default_factory=dict)


def pick_cases(conn, repo: Path, rev: str, n: int, seed: int = 11) -> list[Case]:
    log = _git(repo, "log", "--no-merges", "--format=%H%x09%s", f"{rev}..HEAD")
    fixes = [
        line.split("\t", 1) for line in log.splitlines()
        if "\t" in line and FIX_PATTERN.search(line.split("\t", 1)[1]) and _HANGUL.search(line)
    ]
    random.Random(seed).shuffle(fixes)
    cases: list[Case] = []
    for sha, subject in fixes:
        if len(cases) >= n:
            break
        status = _git(repo, "diff", "--name-status", "-M", f"{sha}^", sha)
        paths = [line.split("\t")[-1] for line in status.splitlines() if line.split("\t")[-1].endswith(".java")]
        for path in paths:
            case = _surviving_line(conn, repo, sha, subject, path)
            if case:
                cases.append(case)
                break
    return cases


def _surviving_line(conn, repo: Path, sha: str, subject: str, path: str) -> Case | None:
    """그 커밋이 넣은 줄 중 지금도 남아 있고, 메서드 안에 있는 의미 있는 줄."""
    try:
        out = _git(repo, "blame", "--line-porcelain", "HEAD", "--", path)
    except subprocess.CalledProcessError:
        return None
    current_sha, line_no = None, None
    for raw in out.splitlines():
        m = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", raw)
        if m:
            current_sha, line_no = m.group(1), int(m.group(2))
        elif raw.startswith("\t") and current_sha == sha:
            code = raw[1:].strip()
            if _TRIVIAL_LINE.match(code) or len(code) < 12:
                continue
            targets = resolve_target(conn, f"{path}:{line_no}")
            if targets:
                return Case(sha, subject, path, line_no, code[:200], targets[0].label)
    return None


def run(conn, repo: Path, client, cases: list[Case], cache_dir: Path, progress=None) -> list[Outcome]:
    outcomes = []
    for i, case in enumerate(cases, 1):
        target = resolve_target(conn, f"{case.path}:{case.line}")[0]
        evidence = gather(conn, target, repo, cache_dir)
        # 정답(이 커밋의 메시지)을 근거에서 숨긴다
        for e in evidence:
            if e.title == "이 코드를 마지막으로 바꾼 커밋":
                e.text = "\n".join(l for l in e.text.splitlines() if case.sha[:8] not in l) or "(없음)"
        question = f"{case.path.rsplit('/', 1)[-1]} {case.line}번째 줄 `{case.code}` 는 왜 이렇게 되어 있나요?"

        rag = answer(question, target.label, evidence, client)
        rag_text = "\n".join(f"[{s.label}] {s.text}" for s in rag.sentences)
        baseline_text = client.chat([
            {"role": "system", "content": BASELINE_SYSTEM},
            {"role": "user", "content": f"{question}\n(대상 메서드: {target.label})"},
        ])
        rag_judge = _judge(client, case.subject, rag_text)
        base_judge = _judge(client, case.subject, baseline_text)
        outcomes.append(Outcome(
            case=case,
            rag_answer=rag_text,
            rag_score=rag_judge["score"],
            rag_confident_wrong=rag_judge["confident_wrong"],
            rag_downgraded=sum(1 for s in rag.sentences if s.downgraded),
            baseline_answer=baseline_text.strip(),
            baseline_score=base_judge["score"],
            baseline_confident_wrong=base_judge["confident_wrong"],
            notes={"rag": rag_judge.get("note"), "baseline": base_judge.get("note")},
        ))
        if progress:
            progress(i, len(cases))
    return outcomes


def _judge(client, reason: str, text: str) -> dict:
    raw = client.chat([
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": f"실제 이유: {reason}\n\n답변:\n{text}"},
    ])
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    score = data.get("score", 0)
    return {
        "score": int(score) if str(score).isdigit() else 0,
        "confident_wrong": bool(data.get("confident_wrong", False)),
        "note": data.get("note", raw[:200]),
    }


def summarize(outcomes: list[Outcome]) -> dict:
    n = len(outcomes) or 1

    def stats(prefix: str) -> dict:
        scores = [getattr(o, f"{prefix}_score") for o in outcomes]
        return {
            "평균 점수 (0~2)": round(sum(scores) / n, 2),
            "이유를 맞힘 (2점)": sum(1 for s in scores if s == 2),
            "일부라도 맞힘 (1점 이상)": sum(1 for s in scores if s >= 1),
            "틀린 이유를 단정": sum(1 for o in outcomes if getattr(o, f"{prefix}_confident_wrong")),
        }

    return {"질문 수": len(outcomes), "사수 (RAG)": stats("rag"), "LLM만": stats("baseline")}


def save(outcomes: list[Outcome], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"summary": summarize(outcomes), "cases": [asdict(o) for o in outcomes]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
