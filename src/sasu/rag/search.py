"""질문 문장으로 관련 메서드를 찾는다 (BM25, 한국어는 글자 2-gram).

코드는 영어 식별자(nttId), 질문은 한국어(글 번호)라서 그대로는 안 맞는다. 그래서
- 메서드마다 이름(카멜 표기를 쪼갬)·Javadoc·주석·문자열·부르는 메서드 이름·SQL 테이블·
  인수인계 답변을 한 문서로 묶고,
- VO 필드 주석(/** 게시물 아이디 */ private Long nttId)으로 용어 사전을 자동으로 만들어
  질문의 한국어를 식별자로 넓힌다 ("게시물 번호" → ntt, no).
"""

import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from sasu.analysis.labels import Labels

_GRAPH_BOOST = 0.25  # 부르는 메서드 점수의 몇 %를 부르는 쪽에 더할지
_CANDIDATES = 100  # 하이브리드에서 각 검색이 넘기는 후보 수
_VECTOR_WEIGHT = 0.3  # 하이브리드: 정규화한 BM25 + 0.3 × 벡터 코사인 (평가로 정함)
_MAX_DOC_CHARS = 1500  # 조각 하나의 최대 길이 (임베딩 모델 입력 한도 고려)
_ANSWER_BOOST = 0.5  # 인수인계 답변이 질문과 맞으면 1위 점수의 최대 50%를 더한다
_ANSWER_MIN_COVERAGE = 0.34  # 질문 단어(idf 가중) 중 답변에 있는 비율이 이 이상일 때만

_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+|[가-힣]+")

# 질문하는 말투라 어디에나 걸리는 표현
_STOP_KO = {
    "어떻게", "어떻", "무엇", "뭐야", "뭔가", "무슨", "언제", "어디", "어디서", "하나요", "되나요", "되나", "돼요",
    "있나", "있나요", "없으면", "있으면", "하면", "할때", "만들어", "만드", "만들", "알려", "알려줘", "이유", "경우",
    "처리", "규칙", "때문", "그래서", "하는", "되는", "대해", "관련", "코드", "기능",
    # 무엇을 묻는지(테이블·SQL을 읽나)를 나타내는 말. 코드 주석의 "~ 테이블"과 겹쳐 엉뚱한 메서드가 뜬다
    "어떤", "테이블", "읽어", "읽는", "읽나", "쓰는", "하는지",
}
_STOP_EN = {"get", "set", "is", "to", "the", "and", "of", "egov", "vo", "impl", "do", "java", "com", "egovframework"}
_STOP_BIGRAMS = {w[i:i + 2] for w in _STOP_KO for i in range(len(w) - 1)} | {w for w in _STOP_KO if len(w) == 2}


# 말끝의 조사·어미. "코드는"이 "드는", "처리해"가 "리해" 같은 엉뚱한 2-gram을 만들지 않게 뗀다.
# 명사 끝과 헷갈리는 것(이·가·의·과·도·요 등)은 넣지 않는다 ("휴가", "처리결과", "불필요")
_ENDINGS = tuple(sorted({
    "했습니다", "합니다", "하는지", "하나요", "해서", "하면", "할때", "해요", "나요", "까요",
    "으로는", "에서는", "으로", "에서", "에게", "까지", "부터", "은", "는", "을", "를", "해", "할",
}, key=len, reverse=True))


def _strip_ending(word: str) -> str:
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 2:
            return word[: -len(ending)]
    return word


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD.findall(text):
        if re.match(r"[가-힣]", word):
            word = _strip_ending(word)
            if word in _STOP_KO:
                continue
            tokens.extend(
                bigram for i in range(len(word) - 1)
                if (bigram := word[i:i + 2]) not in _STOP_BIGRAMS
            )
        else:
            parts = [p.lower() for p in _CAMEL.findall(word) or [word] if len(p) > 1]
            tokens.extend(p for p in parts if p not in _STOP_EN)
            if len(parts) > 1:
                tokens.append(word.lower())
    return tokens


def _korean_bigrams(text: str) -> set[str]:
    return {t for t in tokenize(text) if re.match(r"[가-힣]", t)}


@dataclass
class Hit:
    method_id: int
    score: float
    label: str


