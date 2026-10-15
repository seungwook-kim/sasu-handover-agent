"""웹 화면용 API. 브라우저에서 CLI와 같은 기능(지도·추적·질문·물어보기·정리)을 쓴다.

화면 파일(static/)은 외부 CDN 없이 이 패키지 안에 있다. 인터넷이 막힌 PC에서도 그대로 열린다.
요청마다 지금 프로젝트의 분석 파일을 새로 연다 (sasu use로 바꾸면 바로 반영).
"""

import mimetypes
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sasu import db
from sasu.config import active_db_path, current_project, list_projects, load_settings, set_current_project
from sasu.analysis.labels import Labels
from sasu.handover import draft_message, meta, refresh_questions, repo_of, save_answer, skip_question
from sasu.rag.pipeline import choose_target, evidence_for
from sasu.trace import TraceNode, coverage, find_urls, trace_url

STATIC_DIR = Path(__file__).parent / "static"
_CODE_CONTEXT = 3  # 코드 보기에서 앞뒤로 더 보여 줄 줄 수
_MAX_CODE_LINES = 400
mimetypes.add_type("font/woff2", ".woff2")  # Python mimetypes에 없어 octet-stream으로 나가던 것

_UNUSED_TITLES = {
    "broken_sql": "없는 SQL 호출",
    "sql": "미사용 SQL",
    "url": "부르는 곳 못 찾은 URL",
    "method": "미사용 메서드",
    "jsp": "미사용 JSP",
}
_UNUSED_HELP = {
    "broken_sql": "코드가 부르는데 Mapper에 없는 쿼리. 실행하면 오류가 날 수 있습니다.",
    "sql": "어느 코드도 부르지 않는 쿼리입니다.",
    "url": "화면·설정 어디에서도 부르지 않는 URL입니다.",
    "method": "Service·DAO·Component 중 부르는 코드나 설정이 없는 메서드입니다.",
    "jsp": "컨트롤러·include·링크 어디에서도 쓰지 않는 JSP입니다.",
}


class AnswerIn(BaseModel):
    answer: str
    by: str | None = None


class SkipIn(BaseModel):
    reason: str = ""
    by: str | None = None


class ProjectIn(BaseModel):
    name: str


class AskIn(BaseModel):
    question: str
    target: str | None = None  # Class.method, URL, 파일:줄
    method_id: int | None = None  # 후보 중 고른 것
    use_git: bool = True


