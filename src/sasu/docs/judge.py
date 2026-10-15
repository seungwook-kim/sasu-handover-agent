"""문서–코드 불일치 LLM 판정 (선택). 규칙 판정(conflicts.py)이 못 보는 것: 설정 값, 상태값의 뜻, 동작(물리/논리 삭제).

코드와 이어진 조각마다 한 번 묻는다: 조각의 문장들 + 그 문장들과 관계있는 코드 근거
(조각이 가리키는 URL·메서드의 소스, 문장마다 코드 검색 상위 메서드와 그 SQL, 문장과 비슷한 설정 값) →
"코드 근거가 이 문장과 다른 사실을 말하나". 근거가 그 대상을 직접 말할 때만 다르다고 하게 한다.

- 켜기: `sasu docs add --llm-judge`, `sasu docs conflicts --llm-judge`, `sasu eval-docs --llm-judge`.
  공급자 SASU_JUDGE_PROVIDER (기본 anthropic: SASU_ANTHROPIC_API_KEY, 모델 SASU_JUDGE_MODEL 기본 claude-haiku-5-5).
- 결과는 프롬프트 해시로 SASU_HOME/cache/doc-judge/에 둔다: 같은 조각·같은 코드면 다시 묻지 않는다.
  cached_only면 캐시만 읽는다 (문서를 지운 뒤 질문 목록을 맞출 때처럼 API를 부르면 안 될 때).
- 근거의 설정 값은 비밀을 가린 뒤 보낸다 (redact).
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sasu.docs.configs import ConfigFact, config_facts
from sasu.docs.conflicts import CodeFacts, Conflict, sentences
from sasu.docs.linking import METHOD, URL, has_docs

LLM = "llm"
PROMPT_VERSION = "judge-v2"
_MAX_METHODS = 8
_MAX_STATEMENTS = 8
_MAX_CONFIG = 12
_METHOD_LINES = 45
_SQL_LINES = 25

SYSTEM_PROMPT = """너는 인수인계 문서를 코드와 대조하는 검토자다. 전임자가 쓴 문서 문장(S1, S2 …)과
그 시스템의 실제 코드 근거(E1, E2 …: 메서드 소스, SQL, 설정 파일 값)를 받는다.
문서 문장이 코드 근거와 **다른 사실**을 말하는 곳만 찾는다.

다르다고 할 것 (근거가 같은 대상을 직접 말할 때만):
- 숫자·기준값이 다름 (예: 문서 "3회 틀리면 잠김" ↔ 설정 LockCount = 5, 문서 "6시간마다" ↔ repeatInterval 43200000ms = 12시간)
- 단위를 바꿔 같은 값이면 같은 것이다 (10MB = 10485760 바이트, 30분 = 1800초 = 1800000ms)
- 상태값·코드 값의 뜻이나 값이 다름 (예: 문서 "승인하면 'Y'" ↔ 코드가 승인에 'C'를 넣음)
- 테이블·URL·동작이 다름 (예: 문서 "DELETE로 지움" ↔ SQL은 UPDATE … USE_AT='N'로 남김)
- 조건이 다름 (예: 문서 "반려 건도 중복에 걸림" ↔ SQL이 반려 건을 빼고 셈)

다르다고 하지 말 것:
- 근거에 그 대상이 안 나오거나 근거만으로 확실하지 않을 때 (운영 절차·담당자·일정·서버 정보는 코드에 없다)
- 근거가 다른 기능·다른 대상의 값일 때 (비슷한 이름이어도 같은 것이라는 근거가 없으면)
- 문서가 코드보다 자세하거나 덜 자세할 뿐 서로 어긋나지 않을 때
- 문서 문장에 값이 직접 적혀 있지 않을 때 (문장에서 값을 짐작해 만들지 않는다: "분기마다" → 90일로 바꿔 비교하지 않는다)

