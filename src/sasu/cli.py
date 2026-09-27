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
from sasu.config import load_settings
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

SAMPLE_REPO_URL = "https://github.com/eGovFramework/egovframe-common-components.git"
SAMPLE_REPO_DIR = Path("data/repos/egovframe-common-components")


@app.command()
def version() -> None:
    """버전을 출력한다."""
    console.print(f"sasu {__version__}")


@app.command("fetch-sample")
def fetch_sample() -> None:
    """분석 예제로 쓸 전자정부 표준프레임워크 공통컴포넌트를 내려받는다 (전체 커밋 기록 포함)."""
    if SAMPLE_REPO_DIR.exists():
        console.print(f"이미 있어요: {SAMPLE_REPO_DIR}")
        return
    SAMPLE_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    console.print("내려받는 중… (약 250MB)")
    subprocess.run(["git", "clone", SAMPLE_REPO_URL, str(SAMPLE_REPO_DIR)], check=True)
    console.print(f"[green]완료[/green]: {SAMPLE_REPO_DIR}")


@app.command()
def index(repo: Path = typer.Argument(..., exists=True, file_okay=False, help="분석할 프로젝트 폴더")) -> None:
    """프로젝트를 분석해 코드 그래프를 만든다."""
    settings = load_settings()
    started = time.perf_counter()
    with console.status("분석 중…"):
        conn = db.connect(settings.db_path)
        stats = index_repository(conn, repo, settings.sql_dialect)
    elapsed = time.perf_counter() - started

    table = Table(title=f"분석 완료 ({elapsed:.1f}초)", show_header=False)
    table.add_row("Java 파일", f"{stats.java_files:,}")
    table.add_row("클래스 / 메서드", f"{stats.classes:,} / {stats.methods:,}")
    table.add_row(f"Mapper XML ({settings.sql_dialect})", f"{stats.mapper_files:,}")
    table.add_row("SQL 쿼리", f"{stats.statements:,}")
    table.add_row("JSP", f"{stats.jsp_files:,}")
    table.add_row("URL", f"{stats.urls:,}")
    table.add_row("연결", f"{stats.edges:,}")
    console.print(table)
    console.print(f"저장 위치: {settings.db_path}")


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
    for k in kinds:
        items = getattr(report, k)
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


@app.command()
def questions(
    top: int = typer.Option(20, "--top", help="보여 줄 질문 수"),
    export: Path = typer.Option(None, "--export", help="엑셀로 내보낼 경로 (예: 인수인계질문.xlsx)"),
) -> None:
    """떠나는 개발자에게 물어야 할 인수인계 질문을 만든다. 이미 받은 답변은 그대로 둔다."""
    from sasu.analysis.questions import KIND_LABEL, generate_questions, save_questions
    from sasu.handover_sheet import export_sheet

    conn = _open_db()
    repo_path = _meta(conn, "repo_path")
    generated = generate_questions(conn)
    new, updated, stale = save_questions(conn, generated, Path(repo_path) if repo_path != "-" else None)
    console.print(f"질문 {len(generated)}개 (새 질문 {new}, 갱신 {updated}, 코드가 바뀌어 사라진 질문 {stale})")

    rows = conn.execute(
        "SELECT * FROM questions WHERE stale = 0 ORDER BY priority DESC, id LIMIT ?", (top,)
    ).fetchall()
    for i, q in enumerate(rows, 1):
        mark = "[green]답변함[/green]" if q["status"] == "answered" else ""
        console.print(
            f"\n[bold]{i}. {q['question']}[/bold] {mark}\n"
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
        if b.get("servlet_api"):
            lines.append(f"Servlet API: {b['servlet_api']}")
        for category, items in b.get("stack", {}).items():
            lines.append(f"{category}: {', '.join(items)}")
        console.print("\n".join(lines))
        console.print()
    console.print("  ".join(f"{k} [bold]{v:,}[/bold]" for k, v in ov.counts.items()))
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
    console.print("[dim]모듈 하나를 자세히: sasu map <모듈>  (예: sasu map cop.bbs)[/dim]")


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
    settings = load_settings()
    if not settings.db_path.exists():
        console.print("[red]분석 결과가 없어요. 먼저 `sasu index <프로젝트 폴더>`를 실행하세요.[/red]")
        raise typer.Exit(1)
    return db.connect(settings.db_path)


def _meta(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else "-"
