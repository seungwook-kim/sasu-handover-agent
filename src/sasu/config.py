"""환경 변수와 .env 파일에서 설정을 읽는다."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path



def home() -> Path:
    """분석 파일·캐시·설정을 두는 곳. SASU_HOME이 없으면 사용자 폴더의 .sasu.
    어느 폴더에서 실행해도 같은 곳을 쓰게 하려는 것 (개발 중에는 .env에 SASU_HOME=data)."""
    _load_dotenv(Path(".env"))  # SASU_HOME을 지금 폴더의 .env에 적어 둘 수 있다
    return Path(os.environ.get("SASU_HOME") or Path.home() / ".sasu")


def projects_dir() -> Path:
    return home() / "projects"


def cache_dir() -> Path:
    return home() / "cache"


def repos_dir() -> Path:
    return home() / "repos"


def _current_file() -> Path:
    return home() / "current_project"


def _load_dotenv(path: Path) -> None:
    """외부 라이브러리 없이 KEY=VALUE 형식의 .env를 읽는다. 이미 설정된 값은 덮어쓰지 않는다."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    db_path: Path | None  # SASU_DB_PATH로 직접 정했을 때만. 보통은 프로젝트마다 따로 둔다
    sql_dialect: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_provider: str = "openai"  # openai(OpenAI 호환 주소) | claude-code(claude CLI) | anthropic(Claude API)
    # Claude API 키 (SASU_ANTHROPIC_API_KEY). 화면·로그에 찍히지 않게 repr에서 뺀다
    anthropic_api_key: str = field(default="", repr=False)


def load_settings(env_file: Path = Path(".env")) -> Settings:
    _load_dotenv(env_file)  # 지금 폴더의 .env가 먼저
    _load_dotenv(home() / ".env")  # 그다음 SASU_HOME의 .env (sasu setup이 만든다)
    explicit = os.environ.get("SASU_DB_PATH")
    return Settings(
        db_path=Path(explicit) if explicit else None,
        sql_dialect=os.environ.get("SASU_SQL_DIALECT", "oracle"),
        llm_base_url=os.environ.get("SASU_LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        llm_api_key=os.environ.get("SASU_LLM_API_KEY", ""),
        llm_model=os.environ.get("SASU_LLM_MODEL", ""),
        llm_provider=os.environ.get("SASU_LLM_PROVIDER", "openai").lower(),
        anthropic_api_key=os.environ.get("SASU_ANTHROPIC_API_KEY", "").strip(),
    )


def project_name(repo: Path) -> str:
    """분석 대상 폴더 이름을 파일 이름으로 쓸 수 있게 다듬는다."""
    return re.sub(r"[^\w.-]+", "-", repo.resolve().name).strip("-") or "project"


def project_db(name: str) -> Path:
    """프로젝트마다 분석 파일을 따로 둔다. 인수인계 질문과 답이 다른 프로젝트와 섞이지 않게 하려는 것."""
    return projects_dir() / f"{name}.db"


def current_project() -> str | None:
    current = _current_file()
    if current.is_file():
        name = current.read_text(encoding="utf-8").strip()
        return name or None
    return None


def set_current_project(name: str) -> None:
    current = _current_file()
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(name, encoding="utf-8")


def list_projects() -> list[str]:
    folder = projects_dir()
    return sorted(p.stem for p in folder.glob("*.db")) if folder.is_dir() else []


def active_db_path() -> Path | None:
    """지금 쓸 분석 파일: SASU_DB_PATH가 있으면 그것, 없으면 지금 프로젝트의 파일. 아직 분석 전이면 None."""
    explicit = load_settings().db_path
    if explicit is not None:
        return explicit if explicit.exists() else None
    name = current_project()
    path = project_db(name) if name else None
    return path if path is not None and path.exists() else None
