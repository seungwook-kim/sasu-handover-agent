"""사수 명령줄 도구."""

import re
import subprocess
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from sasu import __version__, db
from sasu.config import (
    active_db_path,
    current_project,
    list_projects,
    load_settings,
    project_db,
    project_name,
    set_current_project,
)
from sasu.indexer import index_repository
from sasu.trace import TraceNode, coverage, find_urls, trace_url

app = typer.Typer(help="사수: 떠난 개발자 대신 답하는 SI 인수인계 에이전트", no_args_is_help=True)
console = Console()

_STYLE = {
    "Controller": "bold cyan",
    "RestController": "bold cyan",
    "Service": "green",
    "Repository": "yellow",
}

SAMPLES = {
    "java": ("https://github.com/eGovFramework/egovframe-common-components.git",
             Path("data/repos/egovframe-common-components"), "약 250MB"),
    "python": ("https://github.com/fastapi/full-stack-fastapi-template.git",
               Path("data/repos/full-stack-fastapi-template"), "약 10MB"),
}


@app.command()
def version() -> None:
    """버전을 출력한다."""
    console.print(f"sasu {__version__}")


@app.command("fetch-sample")
def fetch_sample(
    python: bool = typer.Option(False, "--python", help="Python 예제(FastAPI 풀스택 템플릿)를 받는다"),
) -> None:
    """분석 예제를 내려받는다 (전체 커밋 기록 포함). 기본은 전자정부 표준프레임워크 공통컴포넌트(Java)."""
    url, target, size = SAMPLES["python" if python else "java"]
    if target.exists():
        console.print(f"이미 있어요: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"내려받는 중… ({size})")
    subprocess.run(["git", "clone", url, str(target)], check=True)
    console.print(f"[green]완료[/green]: {target}")


@app.command()
def index(repo: Path = typer.Argument(..., exists=True, file_okay=False, help="분석할 프로젝트 폴더")) -> None:
    """프로젝트를 분석해 코드 그래프를 만든다. 분석 파일은 프로젝트마다 따로 둔다."""
    settings = load_settings()
    name = project_name(repo)
    db_path = settings.db_path or project_db(name)
    started = time.perf_counter()
    with console.status("분석 중…"):
        conn = db.connect(db_path)
        stats = index_repository(conn, repo, settings.sql_dialect)
        statements = conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
    set_current_project(name)
    elapsed = time.perf_counter() - started

    table = Table(title=f"{name} 분석 완료 ({elapsed:.1f}초)", show_header=False)
    if stats.java_files:
        table.add_row("Java 파일", f"{stats.java_files:,}")
    if stats.python_files:
        table.add_row("Python 파일", f"{stats.python_files:,}")
    table.add_row("클래스 / 메서드", f"{stats.classes:,} / {stats.methods:,}")
    if stats.mapper_files:
        table.add_row(f"Mapper XML ({settings.sql_dialect})", f"{stats.mapper_files:,}")
    table.add_row("SQL·ORM 쿼리", f"{statements:,}")
    if stats.jsp_files:
        table.add_row("JSP", f"{stats.jsp_files:,}")
    table.add_row("URL", f"{stats.urls:,}")
    table.add_row("연결", f"{stats.edges:,}")
    console.print(table)
    console.print(f"저장 위치: {db_path} (지금 프로젝트로 설정됨)")

    # 다시 분석하면 벡터 색인도 지워진다. 임베딩을 설정해 두었으면 바로 다시 만든다 (바뀐 조각만 새로 임베딩)
    embedder = _embedder_or_none()
    if embedder is not None:
        from sasu.rag.search import MethodIndex
        from sasu.rag.vectors import build_vector_index

        with console.status(f"{embedder.name}로 벡터 색인을 만드는 중…") as status:
            total, new = build_vector_index(
                conn, MethodIndex(conn).documents, embedder,
                progress=lambda done, all_: status.update(f"벡터 색인 {done:,}/{all_:,}"),
            )
        console.print(f"벡터 색인: 조각 {total:,}개 (새로 임베딩 {new:,}, 캐시 재사용 {total - new:,})")


@app.command()
def projects() -> None:
    """분석한 프로젝트 목록. *가 지금 프로젝트다."""
    current = current_project()
    names = list_projects()
    if not names:
        console.print("분석한 프로젝트가 없어요. `sasu index <폴더>`로 시작하세요.")
        return
    for name in names:
        mark = "*" if name == current else " "
        console.print(f"{mark} {name}  [dim]{project_db(name)}[/dim]")


@app.command()
def use(name: str = typer.Argument(..., help="프로젝트 이름 (sasu projects로 확인)")) -> None:
    """다른 명령들이 쓸 프로젝트를 바꾼다."""
    if name not in list_projects():
        console.print(f"[red]'{name}' 프로젝트가 없어요. `sasu projects`로 확인하세요.[/red]")
        raise typer.Exit(1)
    set_current_project(name)
    console.print(f"지금 프로젝트: {name}")


@app.command()
def trace(
    url: str = typer.Argument(..., help="URL 전체 또는 일부 (예: /cop/bbs/selectArticleList.do)"),
    full: bool = typer.Option(False, "--full", help="유틸 클래스 내부 호출까지 모두 펼친다"),
) -> None:
    """URL 하나가 어떤 코드와 SQL, 테이블을 거치는지 따라간다."""
    url = _undo_msys_path(url)
    conn = _open_db()
    urls = find_urls(conn, url)
    if not urls:
        console.print(f"[red]'{url}'와 일치하는 URL이 없어요.[/red]")
        raise typer.Exit(1)
    if len(urls) > 1:
        console.print(f"[yellow]'{url}'와 비슷한 URL이 {len(urls)}개 있어요. 하나를 골라 다시 실행하세요.[/yellow]")
        for u in urls[:30]:
            console.print(f"  {u}")
        raise typer.Exit(1)
    root = trace_url(conn, urls[0], full=full)
    tree = Tree(f"[bold]URL[/bold] {root.label}")
    for child in root.children:
        _render(tree, child)
    console.print(tree)


@app.command()
def stats() -> None:
    """분석 결과 요약과 URL → SQL 연결률을 보여 준다."""
    conn = _open_db()
    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    table = Table(show_header=False)
    table.add_row("분석 대상", _meta(conn, "repo_path"))
    table.add_row("커밋", _meta(conn, "repo_head")[:10])
    table.add_row("분석 시각", _meta(conn, "indexed_at"))
    table.add_row("컨트롤러 / 서비스 / DAO", " / ".join(
        str(count(f"SELECT COUNT(*) FROM classes WHERE stereotype = '{s}'"))
        for s in ("Controller", "Service", "Repository")
    ))
    table.add_row("SQL 쿼리", f"{count('SELECT COUNT(*) FROM statements'):,}")
    cov = coverage(conn)
    pct = lambda n: f"{n:,} ({n / cov.total * 100:.1f}%)" if cov.total else "0"  # noqa: E731
    table.add_row("URL", f"{cov.total:,}")
    table.add_row("  SQL까지 이어짐", pct(cov.to_sql))
    table.add_row("  코드만 거침 (SQL 없음)", pct(cov.code_only))
    table.add_row("  컨트롤러에서 끝남", pct(cov.handler_only))
    console.print(table)


_UNUSED_TITLES = {
    "broken_sql": "끊어진 쿼리 참조 (코드가 부르는데 Mapper에 없음)",
    "sql": "부르는 곳이 없는 SQL",
    "url": "부르는 화면을 못 찾은 URL",
    "method": "부르는 곳이 없는 메서드 (Service·DAO·Component)",
    "jsp": "쓰이지 않는 JSP",
}


@app.command()
def unused(
    kind: str = typer.Option(None, "--kind", help="broken_sql, sql, url, method, jsp 중 하나만 보기"),
    limit: int = typer.Option(15, "--limit", help="종류별로 보여 줄 개수 (0이면 전부)"),
) -> None:
    """부르는 곳이 없거나 연결이 끊어진 SQL·URL·메서드·JSP를 찾는다."""
    from sasu.analysis.unused import find_unused

    report = find_unused(_open_db())
    kinds = [kind] if kind else list(_UNUSED_TITLES)
    empty = []
    for k in kinds:
        items = getattr(report, k)
        if not items and not kind:
            empty.append(_UNUSED_TITLES[k].split(" (")[0])
            continue
        shown = items if limit == 0 else items[:limit]
        table = Table(title=f"{_UNUSED_TITLES[k]}: {len(items)}건", title_justify="left", show_lines=False)
        table.add_column("이름", style="bold")
        table.add_column("위치", style="dim")
        table.add_column("확신")
        table.add_column("설명")
        for f in shown:
            reason = f.reason + (f"\n[yellow]{f.evidence}[/yellow]" if f.evidence else "")
            table.add_row(f.name, _short(f.location), f.confidence, reason)
        console.print(table)
        if len(items) > len(shown):
            console.print(f"[dim]… {len(items) - len(shown)}건 더 있음 (--limit 0)[/dim]")
        console.print()
    if empty:
        console.print(f"[dim]없음: {', '.join(empty)}[/dim]")


@app.command()
def questions(
    top: int = typer.Option(20, "--top", help="보여 줄 질문 수"),
    export: Path = typer.Option(None, "--export", help="엑셀로 내보낼 경로 (예: 인수인계질문.xlsx)"),
) -> None:
    """떠나는 개발자에게 물어야 할 인수인계 질문을 만든다. 이미 받은 답변은 그대로 둔다."""
    from sasu.analysis.questions import KIND_LABEL
    from sasu.handover import refresh_questions
    from sasu.handover_sheet import export_sheet

    conn = _open_db()
    total, new, updated, stale = refresh_questions(conn)
    console.print(f"질문 {total}개 (새 질문 {new}, 갱신 {updated}, 코드가 바뀌어 사라진 질문 {stale})")

    rows = conn.execute(
        "SELECT * FROM questions WHERE stale = 0 ORDER BY priority DESC, id LIMIT ?", (top,)
    ).fetchall()
    for i, q in enumerate(rows, 1):
        mark = "[green]답변함[/green]" if q["status"] == "answered" else ""
        console.print(
            f"\n[bold]{i}. [dim]Q{q['id']}[/dim] {q['question']}[/bold] {mark}\n"
            f"   [cyan]{KIND_LABEL.get(q['kind'], q['kind'])}[/cyan] · {q['target'] or '-'} "
            f"[dim]{_short(q['file_path'])}:{q['line']}[/dim]\n"
            f"   [dim]{q['why']}[/dim]"
            + (f"\n   [dim]> {q['code']}[/dim]" if q["code"] else "")
            + (f"\n   [green]답: {q['answer']}[/green]" if q["answer"] else "")
        )
    if export:
        n = export_sheet(conn, export)
        console.print(f"\n[green]엑셀로 내보냄[/green]: {export} ({n}개). 답변 열을 채운 뒤 `sasu answers {export}`로 가져오세요.")


@app.command()
def answers(sheet: Path = typer.Argument(..., exists=True, dir_okay=False, help="답변을 적은 질문지 엑셀")) -> None:
    """답변을 적은 인수인계 질문지를 가져와 코드 위치에 연결해 저장한다."""
    from sasu.handover_sheet import import_sheet

    conn = _open_db()
    saved, missing = import_sheet(conn, sheet)
    total = conn.execute("SELECT COUNT(*) FROM questions WHERE status = 'answered'").fetchone()[0]
    console.print(f"답변 {saved}개를 저장했어요. (지금까지 답변 {total}개)")
    if missing:
        console.print(f"[yellow]질문을 찾지 못한 행 {missing}개 (key 열이 바뀌었거나 다른 프로젝트 질문지)[/yellow]")


@app.command()
def embed(provider: str = typer.Option(None, "--provider", help="local 또는 api (비우면 .env의 SASU_EMBED_PROVIDER)")) -> None:
    """메서드 조각을 임베딩해 벡터 색인을 만든다. 이후 검색은 BM25와 벡터를 섞는다 (하이브리드)."""
    import os

    from sasu.config import load_settings
    from sasu.rag.embeddings import EmbeddingError, make_embedder
    from sasu.rag.search import MethodIndex
    from sasu.rag.vectors import build_vector_index

    settings = load_settings()
    if provider:
        os.environ["SASU_EMBED_PROVIDER"] = provider
    try:
        embedder = make_embedder(settings)
    except EmbeddingError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if embedder is None:
        console.print("[red]임베딩이 꺼져 있어요. --provider local 또는 .env의 SASU_EMBED_PROVIDER를 설정하세요.[/red]")
        raise typer.Exit(1)
    conn = _open_db()
    index = MethodIndex(conn)
    started = time.perf_counter()
    with console.status(f"{embedder.name}로 임베딩하는 중…") as status:
        total, new = build_vector_index(
            conn, index.documents, embedder,
            progress=lambda done, all_: status.update(f"{embedder.name}로 임베딩하는 중… {done:,}/{all_:,}"),
        )
    console.print(
        f"[green]벡터 색인 완료[/green]: 조각 {total:,}개 (새로 임베딩 {new:,}, 캐시 재사용 {total - new:,}) "
        f"{time.perf_counter() - started:.1f}초"
    )


def _embedder_or_none():
    """.env에 임베딩이 설정돼 있으면 만든다. 실패하면 조용히 BM25만 쓴다."""
    from sasu.config import load_settings
    from sasu.rag.embeddings import EmbeddingError, make_embedder

    try:
        return make_embedder(load_settings())
    except EmbeddingError:
        return None


_LABEL_STYLE = {"사실": "green", "답변": "cyan", "추정": "yellow", "모름": "red", "질문": "magenta"}


@app.command()
def ask(
    question: str = typer.Argument(..., help='물어볼 내용 (예: "휴가 신청할 때 연차가 없으면 어떻게 돼?")'),
    target: str = typer.Option(None, "--target", "-t", help="대상: Class.method, URL, 파일:줄"),
    no_git: bool = typer.Option(False, "--no-git", help="커밋 기록을 근거로 쓰지 않는다"),
    show_code: bool = typer.Option(False, "--show-code", help="근거의 코드 원문을 모두 보여 준다"),
) -> None:
    """코드·SQL·인수인계 답변·커밋 기록을 근거로 "왜?"에 답한다. 모든 문장에 근거와 등급을 붙인다."""
    from sasu.config import load_settings
    from sasu.rag.answer import LLMError, answer, make_llm_client
    from sasu.rag.evidence import gather, resolve_target, target_of
    from sasu.rag.search import MethodIndex

    conn = _open_db()
    repo = _repo(conn)
    targets = resolve_target(conn, _undo_msys_path(target)) if target else resolve_target(conn, question)
    if not targets:
        if target:
            console.print(f"[red]대상 '{target}'을 찾지 못했어요.[/red]")
            raise typer.Exit(1)
        index = MethodIndex(conn, _embedder_or_none())
        hits = index.search(question, top=5)
        if not hits:
            console.print("[red]관련 코드를 찾지 못했어요. --target으로 대상을 지정해 주세요.[/red]")
            raise typer.Exit(1)
        how = "BM25 + 벡터 하이브리드" if index.has_vectors else "BM25 (벡터 색인 없음: sasu embed)"
        console.print(f"[dim]질문으로 찾은 후보 · {how} (다른 것이면 --target으로 지정):[/dim]")
        for i, h in enumerate(hits, 1):
            console.print(f"[dim]  {i}. {h.label} ({h.score})[/dim]")
        targets = [target_of(conn, hits[0].method_id)]
    chosen = targets[0]
    console.print(f"\n[bold]대상[/bold] {chosen.label}  [dim]{_short(chosen.path)}:{chosen.line_start}[/dim]")

    evidence = gather(conn, chosen, repo, Path("data/cache"), use_git=not no_git)
    for e in evidence:
        body = e.text
        if e.title.endswith(" 코드") and not show_code:
            lines = body.splitlines()
            body = "\n".join(lines[:12]) + (f"\n… {len(lines) - 12}줄 더 (--show-code)" if len(lines) > 12 else "")
        console.print(f"\n[bold]{e.id}[/bold] [dim]({e.level})[/dim] {e.title}")
        console.print(body, style="dim", markup=False, highlight=False)

    client = make_llm_client(load_settings())
    try:
        with console.status("근거로 답을 만드는 중…"):
            result = answer(question, chosen.label, evidence, client)
    except LLMError as e:
        console.print(f"\n[red]{e}[/red]\n근거만 보여 줬어요.")
        raise typer.Exit(1)
    cost = getattr(client, "last_cost_usd", None)
    meta = ", ".join(x for x in [result.model, f"${cost:.4f}" if cost else None] if x)
    console.print("\n[bold]답[/bold]" + (f" [dim]({meta})[/dim]" if meta else ""))
    for s in result.sentences:
        style = _LABEL_STYLE.get(s.label, "white")
        cites = f" [dim]({', '.join(s.citations)})[/dim]" if s.citations else ""
        console.print(f"[{style}][{s.label}][/{style}] {s.text}{cites}")
    for issue in result.issues:
        console.print(f"[dim]※ {issue}[/dim]")


@app.command("answer")
def answer_cmd(
    question_id: str = typer.Argument(..., help="질문 번호 (예: Q12 또는 12)"),
    text: str = typer.Argument(..., help="받은 답"),
    by: str = typer.Option(None, "--by", help="답한 사람"),
) -> None:
    """떠난 뒤 연락해서 받은 답처럼, 질문 하나의 답을 바로 기록한다."""
    from sasu.handover import save_answer

    qid = int(question_id.lstrip("Qq"))
    if not save_answer(_open_db(), qid, text, by):
        console.print(f"[red]Q{qid} 질문이 없어요. `sasu questions`로 번호를 확인하세요.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Q{qid}에 답을 저장했어요.[/green] 이제 `sasu ask`가 이 답을 근거로 씁니다.")


@app.command()
def draft(question_id: str = typer.Argument(..., help="질문 번호 (예: Q12)")) -> None:
    """떠난 개발자에게 보낼 질문 메시지 초안을 만든다. 한 번에 정확히 묻도록 코드 위치와 근거를 담는다."""
    from sasu.handover import draft_message

    qid = int(question_id.lstrip("Qq"))
    message = draft_message(_open_db(), qid)
    if message is None:
        console.print("[red]질문이 없어요.[/red]")
        raise typer.Exit(1)
    console.print(message, markup=False, highlight=False)
    console.print(f"\n[dim]답을 받으면: sasu answer Q{qid} \"받은 답\" --by 이름[/dim]")


def _repo(conn) -> Path | None:
    from sasu.handover import repo_of

    return repo_of(conn)


@app.command("eval-questions")
def eval_questions(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, help="전체 커밋 기록이 있는 git 저장소"),
    at: str = typer.Option(..., "--at", help="인수인계 시점으로 볼 커밋·태그 (예: v5.0.0)"),
) -> None:
    """과거 시점의 코드로 질문을 만들고, 그 뒤 버그 수정이 난 메서드를 미리 짚었는지 채점한다."""
    from sasu.analysis.questions import KIND_LABEL
    from sasu.evaluation.history import evaluate

    with console.status(f"{at} 시점 코드를 꺼내 분석하고, 이후 버그 수정 커밋을 읽는 중…"):
        r = evaluate(repo, at)
    console.print(
        f"인수인계 시점 [bold]{r.snapshot}[/bold] {r.snapshot_date} · 이후 버그 수정 커밋 {r.fix_commits}개 · "
        f"고쳐진 메서드 {r.fixed_methods}개 / 전체 {r.candidate_methods:,}개 (기본 확률 {r.base_rate:.1%})"
    )
    ks = sorted(r.rankings[0].hits)
    table = Table(title="상위 K개가 나중에 고쳐진 메서드를 맞힌 수", title_justify="left")
    table.add_column("고르는 방법")
    for k in ks:
        table.add_column(f"K={k}", justify="right")
    for rk in r.rankings:
        table.add_row(rk.name, *[f"{rk.hits[k]} / {rk.sizes[k]} ({rk.precision(k):.0%})" for k in ks])
    console.print(table)
    kinds = Table(title="질문 종류별 (전체 질문 기준)", title_justify="left")
    kinds.add_column("종류")
    kinds.add_column("맞힘 / 질문", justify="right")
    for kind, (h, n) in sorted(r.by_kind.items(), key=lambda kv: -kv[1][1]):
        kinds.add_row(KIND_LABEL.get(kind, kind), f"{h} / {n}")
    console.print(kinds)
    for question, target, commit in r.examples:
        console.print(f"• {target}: {question}\n  [dim]→ 이후 수정: {commit}[/dim]")


