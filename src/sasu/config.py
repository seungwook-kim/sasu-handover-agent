"""환경 변수와 .env 파일에서 설정을 읽는다."""

import os
from dataclasses import dataclass
from pathlib import Path


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
    db_path: Path
    sql_dialect: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_provider: str = "openai"  # openai(OpenAI 호환 주소) | claude-code(claude CLI)


def load_settings(env_file: Path = Path(".env")) -> Settings:
    _load_dotenv(env_file)
    return Settings(
        db_path=Path(os.environ.get("SASU_DB_PATH", "data/sasu.db")),
        sql_dialect=os.environ.get("SASU_SQL_DIALECT", "oracle"),
        llm_base_url=os.environ.get("SASU_LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        llm_api_key=os.environ.get("SASU_LLM_API_KEY", ""),
        llm_model=os.environ.get("SASU_LLM_MODEL", ""),
        llm_provider=os.environ.get("SASU_LLM_PROVIDER", "openai").lower(),
    )
