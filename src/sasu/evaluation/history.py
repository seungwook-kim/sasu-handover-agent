"""과거 기록으로 인수인계 질문을 채점한다: "나중에 문제가 된 곳을 미리 물었나?"

1. 과거 시점 T의 코드를 꺼내 분석하고 질문을 만든다 (T = 인수인계 받는 순간).
2. T 이후 버그 수정 커밋이 고친 메서드를 모은다 (정답지).
   수정 직전(부모 커밋) 코드를 파싱해, 바뀐 줄이 속한 메서드를 찾는다.
3. 질문 상위 K개가 가리키는 메서드 중 정답지에 있는 비율을 잰다.
   같은 K개를 무작위·복잡도순·영향 범위순으로 골랐을 때와 비교한다.

한계: "나중에 버그가 난 곳 = 인수인계 때 물어봤어야 할 곳"은 완벽한 등식이 아니다.
커밋 메시지로 버그 수정을 가려내므로 분류도 완벽하지 않다. 참고 지표로 쓴다.
"""

import io
import random
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from sasu import db
from sasu.analysis.questions import generate_questions, url_reach
from sasu.config import load_settings
from sasu.indexer import index_repository
from sasu.parsers.java_parser import parse_java

FIX_PATTERN = re.compile(r"^fix\b|^fix\(|버그|오류|결함|취약|hotfix|\bbug\b", re.IGNORECASE)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)


@dataclass
class FixedMethod:
    commits: list[str] = field(default_factory=list)  # "sha 제목"


@dataclass
class Ranking:
    name: str
    hits: dict[int, int]  # K → 맞힌 수
    sizes: dict[int, int]  # K → 실제로 고른 수 (후보가 K보다 적을 수 있음)

    def precision(self, k: int) -> float:
        return self.hits[k] / self.sizes[k] if self.sizes[k] else 0.0


@dataclass
class EvalResult:
    snapshot: str
    snapshot_date: str
    fix_commits: int
    fixed_methods: int
    candidate_methods: int
    base_rate: float  # 아무 메서드나 골랐을 때 맞을 확률
    rankings: list[Ranking]
    by_kind: dict[str, tuple[int, int]]  # 종류 → (맞힌 수, 질문 수)  [전체 질문 기준]
    examples: list[tuple[str, str, str]]  # (질문, 대상, 고친 커밋)


