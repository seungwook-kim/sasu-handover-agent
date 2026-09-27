"""과거 기록 평가: 버그 수정 커밋이 고친 메서드를 수정 전 코드 기준으로 찾는지 확인한다."""

import subprocess
from pathlib import Path

import pytest

from sasu.evaluation.history import FIX_PATTERN, _changed_old_lines, fixed_methods_after

V1 = """package demo;
public class OrderService {
    public int total(int a, int b) {
        return a + b;
    }

    public String status(String code) {
        if ("R3".equals(code)) {
            return "hold";
        }
        return "ok";
    }
}
"""

# status()만 고친 버그 수정 커밋
V2_FIX = V1.replace('return "hold";', 'return "hold-review";')
# total()만 바꾼 기능 추가 커밋 (버그 수정이 아님)
V3_FEAT = V2_FIX.replace("return a + b;", "return a + b + 0;")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    source = root / "src" / "demo" / "OrderService.java"
    source.parent.mkdir(parents=True)
    for content, message in [
        (V1, "init"),
        (V2_FIX, "fix: 보류 상태 문구 오류 수정"),
        (V3_FEAT, "feat: 합계 계산 방식 변경"),
    ]:
        source.write_text(content, encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", message)
        if message == "init":
            _git(root, "tag", "snapshot")
    return root


def test_fixed_methods_come_only_from_fix_commits(repo):
    fixed, fix_count = fixed_methods_after(repo, "snapshot")
    assert fix_count == 1
    assert list(fixed) == ["OrderService.status"]
    assert "보류 상태 문구 오류 수정" in fixed["OrderService.status"].commits[0]


def test_fix_pattern():
    assert FIX_PATTERN.search("fix: 첨부파일 미사용 처리")
    assert FIX_PATTERN.search("fix(bbs): 조회수")
    assert FIX_PATTERN.search("로그인 오류 수정")
    assert not FIX_PATTERN.search("feat: 휴가 관리 추가")
    assert not FIX_PATTERN.search("prefix 정리")
    assert not FIX_PATTERN.search("chore: 디버그용 System.out.println을 LOGGER로 대체")


def test_changed_old_lines():
    diff = "@@ -8 +8 @@\n-a\n+b\n@@ -20,0 +21,2 @@\n+c\n+d\n@@ -30,3 +32 @@\n"
    assert _changed_old_lines(diff) == {8, 20, 30, 31, 32}
