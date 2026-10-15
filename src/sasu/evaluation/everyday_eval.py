"""일상어 검색 평가: 페르소나가 실제로 친 한국어 질문으로 묻기 대상 후보(상위 6)를 잰다.

- 평가 세트: everyday.json (질문, 코드를 읽어 정한 정답 메서드 집합). gold가 빈 질문은 일반 개념 질문.
  질문마다 split: dev(가중치·문턱을 정할 때 쓴 질문, 없으면 dev) | test(새 결정에 쓰지 않고 채점만).
- 지표: 상위 6 후보에 정답이 하나라도 있는 비율, 일반 질문을 low_confidence로 표시한 비율,
  (참고) 정답 있는 질문을 잘못 low_confidence로 표시한 비율, 질문당 지연.
- 넓히기 LLM 출력은 실행마다 달라서 같은 설정을 N번 재고 실행별 값·평균·최소~최대,
  95% Wilson 신뢰구간(실행마다, 그리고 질문×실행을 합쳐 하나), 질문별 안정성, 기준선 대비 짝 비교를 낸다.
  합친 구간은 질문×실행을 독립으로 본 근사라 질문 사이의 차이는 덜 반영한다 (실제보다 좁을 수 있다).
- 분석 파일은 읽기 전용으로 연다.
"""

import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_SET = Path(__file__).with_name("everyday.json")
SPLITS = ("dev", "test")


def load_set(path: Path | None = None) -> dict:
    return json.loads(Path(path or DEFAULT_SET).read_text(encoding="utf-8"))


def split_of(question: dict) -> str:
    return question.get("split") or "dev"


def select(questions: list[dict], split: str = "all") -> list[dict]:
    """split: dev | test | all. 알 수 없는 split이 적힌 질문이 있으면 ValueError."""
    unknown = sorted({split_of(q) for q in questions} - set(SPLITS))
    if unknown:
        raise ValueError(f"split은 {', '.join(SPLITS)} 중 하나여야 해요: {', '.join(unknown)}")
    if split == "all":
        return list(questions)
    if split not in SPLITS:
        raise ValueError(f"--split은 dev, test, all 중 하나: {split}")
    return [q for q in questions if split_of(q) == split]


@dataclass
class CaseResult:
    id: str
    question: str
    gold: list[str]
    top: list[str]
    rank: int | None  # 정답이 상위 몇 번째에 (없으면 None)
    low_confidence: bool
    coverage: float
    expanded_terms: list[str]
    seconds: float  # 검색 전체 (넓히기 포함)
    expand_seconds: float  # 넓히기 LLM만 (캐시면 0에 가깝다)
    why: list[str] = field(default_factory=list)
    split: str = "dev"
    expand_error: str | None = None  # 넓히기 LLM이 실패했으면 그 이유 (그 질문은 넓히기 없이 검색)