@app.command("eval-search")
def eval_search(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, help="전체 커밋 기록이 있는 git 저장소"),
    at: str = typer.Option(..., "--at", help="수정 전 시점으로 볼 커밋·태그 (예: v5.0.0)"),
    embed_provider: str = typer.Option(None, "--embed", help="벡터 검색도 비교: local 또는 api"),
) -> None:
    """버그 증상 설명(커밋 메시지)으로 고칠 메서드를 찾는지 잰다. 기법을 하나씩 켜며 비교한다."""
    import os

    from sasu.evaluation.search_eval import evaluate_search

    if embed_provider:
        os.environ["SASU_EMBED_PROVIDER"] = embed_provider
    embedder = _embedder_or_none()
    with console.status(f"{at} 시점 코드로 색인하고, 이후 버그 수정 커밋을 질문으로 만드는 중…"):
        r = evaluate_search(repo, at, embedder=embedder)
    console.print(f"색인 시점 [bold]{r.snapshot}[/bold] · 질문 {r.queries}개 (한국어 버그 수정 커밋 제목)")
    ks = sorted(r.variants[0].recall)
    table = Table(title="상위 K개 안에 고친 메서드가 있는 비율", title_justify="left")
    table.add_column("검색 방법")
    for k in ks:
        table.add_column(f"Recall@{k}", justify="right")
    table.add_column("MRR", justify="right")
    for v in r.variants:
        table.add_row(v.name, *[f"{v.recall[k]:.0%}" for k in ks], f"{v.mrr:.3f}")
    console.print(table)
    for query, gold, rank in r.examples:
        console.print(f"• {query}\n  [dim]정답 {gold} → {'순위 ' + str(rank) if rank else '10위 밖'}[/dim]")


