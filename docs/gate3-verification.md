# 3단계 검증: 프로젝트 지도·공통화 진단·불필요한 소스

- 날짜: 2026-09-27
- 대상: eGovFrame 공통컴포넌트 `6241e394`, Oracle Mapper
- 통과 기준: 결과를 원본과 대조해 오탐 원인을 확인하고 고친다

## 불필요한 소스 (`sasu unused`)

| 종류 | 건수 | 확신 | 원본 대조 결과 |
| --- | --- | --- | --- |
| 끊어진 쿼리 참조 | 11 | 높음 | 6건을 Mapper에서 직접 확인, 모두 실제로 없는 ID. 2건은 URL에서 이어짐 |
| 부르는 곳이 없는 SQL | 15 | 높음 | 8건을 Java 전체 grep으로 확인, 호출 없음 |
| 부르는 화면을 못 찾은 URL | 96 | 후보 | 원본 전체(JSP·JS·XML·properties) grep: 처음 100건 중 4건이 .do 없는 AJAX 경로 → 고침 |
| 부르는 곳이 없는 메서드 | 75 | 후보 | 같은 이름 메서드가 다른 클래스에 있어 grep으로는 판별이 어려워 6건을 직접 확인. SpEL로 부르는 1건 오탐 → 고침 |
| 쓰이지 않는 JSP | 15 | 후보 | 원본 grep: 14건 언급 없음, 1건은 컨트롤러 주석에만 이름이 있음 (정탐) |

### 찾은 실제 버그 (URL에서 이어져 실행하면 오류가 날 수 있음)

| URL | 부르는 쿼리 | Mapper 상태 |
| --- | --- | --- |
| `/uss/ion/bnt/selectBndtManageList.do` (당직관리 목록) | `bndtManageDAO.selectBndtManageListTotCnt` | 네임스페이스는 있으나 이 ID가 없음 |
| 인쇄 출력 기능 (`PrntngOutptDAO.selectErncsl`) | `PrntngOutptDAO.selectErncsl` | 네임스페이스가 `PrntngOutptDA`(O 빠짐). 8개 DB 버전 모두 같고 2020년 첫 커밋부터 |

### 사람이 남긴 근거와 일치한 결과

`BBSArticle.selectMaxNttId`를 부르는 곳이 없는 SQL로 찾았고, Mapper 머리말 수정 이력의 "NTT_ID를 IDGENT서비스를 사용해서 생성하므로 selectMaxNttId문은 실제 사용하지 않음"(2011-09-21)을 근거로 붙였다.

### 고친 오탐

| 오탐 | 원인 | 조치 |
| --- | --- | --- |
| 에러 페이지 JSP 5건 | 설정 XML에 확장자 없는 뷰 경로(`egovframework/com/cmm/error/egovBizException`)로 적힘 | 설정의 뷰 경로를 참조로 수집 |
| `/twitter/tweets` 등 URL 4건 | .do 없는 REST 경로를 JSP가 AJAX로 부름 | 따옴표 안의 절대 경로를 URL 참조로 수집 |
| `EgovPasswordResolver.resolvePassword` | 설정 XML이 SpEL `#{egovPasswordResolver.resolvePassword(...)}`로 부름 | SpEL 메서드 호출을 참조로 수집 |
| `LoginUsr.updateLoginIncorrect` 끊어진 참조 | 동적 ID의 앞부분 (`+ userSe`) | 다른 쿼리의 접두어면 제외 |
| `Note.deleteOld`가 쓰인다고 판단 | `Note.deleteOldNotes` 안에 부분 문자열로 있음 | 단어 경계 비교 |

### 남은 한계

- 다른 시스템이나 즐겨찾기로 직접 부르는 URL, 리플렉션으로 부르는 메서드는 정적 분석으로 알 수 없다. 그래서 URL·메서드·JSP는 "후보"로만 표시한다.
- 메서드 75건 전체의 정밀도는 재지 않았다. 표본 6건 중 1건이 오탐이었고 고쳤다.

## 공통화 진단 (`sasu common`)

- 복사해 붙인 메서드 99묶음 383개 (식별자만 다르고 구조가 같은 코드, 토큰 60개 이상).
- `egovAssertAdminOrOwner` 49곳: 2026-07-13 KISA 보안취약점 조치로 공통 부모 `EgovComAbstractController`에 넣었지만, 상속하는 컨트롤러는 2개뿐이고 47곳이 복사본을 쓴다. `git log -S`로 도입 시점을 확인했다.
- 같은 담당자 조회 SQL이 5개 ID(`DeptJobDAO.selectChargerList` 등)로 중복.

## 프로젝트 지도 (`sasu map`)

- 모듈 61개, URL 1,182개, SQL 1,241개, 테이블 205개.
- 모듈별 URL의 읽기·쓰기 테이블은 관문 1에서 검증한 경로 추적(`trace`)을 그대로 쓴다.
- 빌드 정보: `egovframe-web-config-parent 5.0.1`, Jakarta Servlet, 배치(Quartz), 보안(fdl-security·crypto).

## 인수인계 질문 후보 (다음 단계 재료)

- 당직관리 목록 화면은 실제로 동작하나? 개수 쿼리가 Mapper에 없다.
- `egovAssertAdminOrOwner`를 바꿔야 하면 47개 복사본도 모두 바꿔야 하나? 왜 공통 부모를 상속하지 않았나?
- `selectMaxNttId`처럼 남아 있는 안 쓰는 SQL은 지워도 되나, 운영에서 다른 시스템이 쓰나?
