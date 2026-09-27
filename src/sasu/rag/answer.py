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
import shutil
import subprocess
import tempfile
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
4. 한 줄에 한 문장, 6문장 이내, 한국어. 근거에 없는 테이블·메서드·값을 만들어 내지 않는다.
5. 질문에 대한 답부터 쓴다. 어떤 종류의 근거가 없다는 말(예: 인수인계 답변이 없다)은 쓰지 않는다.
6. 코드 원문 근거가 있으면 먼저 코드에서 확인되는 동작을 [사실]로 말하고, 이유는 그다음에 쓴다.
7. 인수인계 답변이 코드에서 확인되는 동작과 어긋나 보이면 답변을 코드에 억지로 맞춰 해석하지 않는다.
   어긋나는 점을 [추정]으로 짚고, 마지막 줄에 "물어볼 질문: …"으로 어느 쪽이 맞는지 확인할 질문을 쓴다.
   (사람의 기억은 틀릴 수 있고, 답한 뒤 코드가 바뀌었을 수도 있다)"""


class LLMError(RuntimeError):
    pass


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.lower() != "none":
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class LLMClient:
    """OpenAI 호환 chat/completions. 외부 라이브러리 없이 urllib만 쓴다."""

    def __init__(self, settings: Settings, timeout: float = 90.0):
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        # 사내 LLM 서버는 키 없이 여는 경우가 많다. 주소와 모델만 있으면 쓴다
        return bool(self.base_url and self.model)

    def chat(self, messages: list[dict]) -> str:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=_headers(self.api_key),
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


class ClaudeCodeClient:
    """claude CLI의 헤드리스 모드(claude -p)를 LLM으로 쓴다. API 키 없이 Claude Code 로그인으로 동작한다.

    - --tools "" : 파일 읽기·명령 실행 없이 사수가 준 근거만으로 답하게 한다 (RAG 구조 유지).
    - --system-prompt : Claude Code 기본 프롬프트 대신 사수의 규칙만 쓴다.
    - --setting-sources "" : 사용자·프로젝트 설정(CLAUDE.md 등)을 섞지 않는다.
    - 근거가 길어서 명령줄 대신 표준 입력으로 넘긴다 (Windows 명령줄 길이 한도).
    개발용이다. 폐쇄망 현장에서는 사내 LLM 서버(openai 방식)를 쓴다.
    """

    DEFAULT_MODEL = "sonnet"

    def __init__(self, model: str = "", timeout: float = 300.0):
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout
        self.executable = shutil.which("claude")
        self.last_cost_usd: float | None = None

    @property
    def available(self) -> bool:
        return self.executable is not None

    def chat(self, messages: list[dict]) -> str:
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        command = [
            self.executable, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
            "--setting-sources", "", "--system-prompt", system, "--model", self.model,
        ]
        try:
            done = subprocess.run(
                command, input=user.encode("utf-8"), capture_output=True, timeout=self.timeout,
                cwd=tempfile.gettempdir(),  # 프로젝트 폴더의 CLAUDE.md를 읽지 않게
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude 응답이 {self.timeout:.0f}초 안에 오지 않음") from e
        out = done.stdout.decode("utf-8", errors="replace")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            err = done.stderr.decode("utf-8", errors="replace")
            raise LLMError(f"claude 실행 실패 (exit {done.returncode}): {(err or out)[:300]}") from e
        if data.get("is_error"):
            raise LLMError(f"claude 오류: {str(data.get('result'))[:300]}")
        self.last_cost_usd = data.get("total_cost_usd")
        return data.get("result") or ""


def make_llm_client(settings: Settings):
    if settings.llm_provider == "claude-code":
        return ClaudeCodeClient(settings.llm_model)
    return LLMClient(settings)


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
            issues=[
                "LLM이 설정되지 않아 근거만 보여 줍니다. "
                ".env에 SASU_LLM_PROVIDER=claude-code(claude CLI 로그인) 또는 SASU_LLM_API_KEY·SASU_LLM_MODEL을 넣으세요."
            ],
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
        cites = list(dict.fromkeys(re.findall(r"\bE\d+\b", line)))
        # 문장 끝의 "(E1, E3)." 는 따로 보여 주므로 뗀다 (마침표가 붙어 있어도)
        text = re.sub(r"\s*\((?:\s*E\d+\s*,?\s*)+\)\s*\.?\s*$", "", line)
        if not text.endswith((".", "?", "!", "다", "요")):
            text = text.rstrip() + "."
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
