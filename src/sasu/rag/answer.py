"""근거만으로 "왜?"에 답한다. LLM이 지어내지 못하게 형식을 강제하고, 답을 기계적으로 검사한다.

- 모든 문장은 [사실]/[답변]/[추정]으로 시작하고 근거 번호(E1…)를 단다.
- [사실]은 "확인된 사실" 근거로만, [답변]은 "인수인계 답변" 근거로만 쓸 수 있다.
  어기면 [추정]으로 낮춘다. 없는 근거 번호를 달면 경고한다.
- 근거로 답할 수 없으면 "모름"과 함께 떠난 개발자에게 물을 질문을 제안한다.
- LLM이 설정되지 않았으면 근거만 보여 준다.

LLM은 OpenAI 호환 주소 하나로 연결한다 (개발: OpenRouter, 현장: 사내 LLM 서버).
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from sasu.config import Settings
from sasu.rag.evidence import ANSWER, FACT, Evidence

LABELS = {"사실": FACT, "답변": ANSWER, "추정": None}

SYSTEM_PROMPT = """너는 SI 프로젝트 인수인계를 돕는 '사수'다. 떠난 개발자 대신, 주어진 근거만으로 답한다.

규칙:
1. 모든 문장을 [사실], [답변], [추정] 중 하나로 시작한다.
   - [사실]: '확인된 사실' 근거(코드·SQL·호출 관계)에서 바로 확인되는 내용만.
   - [답변]: '인수인계 답변' 근거(떠난 개발자가 적은 답)에 있는 내용만.
   - [추정]: 근거를 바탕으로 한 해석. 주석·커밋 메시지('기록')만으로 말하는 이유도 [추정]이다.
2. 문장 끝에 근거 번호를 (E1, E3)처럼 단다. [추정]에도 바탕이 된 근거를 단다.
3. 근거로 답할 수 없으면 첫 문장을 "모름:"으로 시작해 무엇이 부족한지 말하고,
   마지막 줄에 "물어볼 질문: …" 형식으로 떠난 개발자나 PM에게 물을 질문 한 줄을 쓴다.
4. 한 줄에 한 문장, 6문장 이내, 한국어. 근거에 없는 테이블·메서드·값을 만들어 내지 않는다."""


class LLMError(RuntimeError):
    pass


class LLMClient:
    """OpenAI 호환 chat/completions. 외부 라이브러리 없이 urllib만 쓴다."""

    def __init__(self, settings: Settings, timeout: float = 90.0):
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.model)

    def chat(self, messages: list[dict]) -> str:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"LLM 요청 실패 ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise LLMError(f"LLM 서버에 연결할 수 없음: {e}") from e
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 응답 형식이 예상과 다름: {str(data)[:300]}") from e


@dataclass
class Sentence:
    label: str  # 사실, 답변, 추정, 모름, 질문
    text: str
    citations: list[str]
    downgraded: str | None = None  # 낮춘 이유


@dataclass
class AnswerResult:
    mode: str  # llm, evidence_only
    sentences: list[Sentence] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    model: str | None = None
    raw: str | None = None


def build_messages(question: str, target_label: str, evidence: list[Evidence]) -> list[dict]:
    blocks = "\n\n".join(
        f"[{e.id}] ({e.level}) {e.title}" + (f" — {e.location}" if e.location else "") + f"\n{e.text}"
        for e in evidence
    )
    user = f"질문: {question}\n대상 코드: {target_label}\n\n근거:\n{blocks}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def answer(question: str, target_label: str, evidence: list[Evidence], client: LLMClient | None) -> AnswerResult:
    if client is None or not client.available:
        return AnswerResult(
            mode="evidence_only",
            issues=["LLM이 설정되지 않아 근거만 보여 줍니다 (.env의 SASU_LLM_API_KEY, SASU_LLM_MODEL)."],
        )
    raw = client.chat(build_messages(question, target_label, evidence))
    result = validate(raw, evidence)
    result.model = client.model
    return result


def validate(raw: str, evidence: list[Evidence]) -> AnswerResult:
    levels = {e.id: e.level for e in evidence}
    result = AnswerResult(mode="llm", raw=raw)
    for line in (l.strip() for l in raw.splitlines()):
        if not line:
            continue
        if line.startswith("물어볼 질문"):
            result.sentences.append(Sentence("질문", line.split(":", 1)[-1].strip(), []))
            continue
        cites = re.findall(r"\bE\d+\b", line)
        text = re.sub(r"\s*\((?:\s*E\d+\s*,?\s*)+\)\s*$", "", line)
        label_match = re.match(r"^\[(사실|답변|추정)\]\s*", text)
        if text.startswith("모름"):
            result.sentences.append(Sentence("모름", text.split(":", 1)[-1].strip(), cites))
            continue
        label = label_match.group(1) if label_match else None
        text = text[label_match.end():] if label_match else text
        sentence = Sentence(label or "추정", text, cites)
        unknown = [c for c in cites if c not in levels]
        if unknown:
            result.issues.append(f"없는 근거를 인용함: {', '.join(unknown)} → \"{text[:40]}…\"")
        if label is None:
            sentence.downgraded = "표시가 없어 추정으로 봄"
        elif label in ("사실", "답변"):
            required = LABELS[label]
            valid = [c for c in cites if levels.get(c) == required]
            if not valid:
                sentence.label = "추정"
                sentence.downgraded = f"[{label}]인데 '{required}' 근거가 없음"
        if not cites and sentence.label != "추정":
            sentence.label = "추정"
            sentence.downgraded = "근거 번호가 없음"
        if sentence.downgraded:
            result.issues.append(f"추정으로 낮춤: {sentence.downgraded} → \"{text[:40]}…\"")
        result.sentences.append(sentence)
    return result
