from sasu.parsers.mybatis_parser import extract_tables, parse_mapper

MAPPER = '''<?xml version="1.0" encoding="UTF-8"?><!--
  2011.09.21  seo  selectMaxNttId is not used because NTT_ID comes from IDGEN
-->
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="BBSArticle">

    <sql id="articleWhere">
        WHERE a.BBS_ID = #{bbsId}
    </sql>

    <select id="selectArticleList" parameterType="BoardVO" resultMap="boardList">
        SELECT a.NTT_ID, b.USER_NM
        FROM COMTNBBS a
        LEFT OUTER JOIN COMVNUSERMASTER b ON a.FRST_REGISTER_ID = b.ESNTL_ID
        <include refid="articleWhere"/>
        <if test="searchCnd == 0">
            <![CDATA[ AND a.NTT_SJ LIKE '%' || #{searchWrd} || '%' ]]>
        </if>
    </select>

    <insert id="insertArticle" parameterType="BoardVO">
        INSERT INTO COMTNBBS (NTT_ID) VALUES (#{nttId})
    </insert>

    <update id="deleteArticle">
        UPDATE COMTNBBS SET USE_AT = 'N' WHERE NTT_ID = #{nttId}
    </update>
</mapper>
'''


def test_namespace_and_header():
    m = parse_mapper(MAPPER)
    assert m.namespace == "BBSArticle"
    assert "IDGEN" in m.header_comment


def test_statements_with_lines_and_tables():
    m = parse_mapper(MAPPER)
    by_id = {s.id: s for s in m.statements}
    assert list(by_id) == ["selectArticleList", "insertArticle", "deleteArticle"]
    sel = by_id["selectArticleList"]
    assert sel.kind == "select"
    assert sel.line == 11
    assert sel.tables == ["COMTNBBS", "COMVNUSERMASTER"]
    assert "WHERE a.BBS_ID" in sel.sql  # <include>가 펼쳐졌는지
    assert "/>" not in sel.sql
    assert by_id["insertArticle"].tables == ["COMTNBBS"]
    assert by_id["deleteArticle"].kind == "update"


def test_comma_joined_tables_and_dual():
    sql = "SELECT 1 FROM COMTNA a, COMTNB b WHERE a.X = b.X UNION SELECT 1 FROM DUAL"
    assert extract_tables(sql) == ["COMTNA", "COMTNB"]


def test_subquery_columns_are_not_tables():
    sql = (
        "SELECT * FROM ( SELECT rownum rn, TB.* FROM ( SELECT a.NTT_ID, a.NTT_SJ "
        "FROM COMTNBBS a LEFT OUTER JOIN COMVNUSERMASTER b ON a.X = b.X ) TB )"
    )
    assert extract_tables(sql) == ["COMTNBBS", "COMVNUSERMASTER"]


def test_not_a_mapper():
    assert parse_mapper("<beans></beans>") is None
