"""MCP 서버: Claude Code·Cursor 같은 에이전트가 사수의 분석과 근거 모으기를 도구로 쓴다.

에이전트(호스트 LLM)가 답을 쓰고, 사수는 확정할 수 있는 것(호출 관계·SQL·테이블·인수인계 답변·
커밋 기록)을 근거 번호와 함께 돌려준다. 그래서 사수 쪽에 LLM 설정이 없어도 된다.

실행: sasu mcp  (stdio)
등록: claude mcp add sasu -- sasu mcp
"""

import sqlite3

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from sasu import __version__, db
from sasu.config import active_db_path, current_project, list_projects, project_db
from sasu.handover import meta, refresh_questions, repo_of, save_answer
from sasu.rag.evidence import ANSWER, FACT
from sasu.trace import TraceNode, find_urls, trace_url

INSTRUCTIONS = """사수(SASU)는 SI 프로젝트 인수인계 도구다. Java(Spring·MyBatis·JSP)와 Python(FastAPI·Flask·Django)
코드를 정적 분석해 URL → 메서드 → SQL → 테이블 연결, 인수인계 질문과 떠난 개발자의 답을 가지고 있다.

권장 흐름
- 처음: list_projects → project_overview
- "이 기능/코드는 왜 이래?": gather_evidence로 근거를 받고, 근거만으로 답한다.
  문장마다 [사실](확인된 사실 근거)·[답변](인수인계 답변 근거)·[추정](그 밖의 해석)을 붙이고 (E1)처럼 근거 번호를 단다.
  근거로 답할 수 없으면 모른다고 하고, 떠난 개발자에게 물을 질문을 제안한다.
- 화면·URL 흐름: trace_url. 코드 원문: read_code.
- 인수인계 준비: handover_questions로 물어볼 것을 보고, 받은 답은 record_answer로 남긴다.
project 인자를 비우면 사용자가 마지막으로 분석한(sasu index) 프로젝트를 쓴다."""

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# 함수 이름의 _tool 꼬리는 가져온 함수(list_projects, trace_url …)와 이름이 겹치지 않게 하려는 것. 도구 이름에는 없다

server = MCPServer("sasu", instructions=INSTRUCTIONS, version=__version__)


def _open(project: str | None) -> sqlite3.Connection:
    if project:
        if project not in list_projects():
            raise ToolError(f"'{project}' 프로젝트가 없습니다. 있는 프로젝트: {', '.join(list_projects()) or '없음'}")
        return db.connect(project_db(project))
    path = active_db_path()
    if path is None:
        raise ToolError("분석한 프로젝트가 없습니다. 사용자에게 `sasu index <프로젝트 폴더>` 실행을 안내하세요.")
    return db.connect(path)


@server.tool(name="list_projects", annotations=_READ)
def list_projects_tool() -> str:
    """분석해 둔 프로젝트 목록과 지금 프로젝트(*)."""
    names = list_projects()
    if not names:
        return "분석한 프로젝트가 없습니다. `sasu index <프로젝트 폴더>`로 먼저 분석해야 합니다."
    current = current_project()
    lines = []
    for name in names:
        conn = db.connect(project_db(name))
        lines.append(f"{'*' if name == current else '-'} {name}  (원본: {meta(conn, 'repo_path')}, 분석: {meta(conn, 'indexed_at')})")
        conn.close()
    return "\n".join(lines)


