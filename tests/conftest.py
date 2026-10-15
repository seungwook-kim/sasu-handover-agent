"""모든 테스트가 사용자 폴더(~/.sasu)나 개발용 data/ 대신 임시 폴더를 쓰게 한다."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SASU_HOME", str(tmp_path / "sasu-home"))


@pytest.fixture(autouse=True)
def _no_query_expand(monkeypatch):
    """검색 전 LLM 질문 넓히기를 끈다 (.env의 claude-code가 테스트 중에 불리지 않게).
    넓히기 테스트는 가짜 LLM을 직접 넘긴다."""
    monkeypatch.setenv("SASU_QUERY_EXPAND", "off")
