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

_GRAPH_BOOST = 0.25  # 부르는 메서드 점수의 몇 %를 부르는 쪽에 더할지

_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+|[가-힣]+")

# 질문하는 말투라 어디에나 걸리는 표현
_STOP_KO = {
    "어떻게", "어떻", "무엇", "뭐야", "뭔가", "무슨", "언제", "어디", "어디서", "하나요", "되나요", "되나", "돼요",
    "있나", "있나요", "없으면", "있으면", "하면", "할때", "만들어", "만드", "만들", "알려", "알려줘", "이유", "경우",
    "처리", "규칙", "때문", "그래서", "하는", "되는", "대해", "관련", "코드", "기능",
}
_STOP_EN = {"get", "set", "is", "to", "the", "and", "of", "egov", "vo", "impl", "do", "java", "com", "egovframework"}
_STOP_BIGRAMS = {w[i:i + 2] for w in _STOP_KO for i in range(len(w) - 1)} | {w for w in _STOP_KO if len(w) == 2}


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD.findall(text):
        if re.match(r"[가-힣]", word):
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
    def __init__(self, conn: sqlite3.Connection):
        docs: dict[int, list[str]] = defaultdict(list)
        self.labels: dict[int, str] = {}
        for r in conn.execute(
            """SELECT m.id, m.name, m.doc, m.body_tokens, c.name AS class_name, c.doc AS class_doc
               FROM methods m JOIN classes c ON c.id = m.class_id WHERE c.kind != 'interface'"""
        ):
            # VO의 getter/setter는 필드 이름만 담은 껍데기라 검색에서 뺀다
            if re.match(r"^(get|set|is)[A-Z]", r["name"]) and r["body_tokens"] < 20:
                continue
            self.labels[r["id"]] = f"{r['class_name']}.{r['name']}"
            docs[r["id"]] += [r["name"], r["name"], r["class_name"], r["doc"] or "", (r["class_doc"] or "")[:300]]
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
        for r in conn.execute("SELECT target, question, answer FROM questions WHERE answer IS NOT NULL AND stale = 0"):
            for mid in by_label.get(r["target"] or "", []):
                docs[mid].append(f"{r['question']} {r['answer']}")

        self.tf: dict[int, Counter] = {
            mid: Counter(tokenize(" ".join(p for p in parts if p)))
            for mid, parts in docs.items() if mid in self.labels
        }
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

    def search(self, query: str, top: int = 5, k1: float = 1.2, b: float = 0.75,
               expand: bool = True, graph: bool = True) -> list[Hit]:
        """expand=용어 사전으로 질문 넓히기, graph=부르는 쪽으로 점수 전파 (평가에서 하나씩 끄고 비교)."""
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
        # 그래프 전파: 직접 부르는 메서드 중 가장 잘 맞는 것의 점수 일부를 더한다
        boosted = dict(scores)
        for mid, callees in self.callees.items() if graph else []:
            best_callee = max((scores.get(c, 0.0) for c in callees), default=0.0)
            if best_callee > 0:
                boosted[mid] = boosted.get(mid, 0.0) + _GRAPH_BOOST * best_callee
        best = sorted(boosted.items(), key=lambda kv: -kv[1])[:top]
        return [Hit(mid, round(s, 2), self.labels[mid]) for mid, s in best]
