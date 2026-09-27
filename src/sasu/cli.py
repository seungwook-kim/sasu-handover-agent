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
def trace(url: str = typer.Argument(..., help="URL 전체 또는 일부 (예: /cop/bbs/selectArticleList.do)")) -> None:
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
    root = trace_url(conn, urls[0])
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
    else:
        text = f"[blue]→ {node.label}[/blue]  [dim]({node.detail})[/dim]"
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
