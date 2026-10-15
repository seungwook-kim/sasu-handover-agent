"""설치 뒤 설정 만들기(sasu setup)와 환경 점검(sasu doctor).

처음 쓰는 사람이 .env 형식을 몰라도 되게, 질문에 답하면 SASU_HOME/.env를 만들어 준다.
점검은 "무엇이 안 되는지"와 "어떻게 고치는지"를 한 줄씩 보여 준다.
"""

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sasu import __version__
from sasu.config import active_db_path, current_project, home, list_projects, load_settings

ENV_KEYS = (
    "SASU_LLM_PROVIDER", "SASU_LLM_BASE_URL", "SASU_LLM_API_KEY", "SASU_LLM_MODEL",
    "SASU_EMBED_PROVIDER", "SASU_EMBED_MODEL", "SASU_EMBED_BASE_URL", "SASU_EMBED_API_KEY",
    "SASU_SQL_DIALECT",
)


def write_env(values: dict[str, str], path: Path | None = None) -> Path:
    """SASU_HOME/.env를 쓴다. 이미 있으면 받은 값만 바꾸고 나머지 줄은 그대로 둔다."""
    path = path or home() / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else [
        "# 사수 설정 (sasu setup이 만들었다. 직접 고쳐도 된다)",
    ]
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={values[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass
class Check:
    name: str
    ok: bool | None  # True 정상, False 문제, None 선택 사항(꺼 둠)
    detail: str
    fix: str | None = None


def run_checks(ping_llm: bool = False) -> list[Check]:
    settings = load_settings()
    checks: list[Check] = []

    checks.append(Check("사수", True, f"{__version__} · Python {sys.version.split()[0]}"))

    base = home()
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("저장 폴더", True, str(base)))
    except OSError as e:
        checks.append(Check("저장 폴더", False, f"{base}에 쓸 수 없음 ({e})", "SASU_HOME을 쓸 수 있는 폴더로 바꾸세요"))

    git = shutil.which("git")
    checks.append(Check("git", bool(git), git or "없음",
                        None if git else "git을 설치하면 커밋 기록을 \"왜?\"의 근거로 씁니다 (없어도 분석은 됩니다)"))

    path = active_db_path()
    if path is None:
        checks.append(Check("분석한 프로젝트", False, "없음", "sasu index <프로젝트 폴더>"))
    else:
        conn = sqlite3.connect(path)
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('repo_path', 'indexed_at')").fetchall())
        conn.close()
        repo = meta.get("repo_path", "")
        others = len(list_projects()) - 1
        detail = f"{current_project() or path.stem} (분석 {meta.get('indexed_at', '-')})" + (f", 그 밖에 {others}개" if others > 0 else "")
        checks.append(Check("분석한 프로젝트", True, detail))
        if repo and not Path(repo).exists():
            checks.append(Check("원본 폴더", False, f"{repo}가 없음",
                                "코드 보기와 커밋 기록을 쓰려면 원본 폴더가 있어야 합니다. 옮겼다면 sasu index로 다시 분석하세요"))

    checks.append(_llm_check(settings, ping_llm))
    checks.append(_embed_check(path))
    return checks


def _llm_check(settings, ping: bool) -> Check:
    from sasu.rag.answer import LLMError, make_llm_client

    provider = settings.llm_provider
    client = make_llm_client(settings)
    if provider == "claude-code":
        detail = f"claude CLI · {settings.llm_model or 'sonnet'}"
        fix = "claude CLI를 설치하고 로그인하세요 (개발용). 현장에서는 SASU_LLM_PROVIDER=openai로 사내 LLM 주소를 넣습니다"
    elif provider == "anthropic":
        detail = f"Claude API · {client.model}"
        fix = ".env에 SASU_ANTHROPIC_API_KEY를 넣고 uv sync --extra anthropic으로 SDK를 설치하세요"
    else:
        detail = f"{settings.llm_base_url} · {settings.llm_model or '(모델 없음)'}"
        fix = "sasu setup으로 LLM 주소·키·모델을 넣으세요. 없어도 근거는 보여 주고, 답 문장만 만들지 않습니다"
    if not client.available:
        return Check("LLM", False, detail + " · 설정 안 됨", fix)
    if ping:
        try:
            reply = client.chat([{"role": "user", "content": "'확인'이라고만 답하세요."}])
            return Check("LLM", True, f"{detail} · 응답: {reply.strip()[:20]}")
        except LLMError as e:
            return Check("LLM", False, f"{detail} · 연결 실패: {e}", "주소·키·모델 이름과 사내망 방화벽을 확인하세요")
    return Check("LLM", True, detail + " (sasu doctor --ping으로 실제 연결 확인)")


def _embed_check(db_path: Path | None) -> Check:
    provider = os.environ.get("SASU_EMBED_PROVIDER", "none").lower()
    if provider in ("", "none"):
        return Check("벡터 검색", None, "꺼 둠 (BM25 + 코드 그래프만)",
                     "켜려면: uv sync --extra local-embed 뒤 SASU_EMBED_PROVIDER=local")
    if provider == "local" and importlib.util.find_spec("fastembed") is None:
        return Check("벡터 검색", False, "local인데 fastembed가 없음", "uv sync --extra local-embed (또는 pip install 'sasu[local-embed]')")
    detail = provider
    if db_path is not None:
        from sasu import db
        from sasu.rag.vectors import has_vector_index

        conn = db.connect(db_path)
        built = has_vector_index(conn)
        conn.close()
        if not built:
            return Check("벡터 검색", False, f"{detail} · 지금 프로젝트에 벡터 색인 없음", "sasu embed")
        detail += f" · 색인 있음 ({built.rsplit('/', 1)[-1]})"
    return Check("벡터 검색", True, detail)


def claude_cli_logged_in() -> bool:
    """claude CLI가 있고 버전이 나오면 True (로그인 여부까지는 실제 호출해야 안다)."""
    exe = shutil.which("claude")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "--version"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
