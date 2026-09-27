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