@app.command("eval-why")
def eval_why(
    at: str = typer.Option(..., "--at", help="이 시점 이후의 버그 수정 커밋에서 질문을 고른다 (예: v5.0.0)"),
    n: int = typer.Option(15, "--n", help="질문 수 (질문마다 LLM을 4번 부름)"),
    out: Path = typer.Option(Path("data/out/eval-why.json"), "--out", help="결과 JSON"),
) -> None:
    """커밋 메시지를 숨기고 "왜?"에 답하게 해, 사수(RAG)와 LLM만의 답을 채점 LLM으로 비교한다."""
    from sasu.config import load_settings
    from sasu.evaluation.why_eval import pick_cases, run, save, summarize
    from sasu.rag.answer import make_llm_client

    conn = _open_db()
    repo = _repo(conn)
    client = make_llm_client(load_settings())
    if repo is None or not client.available:
        console.print("[red]분석한 저장소와 LLM 설정이 필요해요 (SASU_LLM_PROVIDER 등).[/red]")
        raise typer.Exit(1)
    with console.status("질문 고르는 중…"):
        cases = pick_cases(conn, repo, at, n)
    console.print(f"질문 {len(cases)}개: 버그 수정 커밋이 넣은 줄이 지금도 남아 있는 곳")
    with console.status("답하고 채점하는 중…") as status:
        outcomes = run(conn, repo, client, cases, Path("data/cache"),
                       progress=lambda i, total: status.update(f"답하고 채점하는 중… {i}/{total}"))
    save(outcomes, out)
    summary = summarize(outcomes)
    table = Table(title=f"숨긴 커밋 메시지(실제 이유)를 맞힌 정도 · 질문 {summary['질문 수']}개", title_justify="left")
    table.add_column("지표")
    table.add_column("사수 (RAG)", justify="right")
    table.add_column("LLM만", justify="right")
    for key in summary["사수 (RAG)"]:
        table.add_row(key, str(summary["사수 (RAG)"][key]), str(summary["LLM만"][key]))
    console.print(table)
    console.print(f"[dim]질문별 답과 채점 이유: {out}[/dim]")


