"""sasu docs …: 인수인계 문서 넣기·목록·보기·지우기·검색·불일치."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

docs_app = typer.Typer(help="인수인계 문서를 넣어 묻기 근거([문서])로 쓴다 (md·txt·docx·xlsx·pptx·pdf·hwpx·hwp)",
                       no_args_is_help=True)
console = Console()


def _judge(llm_judge: bool):
    """--llm-judge면 LLM 판정기 (못 쓰면 알리고 끝냄), 아니면 기본값(SASU_DOC_JUDGE=on일 때만 판정기)."""
    from sasu.docs.conflicts import _DEFAULT

    if not llm_judge:
        return _DEFAULT
    from sasu.docs.judge import ConflictJudge

    judge = ConflictJudge.from_settings()
    if not judge.available:
        console.print("[red]LLM 판정을 쓸 수 없어요: SASU_ANTHROPIC_API_KEY와 `uv sync --extra anthropic`을 확인하세요 "
                      "(SASU_JUDGE_PROVIDER·SASU_JUDGE_MODEL).[/red]")
        raise typer.Exit(1)
    return judge


def _open_db():
    from sasu import db
    from sasu.config import active_db_path

    path = active_db_path()
    if path is None:
        console.print("[red]분석 결과가 없어요. 먼저 `sasu index <프로젝트 폴더>`를 실행하세요.[/red]")
        raise typer.Exit(1)
    return db.connect(path)


@docs_app.command("add")
def add(
    paths: list[Path] = typer.Argument(..., exists=True, help="문서 파일 또는 폴더 (여러 개 가능)"),
    by: str = typer.Option(None, "--by", help="넣은 사람"),
    llm_judge: bool = typer.Option(False, "--llm-judge",
                                   help="문서–코드 불일치를 LLM으로도 판정 (코드와 이어진 조각마다 한 번, 결과는 캐시)"),
) -> None:
    """문서를 넣는다. 비밀은 가리고, 원본은 복사하지 않고 조각만 저장한다. 같은 이름은 새 판으로 바꾼다."""
    from sasu.docs.conflicts import sync_doc_questions
    from sasu.docs.linking import CodeNames
    from sasu.docs.search import DocIndex, embed_chunks
    from sasu.docs.store import add_path

    conn = _open_db()
    judge = _judge(llm_judge)
    names = CodeNames(conn)
    results = []
    with console.status("문서를 읽는 중…"):
        for p in paths:
            results.extend(add_path(conn, p, by, names))
    if not results:
        console.print("[yellow]넣을 문서가 없어요 (지원: md·txt·docx·xlsx·pptx·pdf·hwpx·hwp).[/yellow]")
        raise typer.Exit(1)
    table = Table(title="문서 넣기", title_justify="left")
    for col in ("문서", "결과", "조각", "가림", "코드 연결", "문서 날짜"):
        table.add_column(col)
    status_text = {"added": "[green]넣음[/green]", "replaced": "[green]새 판으로 바꿈[/green]",
                   "unchanged": "[dim]같은 파일 (건너뜀)[/dim]", "failed": "[red]못 읽음[/red]"}
    for r in results:
        if r.status == "failed":
            table.add_row(r.name, status_text[r.status], "-", "-", "-", f"[red]{r.reason}[/red]")
            continue
        hidden = ", ".join(f"{k} {v}" for k, v in r.redactions.items()) or "없음"
        links = f"{r.links}" + (f" ({', '.join(f'{k} {v}' for k, v in r.links_by_kind.items())})" if r.links else "")
        table.add_row(r.name, status_text[r.status], str(r.chunks), hidden, links,
                      f"{r.doc_date} ({r.date_source})" if r.doc_date else "-")
    console.print(table)
    missing = sorted({m for r in results for m in r.missing})
    if missing:
        console.print(f"[yellow]문서에는 있는데 코드에 없는 이름 {len(missing)}개[/yellow]: {', '.join(missing[:12])}"
                      + (" …" if len(missing) > 12 else ""))
    if any(r.status in ("added", "replaced") for r in results):
        from sasu.rag.pipeline import embedder_or_none

        embedder = embedder_or_none()
        if embedder is not None:
            with console.status(f"{embedder.name}로 문서 조각을 임베딩하는 중…"):
                index = DocIndex(conn)
                embed_chunks(index.texts, embedder)
        with console.status("문서와 코드가 다른 곳을 찾는 중…"):
            new, updated, gone = sync_doc_questions(conn, judge)
        total = conn.execute("SELECT COUNT(*) FROM questions WHERE kind = 'doc_check' AND stale = 0").fetchone()[0]
        if total:
            console.print(f"문서와 코드가 다른 곳: [bold]{total}[/bold]건 → 인수인계 질문 '문서 확인'에 넣었어요 "
                          f"(새 {new}). `sasu docs conflicts`로 보기")
    if any(r.status == "failed" for r in results):
        raise typer.Exit(1)


@docs_app.command("list")
def list_() -> None:
    """넣은 문서 목록."""
    from sasu.docs.store import list_documents

    docs = list_documents(_open_db())
    if not docs:
        console.print("넣은 문서가 없어요. `sasu docs add <파일|폴더>`로 넣으세요.")
        return
    table = Table(title=f"인수인계 문서 {len(docs)}개", title_justify="left")
    for col in ("id", "문서", "형식", "문서 날짜", "조각", "코드 연결", "가림", "넣은 때"):
        table.add_column(col)
    for d in docs:
        hidden = ", ".join(f"{k} {v}" for k, v in d["redactions"].items()) or "-"
        table.add_row(str(d["id"]), d["name"], d["format"], f"{d['doc_date']} ({d['date_source']})",
                      str(d["chunks"]), str(d["links"]), hidden, (d["added_at"] or "")[:16])
    console.print(table)


@docs_app.command("show")
def show(doc_id: int = typer.Argument(..., help="문서 id (sasu docs list)")) -> None:
    """문서의 조각과 이어진 코드를 보여 준다."""
    from sasu.docs.store import document_chunks

    chunks = document_chunks(_open_db(), doc_id)
    if not chunks:
        console.print(f"[red]문서 {doc_id}가 없어요.[/red]")
        raise typer.Exit(1)
    for c in chunks:
        links = ", ".join(f"{l['value']}" + ("" if l["found"] else " [red](코드에 없음)[/red]") for l in c["links"])
        console.print(f"\n[bold]#{c['id']} {c['title']}[/bold]" + (f"\n[cyan]코드: {links}[/cyan]" if links else ""))
        console.print(c["text"], style="dim", markup=False, highlight=False)


@docs_app.command("remove")
def remove(doc_id: int = typer.Argument(..., help="문서 id (sasu docs list)")) -> None:
    """문서를 지운다 (조각·연결과, 답하지 않은 '문서 확인' 질문)."""
    from sasu.docs.conflicts import sync_doc_questions
    from sasu.docs.store import delete_document

    conn = _open_db()
    if not delete_document(conn, doc_id):
        console.print(f"[red]문서 {doc_id}가 없어요.[/red]")
        raise typer.Exit(1)
    sync_doc_questions(conn)
    console.print(f"문서 {doc_id}를 지웠어요.")


@docs_app.command("search")
def search(question: str = typer.Argument(..., help="찾을 내용"),
           top: int = typer.Option(6, "--top", help="보여 줄 조각 수")) -> None:
    """질문과 맞는 문서 조각을 찾는다."""
    from sasu.rag.pipeline import search_docs

    conn = _open_db()
    hits = search_docs(conn, question, top)
    if not hits:
        console.print("맞는 조각이 없어요 (문서를 넣었는지 `sasu docs list`로 확인하세요).")
        return
    for i, h in enumerate(hits, 1):
        text = conn.execute("SELECT text FROM doc_chunks WHERE id = ?", (h.chunk_id,)).fetchone()[0]
        console.print(f"\n[bold]{i}. [문서] {h.title}[/bold] [dim](#{h.chunk_id}, 점수 {h.score}, 맞은 비율 {h.coverage:.0%})[/dim]")
        console.print(text[:300] + ("…" if len(text) > 300 else ""), style="dim", markup=False, highlight=False)


@docs_app.command("conflicts")
def conflicts(llm_judge: bool = typer.Option(False, "--llm-judge", help="LLM 판정도 함께 (설정 값·상태값 뜻·동작)")) -> None:
    """문서와 코드가 다른 값을 말하는 곳 (규칙: URL·테이블·숫자·코드 값 / LLM 판정: --llm-judge 또는 SASU_DOC_JUDGE=on)."""
    from sasu.docs.conflicts import all_conflicts

    judge = _judge(llm_judge)
    with console.status("문서와 코드가 다른 곳을 찾는 중…"):
        found = all_conflicts(_open_db(), judge)
    if not found:
        console.print("문서와 코드가 다른 곳을 찾지 못했어요.")
        return
    for i, c in enumerate(found, 1):
        console.print(f"\n[bold]{i}. [yellow]{c.message}[/yellow][/bold]\n   [문서] {c.doc_title}\n"
                      f"   [dim]문장: {c.sentence[:160]}[/dim]\n   코드: {c.code_says}")


@docs_app.command("relink")
def relink() -> None:
    """다시 분석한 뒤 문서 조각과 코드를 다시 잇는다 (sasu index가 자동으로 한다)."""
    from sasu.docs.conflicts import sync_doc_questions
    from sasu.docs.linking import relink_all

    conn = _open_db()
    n = relink_all(conn)
    sync_doc_questions(conn)
    console.print(f"코드 연결 {n}개")
