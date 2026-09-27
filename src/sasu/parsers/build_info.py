"""pom.xml·build.gradle에서 "어떤 기술로 돌아가는 시스템인가"를 뽑는다."""

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

# (분류, artifactId에 들어 있으면 해당하는 조각)
_CATEGORIES = [
    ("프레임워크", ("egovframe-rte-ptl-mvc", "spring-webmvc", "spring-boot-starter-web", "egovframe-web-config")),
    ("데이터 접근", ("mybatis", "ibatis", "hibernate-core", "hibernate-entitymanager", "spring-data-jpa",
                   "egovframe-rte-psl-dataaccess")),
    ("검증", ("hibernate-validator", "validation-api", "jakarta.validation")),
    ("DB 드라이버", ("ojdbc", "mysql-connector", "mariadb", "postgresql", "tibero", "cubrid", "altibase", "goldilocks", "mssql", "db2")),
    ("화면", ("jstl", "tiles", "thymeleaf", "sitemesh", "jsp-api")),
    ("보안", ("spring-security", "egovframe-rte-fdl-security", "fdl-crypto")),
    ("배치·스케줄", ("spring-batch", "egovframe-rte-bat", "quartz")),
    ("기타 라이브러리", ("lombok", "jackson-databind", "poi", "commons-fileupload", "log4j", "logback")),
]


@dataclass
class BuildInfo:
    tool: str  # maven, gradle
    project: str
    version: str | None
    packaging: str | None
    parent: str | None
    java_version: str | None
    stack: dict[str, list[str]] = field(default_factory=dict)  # 분류 → artifactId 목록
    servlet_api: str | None = None  # jakarta, javax
    python_version: str | None = None  # requires-python

    def to_dict(self) -> dict:
        return asdict(self)


# Python: (분류, 패키지 이름에 들어 있으면 해당하는 조각)
_PY_CATEGORIES = [
    ("프레임워크", ("django", "flask", "fastapi", "starlette", "sanic", "tornado", "aiohttp")),
    ("데이터 접근", ("sqlalchemy", "sqlmodel", "alembic", "peewee", "tortoise", "django-", "psycopg", "asyncpg")),
    ("DB 드라이버", ("psycopg", "asyncpg", "pymysql", "mysqlclient", "oracledb", "cx-oracle", "cx_oracle", "pymssql", "aiomysql")),
    ("화면", ("jinja2", "django-templates", "mako")),
    ("검증", ("pydantic", "marshmallow", "cerberus")),
    ("보안", ("passlib", "bcrypt", "pyjwt", "python-jose", "authlib", "cryptography")),
    ("배치·스케줄", ("celery", "apscheduler", "rq", "dramatiq")),
    ("기타 라이브러리", ("requests", "httpx", "pandas", "numpy", "redis", "boto3")),
]


def read_build_info(repo: Path) -> BuildInfo | None:
    pom = repo / "pom.xml"
    if pom.is_file():
        return _from_pom(pom)
    for name in ("build.gradle", "build.gradle.kts"):
        gradle = repo / name
        if gradle.is_file():
            return _from_gradle(gradle)
    return _from_python(repo)


def _from_python(repo: Path) -> BuildInfo | None:
    """pyproject.toml(한 단계 아래 폴더까지) 또는 requirements*.txt."""
    import tomllib

    candidates = [repo / "pyproject.toml", *sorted(repo.glob("*/pyproject.toml"))]
    for path in candidates:
        if not path.is_file():
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        poetry = data.get("tool", {}).get("poetry", {})
        deps = list(project.get("dependencies", [])) + list(poetry.get("dependencies", {}).keys())
        for group in project.get("optional-dependencies", {}).values():
            deps.extend(group)
        names = [_package_name(d) for d in deps]
        python = project.get("requires-python") or poetry.get("dependencies", {}).get("python")
        return BuildInfo(
            tool="python (pyproject.toml)", project=project.get("name") or poetry.get("name") or repo.name,
            version=project.get("version") or poetry.get("version"), packaging=None, parent=None,
            java_version=None, stack=_categorize_py(names),
            servlet_api=None, python_version=python if isinstance(python, str) else None,
        )
    requirements = sorted(repo.glob("requirements*.txt")) + sorted(repo.glob("*/requirements*.txt"))
    if requirements:
        names = []
        for req in requirements:
            for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split("#")[0].strip()
                if line and not line.startswith("-"):
                    names.append(_package_name(line))
        return BuildInfo(tool="python (requirements.txt)", project=repo.name, version=None, packaging=None,
                         parent=None, java_version=None, stack=_categorize_py(names))
    return None


def _package_name(spec: str) -> str:
    return re.split(r"[<>=!~\[;\s@]", spec.strip(), maxsplit=1)[0].lower()


def _categorize_py(names: list[str]) -> dict[str, list[str]]:
    stack: dict[str, list[str]] = {}
    for category, needles in _PY_CATEGORIES:
        hits = sorted({n for n in names if any(n == needle or n.startswith(needle) for needle in needles)})
        if hits:
            stack[category] = hits
    return stack


def _from_pom(path: Path) -> BuildInfo:
    root = ET.parse(path).getroot()
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    p = "m:" if ns else ""

    def text(elem, tag: str) -> str | None:
        found = elem.find(f"{p}{tag}", ns)
        return found.text.strip() if found is not None and found.text else None

    parent_elem = root.find(f"{p}parent", ns)
    parent = None
    if parent_elem is not None:
        parent = f"{text(parent_elem, 'artifactId')} {text(parent_elem, 'version') or ''}".strip()
    props = root.find(f"{p}properties", ns)
    java = None
    if props is not None:
        for key in ("java.version", "maven.compiler.source", "maven.compiler.release", "jdk.version"):
            java = java or text(props, key)
    artifacts = [
        a.text.strip()
        for a in root.iterfind(f".//{p}dependency/{p}artifactId", ns)
        if a.text
    ]
    return BuildInfo(
        tool="maven",
        project=text(root, "artifactId") or path.parent.name,
        version=text(root, "version"),
        packaging=text(root, "packaging"),
        parent=parent,
        java_version=java,
        stack=_categorize(artifacts),
        servlet_api=_servlet_api(artifacts),
    )


def _from_gradle(path: Path) -> BuildInfo:
    text = path.read_text(encoding="utf-8", errors="replace")
    artifacts = [m.group(2) for m in re.finditer(r"""["']([\w.\-]+):([\w.\-]+)(?::[\w.\-]+)?["']""", text)]
    java = re.search(r"(?:sourceCompatibility|languageVersion)\s*[=(]\s*(?:JavaVersion\.VERSION_|JavaLanguageVersion\.of\()?['\"]?([\d._]+)", text)
    return BuildInfo(
        tool="gradle",
        project=path.parent.name,
        version=None,
        packaging="war" if "war" in text else None,
        parent=None,
        java_version=java.group(1).replace("_", ".") if java else None,
        stack=_categorize(artifacts),
        servlet_api=_servlet_api(artifacts),
    )


def _categorize(artifacts: list[str]) -> dict[str, list[str]]:
    stack: dict[str, list[str]] = {}
    for category, needles in _CATEGORIES:
        hits = sorted({a for a in artifacts if any(n in a for n in needles)})
        if hits:
            stack[category] = hits
    return stack


def _servlet_api(artifacts: list[str]) -> str | None:
    if any(a.startswith("jakarta.servlet") for a in artifacts):
        return "jakarta"
    if any(a in ("servlet-api", "javax.servlet-api") for a in artifacts):
        return "javax"
    return None