@dataclass
class VariantResult:
    name: str
    cases: list[CaseResult]

    def only(self, split: str) -> "VariantResult":
        return VariantResult(self.name, [c for c in self.cases if c.split == split])

    @property
    def specific(self) -> list[CaseResult]:
        return [c for c in self.cases if c.gold]

    @property
    def general(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.gold]

    @property
    def hit_rate(self) -> float:
        s = self.specific
        return sum(c.rank is not None for c in s) / len(s) if s else 0.0

    @property
    def top1_rate(self) -> float:
        s = self.specific
        return sum(c.rank == 1 for c in s) / len(s) if s else 0.0

    @property
    def low_conf_accuracy(self) -> float:
        g = self.general
        return sum(c.low_confidence for c in g) / len(g) if g else 0.0

    @property
    def false_low_conf(self) -> float:
        s = self.specific
        return sum(c.low_confidence for c in s) / len(s) if s else 0.0

    @property
    def avg_seconds(self) -> float:
        return sum(c.seconds for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def avg_expand_seconds(self) -> float:
        return sum(c.expand_seconds for c in self.cases) / len(self.cases) if self.cases else 0.0


def short(label: str) -> str:
    parts = label.split(".")
    return ".".join(parts[-2:])


def run_variant(conn, name: str, questions: list[dict], index, expander, top: int = 6) -> VariantResult:
    from sasu.rag.pipeline import search_question

    cases = []
    for q in questions:
        started = time.perf_counter()
        r = search_question(conn, q["question"], top=top, expander=expander, index=index)
        seconds = time.perf_counter() - started
        labels = [short(c.label) for c in r.candidates]
        gold = set(q.get("gold") or [])
        rank = next((i + 1 for i, label in enumerate(labels) if label in gold), None)
        cases.append(CaseResult(
            q["id"], q["question"], sorted(gold), labels, rank, r.low_confidence, r.coverage, r.expanded_terms,
            seconds, expander.last_seconds if expander is not None else 0.0, [c.why for c in r.candidates],
            split_of(q), expander.last_error if expander is not None else None,
        ))
    return VariantResult(name, cases)


# ---- 통계 (순수 함수) ----

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """n번 중 k번 맞힌 비율의 Wilson 신뢰구간 (기본 95%). n=0이면 (0, 1)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def stability(hits: dict[str, list[bool]]) -> dict:
    """질문별로 N번 중 맞힌 횟수: 항상 맞힘 / 항상 틀림 / 흔들림 개수와 맞힌 횟수 분포 {횟수: 질문 수}."""
    always = sum(1 for h in hits.values() if h and all(h))
    never = sum(1 for h in hits.values() if not any(h))
    counts = Counter(sum(h) for h in hits.values())
    return {"always": always, "never": never, "flaky": len(hits) - always - never,
            "distribution": {k: counts[k] for k in sorted(counts)}}


def majority(hits: list[bool]) -> bool:
    """N번 중 과반을 맞혔으면 맞음. 반반이면 틀림으로 본다."""
    return sum(hits) * 2 > len(hits)


def paired(base: dict[str, list[bool]], other: dict[str, list[bool]]) -> dict:
    """같은 질문끼리 짝지어 기준선 대비 바뀐 것 (질문마다 맞음/틀림은 다수결)."""
    ids = sorted(base.keys() & other.keys())
    b = {i: majority(base[i]) for i in ids}
    o = {i: majority(other[i]) for i in ids}
    gained = [i for i in ids if not b[i] and o[i]]
    lost = [i for i in ids if b[i] and not o[i]]
    return {"wrong_to_right": len(gained), "right_to_wrong": len(lost),
            "both_right": sum(b[i] and o[i] for i in ids), "both_wrong": sum(not b[i] and not o[i] for i in ids),
            "gained": gained, "lost": lost}


def hits_by_question(runs: list[VariantResult], top1: bool = False) -> dict[str, list[bool]]:
    """정답 있는 질문마다 실행별 맞힘 (top1=True면 1위만 맞힘으로)."""
    hits: dict[str, list[bool]] = {}
    for run in runs:
        for c in run.specific:
            hits.setdefault(c.id, []).append(c.rank == 1 if top1 else c.rank is not None)
    return hits


def _rate(ks: list[int], n: int) -> dict:
    rates = [k / n if n else 0.0 for k in ks]
    return {"n": n, "k": ks, "per_run": rates, "per_run_ci": [wilson(k, n) for k in ks],
            "mean": sum(rates) / len(rates) if rates else 0.0, "min": min(rates, default=0.0),
            "max": max(rates, default=0.0), "pooled_ci": wilson(sum(ks), n * len(ks))}


def summarize(runs: list[VariantResult], base: list[VariantResult] | None = None) -> dict:
    """같은 설정을 여러 번 잰 결과 (같은 질문들). base가 있으면 그것과 짝 비교 (상위 6 맞힘 기준)."""
    n = len(runs[0].specific) if runs else 0
    hits = hits_by_question(runs)
    general = [r.low_conf_accuracy for r in runs]
    false_low = [r.false_low_conf for r in runs]
    return {
        "runs": len(runs), "specific": n, "general": len(runs[0].general) if runs else 0,
        "top6": _rate([sum(c.rank is not None for c in r.specific) for r in runs], n),
        "top1": _rate([sum(c.rank == 1 for c in r.specific) for r in runs], n),
        "stability": stability(hits),
        "paired_vs_base": paired(hits_by_question(base), hits) if base else None,
        "low_conf_general": {"per_run": general, "mean": sum(general) / len(general) if general else 0.0},
        "low_conf_specific": {"per_run": false_low, "mean": sum(false_low) / len(false_low) if false_low else 0.0},
        "avg_seconds": sum(r.avg_seconds for r in runs) / len(runs) if runs else 0.0,
        "avg_expand_seconds": sum(r.avg_expand_seconds for r in runs) / len(runs) if runs else 0.0,
        "expand_errors": sum(c.expand_error is not None for r in runs for c in r.cases),
    }


def report(groups: dict[str, list[VariantResult]], splits: list[str], base: str | None = None) -> dict:
    """{split: {설정 이름: summarize}}. base: 짝 비교 기준이 되는 설정 이름 (보통 (a))."""
    out: dict[str, dict] = {}
    for split in splits:
        per = {name: [r.only(split) for r in runs] for name, runs in groups.items()}
        out[split] = {name: summarize(runs, per[base] if base and name != base else None)
                      for name, runs in per.items()}
    return out


def to_json(groups: dict[str, list[VariantResult]], splits: list[str], base: str | None = None,
            meta: dict | None = None) -> dict:
    """요약 + 질문별·실행별 원자료."""
    questions: dict[str, dict] = {}
    for name, runs in groups.items():
        for i, run in enumerate(runs, 1):
            for c in run.cases:
                q = questions.setdefault(c.id, {"id": c.id, "question": c.question, "split": c.split,
                                                "gold": c.gold, "results": {}})
                row = {k: v for k, v in asdict(c).items() if k not in ("id", "question", "split", "gold")}
                q["results"].setdefault(name, []).append({"run": i, **row})
    return {**(meta or {}), "summary": report(groups, splits, base), "questions": list(questions.values())}