@app.command()
def serve(
    port: int = typer.Option(8765, "--port", help="포트"),
    host: str = typer.Option("127.0.0.1", "--host", help="0.0.0.0이면 같은 망의 다른 PC에서도 열린다"),
    no_browser: bool = typer.Option(False, "--no-browser", help="브라우저를 자동으로 열지 않는다"),
) -> None:
    """웹 화면을 연다: 한눈에 보기, 인수인계 질문, 물어보기, 경로 추적, 정리하기."""
    import threading
    import webbrowser

    import uvicorn

    from sasu.web.server import create_app

    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
    console.print(f"사수 웹 화면: [bold]{url}[/bold]  (끝내려면 Ctrl+C)")
    if active_db_path() is None:
        console.print("[yellow]아직 분석한 프로젝트가 없어요. `sasu index <프로젝트 폴더>`를 먼저 실행하세요.[/yellow]")
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.command("map")
def map_(module: str = typer.Argument(None, help="모듈 이름 (예: cop.bbs). 비우면 전체 지도")) -> None:
    """프로젝트 지도: 기술 스택, 모듈 구성, 여러 모듈이 함께 쓰는 핵심 테이블."""
    from sasu.analysis.overview import build_overview, module_urls

    conn = _open_db()
    if module:
        rows = module_urls(conn, module)
        if not rows:
            console.print(f"[red]'{module}' 모듈의 URL이 없어요. `sasu map`으로 모듈 이름을 확인하세요.[/red]")
            raise typer.Exit(1)
        table = Table(title=f"{module} 모듈: URL {len(rows)}개", title_justify="left")
        table.add_column("URL", style="bold")
        table.add_column("처리 메서드", style="cyan")
        table.add_column("읽는 테이블")
        table.add_column("쓰는 테이블", style="yellow")
        for r in rows:
            reads = [t for t in r.tables if t not in r.writes]
            table.add_row(r.url, r.handler, ", ".join(reads) or "-", ", ".join(r.writes) or "-")
        console.print(table)
        return

    ov = build_overview(conn)
    if ov.build:
        b = ov.build
        lines = [f"[bold]{b['project']}[/bold] {b.get('version') or ''} ({b['tool']}, {b.get('packaging') or '-'})"]
        if b.get("parent"):
            lines.append(f"부모: {b['parent']}")
        if b.get("java_version"):
            lines.append(f"Java {b['java_version']}")
        if b.get("python_version"):
            lines.append(f"Python {b['python_version']}")
        if b.get("servlet_api"):
            lines.append(f"Servlet API: {b['servlet_api']}")
        for category, items in b.get("stack", {}).items():
            lines.append(f"{category}: {', '.join(items)}")
        console.print("\n".join(lines))
        console.print()
    console.print("  ".join(f"{k} [bold]{v:,}[/bold]" for k, v in ov.counts.items() if v or k in ("URL", "SQL")))
    console.print()

    table = Table(title="모듈 (URL 많은 순)", title_justify="left")
    for col in ("모듈", "URL", "컨트롤러", "서비스", "DAO", "SQL"):
        table.add_column(col, justify="right" if col != "모듈" else "left")
    table.add_column("주요 테이블")
    for s in ov.modules:
        if s.urls == 0 and s.statements == 0:
            continue
        top = ", ".join(t for t, _ in s.tables.most_common(3))
        table.add_row(s.name, str(s.urls), str(s.controllers), str(s.services), str(s.daos), str(s.statements), top)
    console.print(table)

    table = Table(title="여러 모듈이 함께 쓰는 테이블 (바꾸면 영향이 큼)", title_justify="left")
    table.add_column("테이블", style="bold")
    table.add_column("모듈 수", justify="right")
    table.add_column("SQL 수", justify="right")
    table.add_column("조회/등록/수정/삭제")
    table.add_column("쓰는 모듈", style="dim")
    for t in ov.tables:
        k = t.kinds
        crud = f"{k.get('select', 0)}/{k.get('insert', 0)}/{k.get('update', 0)}/{k.get('delete', 0)}"
        mods = ", ".join(t.modules[:6]) + (f" 외 {len(t.modules) - 6}" if len(t.modules) > 6 else "")
        table.add_row(t.name, str(len(t.modules)), str(t.statements), crud, mods)
    console.print(table)
    example = next((s.name for s in ov.modules if s.urls), None)
    if example:
        console.print(f"[dim]모듈 하나를 자세히: sasu map <모듈>  (예: sasu map {example})[/dim]")