@server.tool(annotations=_READ)
def project_overview(project: str | None = None) -> str:
    """프로젝트 지도: 기술 스택, 규모, 모듈별 URL·SQL·주요 테이블, 여러 모듈이 함께 쓰는 테이블."""
    from sasu.analysis.overview import build_overview

    conn = _open(project)
    ov = build_overview(conn, top_tables=10)
    lines = []
    if ov.build:
        b = ov.build
        lines.append(f"# {b['project']} {b.get('version') or ''} ({b['tool']})")
        for key, label in (("java_version", "Java"), ("python_version", "Python")):
            if b.get(key):
                lines.append(f"- {label}: {b[key]}")
        lines += [f"- {cat}: {', '.join(items)}" for cat, items in b.get("stack", {}).items()]
    lines.append("\n규모: " + ", ".join(f"{k} {v:,}" for k, v in ov.counts.items() if v))
    lines.append("\n## 모듈 (URL 많은 순)\n| 모듈 | URL | SQL | 주요 테이블 |\n| --- | --- | --- | --- |")
    for m in [m for m in ov.modules if m.urls or m.statements][:20]:
        lines.append(f"| {m.name} | {m.urls} | {m.statements} | {', '.join(t for t, _ in m.tables.most_common(3))} |")
    if ov.tables:
        lines.append("\n## 여러 모듈이 함께 쓰는 테이블 (바꾸면 영향이 큼)")
        for t in ov.tables:
            lines.append(f"- {t.name}: 모듈 {len(t.modules)}개, SQL {t.statements}개 ({', '.join(t.modules[:5])})")
    conn.close()
    return "\n".join(lines)


@server.tool(name="trace_url", annotations=_READ)
def trace_url_tool(url: str, project: str | None = None, full: bool = False) -> str:
    """URL 하나가 거치는 메서드 → SQL → 테이블. url은 전체 또는 일부. full=True면 유틸 내부까지."""
    conn = _open(project)
    found = find_urls(conn, url)
    if not found:
        conn.close()
        return f"'{url}'와 일치하는 URL이 없습니다."
    if url not in found and len(found) > 1:
        conn.close()
        return f"'{url}'와 비슷한 URL {len(found)}개 중 하나를 골라 다시 부르세요:\n" + "\n".join(found[:30])
    root = trace_url(conn, url if url in found else found[0], full=full)
    conn.close()
    lines = [f"URL {root.label}"]
    for child in root.children:
        _tree(child, 1, lines)
    return "\n".join(lines)


def _tree(node: TraceNode, depth: int, lines: list[str]) -> None:
    pad = "  " * depth
    if node.kind == "method":
        text = f"{node.label} [{node.detail or '유틸'}] {node.location or ''}" + (" (위에서 펼침)" if node.repeated else "")
    elif node.kind == "statement":
        text = f"SQL {node.label} {node.location or ''} → 테이블 {node.detail or '-'}"
    elif node.kind == "view":
        text = f"화면 {node.label}" + (f" ({node.detail})" if node.detail else "")
    else:
        text = f"{node.label} ({node.detail or node.kind})"
    if node.note:
        text += f"  ※ {node.note}"
    if node.count > 1:
        text += f"  ×{node.count}"
    lines.append(f"{pad}- {text}")
    for child in node.children:
        _tree(child, depth + 1, lines)


@server.tool(annotations=_READ)
def existing_behaviors(module: str | None = None, url: str | None = None, project: str | None = None,
                       limit: int = 30) -> str:
    """기존 동작 목록: URL마다 처리 메서드, 화면, 읽는·쓰는 테이블, 업무 코드 분기, 주의 주석, 인수인계 답변.
    고도화 전후에 "원래 하던 일"을 확인할 때 쓴다. module(예: cop.bbs)이나 url 일부로 좁힌다."""
    from sasu.analysis.behaviors import list_behaviors

    conn = _open(project)
    items = list_behaviors(conn, module)
    conn.close()
    if url:
        items = [b for b in items if url in b.url]
    lines = [f"URL {len(items)}개 (보여 주는 것 {min(len(items), limit)}개)"]
    for b in items[:max(1, min(limit, 200))]:
        lines.append(f"\n## {b.url}  [{b.module}]  {b.handler}")
        for label, values in (("화면", b.screens), ("이동", b.redirects), ("읽기", b.reads), ("쓰기", b.writes),
                              ("분기", b.branches), ("주의 주석", b.notes), ("인수인계 답변", b.answers),
                              ("확인할 연결", b.external)):
            if values:
                lines.append(f"- {label}: " + "; ".join(values))
    return "\n".join(lines)