def _git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def export_snapshot(repo: Path, rev: str, dest: Path) -> Path:
    """rev 시점의 파일을 dest에 푼다 (git archive, 작업 폴더는 건드리지 않음)."""
    if dest.exists() and any(dest.iterdir()):
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    data = _git(repo, "archive", "--format=tar", rev, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def fixed_methods_after(repo: Path, rev: str) -> tuple[dict[str, FixedMethod], int]:
    """rev 이후 버그 수정 커밋이 고친 메서드. 키는 "Class.method" (중첩 클래스는 마지막 이름)."""
    log = _git(repo, "log", "--no-merges", "--format=%H%x09%s", f"{rev}..HEAD")
    commits = [line.split("\t", 1) for line in log.splitlines() if "\t" in line]
    fixes = [(sha, subject) for sha, subject in commits if FIX_PATTERN.search(subject)]
    result: dict[str, FixedMethod] = {}
    parse_cache: dict[tuple[str, str], list] = {}
    for sha, subject in fixes:
        status = _git(repo, "diff", "--name-status", "-M", f"{sha}^", sha)
        for line in status.splitlines():
            parts = line.split("\t")
            if not parts or not parts[-1].endswith(".java") or parts[0].startswith("A"):
                continue
            old_path = parts[1]
            new_path = parts[-1]
            diff = _git(repo, "diff", "-U0", "-M", f"{sha}^", sha, "--", old_path, new_path)
            lines = _changed_old_lines(diff)
            if not lines:
                continue
            key = (sha, old_path)
            if key not in parse_cache:
                try:
                    source = _git(repo, "show", f"{sha}^:{old_path}", binary=True)
                except subprocess.CalledProcessError:
                    continue
                parse_cache[key] = [
                    (cls.name.split(".")[-1], m.name, m.line_start, m.line_end)
                    for cls in parse_java(source).classes
                    for m in cls.methods
                ]
            for cls_name, method_name, start, end in parse_cache[key]:
                if any(start <= n <= end for n in lines):
                    fm = result.setdefault(f"{cls_name}.{method_name}", FixedMethod())
                    label = f"{sha[:8]} {subject}"
                    if label not in fm.commits:
                        fm.commits.append(label)
    return result, len(fixes)


def _changed_old_lines(diff: str) -> set[int]:
    """-U0 diff에서 수정 전 파일 기준으로 바뀐 줄. 순수 추가는 그 앞 줄로 본다."""
    lines: set[int] = set()
    for m in _HUNK.finditer(diff):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            lines.add(max(start, 1))
        else:
            lines.update(range(start, start + count))
    return lines


def evaluate(repo: Path, rev: str, ks: tuple[int, ...] = (10, 20, 50), seed: int = 7,
             work_dir: Path | None = None) -> EvalResult:
    work_dir = work_dir or Path("data/cache")
    short = _git(repo, "rev-parse", "--short", rev).strip()
    date = _git(repo, "log", "-1", "--format=%ad", "--date=short", rev).strip()
    snapshot = export_snapshot(repo, rev, work_dir / f"snapshot-{short}")
    conn = db.connect(work_dir / f"eval-{short}.db")
    index_repository(conn, snapshot, load_settings().sql_dialect)

    fixed, fix_count = fixed_methods_after(repo, rev)
    questions = generate_questions(conn)

    # 후보: 본문이 있는 모든 메서드 (Class.method로 같은 이름은 하나로)
    methods = conn.execute(
        """SELECT m.id, m.name, m.complexity, c.name AS class_name FROM methods m
           JOIN classes c ON c.id = m.class_id WHERE c.kind != 'interface' AND m.body_tokens > 0"""
    ).fetchall()
    key_of = {m["id"]: f"{m['class_name'].split('.')[-1]}.{m['name']}" for m in methods}
    candidates = sorted(set(key_of.values()))
    candidate_set = set(candidates)
    fixed_in_candidates = {k for k in fixed if k in candidate_set}
    base_rate = len(fixed_in_candidates) / len(candidates) if candidates else 0.0

    def question_key(target: str | None) -> str | None:
        if not target or "." not in target:
            return None
        cls, method = target.rsplit(".", 1)
        key = f"{cls.split('.')[-1]}.{method}"
        return key if key in candidate_set else None

    sasu_keys = list(dict.fromkeys(k for q in questions if (k := question_key(q.target))))
    # 끊어진 쿼리는 "나중에 고칠 곳 예측"이 아니라 지금 확인된 고장이라, 아직 안 고쳐졌으면 맞힌 것으로 안 잡힌다.
    # 예측력만 보려고 뺀 순서도 함께 잰다.
    predictive_keys = list(dict.fromkeys(
        k for q in questions if q.kind != "broken_sql" and (k := question_key(q.target))
    ))
    reach = url_reach(conn)
    by_complexity = list(dict.fromkeys(
        key_of[m["id"]] for m in sorted(methods, key=lambda m: -m["complexity"])
    ))
    by_reach = list(dict.fromkeys(
        key_of[mid] for mid, _ in sorted(reach.items(), key=lambda kv: -kv[1]) if mid in key_of
    ))

    def ranking(name: str, ordered: list[str]) -> Ranking:
        hits, sizes = {}, {}
        for k in ks:
            chosen = ordered[:k]
            sizes[k] = len(chosen)
            hits[k] = sum(1 for c in chosen if c in fixed_in_candidates)
        return Ranking(name, hits, sizes)

    rng = random.Random(seed)
    random_hits = {k: 0.0 for k in ks}
    trials = 500
    for _ in range(trials):
        sample = rng.sample(candidates, max(ks))
        for k in ks:
            random_hits[k] += sum(1 for c in sample[:k] if c in fixed_in_candidates)
    random_rank = Ranking("무작위 (500회 평균)", {k: round(random_hits[k] / trials, 1) for k in ks}, {k: k for k in ks})

    by_kind: dict[str, tuple[int, int]] = {}
    examples = []
    for q in questions:
        key = question_key(q.target)
        hit = key is not None and key in fixed_in_candidates
        h, n = by_kind.get(q.kind, (0, 0))
        by_kind[q.kind] = (h + int(hit), n + 1)
        if hit and len(examples) < 8:
            examples.append((q.question, q.target, fixed[key].commits[0]))
    conn.close()

    return EvalResult(
        snapshot=f"{rev} ({short})",
        snapshot_date=date,
        fix_commits=fix_count,
        fixed_methods=len(fixed_in_candidates),
        candidate_methods=len(candidates),
        base_rate=base_rate,
        rankings=[
            ranking("사수 질문 순서", sasu_keys),
            ranking("사수 질문 순서 (지금 고장난 쿼리 제외)", predictive_keys),
            ranking("복잡도 높은 순", by_complexity),
            ranking("영향 범위 넓은 순 (닿는 URL 수)", by_reach),
            random_rank,
        ],
        by_kind=by_kind,
        examples=examples,
    )
