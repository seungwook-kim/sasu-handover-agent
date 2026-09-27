"""환경 변수와 .env 파일에서 설정을 읽는다."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECTS_DIR = Path("data/projects")
CURRENT_FILE = Path("data/current_project")


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
    llm_provider: str = "openai"  # openai(OpenAI 호환 주소) | claude-code(claude CLI)


def load_settings(env_file: Path = Path(".env")) -> Settings:
    _load_dotenv(env_file)
    explicit = os.environ.get("SASU_DB_PATH")
    return Settings(
        db_path=Path(explicit) if explicit else None,
        sql_dialect=os.environ.get("SASU_SQL_DIALECT", "oracle"),
        llm_base_url=os.environ.get("SASU_LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        llm_api_key=os.environ.get("SASU_LLM_API_KEY", ""),
        llm_model=os.environ.get("SASU_LLM_MODEL", ""),
        llm_provider=os.environ.get("SASU_LLM_PROVIDER", "openai").lower(),
    )


def project_name(repo: Path) -> str:
    """분석 대상 폴더 이름을 파일 이름으로 쓸 수 있게 다듬는다."""
    return re.sub(r"[^\w.-]+", "-", repo.resolve().name).strip("-") or "project"


def project_db(name: str) -> Path:
    """프로젝트마다 분석 파일을 따로 둔다. 인수인계 질문과 답이 다른 프로젝트와 섞이지 않게 하려는 것."""
    return PROJECTS_DIR / f"{name}.db"


def current_project() -> str | None:
    if CURRENT_FILE.is_file():
        name = CURRENT_FILE.read_text(encoding="utf-8").strip()
        return name or None
    return None


def set_current_project(name: str) -> None:
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(name, encoding="utf-8")


def list_projects() -> list[str]:
    return sorted(p.stem for p in PROJECTS_DIR.glob("*.db")) if PROJECTS_DIR.is_dir() else []


def active_db_path() -> Path | None:
    """지금 쓸 분석 파일: SASU_DB_PATH가 있으면 그것, 없으면 지금 프로젝트의 파일. 아직 분석 전이면 None."""
    explicit = load_settings().db_path
    if explicit is not None:
        return explicit if explicit.exists() else None
    name = current_project()
    path = project_db(name) if name else None
    return path if path is not None and path.exists() else None
