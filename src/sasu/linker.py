"""파싱 결과를 이어 URL → Controller → Service → DAO → SQL → 테이블 그래프를 만든다.

Spring MVC + MyBatis SI 프로젝트의 관례를 따른다.
- 필드 주입은 @Resource(name=빈이름)이 우선이고, 없으면 타입으로 찾는다.
- 타입이 인터페이스면 그 인터페이스를 구현한 클래스로 잇는다 (EgovXxxService → EgovXxxServiceImpl).
- DAO는 selectList("네임스페이스.쿼리ID") 처럼 문자열로 SQL을 부른다.
- 컨트롤러가 반환한 문자열은 /WEB-INF/jsp/{뷰}.jsp 이고, redirect:/forward:는 다른 URL이다.
"""

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

VIEW_PREFIX = "WEB-INF/jsp/"
VIEW_SUFFIX = ".jsp"


@dataclass
class LinkStats:
    urls: int = 0
    edges: int = 0


def link(conn: sqlite3.Connection) -> LinkStats:
    stats = LinkStats()
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM url_mappings")

    classes = {r["id"]: r for r in conn.execute("SELECT * FROM classes")}
    bean_map: dict[str, list[int]] = defaultdict(list)
    name_map: dict[str, list[int]] = defaultdict(list)
    impl_map: dict[str, list[int]] = defaultdict(list)
    for cid, c in classes.items():
        if c["bean_name"]:
            bean_map[c["bean_name"]].append(cid)
        name_map[c["name"].split(".")[-1]].append(cid)
        for iface in json.loads(c["interfaces"]):
            impl_map[iface].append(cid)

    methods_by_class: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    methods = {r["id"]: r for r in conn.execute("SELECT * FROM methods")}
    for mid, m in methods.items():
        methods_by_class[m["class_id"]][m["name"]].append(mid)

    fields_by_class: dict[int, dict[str, sqlite3.Row]] = defaultdict(dict)
    for f in conn.execute("SELECT * FROM fields"):
        fields_by_class[f["class_id"]][f["name"]] = f

    statements = {r["full_id"]: r["id"] for r in conn.execute("SELECT id, full_id FROM statements")}
    # Python 파일의 클래스는 아래 Python 규칙(linker_python)으로 잇는다
    python_classes = {
        r["id"] for r in conn.execute(
            "SELECT c.id FROM classes c JOIN files f ON f.id = c.file_id WHERE f.kind = 'python'"
        )
    }
    jsp_files = {
        r["path"].split(VIEW_PREFIX, 1)[1]: r["id"]
        for r in conn.execute("SELECT id, path FROM files WHERE kind = 'jsp'")
        if VIEW_PREFIX in r["path"]
    }

    def resolve_type(type_name: str) -> list[int]:
        found = []
        for cid in name_map.get(type_name, []):
            if classes[cid]["kind"] == "interface":
                found.extend(impl_map.get(type_name, []) or [cid])
            else:
                found.append(cid)
        return found or impl_map.get(type_name, [])

    def resolve_field(class_id: int, field_name: str) -> list[int]:
        f = fields_by_class[class_id].get(field_name)
        if f is None:
            return []
        if f["bean_name"] and f["bean_name"] in bean_map:
            return bean_map[f["bean_name"]]
        return resolve_type(f["type_name"])

    def with_superclasses(class_id: int) -> list[int]:
        chain, seen = [class_id], {class_id}
        current = classes[class_id]
        while current["superclass"]:
            parents = [p for p in name_map.get(current["superclass"], []) if p not in seen]
            if not parents:
                break
            chain.append(parents[0])
            seen.add(parents[0])
            current = classes[parents[0]]
        return chain

    edges: set[tuple] = set()

    # 1. URL 매핑: 클래스 접두어 × 메서드 경로
    for mid, m in methods.items():
        paths = json.loads(m["mapping_paths"])
        if not paths:
            continue
        prefixes = json.loads(classes[m["class_id"]]["mapping_paths"]) or [""]
        for prefix in prefixes:
            for path in paths:
                conn.execute(
                    "INSERT INTO url_mappings (url, method_id) VALUES (?, ?)",
                    (normalize_url(prefix + "/" + path), mid),
                )
                stats.urls += 1

    # 2. 호출 해석
    for call in conn.execute("SELECT * FROM calls"):
        caller = call["method_id"]
        class_id = methods[caller]["class_id"]
        if class_id in python_classes:
            continue
        line = call["line"]
        for arg in json.loads(call["string_args"]):
            if arg in statements:
                edges.add(("method", caller, "statement", statements[arg], None, line, None))
        # "LoginUsr.updateLoginIncorrect" + userSe → 이 접두어로 시작하는 쿼리 전부
        for prefix in json.loads(call["string_prefixes"]):
            if "." not in prefix:
                continue
            for full_id, sid in statements.items():
                if full_id.startswith(prefix) and full_id != prefix:
                    edges.add(("method", caller, "statement", sid, None, line, f"동적 ID: \"{prefix}\" + …"))

        receiver = call["receiver"]
        field_name = None
        if receiver in (None, "this", "super"):
            targets = with_superclasses(class_id)
        elif receiver.startswith("this.") and receiver.removeprefix("this.") in fields_by_class[class_id]:
            field_name = receiver.removeprefix("this.")
            targets = resolve_field(class_id, field_name)
        elif receiver.isidentifier():
            if receiver in fields_by_class[class_id]:
                field_name = receiver
                targets = resolve_field(class_id, receiver)
            elif receiver[:1].isupper():
                # EgovUserDetailsHelper.getAuthenticatedUser() 같은 static 유틸 호출
                targets = [c for c in name_map.get(receiver, []) if classes[c]["kind"] != "interface"]
            else:
                targets = []
        else:
            targets = []

        linked = False
        for target_class in targets:
            for callee in methods_by_class[target_class].get(call["callee"], []):
                if callee != caller:
                    edges.add(("method", caller, "method", callee, None, line, None))
                    linked = True

        # 주입받은 빈인데 저장소 안에서 구현을 못 찾음 → 프레임워크나 XML 설정에만 있는 빈
        if not linked and field_name is not None:
            f = fields_by_class[class_id][field_name]
            if f["bean_name"]:
                edges.add((
                    "method", caller, "external", None,
                    f"{f['type_name']}.{call['callee']}", line, f"빈 '{f['bean_name']}' (저장소 밖)",
                ))

    # 2-1. String queryId = "Ns.id"; selectOne(queryId) 처럼 변수를 거쳐 부르는 쿼리.
    #      DAO(@Repository) 메서드 안의 문자열 리터럴이 쿼리 ID와 같으면 잇는다.
    for lit in conn.execute(
        """SELECT s.method_id, s.value, s.line FROM string_literals s
           JOIN methods m ON m.id = s.method_id JOIN classes c ON c.id = m.class_id
           WHERE c.stereotype = 'Repository'"""
    ):
        if lit["value"] in statements:
            key_prefix = ("method", lit["method_id"], "statement", statements[lit["value"]], None)
            if not any(e[:5] == key_prefix for e in edges):
                edges.add((*key_prefix, lit["line"], None))

    # 3. 뷰와 이동 URL
    for mid, m in methods.items():
        if m["class_id"] in python_classes or classes[m["class_id"]]["stereotype"] not in ("Controller", "RestController"):
            continue
        for value in json.loads(m["returned_strings"]):
            if value.startswith(("redirect:", "forward:")):
                target = value.split(":", 1)[1].split("?")[0]
                if target.startswith("/"):
                    edges.add(("method", mid, "url", None, normalize_url(target), None, None))
            elif value and "/" in value:
                view = value.lstrip("/") + VIEW_SUFFIX
                edges.add(("method", mid, "view", jsp_files.get(view), VIEW_PREFIX + view, None, None))

    # 4. Python: 라우터 접두어·include, import·self·이름 추정 호출, ORM·SQL, 템플릿
    from sasu.linker_python import link_python  # linker_python이 normalize_url을 가져가므로 여기서 import

    stats.urls += link_python(conn, edges, classes, methods, methods_by_class, fields_by_class, name_map)

    conn.executemany(
        """INSERT INTO edges (src_kind, src_id, dst_kind, dst_id, dst_name, line, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        sorted(edges, key=lambda e: (e[1], e[5] or 0, e[2], e[3] or 0, e[4] or "")),
    )
    stats.edges = len(edges)
    return stats


def normalize_url(url: str) -> str:
    url = "/" + url.strip().lstrip("/")
    while "//" in url:
        url = url.replace("//", "/")
    return url.rstrip("/") or "/"
