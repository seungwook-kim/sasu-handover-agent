"""일상어 검색 평가: 페르소나가 실제로 친 한국어 질문으로 묻기 대상 후보(상위 6)를 잰다.

- 평가 세트: everyday.json (질문, 코드를 읽어 정한 정답 메서드 집합). gold가 빈 질문은 일반 개념 질문.
- 지표: 상위 6 후보에 정답이 하나라도 있는 비율, 일반 질문을 low_confidence로 표시한 비율,
  (참고) 정답 있는 질문을 잘못 low_confidence로 표시한 비율, 질문당 지연.
- 분석 파일은 읽기 전용으로 연다.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SET = Path(__file__).with_name("everyday.json")


def load_set(path: Path | None = None) -> dict:
    return json.loads(Path(path or DEFAULT_SET).read_text(encoding="utf-8"))


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


@dataclass
class VariantResult:
    name: str
    cases: list[CaseResult]

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
        ))
    return VariantResult(name, cases)


def to_json(variants: list[VariantResult]) -> dict:
    return {
        v.name: {
            "상위6 정답률": round(v.hit_rate, 3), "1위 정답률": round(v.top1_rate, 3),
            "일반질문 low_confidence 정확도": round(v.low_conf_accuracy, 3),
            "정답있는 질문을 low_confidence로": round(v.false_low_conf, 3),
            "평균 지연(초)": round(v.avg_seconds, 2), "넓히기 LLM 평균(초)": round(v.avg_expand_seconds, 2),
            "cases": [c.__dict__ for c in v.cases],
        }
        for v in variants
    }
