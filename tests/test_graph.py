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

    public List<OrderVO> selectOrderList(OrderVO vo) {
        return orderDAO.selectOrderList(vo);
    }
}
""",
    "src/main/java/com/demo/service/impl/OrderDAO.java": """
package com.demo.service.impl;

@Repository("orderDAO")
public class OrderDAO extends AbstractDAO {
    public List<OrderVO> selectOrderList(OrderVO vo) {
        return selectList("Order.selectOrderList", vo);
    }
}
""",
    "src/main/resources/mapper/Order_SQL_oracle.xml": """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="Order">
    <select id="selectOrderList" resultType="OrderVO">
        SELECT * FROM TB_ORDER o JOIN TB_CUSTOMER c ON o.CUST_ID = c.CUST_ID
    </select>
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
    rows = conn.execute("SELECT full_id FROM statements").fetchall()
    assert [r["full_id"] for r in rows] == ["Order.selectOrderList"]


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
        ("method", "OrderDAO.selectOrderList"),
        ("statement", "SELECT Order.selectOrderList"),
        ("url", "/login.do"),
        ("view", "WEB-INF/jsp/demo/order/OrderList.jsp"),
    ]
    statement = next(n for n in root.walk() if n.kind == "statement")
    assert statement.detail == "TB_ORDER, TB_CUSTOMER"
    assert statement.location == "src/main/resources/mapper/Order_SQL_oracle.xml:3"


def test_missing_jsp_is_flagged(conn):
    view = next(n for n in trace_url(conn, "/order/help.do").walk() if n.kind == "view")
    assert view.detail == "파일 없음"


def test_coverage_breakdown(conn):
    cov = coverage(conn)
    assert (cov.total, cov.to_sql, cov.code_only, cov.handler_only) == (2, 1, 0, 1)