@app.command()
def common(top: int = typer.Option(10, "--top", help="항목별로 보여 줄 개수")) -> None:
    """여러 모듈이 함께 쓰는 공통 코드, 복사해 붙인 코드, 중복 SQL을 보여 준다."""
    from sasu.analysis.common import analyze_common, clone_stats

    conn = _open_db()
    report = analyze_common(conn, top=top)

    table = Table(title="여러 모듈이 함께 쓰는 공통 코드", title_justify="left")
    table.add_column("클래스", style="bold")
    table.add_column("모듈")
    table.add_column("종류")
    table.add_column("쓰는 모듈 수", justify="right")
    table.add_column("호출 수", justify="right")
    for s in report.shared:
        table.add_row(s.class_name, s.module, s.stereotype or "유틸", str(len(s.used_by_modules)), f"{s.call_sites:,}")
    console.print(table)

    groups, methods = clone_stats(conn)
    console.print(f"\n[bold]복사해 붙인 메서드[/bold]: {groups}묶음, {methods}개 메서드 (변수 이름만 다르고 구조가 같은 코드)")
    for i, g in enumerate(report.clones, 1):
        head = f"{i}. {len(g.methods)}곳, 토큰 {g.tokens}개"
        if g.note:
            head += f"  [yellow]{g.note}[/yellow]"
        console.print(head)
        for name, loc in g.methods[:4]:
            console.print(f"   {name}  [dim]{_short(loc)}[/dim]")
        if len(g.methods) > 4:
            console.print(f"   [dim]… 외 {len(g.methods) - 4}곳[/dim]")

    console.print(f"\n[bold]같은 SQL이 다른 ID로[/bold]: {len(report.duplicate_sql)}묶음")
    for d in report.duplicate_sql:
        console.print(f"  [magenta]{', '.join(s[0] for s in d.statements)}[/magenta]\n  [dim]{d.sql_preview}[/dim]")