@server.tool(annotations=_READ)
def search_code(question: str, project: str | None = None, top: int = 5) -> str:
    """질문 문장으로 관련 메서드 후보를 찾는다 (BM25 + 벡터 + 코드 그래프 + 인수인계 답변)."""
    from sasu.rag.pipeline import search_candidates

    conn = _open(project)
    candidates, how = search_candidates(conn, question, top=max(1, min(top, 20)))
    conn.close()
    if not candidates:
        return "관련 코드를 찾지 못했습니다."
    return f"검색: {how}\n" + "\n".join(
        f"{i}. {c.label}  {c.path}:{c.line}  (점수 {c.score})" for i, c in enumerate(candidates, 1))


@server.tool(annotations=_READ)
def gather_evidence(question: str, target: str | None = None, project: str | None = None,
                    use_git: bool = True) -> str:
    """"왜?" 질문의 근거를 모은다. target은 Class.method, /url, 파일:줄 (비우면 질문으로 검색).
    돌려준 근거만으로 답하고, 문장마다 [사실]/[답변]/[추정]과 근거 번호(E1…)를 붙인다."""
    from sasu.rag.pipeline import choose_target, evidence_for

    conn = _open(project)
    sel = choose_target(conn, question, target)
    if sel.not_found:
        conn.close()
        return f"대상 '{sel.not_found}'을 찾지 못했습니다. search_code로 후보를 먼저 찾으세요."
    if sel.target is None:
        conn.close()
        return "관련 코드를 찾지 못했습니다. target에 Class.method나 URL을 넣어 보세요."
    evidence = evidence_for(conn, sel.target, use_git=use_git)
    conn.close()
    t = sel.target
    lines = [f"대상: {t.label}  {t.path}:{t.line_start}-{t.line_end}"]
    if sel.candidates:
        others = ", ".join(c.label for c in sel.candidates[1:])
        found_by = "같은 이름의 메서드 중 첫째(Service 우선)" if sel.how == "같은 이름" else "질문으로 찾은 1위"
        lines.append(f"({found_by}. 다른 후보: {others or '없음'} — 맞지 않으면 target에 Class.method를 넣어 다시 부르세요)")
    lines.append("\n근거 등급: 확인된 사실 → [사실], 인수인계 답변 → [답변], 기록(주석·커밋) → [추정]")
    for e in evidence:
        lines.append(f"\n### {e.id} ({e.level}) {e.title}" + (f"  {e.location}" if e.location else ""))
        lines.append(e.text)
    has_answer = any(e.level == ANSWER for e in evidence)
    has_fact = any(e.level == FACT for e in evidence)
    if not has_answer:
        lines.append("\n※ 이 코드에 대한 인수인계 답변은 아직 없습니다. 이유를 단정하지 말고 [추정]으로 쓰세요.")
    if not has_fact:
        lines.append("※ 코드 원문을 읽지 못했습니다 (원본 폴더가 없음).")
    return "\n".join(lines)


@server.tool(annotations=_READ)
def read_code(path: str | None = None, start: int = 1, end: int | None = None, target: str | None = None,
              project: str | None = None) -> str:
    """분석한 저장소 안의 코드를 줄 번호와 함께 읽는다 (최대 300줄).
    path(저장소 기준 상대 경로)와 줄 범위, 또는 target(Class.method나 메서드 이름)으로 그 메서드 전체를 읽는다."""
    from sasu.rag.evidence import resolve_target

    conn = _open(project)
    repo = repo_of(conn)
    found = resolve_target(conn, target) if target else []
    conn.close()
    if repo is None:
        raise ToolError("분석한 원본 폴더를 찾지 못했습니다.")
    if target:
        if not found:
            raise ToolError(f"'{target}' 메서드를 찾지 못했습니다. search_code로 이름을 확인하세요.")
        path, start, end = found[0].path, found[0].line_start, found[0].line_end
    if not path:
        raise ToolError("path나 target 중 하나를 주세요.")
    file = (repo / path).resolve()
    if not file.is_relative_to(repo.resolve()) or not file.is_file():
        raise ToolError(f"저장소 안에 '{path}' 파일이 없습니다.")
    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start)
    end = min(len(lines), end or start + 80, start + 299)
    return f"{path} ({start}-{end} / {len(lines)}줄)\n" + "\n".join(
        f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))


