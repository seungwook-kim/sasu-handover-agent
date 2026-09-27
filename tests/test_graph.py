"""작은 SI 스타일 프로젝트를 만들어 색인 → 연결 → 경로 추적을 확인한다."""

from pathlib import Path

import pytest

from sasu import db
from sasu.indexer import index_repository
from sasu.trace import coverage, find_urls, trace_url

FILES = {
    "src/main/java/com/demo/web/OrderController.java": """
package com.demo.web;

@Controller
@RequestMapping("/order")
public class OrderController {
    @Resource(name = "orderService")
    private OrderService orderService;

    @RequestMapping("/list.do")
    public String list(OrderVO vo) {
        if (vo == null) {
            return "redirect:/login.do";
        }
        orderService.selectOrderList(vo);
        return "demo/order/OrderList";
    }

    @GetMapping("/help.do")
    public String help() {
        return "demo/order/Help";
    }
}
""",
    "src/main/java/com/demo/service/OrderService.java": """
package com.demo.service;

public interface OrderService {
    List<OrderVO> selectOrderList(OrderVO vo);
}
""",
    "src/main/java/com/demo/service/impl/OrderServiceImpl.java": """
package com.demo.service.impl;

@Service("orderService")
public class OrderServiceImpl implements OrderService {
    @Resource(name = "orderDAO")
    private OrderDAO orderDAO;

    // XML 설정에만 있는 프레임워크 빈. 저장소 안에 구현이 없다.
    @Resource(name = "orderIdGnrService")
    private EgovIdGnrService idGen;

    public List<OrderVO> selectOrderList(OrderVO vo) {
        AuthHelper.check();
        idGen.getNextIntegerId();
        orderDAO.updateStatus(vo);
        return orderDAO.selectOrderList(vo);
    }
}
""",
    "src/main/java/com/demo/util/AuthHelper.java": """
package com.demo.util;

public class AuthHelper {
    public static void check() {
        Props.get("auth");
    }
}
""",
    "src/main/java/com/demo/util/Props.java": """
package com.demo.util;

public class Props {
    public static String get(String key) { return key; }
}
""",
    "src/main/java/com/demo/service/impl/OrderDAO.java": """
package com.demo.service.impl;

@Repository("orderDAO")
public class OrderDAO extends AbstractDAO {
    public List<OrderVO> selectOrderList(OrderVO vo) {
        return selectList("Order.selectOrderList", vo);
    }

    // 쿼리 ID가 실행 때 정해진다: Order.updateStatusGNR / Order.updateStatusENT
    public void updateStatus(OrderVO vo) {
        update("Order.updateStatus" + vo.getUserSe(), vo);
    }
}
""",
    "src/main/resources/mapper/Order_SQL_oracle.xml": """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="Order">
    <select id="selectOrderList" resultType="OrderVO">
        SELECT * FROM TB_ORDER o JOIN TB_CUSTOMER c ON o.CUST_ID = c.CUST_ID
    </select>
    <update id="updateStatusGNR">UPDATE TB_ORDER SET STATUS = 'G'</update>
    <update id="updateStatusENT">UPDATE TB_ORDER_ENT SET STATUS = 'E'</update>
</mapper>
""",
    # 다른 DB용 Mapper는 분석에서 빠져야 한다
    "src/main/resources/mapper/Order_SQL_mysql.xml": """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="Order">
    <select id="selectOrderList">SELECT * FROM tb_order_mysql</select>
</mapper>
""",
    "src/main/webapp/WEB-INF/jsp/demo/order/OrderList.jsp": "<html></html>",
}


@pytest.fixture
def conn(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel, content in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    connection = db.connect(tmp_path / "sasu.db")
    index_repository(connection, repo, dialect="oracle")
    yield connection
    connection.close()


def test_only_configured_dialect_is_indexed(conn):
    rows = conn.execute("SELECT full_id FROM statements ORDER BY id").fetchall()
    assert [r["full_id"] for r in rows] == [
        "Order.selectOrderList", "Order.updateStatusGNR", "Order.updateStatusENT",
    ]


def test_url_combines_class_and_method_mapping(conn):
    assert find_urls(conn, "/order/list.do") == ["/order/list.do"]
    assert find_urls(conn, "order") == ["/order/help.do", "/order/list.do"]


def test_trace_goes_from_url_to_tables(conn):
    root = trace_url(conn, "/order/list.do")
    labels = [(n.kind, n.label) for n in root.walk()]
    assert labels == [
        ("url", "/order/list.do"),
        ("method", "OrderController.list"),
        ("method", "OrderServiceImpl.selectOrderList"),  # 인터페이스가 아니라 구현 클래스로
        ("method", "AuthHelper.check"),  # static 유틸 호출. 기본 보기에서는 펼치지 않는다
        ("external", "EgovIdGnrService.getNextIntegerId"),
        ("method", "OrderDAO.updateStatus"),
        ("statement", "UPDATE Order.updateStatusGNR"),
        ("statement", "UPDATE Order.updateStatusENT"),
        ("method", "OrderDAO.selectOrderList"),
        ("statement", "SELECT Order.selectOrderList"),
        ("url", "/login.do"),
        ("view", "WEB-INF/jsp/demo/order/OrderList.jsp"),
    ]
    select = next(n for n in root.walk() if n.label == "SELECT Order.selectOrderList")
    assert select.detail == "TB_ORDER, TB_CUSTOMER"
    assert select.location == "src/main/resources/mapper/Order_SQL_oracle.xml:3"


def test_dynamic_query_id_is_marked(conn):
    nodes = {n.label: n for n in trace_url(conn, "/order/list.do").walk()}
    assert nodes["UPDATE Order.updateStatusENT"].note == '동적 ID: "Order.updateStatus" + …'
    assert nodes["UPDATE Order.updateStatusENT"].detail == "TB_ORDER_ENT"
    assert nodes["SELECT Order.selectOrderList"].note is None


def test_external_bean_is_marked(conn):
    ext = next(n for n in trace_url(conn, "/order/list.do").walk() if n.kind == "external")
    assert ext.note == "빈 'orderIdGnrService' (저장소 밖)"


def test_full_mode_expands_utilities(conn):
    compact = [n.label for n in trace_url(conn, "/order/list.do").walk()]
    full = [n.label for n in trace_url(conn, "/order/list.do", full=True).walk()]
    assert "Props.get" not in compact
    assert "Props.get" in full


def test_missing_jsp_is_flagged(conn):
    view = next(n for n in trace_url(conn, "/order/help.do").walk() if n.kind == "view")
    assert view.detail == "파일 없음"


def test_coverage_breakdown(conn):
    cov = coverage(conn)
    assert (cov.total, cov.to_sql, cov.code_only, cov.handler_only) == (2, 1, 0, 1)
