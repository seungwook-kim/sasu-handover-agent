"""모든 테스트가 사용자 폴더(~/.sasu)나 개발용 data/ 대신 임시 폴더를 쓰게 한다."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SASU_HOME", str(tmp_path / "sasu-home"))
