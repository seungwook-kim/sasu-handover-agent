"""사수 명령줄 도구."""

import subprocess
from pathlib import Path

import typer
from rich.console import Console

from sasu import __version__

app = typer.Typer(help="사수: 떠난 개발자 대신 답하는 SI 인수인계 에이전트", no_args_is_help=True)
console = Console()

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
