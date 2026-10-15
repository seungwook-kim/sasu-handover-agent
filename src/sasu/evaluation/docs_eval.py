"""인수인계 문서 기능 평가 (sasu eval-docs): 가상 인수인계 문서 + 정답 목록(answer-key.json)으로 잰다.

분석 파일은 복사본에만 문서를 넣는다 (원본은 읽기 전용으로 열어 복사만 한다).
정답 목록 형식 (평가 자료를 만든 사람이 따로 둔 것):
- 문서: {파일 이름: {...}} — 넣을 문서. 폴더의 다른 파일(비교용 md 등)은 읽기 성공만 잰다.
- wrong: 일부러 틀린 문장 [{id, 문서, 문장, anchor, 종류, ...}]
- secrets: 가짜 비밀 [{id, 문서, 종류, 원문, 가려야 할 비밀 부분?}]
- doc_questions: {doc_only: [...], doc_and_code: [...]} — {id, 질문, 문서, anchor}
- code_links: [{id, 문서, 언급, 종류, 이어져야 할 코드: ["Class.method" | "table:X" | "파일:줄"]}]

지표:
- 형식별 읽기 성공, 비밀 가림 재현율(종류별)과 과잉 가림 후보(정답 목록에 없는데 가린 값),
- 불일치 재현율(일부러 틀린 문장 중 잡은 수)·정밀도(잡은 것 중 틀린 문장에 맞는 것),
- 문서 질문 검색 상위 6·1위(정답 조각 = anchor가 들어 있는 조각),
- 코드 연결 재현율(언급이 들어 있는 조각의 연결이 정답 코드에 닿나),
- 기존 코드 질문(everyday.json) 상위 6·1위: 문서 넣기 전 원본 vs 넣은 뒤 복사본 (LLM 없이).
"""

import json
import re
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


def norm(text: str) -> str:
    """비교용: 공백·마크다운 강조·따옴표 모양 차이를 없앤다."""
    text = text.replace("**", "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", "", text)


@dataclass
class DocsEvalResult:
    meta: dict = field(default_factory=dict)
    formats: list[dict] = field(default_factory=list)
    secrets: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    everyday: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"meta": self.meta, "formats": self.formats, "secrets": self.secrets, "conflicts": self.conflicts,
                "questions": self.questions, "links": self.links, "everyday": self.everyday}


# ── 준비: 분석 파일 복사 ───────────────────────────────────

