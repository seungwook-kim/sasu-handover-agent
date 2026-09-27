"""설정 파일 만들기와 환경 점검."""

from typer.testing import CliRunner

from sasu.cli import app
from sasu.config import home, load_settings
from sasu.setup_env import run_checks, write_env

runner = CliRunner()


def test_write_env_keeps_other_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# 메모\nSASU_LLM_MODEL=old\nOTHER=1\n", encoding="utf-8")
    write_env({"SASU_LLM_MODEL": "new", "SASU_EMBED_PROVIDER": "none"}, path)
    assert path.read_text(encoding="utf-8").splitlines() == [
        "# 메모", "SASU_LLM_MODEL=new", "OTHER=1", "SASU_EMBED_PROVIDER=none",
    ]


def test_setup_options_write_only_given_values(monkeypatch):
    for key in ("SASU_LLM_PROVIDER", "SASU_LLM_BASE_URL", "SASU_LLM_MODEL", "SASU_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    result = runner.invoke(app, ["setup", "--llm", "openai", "--base-url", "http://llm.local/v1", "--model", "m1"])
    assert result.exit_code == 0
    text = (home() / ".env").read_text(encoding="utf-8")
    assert "SASU_LLM_BASE_URL=http://llm.local/v1" in text and "SASU_EMBED_PROVIDER" not in text
    runner.invoke(app, ["setup", "--model", "m2"])
    text = (home() / ".env").read_text(encoding="utf-8")
    assert "SASU_LLM_MODEL=m2" in text and "SASU_LLM_PROVIDER=openai" in text


def test_home_env_is_read(monkeypatch):
    monkeypatch.delenv("SASU_SQL_DIALECT", raising=False)
    write_env({"SASU_SQL_DIALECT": "mysql"})
    assert load_settings(env_file=home() / "없는파일").sql_dialect == "mysql"


def test_doctor_without_project(monkeypatch):
    monkeypatch.delenv("SASU_DB_PATH", raising=False)
    monkeypatch.setenv("SASU_EMBED_PROVIDER", "none")
    checks = {c.name: c for c in run_checks()}
    assert checks["저장 폴더"].ok is True
    assert checks["분석한 프로젝트"].ok is False and "sasu index" in checks["분석한 프로젝트"].fix
    assert checks["벡터 검색"].ok is None
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "sasu index" in result.output
