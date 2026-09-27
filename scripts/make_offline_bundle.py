"""인터넷이 막힌 PC에 설치할 꾸러미를 만든다.

인터넷이 되는 PC(설치할 PC와 같은 OS·Python 버전)에서 실행한다:

    uv run python scripts/make_offline_bundle.py                 # 기본 (BM25 + 코드 그래프)
    uv run python scripts/make_offline_bundle.py --local-embed   # 로컬 벡터 검색 + 모델 파일(약 240MB) 포함

만들어지는 dist/sasu-offline-<os>/ 폴더(와 zip)를 옮긴 뒤, 그 안의 install 스크립트를 실행한다.
꾸러미에는 Python 자체도 들어 있어서 설치 PC에 Python이 없어도 된다.
"""

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

INSTALL_PS1 = r"""# 사수 오프라인 설치 (Windows PowerShell). 이 폴더에서 실행: powershell -ExecutionPolicy Bypass -File install.ps1
# 설치 PC에 Python이 없어도 된다 (꾸러미 안의 Python을 복사해 쓴다). 인터넷에 접속하지 않는다.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = if ($env:SASU_INSTALL_DIR) { $env:SASU_INSTALL_DIR } else { Join-Path $env:USERPROFILE ".sasu/app" }
New-Item -ItemType Directory -Force $target | Out-Null
Copy-Item -Recurse -Force "$here\python" "$target\"
$py = Join-Path $target "python\python.exe"
& $py -m ensurepip --default-pip | Out-Null
& $py -m pip install --no-index --no-warn-script-location --find-links "$here\wheels" "sasu{extra}"
if ($LASTEXITCODE -ne 0) { throw "설치 실패" }
if (Test-Path "$here\models") {
  $sasuHome = if ($env:SASU_HOME) { $env:SASU_HOME } else { Join-Path $env:USERPROFILE ".sasu" }
  New-Item -ItemType Directory -Force "$sasuHome\models" | Out-Null
  Copy-Item -Recurse -Force "$here\models\*" "$sasuHome\models\"
}
$bin = Join-Path $target "python\Scripts"
Write-Host ""
Write-Host "설치 완료: $bin\sasu.exe"
Write-Host "PATH에 넣으려면: [Environment]::SetEnvironmentVariable('Path', `$env:Path + ';$bin', 'User')"
Write-Host "다음: sasu setup -> sasu doctor"
"""

INSTALL_SH = """#!/usr/bin/env sh
# 사수 오프라인 설치 (Linux·macOS). 이 폴더에서 실행: sh install.sh
# 설치 PC에 Python이 없어도 된다 (꾸러미 안의 Python을 복사해 쓴다). 인터넷에 접속하지 않는다.
set -e
here=$(cd "$(dirname "$0")" && pwd)
target=${SASU_INSTALL_DIR:-$HOME/.sasu/app}
mkdir -p "$target"
cp -R "$here/python" "$target/"
py="$target/python/bin/python3"
"$py" -m ensurepip --default-pip >/dev/null
"$py" -m pip install --no-index --find-links "$here/wheels" "sasu{extra}"
if [ -d "$here/models" ]; then
  mkdir -p "${SASU_HOME:-$HOME/.sasu}/models"
  cp -R "$here/models/." "${SASU_HOME:-$HOME/.sasu}/models/"
fi
echo ""
echo "설치 완료: $target/python/bin/sasu"
echo "PATH에 $target/python/bin 을 넣으세요. 다음: sasu setup -> sasu doctor"
"""

# 꾸러미에 넣지 않는 것: 시험 코드·헤더·GUI, 그리고 uv의 "수정 금지" 표시(사수 전용 사본이라 pip 설치가 필요하다)
_PY_SKIP = {"EXTERNALLY-MANAGED", "__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo", "tcl", "include", "libs", "site-packages"}


def copy_python(dest: Path) -> None:
    """지금 쓰는 Python(uv가 설치한 독립 실행형)을 꾸러미에 복사한다. 설치 PC에 Python이 없어도 되게."""
    base = Path(sys.base_prefix).resolve()
    print(f"Python 복사: {base}")
    shutil.copytree(base, dest, ignore=lambda _d, names: [n for n in names if n in _PY_SKIP])
    (dest / ("Lib" if sys.platform == "win32" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}")
     / "site-packages").mkdir(parents=True, exist_ok=True)


def run(*cmd: str) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local-embed", action="store_true", help="로컬 벡터 검색 라이브러리와 모델 파일을 넣는다")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="꾸러미를 만들 폴더")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")
    name = f"sasu-offline-{system}-py{sys.version_info.major}{sys.version_info.minor}"
    bundle = args.out / name
    if bundle.exists():
        shutil.rmtree(bundle)
    wheels = bundle / "wheels"
    wheels.mkdir(parents=True)

    # 1. 사수 자신과 의존성 휠 (지금 PC의 OS·Python용)
    run("uv", "build", "--wheel", "-o", str(wheels))
    requirements = bundle / "requirements.txt"
    export = ["uv", "export", "--no-hashes", "--no-dev", "--no-emit-project", "--frozen", "-o", str(requirements)]
    export += ["--extra", "mcp"]  # 에이전트 연결(MCP)은 가벼워서 늘 넣는다
    if args.local_embed:
        export += ["--extra", "local-embed"]
    run(*export)
    run("uv", "run", "--no-project", "--with", "pip", "python", "-m", "pip", "download",
        "--only-binary=:all:", "-r", str(requirements), "-d", str(wheels), "pip", "setuptools")

    # 2. Python 자체 (설치 PC에 Python이 없어도 되게)
    copy_python(bundle / "python")

    # 3. 로컬 임베딩 모델 파일 (설치 PC는 모델을 내려받을 수 없다)
    if args.local_embed:
        models = bundle / "models"
        print(f"모델 준비: {MODEL}")
        from fastembed import TextEmbedding

        TextEmbedding(model_name=MODEL, cache_dir=str(models))
        (bundle / ".env.example").write_text("SASU_EMBED_PROVIDER=local\n", encoding="utf-8")

    extra = "[local-embed,mcp]" if args.local_embed else "[mcp]"
    (bundle / "install.ps1").write_text(INSTALL_PS1.replace("{extra}", extra),
                                        encoding="utf-8-sig")
    (bundle / "install.sh").write_text(INSTALL_SH.replace("{extra}", extra),
                                       encoding="utf-8", newline="\n")
    shutil.copy(ROOT / "docs" / "install.md", bundle / "설치 안내.md")

    size = sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()) / 1024 / 1024
    print(f"\n꾸러미: {bundle} ({size:.0f}MB)")
    if not args.no_zip:
        archive = shutil.make_archive(str(bundle), "zip", bundle.parent, bundle.name)
        print(f"압축: {archive}")


if __name__ == "__main__":
    main()