def copy_db(source: Path, work_dir: Path | None = None) -> Path:
    """원본 분석 파일을 읽기 전용으로 열어 work_dir에 복사한다 (원본에는 쓰지 않는다)."""
    from sasu import db

    work_dir = Path(work_dir or tempfile.mkdtemp(prefix="sasu-docs-eval-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / f"{Path(source).stem}-docs-eval.db"
    if target.exists():
        target.unlink()
    src = db.connect_readonly(Path(source))
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


# ── 1. 형식별 읽기 ─────────────────────────────────────────

def read_formats(folder: Path) -> list[dict]:
    from sasu.docs.chunker import make_chunks
    from sasu.docs.readers import SUPPORTED, DocReadError, read_document

    rows = []
    for f in sorted(p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED):
        try:
            doc = read_document(f)
            chunks = make_chunks(doc)
            rows.append({"file": f.name, "format": doc.format, "ok": bool(chunks), "blocks": len(doc.blocks),
                         "headings": sum(b.kind == "heading" for b in doc.blocks), "chunks": len(chunks),
                         "chars": sum(len(c.text) for c in chunks), "reason": None if chunks else "조각 없음"})
        except DocReadError as e:
            rows.append({"file": f.name, "format": f.suffix.lower().lstrip("."), "ok": False, "blocks": 0,
                         "headings": 0, "chunks": 0, "chars": 0, "reason": str(e)})
    return rows


# ── 2. 비밀 가림 ───────────────────────────────────────────

def secret_value(s: dict) -> str:
    return s.get("가려야 할 비밀 부분") or s["원문"]


def eval_secrets(conn: sqlite3.Connection, folder: Path, key: dict) -> dict:
    """정답 비밀이 저장된 조각에 남아 있나 + 가린 값 중 정답 목록에 없는 것(과잉 가림 후보)."""
    from sasu.docs.chunker import make_chunks
    from sasu.docs.readers import read_document
    from sasu.docs.redact import redact

    stored: dict[str, str] = defaultdict(str)
    for r in conn.execute("SELECT d.name, c.heading, c.page, c.text FROM doc_chunks c JOIN documents d ON d.id = c.doc_id"):
        stored[r["name"]] += "\n".join(x or "" for x in (r["heading"], r["page"], r["text"])) + "\n"
    raw: dict[str, str] = {}
    masked: list[tuple[str, str, str, str]] = []  # (문서, 종류, 값, 앞뒤 글)
    for name in key.get("문서", {}):
        path = Path(folder) / name
        if not path.exists():
            continue
        text = ""
        for c in make_chunks(read_document(path)):
            for part in (c.heading, c.page or "", c.text):
                found: list = []
                redact(part, found)
                text += part + "\n"
                for kind, value in found:
                    i = part.find(value)
                    masked.append((name, kind, value, part[max(0, i - 25):i + len(value) + 10].replace("\n", " ")))
        raw[name] = text

    items, by_kind = [], defaultdict(lambda: [0, 0])
    for s in key.get("secrets", []):
        value = secret_value(s)
        in_raw = value in raw.get(s["문서"], "")
        hidden = in_raw and value not in stored.get(s["문서"], "")
        items.append({"id": s["id"], "kind": s["종류"], "doc": s["문서"], "read": in_raw, "hidden": hidden})
        by_kind[s["종류"]][1] += 1
        by_kind[s["종류"]][0] += hidden
    secrets = [secret_value(s) for s in key.get("secrets", [])]
    over = []
    for name, kind, value, context in masked:
        if any(value in sv or sv in value for sv in secrets):
            continue
        over.append({"doc": name, "kind": kind, "value": value, "context": context})
    hidden_n = sum(i["hidden"] for i in items)
    return {"total": len(items), "hidden": hidden_n, "recall": hidden_n / len(items) if items else 0.0,
            "by_kind": {k: {"hidden": v[0], "total": v[1]} for k, v in by_kind.items()},
            "masks": len(masked), "over_masked": over, "items": items}


# ── 3. 불일치 ──────────────────────────────────────────────

def _matches(sentence: str, wrong: dict) -> bool:
    s, anchor, full = norm(sentence), norm(wrong.get("anchor") or ""), norm(wrong.get("문장") or "")
    if anchor and anchor in s:
        return True
    return len(s) >= 12 and (s in full or (full and full in s))


def eval_conflicts(conn: sqlite3.Connection, key: dict, conflicts: list | None = None) -> dict:
    """conflicts: Conflict 목록 (없으면 규칙 판정 all_conflicts). 문장이 틀린 문장(anchor)과 맞으면 잡은 것."""
    from sasu.docs.conflicts import all_conflicts

    found = all_conflicts(conn, judge=None) if conflicts is None else conflicts
    doc_of = {r["id"]: r["name"] for r in conn.execute(
        "SELECT c.id, d.name FROM doc_chunks c JOIN documents d ON d.id = c.doc_id")}
    wrongs = key.get("wrong", [])
    caught: dict[str, list] = defaultdict(list)
    rows, false = [], []
    for c in found:
        doc = doc_of.get(c.chunk_id)
        hit = next((w["id"] for w in wrongs if w["문서"] == doc and _matches(c.sentence, w)), None)
        row = {"wrong": hit, "kind": c.kind, "doc": doc, "sentence": c.sentence[:200], "doc_says": c.doc_says,
               "code_says": c.code_says[:200], "source": getattr(c, "source", "rule")}
        rows.append(row)
        if hit:
            caught[hit].append(c.kind)
        else:
            false.append(row)
    n = len(found)
    return {"wrong_total": len(wrongs), "caught": len(caught), "recall": len(caught) / len(wrongs) if wrongs else 0.0,
            "reported": n, "true_reports": n - len(false), "false_reports": len(false),
            "precision": (n - len(false)) / n if n else None,
            "per_wrong": [{"id": w["id"], "kind": w["종류"], "caught": w["id"] in caught,
                           "by": sorted(set(caught.get(w["id"], [])))} for w in wrongs],
            "false": false, "all": rows}


# ── 4. 문서 질문 검색 ──────────────────────────────────────

def gold_chunks(conn: sqlite3.Connection, doc: str, anchor: str) -> list[int]:
    a = norm(anchor)
    return [r["id"] for r in conn.execute(
        "SELECT c.id, c.heading, c.text FROM doc_chunks c JOIN documents d ON d.id = c.doc_id WHERE d.name = ?", (doc,))
        if a in norm(r["text"]) or a in norm(r["heading"] or "")]


def eval_questions(conn: sqlite3.Connection, key: dict, index, top: int = 6, expander=None) -> dict:
    """expander: 코드 검색의 질문 넓히기(QueryExpander). 주면 넓힌 말을 문서 검색에도 낮은 가중치로 더한다."""
    out = {}
    for group, items in (key.get("doc_questions") or {}).items():
        cases = []
        for q in items:
            gold = gold_chunks(conn, q["문서"], q["anchor"])
            extra = expander.expand(conn, q["질문"]) if expander is not None else None
            hits = index.search(q["질문"], top=top, extra_terms=extra)
            ids = [h.chunk_id for h in hits]
            rank = next((i + 1 for i, cid in enumerate(ids) if cid in gold), None)
            cases.append({"id": q["id"], "question": q["질문"], "gold": gold, "rank": rank, "top": ids, "extra": extra,
                          "top_titles": [h.title for h in hits[:3]]})
        n = len(cases)
        out[group] = {"n": n, "top6": sum(c["rank"] is not None for c in cases), "top1": sum(c["rank"] == 1 for c in cases),
                      "no_gold": sum(not c["gold"] for c in cases), "cases": cases}
    return out


# ── 5. 코드 연결 ───────────────────────────────────────────

class _Resolver:
    """연결(종류, 이름) → 닿는 코드 이름: 'Class.method', 'Class', 'table:X'."""

    def __init__(self, conn: sqlite3.Connection):
        from sasu.analysis.labels import Labels

        self.conn = conn
        self.class_name = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM classes")}
        self.label_classes: dict[str, set[str]] = defaultdict(set)
        for cid, label in Labels(conn).classes().items():
            if label and cid in self.class_name:
                self.label_classes[label].add(self.class_name[cid])

    def reach(self, kind: str, value: str) -> tuple[set[str], set[str]]:
        """(정확: 메서드·테이블, 넓게: 클래스)."""
        from sasu.docs.linking import CLASS, LABEL, METHOD, TABLE, URL

        if kind == METHOD:
            return {value}, {value.rsplit(".", 1)[0]}
        if kind == URL:
            rows = self.conn.execute(
                """SELECT c.name || '.' || m.name AS label, c.name AS cls FROM url_mappings u JOIN methods m ON m.id = u.method_id
                   JOIN classes c ON c.id = m.class_id WHERE u.url = ?""", (value,)).fetchall()
            return {r["label"] for r in rows}, {r["cls"] for r in rows}
        if kind == TABLE:
            return {f"table:{value.upper()}"}, set()
        if kind == CLASS:
            return set(), {value}
        if kind == LABEL:
            return set(), set(self.label_classes.get(value, set()))
        return set(), set()


def mention_chunks(conn: sqlite3.Connection, doc: str, mention: str) -> list[int]:
    """언급이 들어 있는 조각. 그대로 없으면 [메뉴]·URL·대문자 이름 같은 핵심 낱말이 다 있는 조각."""
    rows = conn.execute("SELECT c.id, c.heading, c.text FROM doc_chunks c JOIN documents d ON d.id = c.doc_id "
                        "WHERE d.name = ? ORDER BY c.seq", (doc,)).fetchall()
    m = norm(mention)
    exact = [r["id"] for r in rows if m in norm(r["text"])]
    if exact:
        return exact
    keys = re.findall(r"\[[^\]]+\]|/[\w./-]+|[A-Za-z_][\w.]{3,}", mention) or [mention]
    return [r["id"] for r in rows if all(norm(k) in norm(r["text"]) for k in keys)]


def eval_links(conn: sqlite3.Connection, key: dict) -> dict:
    resolver = _Resolver(conn)
    cases = []
    for item in key.get("code_links", []):
        expected = item.get("이어져야 할 코드") or []
        code = [e for e in expected if re.fullmatch(r"[A-Z][\w]*\.[a-zA-Z_]\w*|table:\w+", e)]
        files = [e for e in expected if e not in code]
        chunks = mention_chunks(conn, item["문서"], item["언급"])
        links = [(r["kind"], r["value"], r["found"]) for cid in chunks for r in conn.execute(
            "SELECT kind, value, found FROM doc_links WHERE chunk_id = ?", (cid,))]
        exact, broad = set(), set()
        configs = {v for k, v, f in links if f and k == "config"}  # "경로:줄" 또는 "경로"
        for kind, value, found in links:
            if found:
                e, b = resolver.reach(kind, value)
                exact |= e
                broad |= b
        file_hit = any(t in configs or (":" not in t and any(v.split(":")[0] == t for v in configs)) for t in files)
        if not expected:  # 코드에 없는 이름: found=0으로 남았나 (불일치 후보)
            name = norm(item["언급"]).upper()
            ok = any(not f and norm(v).upper() == name for _, v, f in links)
            status = "missing_marked" if ok else "missing_not_marked"
            strict = loose = ok
        else:
            strict = any(e in exact for e in code) or file_hit
            loose = strict or any(e.split(".")[0] in broad for e in code if not e.startswith("table:"))
            status = ("exact" if any(e in exact for e in code) else "config" if file_hit else
                      "class_or_label" if loose else "config_only" if not code else "none")
        cases.append({"id": item["id"], "kind": item["종류"], "mention": item["언급"], "chunks": chunks,
                      "expected": expected, "code_targets": code, "file_targets": files, "status": status,
                      "strict": strict, "loose": loose,
                      "links": sorted({f"{k}:{v}" for k, v, f in links if f})[:12]})
    n = len(cases)
    return {"n": n, "strict": sum(c["strict"] for c in cases), "loose": sum(c["loose"] for c in cases),
            "by_kind": {k: {"n": sum(c["kind"] == k for c in cases), "strict": sum(c["strict"] for c in cases if c["kind"] == k),
                            "loose": sum(c["loose"] for c in cases if c["kind"] == k)}
                        for k in sorted({c["kind"] for c in cases})},
            "no_chunk": [c["id"] for c in cases if not c["chunks"]], "cases": cases}


# ── 6. 기존 코드 질문 ──────────────────────────────────────

def eval_everyday(before: sqlite3.Connection, after: sqlite3.Connection, embedder=None) -> dict:
    from sasu.evaluation.everyday_eval import load_set, run_variant, split_of
    from sasu.rag.search import MethodIndex

    questions = load_set()["questions"]
    out = {}
    for name, conn in (("before", before), ("after", after)):
        index = MethodIndex(conn, embedder)
        run = run_variant(conn, name, questions, index, None)
        per = {}
        for split in sorted({split_of(q) for q in questions}):
            part = run.only(split)
            per[split] = {"n": len(part.specific), "top6": sum(c.rank is not None for c in part.specific),
                          "top1": sum(c.rank == 1 for c in part.specific)}
        out[name] = {"splits": per, "ranks": {c.id: c.rank for c in run.cases}}
    changed = [q for q, r in out["before"]["ranks"].items() if out["after"]["ranks"].get(q) != r]
    out["changed"] = changed
    return out


# ── 전체 ───────────────────────────────────────────────────

def ingest(conn: sqlite3.Connection, folder: Path, key: dict) -> list:
    """정답 목록의 '문서'만 넣는다 (비교용 사본은 넣지 않는다: 같은 내용이 두 번 검색되지 않게)."""
    from sasu.docs.linking import CodeNames
    from sasu.docs.store import add_document

    names = CodeNames(conn)
    results = []
    for name in key.get("문서", {}):
        path = Path(folder) / name
        if path.exists():
            results.append(add_document(conn, path, "eval-docs", names=names))
    return results


def run(docs: Path, key_path: Path, source_db: Path, work_dir: Path | None = None, embedder=None,
        everyday: bool = True, judge=None, expander=None, progress=lambda msg: None) -> DocsEvalResult:
    """judge: 문서–코드 불일치 LLM 판정 (docs.judge.ConflictJudge, 없으면 규칙만).
    expander: 문서 질문 검색에 질문 넓히기도 써서 따로 잰다 (questions["expanded"])."""
    from sasu import db
    from sasu.docs.conflicts import all_conflicts
    from sasu.docs.search import DocIndex

    key = json.loads(Path(key_path).read_text(encoding="utf-8"))
    started = time.perf_counter()
    result = DocsEvalResult()
    progress("분석 파일 복사")
    copy = copy_db(source_db, work_dir)
    conn = db.connect(copy)
    result.formats = read_formats(docs)
    progress("문서 넣기")
    added = ingest(conn, docs, key)
    result.meta = {"docs": str(docs), "key": str(key_path), "source_db": str(source_db), "work_db": str(copy),
                   "search": "hybrid" if embedder is not None else "bm25",
                   "added": [{"name": a.name, "status": a.status, "chunks": a.chunks, "links": a.links,
                              "redactions": a.redactions, "reason": a.reason} for a in added]}
    for f in result.formats:  # 문서 날짜: 정답 목록의 '문서 날짜'와 같은가 (적힌 자리까지만 비교)
        want = (key.get("문서", {}).get(f["file"]) or {}).get("문서 날짜")
        got = conn.execute("SELECT doc_date, date_source FROM documents WHERE name = ?", (f["file"],)).fetchone()
        if want and got:
            f.update({"doc_date": got["doc_date"], "date_source": got["date_source"], "expected_date": want,
                      "date_ok": got["doc_date"][:len(want)] == want[:len(got["doc_date"])]})
    progress("비밀 가림")
    result.secrets = eval_secrets(conn, docs, key)
    progress("불일치")
    found = all_conflicts(conn, judge=None)  # 규칙만 (LLM은 아래에서 따로 재서 합친다)
    rule_only = eval_conflicts(conn, key, found)
    if judge is not None:
        from sasu.docs.judge import merge

        progress("불일치 LLM 판정")
        llm = judge.conflicts(conn)
        result.conflicts = {"rule": _brief(rule_only), "llm": _brief(eval_conflicts(conn, key, llm)),
                            **eval_conflicts(conn, key, merge(found, llm)), "judge": judge.stats()}
    else:
        result.conflicts = rule_only
    progress("문서 질문 검색")
    doc_index = DocIndex(conn, embedder)
    result.questions = eval_questions(conn, key, doc_index)
    if expander is not None:
        progress("문서 질문 검색 + 질문 넓히기")
        counted = CountingClient(expander.client)
        expander.client = counted
        expanded = eval_questions(conn, key, doc_index, expander=expander)
        result.questions.update({f"{g}+넓히기": v for g, v in expanded.items()})
        result.meta["expand"] = counted.stats()
    progress("코드 연결")
    result.links = eval_links(conn, key)
    if everyday:
        progress("기존 코드 질문 (넣기 전·후)")
        before = db.connect_readonly(Path(source_db))
        try:
            result.everyday = eval_everyday(before, conn, embedder)
        finally:
            before.close()
    conn.close()
    result.meta["seconds"] = round(time.perf_counter() - started, 1)
    return result


class CountingClient:
    """LLM 호출 수·토큰을 세는 감싸개 (넓히기처럼 chat()만 부르는 곳에 끼운다)."""

    def __init__(self, client):
        self.client = client
        self.model = getattr(client, "model", "")
        self.calls = self.input_tokens = self.output_tokens = 0

    @property
    def available(self) -> bool:
        return getattr(self.client, "available", False)

    def chat(self, messages: list[dict]) -> str:
        from sasu.docs.judge import _chat

        text, usage = _chat(self.client, messages)
        self.calls += 1
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        return text

    def stats(self) -> dict:
        return {"model": self.model, "calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens}


def _brief(c: dict) -> dict:
    return {k: c[k] for k in ("caught", "recall", "reported", "false_reports", "precision")}


def summary_rows(r: DocsEvalResult) -> list[tuple[str, str]]:
    """사람이 읽는 한 줄 지표 (CLI 표·기록용)."""
    rows = []
    fm = Counter()
    ok = Counter()
    for f in r.formats:
        fm[f["format"]] += 1
        ok[f["format"]] += f["ok"]
    rows.append(("형식별 읽기", " · ".join(f"{k} {ok[k]}/{fm[k]}" for k in sorted(fm))))
    dated = [f for f in r.formats if "date_ok" in f]
    if dated:
        rows.append(("문서 날짜 맞음", f"{sum(f['date_ok'] for f in dated)}/{len(dated)} "
                     + " ".join(f"({f['format']} {f['doc_date']}·{f['date_source']})" for f in dated if not f["date_ok"])))
    s = r.secrets
    if s:
        kinds = ", ".join(f"{k} {v['hidden']}/{v['total']}" for k, v in s["by_kind"].items())
        rows.append(("비밀 가림 재현율", f"{s['hidden']}/{s['total']} ({kinds})"))
        rows.append(("과잉 가림 후보", f"{len(s['over_masked'])} (가린 곳 {s['masks']})"))
    c = r.conflicts
    if c:
        prec = f"{c['true_reports']}/{c['reported']}" if c["reported"] else "-"
        rows.append(("불일치 재현율", f"{c['caught']}/{c['wrong_total']}"))
        rows.append(("불일치 정밀도 (잘못 잡음)", f"{prec} (잘못 {c['false_reports']})"))
        for name in ("rule", "llm"):
            if name in c:
                b = c[name]
                rows.append((f"  {name}만", f"재현 {b['caught']}/{c['wrong_total']} · 보고 {b['reported']} · 잘못 {b['false_reports']}"))
    for group, q in (r.questions or {}).items():
        rows.append((f"문서 질문 {group} 상위 6 / 1위", f"{q['top6']}/{q['n']} / {q['top1']}/{q['n']}"
                     + (f" (정답 조각 못 찾음 {q['no_gold']})" if q["no_gold"] else "")))
    lk = r.links
    if lk:
        rows.append(("코드 연결 재현율 (정확 / 클래스·업무명까지)", f"{lk['strict']}/{lk['n']} / {lk['loose']}/{lk['n']}"))
    e = r.everyday
    if e:
        for split in e["before"]["splits"]:
            b, a = e["before"]["splits"][split], e["after"]["splits"][split]
            rows.append((f"코드 질문 {split} 상위 6 (전→후)", f"{b['top6']}/{b['n']} → {a['top6']}/{a['n']} · "
                         f"1위 {b['top1']} → {a['top1']}"))
        rows.append(("코드 질문 순위가 바뀐 질문", str(len(e["changed"]))))
    return rows


def print_report(console, r: DocsEvalResult) -> None:
    from rich.markup import escape
    from rich.table import Table

    table = Table(title="인수인계 문서 평가", title_justify="left")
    table.add_column("지표")
    table.add_column("값", justify="right")
    for name, value in summary_rows(r):
        table.add_row(escape(name), escape(value))
    console.print(table)
    missed = [w["id"] for w in r.conflicts.get("per_wrong", []) if not w["caught"]]
    if missed:
        console.print(f"[dim]놓친 틀린 문장: {', '.join(missed)}[/dim]")
    for f in r.conflicts.get("false", [])[:10]:
        console.print(f"[dim]잘못 잡음: {escape(f['kind'])} {escape(f['doc_says'])} — {escape(f['sentence'][:80])}[/dim]")
    for o in r.secrets.get("over_masked", [])[:10]:
        console.print(f"[dim]과잉 가림 후보: {escape(o['kind'])} '{escape(o['value'])}' — …{escape(o['context'])}…[/dim]")
    for group, q in r.questions.items():
        miss = [c["id"] for c in q["cases"] if c["rank"] is None]
        if miss:
            console.print(f"[dim]문서 질문 {group} 상위 6 밖: {', '.join(miss)}[/dim]")
    if r.links:
        miss = [f"{c['id']}({c['status']})" for c in r.links["cases"] if not c["strict"]]
        console.print(f"[dim]코드 연결 안 됨(정확): {', '.join(miss)}[/dim]")
