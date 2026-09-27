# 관문 1 검증: 기반 그래프가 실제 코드 흐름과 맞는가

- 날짜: 2026-09-27
- 대상: [eGovFrame 공통컴포넌트](https://github.com/eGovFramework/egovframe-common-components) `6241e394`, Oracle Mapper
- 결과: **통과** (빈틈 4개를 찾아 고친 뒤)

## 방법

1. 성격이 다른 기능 3개를 고른다: 쓰기 + 파일 첨부, 보안·세션, 코드성 관리 화면.
2. **사수를 쓰지 않고** 원본 소스를 열어 URL → Controller → Service → DAO → SQL → 테이블을 한 줄씩 따라가 정답 메모를 쓴다.
3. 같은 URL로 `sasu trace`를 돌려 비교한다.

정답 메모는 사수 개발에 참여한 AI(Claude)가 소스를 직접 읽어 작성했다. 사수의 파서·링커를 쓰지 않았으므로 도구와 독립적이지만, 사람이 쓴 메모는 아니다.

## 정답 메모

경로는 `src/main/java/egovframework/com/`, `src/main/resources/egovframework/mapper/com/` 기준.

### 1. 게시글 등록 `/cop/bbs/insertArticle.do`

| 단계 | 위치 | 내용 |
| --- | --- | --- |
| Controller | `cop/bbs/web/EgovArticleController.java:325` | 인증 확인(`EgovUserDetailsHelper`) → 입력 오류면 게시판 정보 조회 후 등록 화면 → 아니면 `insertArticleAndFiles` 호출 후 목록으로 redirect |
| Service | `cop/bbs/service/impl/EgovArticleServiceImpl.java:102` | 첨부파일 저장 → 글 번호 생성(`egovNttIdGnrService`, XML 설정 빈) → 답글이면 `replyArticle`, 아니면 `insertArticle` |
| 첨부파일 | `cmm/service/impl/EgovFileMngServiceImpl.java:73` → `FileManageDAO.java:35` | `FileManageDAO.insertFileMaster` → COMTNFILE, `insertFileDetail` → COMTNFILEDETAIL |
| DAO | `cop/bbs/service/impl/EgovArticleDAO.java` | `BBSArticle.insertArticle`, `BBSArticle.replyArticle` → COMTNBBS |
| 화면·이동 | | 오류 시 `EgovArticleRegist.jsp`, 정상 시 `/cop/bbs/selectArticleList.do` 또는 블로그 목록, 미인증 시 로그인 |

### 2. 로그인 `/uat/uia/actionLogin.do`

| 단계 | 위치 | 내용 |
| --- | --- | --- |
| Controller | `uat/uia/web/EgovLoginController.java:170` | 잠금 기능이 켜져 있으면 실패 이력 조회·처리 → `actionLogin` → 성공 시 `/uat/uia/actionMain.do` |
| Service | `uat/uia/service/impl/EgovLoginServiceImpl.java:87, 211, 224` | 비밀번호 암호화(`EgovFileScrty`) 후 조회, 실패 횟수 갱신 |
| DAO | `uat/uia/service/impl/LoginDAO.java:48, 96, 105` | `LoginUsr.actionLogin`, `LoginUsr.selectLoginIncorrect`, **`"LoginUsr.updateLoginIncorrect" + USER_SE`** |
| SQL | `uat/uia/EgovLoginUsr_SQL_oracle.xml:91, 270, 313, 346, 379` | 조회는 COMTNGNRLMBER·COMTNENTRPRSMBER·COMTNEMPLYRINFO, 갱신은 사용자 구분별 GNR·ENT·USR 쿼리가 각 테이블 하나씩 |

### 3. 공통코드 등록 `/sym/ccm/cca/RegistCcmCmmnCode.do`

| 단계 | 위치 | 내용 |
| --- | --- | --- |
| Controller | `sym/ccm/cca/web/EgovCcmCmmnCodeManageController.java:150` | 입력 오류면 분류코드 목록과 등록 화면 → 같은 코드ID가 있으면 등록 화면 → 없으면 `insertCmmnCode` 후 목록으로 forward |
| Service | `EgovCcmCmmnCodeManageServiceImpl.java:57, 74`, `EgovCcmCmmnClCodeManageServiceImpl` | 상세 조회, 등록, 분류코드 목록 |
| SQL | `sym/ccm/cca/EgovCmmnCodeManage_SQL_oracle.xml:68, 85` | `selectCmmnCodeDetail` → COMTCCMMNCODE (메모에는 한 개만 적음), `insertCmmnCode` → COMTCCMMNCODE, 분류코드 목록 → COMTCCMMNCLCODE |

## 비교 결과

| 기능 | 처음 비교 | 수정 후 |
| --- | --- | --- |
| 게시글 등록 | 서비스·첨부파일·DAO·SQL·테이블·화면·이동 일치. **인증 확인(static 호출)과 글 번호 생성 빈이 빠짐** | 모두 일치 |
| 로그인 | 조회 쿼리 일치. **실패 횟수 갱신 SQL이 빠짐** (동적 쿼리 ID). 메시지 호출이 여러 번 반복돼 읽기 어려움 | 모두 일치, 반복 호출은 ×N으로 합침 |
| 공통코드 등록 | 일치. **사수 쪽이 더 정확함**: 상세 조회는 `FROM COMTCCMMNCODE A, COMTCCMMNCLCODE B`로 테이블이 두 개인데 정답 메모가 하나를 놓침 | 정답 메모를 고침 |

## 찾아서 고친 빈틈

| 빈틈 | 원인 | 조치 |
| --- | --- | --- |
| 동적 쿼리 ID | `"LoginUsr.updateLoginIncorrect" + map.get("USER_SE")`는 문자열 리터럴이 아니라 읽지 않았음 | 문자열로 시작하는 더하기 인자의 앞부분을 뽑아, 그 접두어로 시작하는 쿼리 전부에 잇고 "동적 ID"로 표시 |
| static 유틸 호출 | 필드 호출만 따라갔음 | 호출 대상이 저장소 안 클래스 이름이면 그 클래스 메서드로 이음 |
| 저장소 밖 빈 | XML 설정에만 있는 빈은 구현을 못 찾아 조용히 사라짐 | `@Resource(name=...)` 필드인데 구현이 없으면 "저장소 밖"으로 표시 |
| 읽기 어려움 | 유틸 내부 호출과 반복 호출이 흐름을 덮음 | 기본은 스테레오타입 클래스만 펼치고, 같은 호출은 ×N으로 합침. `--full`로 전부 보기 |

## 인수인계 관점에서 나온 발견

이 검증을 하며 찾은 것들은 그대로 "떠나는 개발자에게 물어야 할 질문" 후보가 된다.

- 로그인 실패 처리의 쿼리 ID가 `USER_SE` 값으로 정해진다. 새로운 사용자 구분이 생기면 어느 쿼리가 필요한가?
- 글 번호와 파일 ID는 저장소 밖 설정 빈(`egovNttIdGnrService`, `egovFileIdGnrService`)이 만든다. 이 설정은 어디에 있고, 운영 DB의 ID 테이블과 어떻게 맞춰져 있는가?
- 게시글 등록 컨트롤러는 입력 오류일 때 게시판 정보를 다시 조회한다. 이 조회가 실패하면 어떻게 되는가?

## 전체 수치 (수정 후)

`sasu stats` 기준 URL 1,182개:

| 구분 | 개수 |
| --- | --- |
| SQL까지 이어짐 | 984 (83.2%) |
| 코드만 거침 (SQL 없음: LDAP, 메시지, 검증 등) | 81 (6.9%) |
| 컨트롤러에서 끝남 (화면만 보여 주는 URL 등) | 117 (9.9%) |