def create_app() -> FastAPI:
    app = FastAPI(title="사수 SASU", docs_url="/api/docs", redoc_url=None)

    @app.middleware("http")
    async def no_stale_screen(request, call_next):
        # 새 버전을 설치한 뒤 브라우저가 예전 화면 파일을 쓰지 않도록 매번 확인하게 한다 (바뀌지 않았으면 304)
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ── 프로젝트 ─────────────────────────────────────────────

    @app.get("/api/projects")
    def projects():
        path = active_db_path()
        info = None
        if path is not None:
            with _connect() as conn:
                info = {
                    "repo_path": meta(conn, "repo_path"),
                    "indexed_at": meta(conn, "indexed_at"),
                    "head": meta(conn, "repo_head")[:10],
                }
        return {"current": current_project() if path else None, "projects": list_projects(), "info": info}

    @app.post("/api/projects/current")
    def use_project(body: ProjectIn):
        if body.name not in list_projects():
            raise HTTPException(404, f"'{body.name}' 프로젝트가 없어요.")
        set_current_project(body.name)
        return {"current": body.name}

    # ── 지도 ─────────────────────────────────────────────────

    @app.get("/api/overview")
    def overview():
        from sasu.analysis.overview import build_overview

        from sasu.analysis.unused import broken_sql_calls

        with _connect() as conn:
            ov = build_overview(conn)
            cov = coverage(conn)
            names = Labels(conn)
            modules = [
                {**asdict(m), "tables": [t for t, _ in m.tables.most_common(3)],
                 "label": names.module_label(m.name), "labels": names.module_labels(m.name)}
                for m in ov.modules if m.urls or m.statements
            ]
            broken = broken_sql_calls(conn)
            return {
                "build": ov.build,
                "counts": {k: v for k, v in ov.counts.items() if v or k in ("URL", "SQL")},
                "modules": modules,
                # 업무명마다 URL 수 (고도화 준비에서 "휴가관리"처럼 업무로 골라 받을 때)
                "businesses": names.businesses(),
                "tables": [asdict(t) for t in ov.tables],
                "coverage": asdict(cov),
                # 없는 SQL 호출: URL에서 이어지면 실행 오류 위험, 아니면 죽은 코드
                "broken_sql": {"reachable": sum(b.reachable for b in broken),
                               "unreachable": sum(not b.reachable for b in broken)},
            }

    @app.get("/api/modules/{name}")
    def module(name: str):
        from sasu.analysis.overview import module_urls

        with _connect() as conn:
            rows = module_urls(conn, name)
            names = Labels(conn)
            handlers = names.url_handlers()
            urls = [{**asdict(r), "label": names.url_label(r.url, handlers)} for r in rows]
        if not rows:
            raise HTTPException(404, f"'{name}' 모듈의 URL이 없어요.")
        return {"module": name, "urls": urls}

    @app.get("/api/behaviors/export")
    def behaviors_export(module: str | None = None, business: str | None = None):
        """기존 동작 목록 엑셀 (고도화 전후 확인용 체크리스트).
        business=휴가관리: 처리 메서드 클래스의 업무명이 그 값인 URL만 (module과 함께 쓰면 둘 다 맞는 것)."""
        from sasu.analysis.behaviors import export_behaviors, list_behaviors

        name = current_project() or "project"
        for part in (module, business):  # 파일 이름에 모듈·업무명을 넣는다. 파일 이름에 못 쓰는 글자는 뺀다
            if part and (safe := "".join(ch for ch in part.strip() if ch.isalnum() or ch in "._-")):
                name = f"{name}-{safe}"
        out = Path(tempfile.gettempdir()) / f"sasu-{name}-기존동작목록.xlsx"
        with _connect() as conn:
            export_behaviors(list_behaviors(conn, module, business), out)
        return FileResponse(out, filename=f"{name}-기존동작목록.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ── 경로 추적 ────────────────────────────────────────────

    @app.get("/api/urls")
    def urls(q: str = "", limit: int = 30):
        with _connect() as conn:
            names = Labels(conn)
            if q.strip():
                found = _search_urls(conn, names, q.strip())
            else:
                found = [r["url"] for r in conn.execute("SELECT DISTINCT url FROM url_mappings ORDER BY url")]
            handlers = names.url_handlers()
            shown = found[:limit]
            # labels: 돌려준 URL마다 처리 메서드의 한글 설명 (없으면 null)
            return {"total": len(found), "urls": shown,
                    "labels": {u: names.url_label(u, handlers) for u in shown}}

    @app.get("/api/trace")
    def trace(url: str, full: bool = False):
        from sasu.analysis.unused import broken_sql_calls

        with _connect() as conn:
            names = Labels(conn)
            found = _search_urls(conn, names, url)
            if url not in found:
                if len(found) != 1:
                    return {"url": None, "candidates": found[:30]}
                url = found[0]
            broken: dict[int, list[str]] = {}
            for b in broken_sql_calls(conn):
                broken.setdefault(b.method_id, []).append(b.query_id)
            return {"url": url, "tree": _node(trace_url(conn, url, full=full), names, broken)}

    @app.get("/api/code")
    def code(path: str, start: int = 1, end: int | None = None):
        """분석한 저장소 안의 파일만 읽는다."""
        with _connect() as conn:
            repo = repo_of(conn)
        if repo is None:
            raise HTTPException(404, "분석한 저장소 폴더를 찾지 못했어요.")
        file = (repo / path).resolve()
        if not file.is_relative_to(repo.resolve()) or not file.is_file():
            raise HTTPException(404, "파일이 없어요.")
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        end = end or start
        first = max(1, start - _CODE_CONTEXT)
        last = min(len(lines), max(end, start) + _CODE_CONTEXT, first + _MAX_CODE_LINES - 1)
        return {
            "path": path, "start": first, "focus": [start, end],
            "lines": lines[first - 1:last], "total": len(lines),
        }

    # ── 인수인계 질문 ────────────────────────────────────────

    @app.get("/api/questions")
    def questions():
        from sasu.analysis.questions import KIND_LABEL

        with _connect() as conn:
            if not conn.execute("SELECT 1 FROM questions WHERE stale = 0 LIMIT 1").fetchone():
                refresh_questions(conn)
            rows = conn.execute(
                "SELECT * FROM questions WHERE stale = 0 ORDER BY priority DESC, id"
            ).fetchall()
            names = Labels(conn)
            items = []
            for r in rows:
                class_id, method_id = _question_target(conn, r["target"], r["file_path"])
                module = names.module_of_class(class_id)
                items.append({
                    "id": r["id"], "kind": r["kind"], "kind_label": KIND_LABEL.get(r["kind"], r["kind"]),
                    "priority": round(r["priority"], 1), "question": r["question"], "why": r["why"],
                    "file_path": r["file_path"], "line": r["line"], "target": r["target"], "code": r["code"],
                    "status": r["status"], "answer": r["answer"], "answered_by": r["answered_by"],
                    "answered_at": r["answered_at"],
                    # 한글 이름표 "휴가관리 · 휴가를 등록한다"와 대상 클래스의 모듈 (uss.ion, 그 업무명)
                    "label": names.full(method_id) if method_id else names.class_label(class_id),
                    "module": module, "module_label": names.module_label(module),
                })
        answered = sum(1 for q in items if q["status"] == "answered")
        skipped = sum(1 for q in items if q["status"] == "skipped")
        return {"total": len(items), "answered": answered, "skipped": skipped, "kinds": KIND_LABEL, "items": items}

    @app.post("/api/questions/refresh")
    def questions_refresh():
        with _connect() as conn:
            total, new, updated, stale = refresh_questions(conn)
        return {"total": total, "new": new, "updated": updated, "stale": stale}

    @app.post("/api/questions/{question_id}/answer")
    def questions_answer(question_id: int, body: AnswerIn):
        with _connect() as conn:
            if not save_answer(conn, question_id, body.answer, body.by):
                raise HTTPException(404, f"Q{question_id} 질문이 없어요.")
            r = conn.execute("SELECT status, answer, answered_by, answered_at FROM questions WHERE id = ?",
                             (question_id,)).fetchone()
        return dict(r)

    @app.post("/api/questions/{question_id}/skip")
    def questions_skip(question_id: int, body: SkipIn):
        """묻지 않기로 한다. 이유는 답변 칸에 남는다. 되돌리기는 answer에 빈 답."""
        with _connect() as conn:
            if not skip_question(conn, question_id, body.reason, body.by):
                raise HTTPException(404, f"Q{question_id} 질문이 없어요.")
            r = conn.execute("SELECT status, answer, answered_by, answered_at FROM questions WHERE id = ?",
                             (question_id,)).fetchone()
        return dict(r)

    @app.get("/api/questions/{question_id}/draft")
    def questions_draft(question_id: int):
        with _connect() as conn:
            message = draft_message(conn, question_id)
        if message is None:
            raise HTTPException(404, f"Q{question_id} 질문이 없어요.")
        return {"message": message}

    @app.get("/api/questions/export")
    def questions_export(ids: str | None = None):
        """ids=1,2,3이면 그 질문만 (화면에서 거른 목록 그대로)."""
        from sasu.handover_sheet import export_sheet

        chosen = None
        if ids is not None and ids.strip():
            try:
                chosen = [int(x.strip().lstrip("Qq")) for x in ids.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(400, "ids는 쉼표로 나눈 질문 번호예요 (예: 1,2,3).")
        name = current_project() or "project"
        out = Path(tempfile.gettempdir()) / f"sasu-{name}-인수인계질문.xlsx"
        with _connect() as conn:
            export_sheet(conn, out, ids=chosen)
        return FileResponse(out, filename=f"{name}-인수인계질문.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.post("/api/questions/import")
    async def questions_import(file: UploadFile = File(...)):
        from sasu.handover_sheet import import_sheet_result

        if not (file.filename or "").lower().endswith(".xlsx"):
            raise HTTPException(400, "엑셀(.xlsx) 질문지를 올려 주세요.")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheet.xlsx"
            path.write_bytes(await file.read())
            with _connect() as conn:
                try:
                    result = import_sheet_result(conn, path)
                except Exception as e:  # 형식이 다른 엑셀
                    raise HTTPException(400, f"질문지를 읽지 못했어요: {e}")
        # saved: 실제로 바뀐 행, unchanged: 이미 같은 행, missing: 질문을 못 찾은 행
        return {"saved": result.saved, "unchanged": result.unchanged, "missing": result.missing}

    # ── 물어보기 (RAG) ───────────────────────────────────────

    @app.post("/api/ask/evidence")
    def ask_evidence(body: AskIn):
        """대상 코드를 찾고 근거를 모은다. LLM은 부르지 않는다 (빠름)."""
        with _connect() as conn:
            sel = choose_target(conn, body.question, body.target, body.method_id)
            chosen = sel.target
            evidence = evidence_for(conn, chosen, body.use_git) if chosen else []
            return {
                # auto: 사람이 정하지 않고 검색 1위로 골랐다 → 화면이 답 전에 대상이 맞는지 묻는다
                "auto": sel.auto,
                "candidates": [asdict(c) for c in sel.candidates],  # 후보마다 why(고른 이유)
                # 검색 1위가 질문과 약하게만 맞는다 → 화면이 경고하고 직접 고르게 한다
                "low_confidence": sel.low_confidence,
                "expanded_terms": sel.expanded_terms,  # LLM이 넓힌 말 (없으면 [])
                "target": {**asdict(chosen), "label_ko": Labels(conn).full(chosen.method_id)} if chosen else None,
                "evidence": [asdict(e) for e in evidence],
            }

    @app.post("/api/ask/answer")
    def ask_answer(body: AskIn):
        """근거를 다시 모아 LLM에 답을 받는다. 모든 문장에 등급과 근거 번호가 붙는다."""
        from sasu.rag.answer import LLMError, answer, make_llm_client

        with _connect() as conn:
            chosen = choose_target(conn, body.question, body.target, body.method_id).target
            if chosen is None:
                raise HTTPException(404, "관련 코드를 찾지 못했어요. 대상을 지정해 주세요.")
            evidence = evidence_for(conn, chosen, body.use_git)
        client = make_llm_client(load_settings())
        if not client.available:
            return {"error": "LLM이 설정되지 않았어요 (.env의 SASU_LLM_PROVIDER). 근거만 보여 드려요."}
        try:
            result = answer(body.question, chosen.label, evidence, client)
        except LLMError as e:
            return {"error": str(e)}
        return {
            "model": result.model,
            "cost_usd": getattr(client, "last_cost_usd", None),
            "sentences": [asdict(s) for s in result.sentences],
            "issues": result.issues,
        }

    # ── 정리 (불필요한 소스·공통화) ─────────────────────────

    @app.get("/api/unused")
    def unused():
        from sasu.analysis.unused import find_unused

        with _connect() as conn:
            report = find_unused(conn)
        return {
            "sections": [
                {"kind": k, "title": title, "help": _UNUSED_HELP[k], "items": [asdict(f) for f in getattr(report, k)]}
                for k, title in _UNUSED_TITLES.items()
            ]
        }

    @app.get("/api/common")
    def common(top: int = 15):
        from sasu.analysis.common import analyze_common, clone_stats

        with _connect() as conn:
            report = analyze_common(conn, top=top)
            groups, methods = clone_stats(conn)
        return {
            "shared": [asdict(s) for s in report.shared],
            "clones": [asdict(c) for c in report.clones],
            "clone_stats": {"groups": groups, "methods": methods},
            "duplicate_sql": [asdict(d) for d in report.duplicate_sql],
        }

    @app.exception_handler(_NoProject)
    def _no_project(_request, _exc):
        return JSONResponse(
            {"detail": "분석한 프로젝트가 없어요. 먼저 `sasu index <프로젝트 폴더>`를 실행하세요.", "code": "no_project"},
            status_code=409,
        )

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


class _NoProject(Exception):
    pass


class _connect:
    """with _connect() as conn: 요청마다 지금 프로젝트의 분석 파일을 열고 닫는다."""

    def __enter__(self) -> sqlite3.Connection:
        path = active_db_path()
        if path is None:
            raise _NoProject()
        self.conn = db.connect(path)
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.close()


def _node(node: TraceNode, names: Labels | None = None, broken: dict[int, list[str]] | None = None) -> dict:
    data = {
        "kind": node.kind, "label": node.label, "location": node.location, "detail": node.detail,
        "note": node.note, "count": node.count, "repeated": node.repeated,
        "children": [_node(c, names, broken) for c in node.children],
    }
    if node.kind == "method":
        # label은 이미 Class.method라서 한글 설명은 label_ko로 더한다
        data["label_ko"] = names.method_label(node.method_id) if names else None
        data["broken"] = (broken or {}).get(node.method_id, [])  # 이 메서드가 부르는데 Mapper에 없는 쿼리 ID
    elif node.kind == "statement":
        data["estimated"] = node.note is not None  # 동적 ID·메서드 이름 등으로 추정한 연결
    return data


def _search_urls(conn: sqlite3.Connection, names: Labels, query: str) -> list[str]:
    """URL 글자로 찾고, 한글이 있으면 처리 메서드·클래스의 한글 설명으로도 찾는다."""
    found = find_urls(conn, query)
    return found + [u for u in names.search_urls(query) if u not in found]


def _question_target(conn: sqlite3.Connection, target: str | None,
                     file_path: str | None) -> tuple[int | None, int | None]:
    """질문 대상 "Class.method"의 (클래스 id, 메서드 id). 쿼리 ID처럼 코드에 없으면 (None, None)."""
    if not target or "." not in target:
        return None, None
    cls, method = target.rsplit(".", 1)
    row = conn.execute(
        """SELECT c.id AS class_id, m.id AS method_id FROM methods m JOIN classes c ON c.id = m.class_id
           JOIN files f ON f.id = c.file_id WHERE c.name = ? AND m.name = ?
           ORDER BY f.path = ? DESC, m.id LIMIT 1""",
        (cls, method, file_path or ""),
    ).fetchone()
    if row:
        return row["class_id"], row["method_id"]
    row = conn.execute(
        "SELECT c.id FROM classes c JOIN files f ON f.id = c.file_id WHERE c.name = ? ORDER BY f.path = ? DESC LIMIT 1",
        (cls, file_path or ""),
    ).fetchone()
    return (row["id"], None) if row else (None, None)