출력은 JSON 객체 하나만 (설명·코드 블록 없이):
{"conflicts": [{"s": 문장 번호, "e": "근거 번호", "doc": "문서가 말하는 값", "code": "코드가 말하는 값(근거에서 그대로)"}]}
없으면 {"conflicts": []}."""


class _Bm25:
    def __init__(self, texts: list[str]):
        from sasu.rag.search import tokenize

        self.tokenize = tokenize
        self.tf = [Counter(tokenize(t)) for t in texts]
        self.len = [sum(t.values()) or 1 for t in self.tf]
        self.avg = sum(self.len) / max(len(self.len), 1)
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(self.tf)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query: str) -> list[float]:
        # 숫자는 빼고 맞춘다: 확인하려는 문서의 숫자(틀렸을 수 있다)가 엉뚱한 설정 값과 맞지 않게
        terms = {t for t in self.tokenize(query) if not t.isdigit()}
        out = []
        for tf, length in zip(self.tf, self.len):
            s = 0.0
            for t in terms:
                if f := tf.get(t):
                    s += self.idf[t] * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * length / self.avg))
            out.append(s)
        return out


class ConfigIndex:
    """문장과 비슷한 설정 값 찾기: BM25(키·주석) + 임베딩(한국어 문장 ↔ 영어 키, 예: 세션 타임아웃 ↔ session-timeout)."""

    def __init__(self, facts: list[ConfigFact], embedder=None):
        self.facts = facts
        self.values = {f.key: f.value for f in facts}
        texts = [f.text() for f in facts]
        self.bm25 = _Bm25(texts)
        self.embedder = embedder
        self.vectors: list[list[float] | None] = []
        if embedder is not None and facts:
            from sasu.docs.search import embed_chunks

            got = embed_chunks(dict(enumerate(texts)), embedder)
            self.vectors = [got.get(i) for i in range(len(texts))]

    def resolve(self, value: str) -> str:
        """${Globals.login.LockCount} 같은 자리표시를 값과 함께 보여 준다: "${…} (= 5)"."""
        found = [self.values[k] for k in re.findall(r"\$\{([\w.\-]+)(?::[^}]*)?\}", value) if k in self.values]
        return f"{value} (= {', '.join(found)})" if found else value

    def referenced(self, sources: list[str]) -> list[ConfigFact]:
        """코드가 이름으로 읽는 설정 (EgovProperties.getProperty("Globals.ExpirePwdDay"), @Value("${a.b}"))."""
        text = "\n".join(sources)
        keys = set(re.findall(r"[\"{]([A-Za-z][\w.\-]{3,})[\"}]", text))
        return [f for f in self.facts if f.key in keys]

    def in_files(self, text: str, per_file: int = 6) -> list[ConfigFact]:
        """문서가 이름으로 적은 설정 파일(context-scheduling-sts-ust.xml 등)의 값."""
        names = {Path(f.path).name for f in self.facts}
        named = [n for n in names if len(n) >= 8 and n in text]
        out: list[ConfigFact] = []
        for name in sorted(named):
            out.extend([f for f in self.facts if Path(f.path).name == name][:per_file])
        return out

    def search(self, query: str, top: int = 3) -> list[ConfigFact]:
        """벡터가 있으면 코사인 + 0.15 × BM25(최고점 1): 한국어 문장과 영어 키는 BM25로 거의 안 맞아서 벡터를 앞에 둔다."""
        if not self.facts:
            return []
        scores = self.bm25.scores(query)
        peak = max(scores) or 1.0
        weight = 0.15 if self.vectors else 1.0
        fused = [weight * s / peak for s in scores]
        if self.vectors:
            try:
                q = self.embedder.embed([query])[0]
            except Exception:
                q = None
            if q is not None:
                qn = math.sqrt(sum(x * x for x in q)) or 1.0
                for i, v in enumerate(self.vectors):
                    if v:
                        cos = sum(a * b for a, b in zip(q, v)) / (qn * (math.sqrt(sum(x * x for x in v)) or 1.0))
                        fused[i] += max(cos, 0.0)
        out: list[ConfigFact] = []
        seen: set[tuple[str, str]] = set()
        for i in sorted(range(len(fused)), key=lambda i: -fused[i]):
            if fused[i] <= 0 or len(out) >= top:
                break
            if (self.facts[i].key, self.facts[i].value) not in seen:  # web.xml·web_xxx.xml의 같은 값은 하나만
                seen.add((self.facts[i].key, self.facts[i].value))
                out.append(self.facts[i])
        return out


# ── 판정 ──────────────────────────────────────────────────

@dataclass
class JudgeStats:
    calls: int = 0
    cached: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    chunks: int = 0
    errors_text: list[str] = field(default_factory=list)


class ConflictJudge:
    def __init__(self, client, cache: Path | None = None, cached_only: bool = False, embedder=None):
        self.client = client
        self.cache = cache
        self.cached_only = cached_only
        self.embedder = embedder
        self.stat = JudgeStats()
        self._config: ConfigIndex | None = None
        self._index = None

    @classmethod
    def from_settings(cls, settings=None, cached_only: bool = False) -> "ConflictJudge":
        from sasu.config import cache_dir, load_settings
        from sasu.rag.pipeline import embedder_or_none

        settings = settings or load_settings()
        return cls(make_judge_client(settings), cache_dir() / "doc-judge", cached_only, embedder_or_none())

    @property
    def available(self) -> bool:
        return self.client is not None and getattr(self.client, "available", False)

    @property
    def model(self) -> str:
        return getattr(self.client, "model", "") or ""

    def stats(self) -> dict:
        s = self.stat
        return {"model": self.model, "chunks": s.chunks, "calls": s.calls, "cached": s.cached, "errors": s.errors,
                "input_tokens": s.input_tokens, "output_tokens": s.output_tokens, "seconds": round(s.seconds, 1),
                "error_samples": s.errors_text[:3]}

    # 근거 모으기 ─────────────────────────────────────────

    def evidence(self, conn: sqlite3.Connection, chunk: sqlite3.Row, sents: list[str],
                 facts: CodeFacts) -> list[tuple[str, str, str | None]]:
        """[(근거 글, 종류, 대상 이름)] — 대상은 질문 목록에 붙일 Class.method 또는 설정 파일:줄."""
        from sasu.rag.pipeline import search_question
        from sasu.rag.search import MethodIndex

        if self._index is None:
            self._index = MethodIndex(conn, self.embedder)
        if self._config is None:
            self._config = ConfigIndex(config_facts(facts.repo), self.embedder)
        methods: list[int] = []
        for r in conn.execute("SELECT kind, value FROM doc_links WHERE chunk_id = ? AND found = 1 AND kind IN (?, ?)",
                              (chunk["id"], URL, METHOD)):
            for mid in sorted(facts.methods_of([(r["kind"], r["value"])]))[:2]:
                if mid not in methods:
                    methods.append(mid)
        methods = methods[:3]
        ranked: list[list[int]] = []
        for s in sents:
            hits = search_question(conn, s, top=2, per_class=1, expander=None, index=self._index, reranker=None)
            ranked.append([c.method_id for c in hits.candidates])
        for rank in range(2):
            for hits in ranked:
                if rank < len(hits) and hits[rank] not in methods:
                    methods.append(hits[rank])
        methods = methods[:_MAX_METHODS]
        items: list[tuple[str, str, str | None]] = []
        for mid in methods:
            row = conn.execute(
                """SELECT c.name || '.' || m.name AS label, f.path, m.line_start FROM methods m
                   JOIN classes c ON c.id = m.class_id JOIN files f ON f.id = c.file_id WHERE m.id = ?""", (mid,)).fetchone()
            if row is None:
                continue
            lines = facts.source(mid).splitlines()
            body = "\n".join(lines[:_METHOD_LINES]) + ("\n…" if len(lines) > _METHOD_LINES else "")
            items.append((f"메서드 {row['label']} ({row['path']}:{row['line_start']})\n{body}", "method", row["label"]))
        seen: set[str] = set()
        statements = []
        for mid in methods:
            for s in facts.statements_of(facts.reach({mid}, depth=2)):
                if s["full_id"] not in seen:
                    seen.add(s["full_id"])
                    statements.append(s)
        for s in statements[:_MAX_STATEMENTS]:
            lines = (s["sql"] or "").splitlines()
            sql = "\n".join(lines[:_SQL_LINES]) + ("\n…" if len(lines) > _SQL_LINES else "")
            items.append((f"SQL {s['full_id']}\n{sql}", "sql", s["full_id"]))
        config: list[ConfigFact] = list(self._config.referenced([facts.source(mid) for mid in methods]))[:4]
        config += [f for f in self._config.in_files(chunk["text"]) if f not in config]
        per_sentence = [self._config.search(s, top=3) for s in sents]
        for rank in range(3):  # 문장마다 1위부터 (뒤 문장의 1위가 앞 문장의 3위보다 먼저)
            for found in per_sentence:
                if rank < len(found) and found[rank] not in config:
                    config.append(found[rank])
        for fact in config[:_MAX_CONFIG]:
            note = f"  # {fact.comment}" if fact.comment else ""
            items.append((f"설정 {fact.path}:{fact.line}  {fact.key} = {self._config.resolve(fact.value)}{note}", "config",
                          f"{fact.path}:{fact.line}"))
        return items

    def prompt(self, sents: list[str], evidence: list[tuple[str, str, str | None]], title: str) -> list[dict]:
        doc = "\n".join(f"S{i}: {s}" for i, s in enumerate(sents, 1))
        ev = "\n\n".join(f"[E{i}] {text}" for i, (text, _, _) in enumerate(evidence, 1))
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"## 문서: {title}\n{doc}\n\n## 코드 근거\n{ev}"}]

    # 한 조각 ─────────────────────────────────────────────

    def chunk_conflicts(self, conn: sqlite3.Connection, chunk_id: int, facts: CodeFacts | None = None) -> list[Conflict]:
        from sasu.docs.store import chunk_title

        r = conn.execute(
            """SELECT c.id, c.section, c.heading, c.page, c.text, d.name, d.doc_date
               FROM doc_chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?""", (chunk_id,)).fetchone()
        if r is None:
            return []
        if not conn.execute("SELECT 1 FROM doc_links WHERE chunk_id = ? AND found = 1 LIMIT 1", (chunk_id,)).fetchone():
            return []  # 코드와 이어진 조각만
        facts = facts or CodeFacts(conn)
        sents = [s for s in sentences(r["text"]) if len(s) >= 8 and "[가림:" not in s][:20]
        if not sents:
            return []
        self.stat.chunks += 1
        title = chunk_title(r["name"], r["section"], r["heading"], r["doc_date"], r["page"])
        evidence = self.evidence(conn, r, sents, facts)
        if not evidence:
            return []
        messages = self.prompt(sents, evidence, title)
        raw = self._ask(messages)
        if raw is None:
            return []
        found = []
        for item in parse_judgement(raw):
            try:
                s_no = int(str(item.get("s", "")).lstrip("Ss"))
                e_no = int(str(item.get("e", "")).lstrip("Ee") or 0)
            except ValueError:
                continue
            if not 1 <= s_no <= len(sents):
                continue
            ev = evidence[e_no - 1] if 1 <= e_no <= len(evidence) else None
            doc_says = str(item.get("doc", "")).strip()[:120]
            code_says = str(item.get("code", "")).strip()[:200]
            target = ev[2] if ev else None
            where = f" ({target})" if target else ""
            found.append(Conflict(
                LLM, chunk_id, title, sents[s_no - 1], doc_says, code_says + where, target,
                f"문서와 코드가 달라요 (LLM 판정): 문서는 {doc_says}, 코드{where}는 {code_says}",
                f"문서({title})에는 '{doc_says}'로 돼 있는데, 코드{where}는 '{code_says}'예요. 지금은 어느 쪽이 맞나요?"))
        return found

    def _ask(self, messages: list[dict]) -> str | None:
        key = hashlib.sha1(json.dumps([PROMPT_VERSION, self.model, messages], ensure_ascii=False).encode("utf-8")).hexdigest()
        path = self.cache / f"{key}.json" if self.cache else None
        if path and path.exists():
            try:
                self.stat.cached += 1
                return json.loads(path.read_text(encoding="utf-8"))["raw"]
            except (OSError, ValueError, KeyError):
                pass
        if self.cached_only or not self.available:
            return None
        started = time.perf_counter()
        try:
            raw, usage = _chat(self.client, messages)
        except Exception as e:  # LLM 실패: 이 조각은 규칙 판정만
            self.stat.errors += 1
            self.stat.errors_text.append(str(e)[:200])
            return None
        finally:
            self.stat.seconds += time.perf_counter() - started
        self.stat.calls += 1
        self.stat.input_tokens += usage.get("input_tokens", 0)
        self.stat.output_tokens += usage.get("output_tokens", 0)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"raw": raw, "usage": usage, "model": self.model}, ensure_ascii=False),
                            encoding="utf-8")
        return raw

    def conflicts(self, conn: sqlite3.Connection, chunk_ids: list[int] | None = None) -> list[Conflict]:
        if not has_docs(conn):
            return []
        facts = CodeFacts(conn)
        if chunk_ids is None:
            chunk_ids = [r[0] for r in conn.execute(
                "SELECT c.id FROM doc_chunks c JOIN documents d ON d.id = c.doc_id ORDER BY d.name, c.seq")]
        out: list[Conflict] = []
        for cid in chunk_ids:
            out.extend(self.chunk_conflicts(conn, cid, facts))
        return out


def parse_judgement(raw: str) -> list[dict]:
    """LLM 출력에서 {"conflicts": [...]}를 꺼낸다. 코드 블록·앞뒤 말이 붙어도 된다. 못 읽으면 []."""
    text = raw.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    items = data.get("conflicts") if isinstance(data, dict) else None
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def merge(rule: list[Conflict], llm: list[Conflict]) -> list[Conflict]:
    """규칙 판정 + LLM 판정. 같은 조각의 같은 문장을 규칙이 이미 잡았으면 LLM 것은 뺀다."""
    def key(c: Conflict) -> tuple[int, str]:
        return c.chunk_id, re.sub(r"\s+", "", c.sentence)

    taken = {key(c) for c in rule}
    out = list(rule)
    for c in llm:
        if key(c) not in taken:
            taken.add(key(c))
            out.append(c)
    return out


def judge_mode() -> str:
    """SASU_DOC_JUDGE: off(기본) | on. on이면 문서 넣기·질문 목록 맞추기·불일치 보기에서 LLM 판정도 쓴다."""
    from sasu.config import load_settings

    load_settings()  # .env를 읽어 둔다
    value = os.environ.get("SASU_DOC_JUDGE", "off").strip().lower()
    return "on" if value in ("on", "1", "true", "yes") else "off"


def default_judge() -> "ConflictJudge | None":
    """SASU_DOC_JUDGE=on이고 LLM을 쓸 수 있으면 판정기, 아니면 None (규칙 판정만)."""
    if judge_mode() != "on":
        return None
    try:
        judge = ConflictJudge.from_settings()
    except Exception:
        return None
    return judge if judge.available else None


def make_judge_client(settings):
    """SASU_JUDGE_PROVIDER: anthropic(기본) | expand(넓히기와 같은 공급자). 모델 SASU_JUDGE_MODEL (기본 claude-haiku-5-5)."""
    from sasu.rag.answer import AnthropicClient

    provider = os.environ.get("SASU_JUDGE_PROVIDER", "anthropic").strip().lower() or "anthropic"
    model = os.environ.get("SASU_JUDGE_MODEL", "").strip() or "claude-haiku-5-5"
    if provider == "expand":
        from sasu.rag.expand import make_expand_client

        return make_expand_client(settings)
    return AnthropicClient(settings.anthropic_api_key, model, effort="low", max_tokens=4096, timeout=60.0)


def _chat(client, messages: list[dict]) -> tuple[str, dict]:
    """(글, 사용량). Anthropic SDK면 토큰 수도 받는다. 다른 공급자는 chat()만."""
    if hasattr(client, "_sdk") and hasattr(client, "request"):
        response = client._sdk().messages.create(**client.request(messages))
        text = "".join(b.text for b in response.content if b.type == "text")
        usage = {"input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
                 "output_tokens": getattr(response.usage, "output_tokens", 0) or 0}
        return text, usage
    return client.chat(messages), {}
