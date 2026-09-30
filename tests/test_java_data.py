"""MyBatis 문자열 ID 말고 SI 현장에서 흔한 데이터 접근 방식: iBatis, 매퍼 인터페이스·@Select, JPA, JdbcTemplate."""

from pathlib import Path

import pytest

from sasu import db
from sasu.indexer import index_repository
from sasu.parsers.mybatis_parser import parse_mapper
from sasu.trace import trace_url

FILES = {
    # ── iBatis (전자정부 2.x 스타일) ──
    "src/main/java/demo/board/BoardController.java": """
package demo.board;
@Controller
public class BoardController {
    @Resource(name = "boardDAO") private BoardDAO boardDAO;
    @RequestMapping("/board/list.do")
    public String list(BoardVO vo) { boardDAO.selectBoardList(vo); return "board/List"; }
    @RequestMapping("/board/insert.do")
    public String insert(BoardVO vo) { boardDAO.insertBoard(vo); return "redirect:/board/list.do"; }
}
""",
    "src/main/java/demo/board/BoardDAO.java": """
package demo.board;
@Repository("boardDAO")
public class BoardDAO extends EgovAbstractDAO {
    public List selectBoardList(BoardVO vo) { return list("selectBoardList", vo); }
    public void insertBoard(BoardVO vo) { insert("Board.insertBoard", vo); }
}
""",
    "src/main/resources/sqlmap/Board_SQL.xml": """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE sqlMap PUBLIC "-//ibatis.apache.org//DTD SQL Map 2.0//EN" "http://ibatis.apache.org/dtd/sql-map-2.dtd">
<sqlMap namespace="Board">
  <sql id="where"><isNotEmpty property="searchWrd" prepend="AND"> TITLE LIKE '%' || #searchWrd# || '%'</isNotEmpty></sql>
  <select id="selectBoardList" parameterClass="BoardVO" resultClass="egovMap">
    SELECT A.NTT_ID, B.USER_NM FROM TB_BOARD A, TB_USER B WHERE A.WRITER = B.USER_ID <include refid="where"/>
  </select>
  <statement id="insertBoard">INSERT INTO TB_BOARD (NTT_ID) VALUES (#nttId#)</statement>
</sqlMap>""",
    # ── MyBatis 매퍼 인터페이스 + XML, @Select ──
    "src/main/java/demo/member/MemberController.java": """
package demo.member;
@RestController
public class MemberController {
    private final MemberService memberService;
    @GetMapping("/api/members")
    public List<Member> members() { return memberService.members(); }
}
""",
    "src/main/java/demo/member/MemberService.java": """
package demo.member;
@Service
public class MemberService {
    @Autowired private MemberMapper memberMapper;
    public List<Member> members() {
        memberMapper.selectNotice(1L);
        return memberMapper.selectMembers();
    }
}
""",
    "src/main/java/demo/member/MemberMapper.java": """
package demo.member;
@Mapper
public interface MemberMapper {
    List<Member> selectMembers();
    @Select({"SELECT * FROM TB_NOTICE", "WHERE ID = #{id}"})
    Notice selectNotice(long id);
}
""",
    "src/main/resources/mapper/MemberMapper.xml": """<mapper namespace="demo.member.MemberMapper">
  <select id="selectMembers">SELECT * FROM TB_MEMBER</select>
</mapper>""",
    # ── JPA ──
    "src/main/java/demo/order/Order.java": """
package demo.order;
@Entity
@Table(name = "TB_ORDER")
public class Order { @Id private Long id; }
""",
    "src/main/java/demo/order/OrderItem.java": """
package demo.order;
@Entity
public class OrderItem { @Id private Long id; }
""",
    "src/main/java/demo/order/OrderRepository.java": """
package demo.order;
public interface OrderRepository extends JpaRepository<Order, Long> {
    List<Order> findByStatus(String status);
    @Query("select o from Order o join o.items i where i.qty > 0")
    List<Order> findActive();
    @Modifying
    @Query(value = "UPDATE TB_ORDER SET STATUS = 'X'", nativeQuery = true)
    int cancelAll();
}
""",
    "src/main/java/demo/order/OrderController.java": """
package demo.order;
@RestController
public class OrderController {
    @Autowired private OrderService orderService;
    @PostMapping("/api/orders")
    public void create(Order order) { orderService.place(order); }
}
""",
    "src/main/java/demo/order/OrderService.java": """
package demo.order;
@Service
public class OrderService {
    @Autowired private OrderRepository orderRepository;
    @Autowired private JdbcTemplate jdbcTemplate;
    @Autowired private EntityManager em;
    public void place(Order order) {
        orderRepository.findByStatus("READY");
        orderRepository.findActive();
        orderRepository.save(order);
        orderRepository.deleteById(1L);
        orderRepository.cancelAll();
        jdbcTemplate.update("INSERT INTO TB_ORDER_LOG (MSG) VALUES (?)", "placed");
        em.createQuery("select i from OrderItem i where i.qty = 0").getResultList();
    }
}
""",
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


def _statements(conn, url: str) -> dict[str, str]:
    return {n.label: n.detail for n in trace_url(conn, url, full=True).walk() if n.kind == "statement"}


def test_ibatis_parser():
    parsed = parse_mapper(FILES["src/main/resources/sqlmap/Board_SQL.xml"])
    assert parsed.flavor == "ibatis" and parsed.namespace == "Board"
    kinds = {s.id: (s.kind, s.tables) for s in parsed.statements}
    assert kinds["selectBoardList"] == ("select", ["TB_BOARD", "TB_USER"])
    assert kinds["insertBoard"] == ("insert", ["TB_BOARD"])  # <statement>는 본문으로 종류를 정한다


def test_ibatis_short_and_full_ids(conn):
    assert _statements(conn, "/board/list.do") == {"SELECT Board.selectBoardList": "TB_BOARD, TB_USER"}
    assert _statements(conn, "/board/insert.do") == {"INSERT Board.insertBoard": "TB_BOARD"}


def test_mybatis_mapper_interface_and_annotation(conn):
    assert _statements(conn, "/api/members") == {
        "SELECT demo.member.MemberMapper.selectNotice": "TB_NOTICE",
        "SELECT demo.member.MemberMapper.selectMembers": "TB_MEMBER",
    }


def test_jpa_repository_and_jdbc(conn):
    found = _statements(conn, "/api/orders")
    assert found["SELECT JPA Order"] == "TB_ORDER"  # findByStatus (이름으로 추정)
    assert found["SELECT demo.order.OrderRepository.findActive"] == "TB_ORDER"  # JPQL: Order → TB_ORDER, o.items는 뺌
    assert found["MERGE JPA Order"] == "TB_ORDER"  # 상속 메서드 save
    assert found["DELETE JPA Order"] == "TB_ORDER"  # 상속 메서드 deleteById
    assert found["UPDATE demo.order.OrderRepository.cancelAll"] == "TB_ORDER"  # 네이티브 @Query
    sql = {label: tables for label, tables in found.items() if label.startswith(("INSERT SQL", "SELECT SQL"))}
    assert sorted(sql.values()) == ["ORDER_ITEM", "TB_ORDER_LOG"]  # JdbcTemplate, em.createQuery(JPQL)


def test_entity_default_table_name(conn):
    tables = dict(conn.execute("SELECT name, table_name FROM classes WHERE table_name IS NOT NULL").fetchall())
    assert tables == {"Order": "TB_ORDER", "OrderItem": "ORDER_ITEM"}


def test_log_messages_are_not_sql():
    from sasu.linker_python import sql_kind

    assert sql_kind("insert BackupResult Data : {}") is None
    assert sql_kind("update 완료: count={}, elapsed={}") is None
    assert sql_kind("INSERT INTO TB_LOG (MSG) VALUES (?)") == "insert"
    assert sql_kind("WITH x AS (SELECT 1 FROM DUAL) SELECT * FROM x") == "select"
