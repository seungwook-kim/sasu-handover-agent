"""git 기록에서 "왜 이렇게 바꿨나"를 찾는다.

Git·Jira를 쓰는 SI 현장에서 커밋 메시지는 떠난 개발자가 남긴 설명이다.
다만 포맷 정리·릴리스 일괄 반영 커밋은 줄 전체를 덮어써서 진짜 이유를 가린다.
그런 커밋은 git blame --ignore-revs-file로 건너뛰고, 그 전에 줄을 바꾼 커밋을 찾는다.
"""

import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 이유를 설명하지 않는 커밋: 포맷 정리, 릴리스·버전 일괄 반영, 첫 커밋
NOISE_COMMIT = re.compile(
    r"format|포맷|정렬|들여쓰기|indent|whitespace|공백|cleanup|코드\s*정리|이클립스|eclipse"
    r"|initial commit|^\s*v?\d+(\.\d+)+\s*(beta|alpha|rc\d*|final)?\s*$"
    r"|\d+\.\d+\.\d+\s*(final|release|beta)?\s*$|릴리스|release\s*\d",
    re.IGNORECASE,
)


@dataclass
class CommitNote:
    sha: str
    date: str
    author: str
    summary: str
    lines: int  # 이 커밋이 마지막으로 바꾼 줄 수 (범위 안에서)


def is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def ignore_revs_file(repo: Path, cache_dir: Path) -> Path | None:
    """이유를 설명하지 않는 커밋 목록 파일. HEAD가 바뀌면 새로 만든다."""
    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
    except subprocess.CalledProcessError:
        return None
    path = cache_dir / f"ignore-revs-{head[:12]}.txt"
    if path.exists():
        return path
    log = _git(repo, "log", "--format=%H%x09%s")
    noise = [line.split("\t", 1)[0] for line in log.splitlines() if "\t" in line and NOISE_COMMIT.search(line.split("\t", 1)[1])]
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(noise) + "\n", encoding="utf-8")
    return path


def blame_notes(repo: Path, file_path: str, start: int, end: int, cache_dir: Path, limit: int = 5) -> list[CommitNote]:
    """start~end 줄을 마지막으로 바꾼 커밋들 (의미 없는 커밋은 건너뜀). 많이 바꾼 순."""
    if not is_git_repo(repo):
        return []
    args = ["blame", "-w", "--line-porcelain", "-L", f"{start},{end}"]
    ignore = ignore_revs_file(repo, cache_dir)
    if ignore is not None:
        args += ["--ignore-revs-file", str(ignore.resolve())]
    try:
        out = _git(repo, *args, "--", file_path)
    except subprocess.CalledProcessError:
        return []
    counts: Counter = Counter()
    info: dict[str, dict] = {}
    sha = None
    for line in out.splitlines():
        if re.match(r"^[0-9a-f]{40} ", line):
            sha = line.split(" ", 1)[0]
            counts[sha] += 1
            info.setdefault(sha, {})
        elif sha and line.startswith("author "):
            info[sha]["author"] = line[7:]
        elif sha and line.startswith("author-time "):
            info[sha]["date"] = datetime.fromtimestamp(int(line[12:]), timezone.utc).strftime("%Y-%m-%d")
        elif sha and line.startswith("summary "):
            info[sha]["summary"] = line[8:]
    notes = [
        CommitNote(sha=s[:8], date=info[s].get("date", ""), author=info[s].get("author", ""),
                   summary=info[s].get("summary", ""), lines=n)
        for s, n in counts.most_common()
        if not NOISE_COMMIT.search(info[s].get("summary", ""))
    ]
    return notes[:limit]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")
