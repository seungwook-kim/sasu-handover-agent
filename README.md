# 사수 (SASU) · 떠난 개발자 대신 답하는 SI 인수인계 에이전트

떠나는 개발자에게 무엇을 물어야 할지 코드 분석으로 먼저 찾아 주는 SI 인수인계 에이전트입니다. 받은 답변을 코드 위치에 연결해 두고, 신규 투입자의 "왜?"에 근거와 함께 답합니다.

> **상태: 개발 중.** 코드 분석(경로 추적, 프로젝트 지도, 공통화 진단, 불필요한 소스)까지 동작합니다. 인수인계 질문 생성과 "왜?" 답변은 아직입니다.

## 사용해 보기

[uv](https://docs.astral.sh/uv/)가 필요합니다.

```bash
uv sync
uv run sasu fetch-sample                                   # 예제: 전자정부 표준프레임워크 공통컴포넌트
uv run sasu index data/repos/egovframe-common-components   # 분석 (약 2초)
uv run sasu map                                            # 프로젝트 지도: 기술 스택, 모듈, 핵심 테이블
uv run sasu map cop.bbs                                    # 모듈 하나: URL마다 읽고 쓰는 테이블
uv run sasu trace /cop/bbs/selectArticleList.do            # URL 하나의 경로 추적
uv run sasu common                                         # 공통 코드 사용 현황, 복사해 붙인 코드, 중복 SQL
uv run sasu unused                                         # 끊어진 쿼리 참조, 안 쓰는 SQL·URL·메서드·JSP
uv run sasu stats                                          # 분석 요약
```

`trace` 결과 예시:

```
URL /cop/bbs/selectArticleList.do
└── EgovArticleController.selectArticleList  egovframework/com/cop/bbs/web/EgovArticleController.java:149
    ├── EgovArticleServiceImpl.selectArticleList  egovframework/com/cop/bbs/service/impl/EgovArticleServiceImpl.java:57
    │   ├── EgovArticleDAO.selectArticleList  egovframework/com/cop/bbs/service/impl/EgovArticleDAO.java:15
    │   │   └── SELECT BBSArticle.selectArticleList  egovframework/mapper/com/cop/bbs/EgovArticle_SQL_oracle.xml:213
    │   │       테이블 COMTNBBS, COMVNUSERMASTER, COMTNCOMMENT
    │   └── ...
    ├── → /uat/uia/egovLoginUsr.do  (이동)
    └── 화면 WEB-INF/jsp/egovframework/com/cop/bbs/EgovArticleList.jsp
```

eGovFrame 공통컴포넌트(URL 1,182개)에서 URL의 83.2%가 SQL까지 이어집니다. 나머지는 화면만 보여 주는 URL(9.9%)과 LDAP·메시지·검증처럼 DB를 쓰지 않는 URL(6.9%)입니다.

문자열을 더해 만드는 동적 쿼리 ID(`"LoginUsr.updateLoginIncorrect" + userSe`), static 유틸 호출, XML 설정에만 있는 저장소 밖 빈도 따라가거나 표시합니다. 기본 보기는 Controller·Service·DAO만 펼치고, `--full`을 붙이면 유틸 내부까지 모두 펼칩니다. 검증 과정은 [관문 1 검증](docs/gate1-verification.md)에 정리했습니다.

### eGovFrame에서 찾은 것

- **실제 버그 2건:** 당직관리 목록(`/uss/ion/bnt/selectBndtManageList.do`)이 Mapper에 없는 개수 쿼리를 부르고, 인쇄 출력 DAO는 네임스페이스 오타(`PrntngOutptDA`)로 쿼리를 찾을 수 없습니다.
- **사람의 메모와 일치:** `BBSArticle.selectMaxNttId`를 안 쓰는 SQL로 찾았고, Mapper 수정 이력의 "selectMaxNttId문은 실제 사용하지 않음"을 근거로 붙입니다.
- **상속 대신 복사:** 보안 조치로 공통 부모 클래스에 넣은 권한 검사(`egovAssertAdminOrOwner`)를 47개 컨트롤러가 복사해 씁니다.

검증과 고친 오탐은 [3단계 검증](docs/gate3-verification.md)에 정리했습니다.

## 왜 만드나

SI 고도화·유지보수 프로젝트에서 인수인계는 보통 1주 정도입니다. 문서와 말로 설명을 듣고 질문할 기회는 있지만, **무엇을 물어봐야 할지 모르는 채로** 기간이 끝납니다. 기존 개발자가 떠난 뒤에야 처음 보는 로직과 버그를 만나고, 결국 다시 연락하게 됩니다.

코드는 "무엇을 하는지"는 보여 주지만 "왜 그렇게 했는지"는 보여 주지 않습니다. 그 "왜"는 떠나는 개발자의 머릿속에 있고, 인수인계 기간이 그것을 붙잡을 수 있는 유일한 때입니다.

## 사수가 돕는 세 번의 순간

| 순간 | 사수가 하는 일 |
| --- | --- |
| 인수인계 전 | 코드 위험 분석으로 **"반드시 물어봐야 할 질문 Top 20"**을 만든다 |
| 인수인계 1주 | 떠나는 개발자의 짧은 텍스트 답변을 **해당 코드 위치에 연결**해 저장한다 |
| 떠난 후 | 신규 투입자의 "왜?"에 코드 근거와 인수인계 답변으로 답한다. 답이 없으면 떠난 개발자에게 보낼 질문 초안을 만들고, 받은 답도 저장한다 |

보조 기능으로 프로젝트 지도(화면→컨트롤러→서비스→쿼리→테이블), 공통 모듈 현황, 호출되지 않는 코드 목록을 제공합니다.

## 설계 원칙

- **확정할 수 있는 건 정적 분석으로 확정한다.** 호출 관계, 참조 여부, 위험 패턴은 코드 분석으로 찾고, LLM은 설명과 질의응답만 맡는다.
- **답변의 근거를 구분해 표시한다.** 모든 답에 "확인된 사실 / 인수인계자 답변 / 추정"을 붙이고, 근거가 없으면 "모름"이라고 답한다.
- **LLM은 주소 하나로 연결한다.** OpenAI 호환 주소만 바꾸면 개발용 API에서 사내 LLM 서버로 옮겨 갈 수 있다. 폐쇄망 기관의 자체 sLLM 서버에 붙이는 것을 전제로 설계한다.
- **가볍게 돈다.** 서버 없는 파일형 DB를 쓰고, 개발 PC에서 모델을 직접 돌리지 않는다.

## 기술 구성 (계획)

| 역할 | 도구 |
| --- | --- |
| 코드 분석 | tree-sitter (Java), MyBatis XML 파서 |
| 산출물 파싱 | openpyxl, python-pptx |
| 저장 | SQLite + sqlite-vec |
| 에이전트 | LangGraph |
| LLM | OpenAI 호환 API (개발: OpenRouter / 현장: 사내 LLM 서버) |
| API · 화면 | FastAPI, 웹 UI, MCP 도구 |
| 평가 | PyDriller로 공개 프로젝트의 과거 커밋 기록 분석 |

## 평가 방법 (계획)

실제 회사 코드 대신 **공개 프로젝트의 과거 기록**을 정답지로 씁니다.

1. **질문 생성:** 과거 시점의 코드로 질문 Top 20을 만들고, 이후 실제로 버그가 수정된 위치를 얼마나 미리 짚었는지 채점합니다. 무작위 선택, 복잡도 순 선택과 비교합니다.
2. **"왜?" 답변:** 커밋 메시지를 숨긴 채 "이 줄은 왜 이렇게 돼 있나"를 묻고, 숨긴 메시지와 비교합니다. 근거가 없을 때 추정이라고 표시하는지도 봅니다.

"나중에 버그가 난 곳 = 인수인계 때 물어봤어야 할 곳"은 완벽한 등식이 아니므로, 1번은 참고 지표로 다룹니다.

분석·평가 대상 후보는 [전자정부 표준프레임워크 공통컴포넌트](https://github.com/eGovFramework/egovframe-common-components)(Java · Spring · MyBatis · JSP, Apache-2.0)입니다.

## 진행 단계

날짜 대신 관문을 통과해야 다음 단계로 넘어갑니다.

- [x] 준비: 대상 프로젝트를 사수 없이 직접 읽고 기능 3개의 흐름 정답 메모 작성
- [x] 기반 그래프: Java + MyBatis 연결 (관문 1: 정답 메모의 흐름이 그대로 나옴 → [검증 기록](docs/gate1-verification.md))
- [x] 보조 기능: 지도, 공통화, 불필요한 소스 → [검증 기록](docs/gate3-verification.md)
- [ ] 질문 생성
- [ ] 답변 연결 + "왜?" 질의응답 (관문 2: 폴더 하나로 질문 → 답 입력 → "왜?" 답변까지)
- [ ] 평가와 개선 (관문 3: 비교표 완성, 약한 결과도 기록)
- [ ] 다듬기: 웹 UI, 설치 패키지, 엑셀 내보내기
- [ ] 사용 테스트
- [ ] 확장: MCP, 기존 동작 목록
