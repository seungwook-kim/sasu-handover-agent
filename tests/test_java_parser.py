from sasu.parsers.java_parser import parse_java

CONTROLLER = b'''/*
 * 2011.09.07  hong  add paging
 */
package egovframework.com.cop.bbs.web;

import egovframework.com.cop.bbs.service.EgovArticleService;

@Controller
@RequestMapping("/cop/bbs")
public class EgovArticleController {

    @Resource(name = "EgovArticleService")
    private EgovArticleService egovArticleService;

    @Autowired
    @Qualifier("fileMngService")
    private FileMngService fileService;

    /** list articles */
    @PostMapping(value = {"/selectArticleList.do", "/list.do"})
    public String selectArticleList(BoardVO boardVO, ModelMap model) {
        if (boardVO == null && model != null) {
            return "redirect:/uat/uia/egovLoginUsr.do";
        }
        try {
            egovArticleService.selectArticleList(boardVO);
        } catch (Exception e) {
        }
        return "egovframework/com/cop/bbs/EgovArticleList";
    }

    static class Inner {
        void run() {}
    }
}
'''

DAO = b'''package egovframework.com.cop.bbs.service.impl;

@Repository("EgovArticleDAO")
public class EgovArticleDAO extends EgovComAbstractDAO {
    public List<BoardVO> selectArticleList(BoardVO boardVO) {
        return selectList("BBSArticle.selectArticleList", boardVO);
    }
}
'''

SERVICE = b'''package egovframework.com.cop.bbs.service.impl;

@Service
public class EgovArticleServiceImpl extends EgovAbstractServiceImpl implements EgovArticleService, Serializable {
}
'''


def test_controller_class_info():
    f = parse_java(CONTROLLER)
    assert f.package == "egovframework.com.cop.bbs.web"
    assert "2011.09.07" in f.header_comment
    cls = f.classes[0]
    assert cls.fqcn == "egovframework.com.cop.bbs.web.EgovArticleController"
    assert cls.stereotype == "Controller"
    assert cls.mapping_paths == ["/cop/bbs"]


def test_field_bean_names():
    cls = parse_java(CONTROLLER).classes[0]
    by_name = {f.name: f for f in cls.fields}
    assert by_name["egovArticleService"].bean_name == "EgovArticleService"
    assert by_name["egovArticleService"].type_name == "EgovArticleService"
    assert by_name["fileService"].bean_name == "fileMngService"


def test_method_mapping_calls_and_risks():
    cls = parse_java(CONTROLLER).classes[0]
    m = cls.methods[0]
    assert m.name == "selectArticleList"
    assert m.mapping_paths == ["/selectArticleList.do", "/list.do"]
    assert m.doc == "/** list articles */"
    assert m.parameters == ["BoardVO", "ModelMap"]
    assert [(c.receiver, c.method) for c in m.calls] == [("egovArticleService", "selectArticleList")]
    assert m.returned_strings == ["redirect:/uat/uia/egovLoginUsr.do", "egovframework/com/cop/bbs/EgovArticleList"]
    # if + && + catch
    assert m.complexity == 4
    assert len(m.empty_catches) == 1
    assert m.empty_catches[0].exception_type == "Exception"


def test_inner_class_is_listed():
    names = [c.name for c in parse_java(CONTROLLER).classes]
    assert names == ["EgovArticleController", "EgovArticleController.Inner"]


def test_dao_string_argument():
    cls = parse_java(DAO).classes[0]
    assert cls.bean_name == "EgovArticleDAO"
    assert cls.superclass == "EgovComAbstractDAO"
    call = cls.methods[0].calls[0]
    assert call.receiver is None
    assert call.method == "selectList"
    assert call.string_args == ["BBSArticle.selectArticleList"]


def test_default_bean_name_and_interfaces():
    cls = parse_java(SERVICE).classes[0]
    assert cls.bean_name == "egovArticleServiceImpl"
    assert cls.interfaces == ["EgovArticleService", "Serializable"]
