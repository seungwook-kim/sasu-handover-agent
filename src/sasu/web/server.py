"""웹 화면용 API. 브라우저에서 CLI와 같은 기능(지도·추적·질문·물어보기·정리)을 쓴다.

화면 파일(static/)은 외부 CDN 없이 이 패키지 안에 있다. 인터넷이 막힌 PC에서도 그대로 열린다.
요청마다 지금 프로젝트의 분석 파일을 새로 연다 (sasu use로 바꾸면 바로 반영).
"""

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
from sasu.handover import draft_message, meta, refresh_questions, repo_of, save_answer
from sasu.rag.pipeline import choose_target, evidence_for
from sasu.trace import TraceNode, coverage, find_urls, trace_url

STATIC_DIR = Path(__file__).parent / "static"
_CODE_CONTEXT = 3  # 코드 보기에서 앞뒤로 더 보여 줄 줄 수
_MAX_CODE_LINES = 400

_UNUSED_TITLES = {
    "broken_sql": "끊어진 쿼리 참조",
    "sql": "부르는 곳이 없는 SQL",
    "url": "부르는 화면을 못 찾은 URL",
    "method": "부르는 곳이 없는 메서드",
    "jsp": "쓰이지 않는 JSP",
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

        with _connect() as conn:
            ov = build_overview(conn)
            cov = coverage(conn)
            modules = [
                {**asdict(m), "tables": [t for t, _ in m.tables.most_common(3)]}
                for m in ov.modules if m.urls or m.statements
            ]
            return {
                "build": ov.build,
                "counts": {k: v for k, v in ov.counts.items() if v or k in ("URL", "SQL")},
                "modules": modules,
                "tables": [asdict(t) for t in ov.tables],
                "coverage": asdict(cov),
            }

    @app.get("/api/modules/{name}")
    def module(name: str):
        from sasu.analysis.overview import module_urls

        with _connect() as conn:
            rows = module_urls(conn, name)
        if not rows:
            raise HTTPException(404, f"'{name}' 모듈의 URL이 없어요.")
        return {"module": name, "urls": [asdict(r) for r in rows]}

    # ── 경로 추적 ────────────────────────────────────────────

    @app.get("/api/urls")
    def urls(q: str = "", limit: int = 30):
        with _connect() as conn:
            if q.strip():
                found = find_urls(conn, q.strip())
            else:
                found = [r["url"] for r in conn.execute("SELECT DISTINCT url FROM url_mappings ORDER BY url")]
        return {"total": len(found), "urls": found[:limit]}

    @app.get("/api/trace")
    def trace(url: str, full: bool = False):
        with _connect() as conn:
            found = find_urls(conn, url)
            if url not in found:
                if len(found) != 1:
                    return {"url": None, "candidates": found[:30]}
                url = found[0]
            return {"url": url, "tree": _node(trace_url(conn, url, full=full))}

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
            items = [
                {
                    "id": r["id"], "kind": r["kind"], "kind_label": KIND_LABEL.get(r["kind"], r["kind"]),
                    "priority": round(r["priority"], 1), "question": r["question"], "why": r["why"],
                    "file_path": r["file_path"], "line": r["line"], "target": r["target"], "code": r["code"],
                    "status": r["status"], "answer": r["answer"], "answered_by": r["answered_by"],
                    "answered_at": r["answered_at"],
                }
                for r in rows
            ]
        answered = sum(1 for q in items if q["status"] == "answered")
        return {"total": len(items), "answered": answered, "kinds": KIND_LABEL, "items": items}

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

    @app.get("/api/questions/{question_id}/draft")
    def questions_draft(question_id: int):
        with _connect() as conn:
            message = draft_message(conn, question_id)
        if message is None:
            raise HTTPException(404, f"Q{question_id} 질문이 없어요.")
        return {"message": message}

    @app.get("/api/questions/export")
    def questions_export():
        from sasu.handover_sheet import export_sheet

        name = current_project() or "project"
        out = Path(tempfile.gettempdir()) / f"sasu-{name}-인수인계질문.xlsx"
        with _connect() as conn:
            export_sheet(conn, out)
        return FileResponse(out, filename=f"{name}-인수인계질문.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.post("/api/questions/import")
    async def questions_import(file: UploadFile = File(...)):
        from sasu.handover_sheet import import_sheet

        if not (file.filename or "").lower().endswith(".xlsx"):
            raise HTTPException(400, "엑셀(.xlsx) 질문지를 올려 주세요.")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheet.xlsx"
            path.write_bytes(await file.read())
            with _connect() as conn:
                try:
                    saved, missing = import_sheet(conn, path)
                except Exception as e:  # 형식이 다른 엑셀
                    raise HTTPException(400, f"질문지를 읽지 못했어요: {e}")
        return {"saved": saved, "missing": missing}

    # ── 물어보기 (RAG) ───────────────────────────────────────

    @app.post("/api/ask/evidence")
    def ask_evidence(body: AskIn):
        """대상 코드를 찾고 근거를 모은다. LLM은 부르지 않는다 (빠름)."""
        with _connect() as conn:
            sel = choose_target(conn, body.question, body.target, body.method_id)
            chosen = sel.target
            evidence = evidence_for(conn, chosen, body.use_git) if chosen else []
            return {
                "candidates": [asdict(c) for c in sel.candidates],
                "target": asdict(chosen) if chosen else None,
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


def _node(node: TraceNode) -> dict:
    return {
        "kind": node.kind, "label": node.label, "location": node.location, "detail": node.detail,
        "note": node.note, "count": node.count, "repeated": node.repeated,
        "children": [_node(c) for c in node.children],
    }