class MethodIndex:
    def __init__(self, conn: sqlite3.Connection, embedder=None):
        """embedder가 있고 벡터 색인(sasu embed)이 있으면 BM25와 벡터 검색을 섞는다."""
        self.conn = conn
        self.embedder = embedder
        docs: dict[int, list[str]] = defaultdict(list)
        self.labels: dict[int, str] = {}
        names = Labels(conn)
        for r in conn.execute(
            """SELECT m.id, m.name, m.doc, m.body_tokens, m.class_id, c.name AS class_name, c.doc AS class_doc
               FROM methods m JOIN classes c ON c.id = m.class_id WHERE c.kind != 'interface'"""
        ):
            # VO의 getter/setter는 필드 이름만 담은 껍데기라 검색에서 뺀다
            if re.match(r"^(get|set|is)[A-Z]", r["name"]) and r["body_tokens"] < 20:
                continue
            self.labels[r["id"]] = f"{r['class_name']}.{r['name']}"
            words = " ".join(_CAMEL.findall(r["name"]))  # insertVcatnManage → insert Vcatn Manage
            # 클래스 업무명(휴가관리)·메서드 한글 설명은 따로 넣는다. 클래스 주석 앞 300자에는
            # 파일 이름·수정 이력만 있고 업무명이 빠지는 경우가 많다
            docs[r["id"]] += [r["name"], words, r["class_name"], names.class_label(r["class_id"]) or "",
                              names.method_label(r["id"]) or "", r["doc"] or "", (r["class_doc"] or "")[:300]]
        for r in conn.execute("SELECT method_id, text FROM notes"):
            docs[r["method_id"]].append(r["text"])
        for r in conn.execute("SELECT method_id, value FROM string_literals WHERE length(value) BETWEEN 2 AND 60"):
            docs[r["method_id"]].append(r["value"])
        for r in conn.execute("SELECT method_id, group_concat(callee, ' ') AS callees FROM calls GROUP BY method_id"):
            docs[r["method_id"]].append(r["callees"])
        for r in conn.execute(
            """SELECT e.src_id, s.full_id, group_concat(t.table_name, ' ') AS tables
               FROM edges e JOIN statements s ON s.id = e.dst_id
               LEFT JOIN statement_tables t ON t.statement_id = s.id
               WHERE e.dst_kind = 'statement' GROUP BY e.src_id, s.id"""
        ):
            docs[r["src_id"]].append(f"{r['full_id']} {r['tables'] or ''}")
        # 인수인계 답변은 대상 메서드 이름으로 붙인다 (id는 색인마다 바뀜)
        by_label = defaultdict(list)
        for mid, label in self.labels.items():
            by_label[label].append(mid)
        self.answer_terms: dict[int, set[str]] = defaultdict(set)
        for r in conn.execute("SELECT target, question, answer FROM questions WHERE answer IS NOT NULL AND status != 'skipped' AND stale = 0"):
            for mid in by_label.get(r["target"] or "", []):
                docs[mid].append(f"{r['question']} {r['answer']}")
                self.answer_terms[mid] |= set(tokenize(f"{r['question']} {r['answer']}"))

        # 메서드 하나 = 조각 하나. BM25와 임베딩이 같은 글을 쓴다.
        self.documents: dict[int, str] = {
            mid: _clean(" ".join(p for p in parts if p))[:_MAX_DOC_CHARS]
            for mid, parts in docs.items() if mid in self.labels
        }
        self.tf: dict[int, Counter] = {mid: Counter(tokenize(text)) for mid, text in self.documents.items()}
        self.lengths = {mid: sum(tf.values()) for mid, tf in self.tf.items()}
        self.avg_len = sum(self.lengths.values()) / max(len(self.lengths), 1)
        df: Counter = Counter()
        for tf in self.tf.values():
            df.update(tf.keys())
        n = len(self.tf)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

        # 코드 그래프: 잘 맞는 메서드를 부르는 쪽(규칙이 있는 서비스)도 끌어올리기 위해
        self.callees: dict[int, list[int]] = defaultdict(list)
        for e in conn.execute("SELECT src_id, dst_id FROM edges WHERE src_kind = 'method' AND dst_kind = 'method'"):
            if e["src_id"] in self.tf and e["dst_id"] in self.tf:
                self.callees[e["src_id"]].append(e["dst_id"])

        # 용어 사전: 필드 주석의 한국어 2-gram → 필드 이름 토큰
        self.glossary: list[tuple[set[str], list[str]]] = []
        seen = set()
        for r in conn.execute("SELECT DISTINCT name, doc FROM fields WHERE doc IS NOT NULL AND doc GLOB '*[가-힣]*'"):
            key = (r["name"], r["doc"])
            if key in seen:
                continue
            seen.add(key)
            ko = _korean_bigrams(r["doc"])
            ident = [t for t in tokenize(r["name"]) if not re.match(r"[가-힣]", t)]
            if ko and ident:
                self.glossary.append((ko, ident))

    def expand(self, query: str, limit: int = 6) -> list[str]:
        """질문의 한국어와 필드 주석이 2-gram 두 개 이상 겹치면 그 필드 이름 토큰을 더한다."""
        ko = _korean_bigrams(query)
        if not ko:
            return []
        scores: Counter = Counter()
        for doc_ko, ident in self.glossary:
            overlap = len(ko & doc_ko)
            if overlap >= 2 or (overlap == 1 and len(doc_ko) == 1):
                for t in ident:
                    scores[t] += overlap
        return [t for t, _ in scores.most_common(limit)]

    @property
    def has_vectors(self) -> bool:
        from sasu.rag.vectors import has_vector_index

        return self.embedder is not None and has_vector_index(self.conn) == self.embedder.name

    def bm25(self, query: str, expand: bool = True, k1: float = 1.2, b: float = 0.75) -> dict[int, float]:
        terms = Counter(tokenize(query))
        for t in self.expand(query) if expand else []:
            terms[t] += 0.5
        scores: dict[int, float] = {}
        for mid, tf in self.tf.items():
            score = 0.0
            for term, qf in terms.items():
                f = tf.get(term)
                if not f:
                    continue
                norm = f * (k1 + 1) / (f + k1 * (1 - b + b * self.lengths[mid] / self.avg_len))
                score += self.idf.get(term, 0.0) * norm * qf
            if score > 0:
                scores[mid] = score
        return scores

    def vector(self, query: str, top: int = _CANDIDATES) -> dict[int, float]:
        """코사인 유사도 (1 - 거리)."""
        from sasu.rag.vectors import vector_search

        query_vector = self.embedder.embed([query])[0]
        return {mid: 1.0 - dist for mid, dist in vector_search(self.conn, query_vector, top) if mid in self.labels}

    def search(self, query: str, top: int = 5, expand: bool = True, graph: bool = True,
               mode: str = "auto") -> list[Hit]:
        """mode: bm25, vector, hybrid(선형 결합), rrf(비교용), auto(벡터 색인이 있으면 hybrid).
        expand=용어 사전으로 질문 넓히기, graph=부르는 쪽으로 점수 전파 (평가에서 하나씩 끄고 비교)."""
        if mode == "auto":
            mode = "hybrid" if self.has_vectors else "bm25"
        if mode == "bm25":
            scores = self.bm25(query, expand)
        elif mode == "vector":
            scores = self.vector(query)
        elif mode == "rrf":  # 비교용: 순위만 합치면 BM25의 확신 있는 1순위가 희석된다
            scores = _rrf([self.bm25(query, expand), self.vector(query)])
        else:
            scores = _linear(self.bm25(query, expand), self.vector(query), _VECTOR_WEIGHT)
        scores = self._answer_boost(query, scores)
        # 그래프 전파: 직접 부르는 메서드 중 가장 잘 맞는 것의 점수 일부를 더한다
        boosted = dict(scores)
        for mid, callees in self.callees.items() if graph else []:
            best_callee = max((scores.get(c, 0.0) for c in callees), default=0.0)
            if best_callee > 0:
                boosted[mid] = boosted.get(mid, 0.0) + _GRAPH_BOOST * best_callee
        best = sorted(boosted.items(), key=lambda kv: -kv[1])[:top]
        return [Hit(mid, round(s, 4), self.labels[mid]) for mid, s in best]


    def _answer_boost(self, query: str, scores: dict[int, float]) -> dict[int, float]:
        """사람이 남긴 인수인계 답변이 질문과 맞는 메서드를 끌어올린다.
        메서드 문서는 코드·주석으로 길어서 답변 몇 줄이 BM25 길이 보정에 묻히기 때문이다."""
        if not self.answer_terms:
            return scores
        terms = set(tokenize(query))
        total = sum(self.idf.get(t, 0.0) for t in terms)
        if not total:
            return scores
        top = max(scores.values(), default=0.0) or 1.0
        boosted = dict(scores)
        for mid, answer in self.answer_terms.items():
            coverage = sum(self.idf.get(t, 0.0) for t in terms & answer) / total
            if coverage >= _ANSWER_MIN_COVERAGE:
                boosted[mid] = boosted.get(mid, 0.0) + _ANSWER_BOOST * coverage * top
        return boosted


def _linear(bm25: dict[int, float], vector: dict[int, float], weight: float) -> dict[int, float]:
    """BM25 점수를 최고점 1로 맞추고 벡터 코사인 유사도를 weight만큼 더한다."""
    top = max(bm25.values(), default=0.0) or 1.0
    fused = {mid: s / top for mid, s in bm25.items()}
    for mid, cos in vector.items():
        fused[mid] = fused.get(mid, 0.0) + weight * max(cos, 0.0)
    return fused


def _rrf(rankings: list[dict[int, float]], k: int = 60, depth: int = _CANDIDATES) -> dict[int, float]:
    """Reciprocal Rank Fusion: 점수 크기가 다른 두 검색을 순위로 합친다."""
    fused: dict[int, float] = defaultdict(float)
    for scores in rankings:
        for rank, (mid, _) in enumerate(sorted(scores.items(), key=lambda kv: -kv[1])[:depth], 1):
            fused[mid] += 1.0 / (k + rank)
    return dict(fused)


def _clean(text: str) -> str:
    return " ".join(text.replace("/**", " ").replace("*/", " ").replace("*", " ").split())