@server.tool(annotations=_READ)
def handover_questions(project: str | None = None, status: str = "open", kind: str | None = None,
                       limit: int = 20) -> str:
    """떠나는 개발자에게 물어야 할 인수인계 질문 (우선순위 순). status: open, answered, all.
    kind: broken_sql, dynamic_sql, magic_value, note, external_bean, empty_catch, complex, copied_code, unused_sql."""
    from sasu.analysis.questions import KIND_LABEL

    conn = _open(project)
    if not conn.execute("SELECT 1 FROM questions WHERE stale = 0 LIMIT 1").fetchone():
        refresh_questions(conn)
    where, args = ["stale = 0"], []
    if status == "open":
        where.append("status != 'answered'")
    elif status == "answered":
        where.append("status = 'answered'")
    if kind:
        where.append("kind = ?")
        args.append(kind)
    rows = conn.execute(
        f"SELECT * FROM questions WHERE {' AND '.join(where)} ORDER BY priority DESC, id LIMIT ?",
        (*args, max(1, min(limit, 100))),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*), SUM(status = 'answered') FROM questions WHERE stale = 0").fetchone()
    conn.close()
    lines = [f"질문 {total[0]}개 중 {total[1] or 0}개 답변됨. 보여 주는 것: {len(rows)}개 ({status})"]
    for q in rows:
        lines.append(f"\nQ{q['id']} [{KIND_LABEL.get(q['kind'], q['kind'])}] {q['question']}")
        lines.append(f"  위치: {q['target'] or '-'}  {q['file_path'] or ''}:{q['line'] or ''}")
        lines.append(f"  근거: {q['why']}")
        if q["code"]:
            lines.append(f"  코드: {q['code']}")
        if q["answer"]:
            lines.append(f"  답({q['answered_by'] or '-'}): {q['answer']}")
    return "\n".join(lines)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
def record_answer(question_id: int, answer: str, answered_by: str | None = None,
                  project: str | None = None) -> str:
    """인수인계 질문(Q번호)에 받은 답을 기록한다. 이후 gather_evidence가 이 답을 [답변] 근거로 쓴다.
    사용자가 준 답만 기록한다 (추측으로 채우지 않는다). 빈 답이면 답을 지운다."""
    conn = _open(project)
    ok = save_answer(conn, question_id, answer, answered_by)
    conn.close()
    if not ok:
        raise ToolError(f"Q{question_id} 질문이 없습니다. handover_questions로 번호를 확인하세요.")
    return f"Q{question_id}에 답을 저장했습니다." if answer.strip() else f"Q{question_id}의 답을 지웠습니다."


@server.tool(name="find_unused", annotations=_READ)
def find_unused_tool(kind: str | None = None, project: str | None = None, limit: int = 20) -> str:
    """정리 후보: broken_sql(코드가 부르는데 Mapper에 없는 쿼리), sql, url, method, jsp (부르는 곳이 없는 것)."""
    from sasu.analysis.unused import find_unused as analyze

    conn = _open(project)
    report = analyze(conn)
    conn.close()
    kinds = [kind] if kind else ["broken_sql", "sql", "url", "method", "jsp"]
    lines = []
    for k in kinds:
        items = getattr(report, k, None)
        if items is None:
            raise ToolError(f"kind는 broken_sql, sql, url, method, jsp 중 하나입니다: {k}")
        lines.append(f"\n## {k}: {len(items)}건")
        for f in items[:limit]:
            lines.append(f"- {f.name}  {f.location} [{f.confidence}] {f.reason}" + (f" / {f.evidence}" if f.evidence else ""))
    return "\n".join(lines).strip()


def main() -> None:
    server.run("stdio")