def _short(location: str | None) -> str:
    """표준 Maven 경로 접두어는 줄여서 보여 준다."""
    if not location:
        return ""
    for prefix in ("src/main/java/", "src/main/resources/", "src/main/webapp/"):
        location = location.replace(prefix, "")
    return location


def _render(parent: Tree, node: TraceNode) -> None:
    if node.kind == "method":
        style = _STYLE.get(node.detail or "", "white")
        text = f"[{style}]{node.label}[/{style}]  [dim]{_short(node.location)}[/dim]"
        if node.repeated:
            text += "  [dim](위에서 펼침)[/dim]"
    elif node.kind == "statement":
        text = (
            f"[magenta]{node.label}[/magenta]  [dim]{_short(node.location)}[/dim]\n"
            f"[bold]테이블[/bold] {node.detail or '-'}"
        )
    elif node.kind == "view":
        text = f"[blue]화면[/blue] {node.label}" + (f"  [red]({node.detail})[/red]" if node.detail else "")
    elif node.kind == "external":
        text = f"[dim]{node.label}[/dim]  [yellow]{node.note}[/yellow]"
    else:
        text = f"[blue]→ {node.label}[/blue]  [dim]({node.detail})[/dim]"
    if node.kind == "statement" and node.note:
        text = text.replace("\n", f"  [yellow]{node.note}[/yellow]\n", 1)
    if node.count > 1:
        first, sep, rest = text.partition("\n")
        text = f"{first}  [dim]×{node.count}[/dim]{sep}{rest}"
    branch = parent.add(text)
    for child in node.children:
        _render(branch, child)


def _undo_msys_path(arg: str) -> str:
    """Git Bash는 /로 시작하는 인자를 C:/Program Files/Git/... 로 바꿔 넘긴다. 원래 URL로 되돌린다."""
    match = re.match(r"^[A-Za-z]:/.*?/Git(/.*)$", arg)
    return match.group(1) if match else arg


def _open_db():
    path = active_db_path()
    if path is None:
        console.print("[red]분석 결과가 없어요. 먼저 `sasu index <프로젝트 폴더>`를 실행하세요.[/red]")
        raise typer.Exit(1)
    return db.connect(path)


def _meta(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else "-"
