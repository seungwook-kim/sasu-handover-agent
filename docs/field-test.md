# 처음 보는 공개 프로젝트로 써 보기

eGovFrame과 FastAPI 예제는 개발하면서 계속 본 코드라서, 사수가 거기에만 맞춰져 있을 수 있다. 그래서 개발 중에 한 번도 보지 않은 공개 프로젝트를 분석해 보고, 막히거나 틀린 곳을 고쳤다. 고친 뒤에는 기존 예제의 결과가 그대로인지 다시 확인했다.

## 대상

| 프로젝트 | 방식 | 규모 |
| --- | --- | --- |
| [spring-projects/spring-petclinic](https://github.com/spring-projects/spring-petclinic) | Spring Boot, Spring Data JPA, Thymeleaf | Java 30개, URL 12개 |
| [mybatis/jpetstore-6](https://github.com/mybatis/jpetstore-6) | Spring MVC, MyBatis 매퍼 인터페이스 + XML, JSP | Java 26개, Mapper 7개, URL 19개 |

## 찾은 문제와 고친 것

| 증상 | 원인 | 고친 것 |
| --- | --- | --- |
| petclinic: 저장(save)이 테이블 표에 안 나옴 | 지도의 조회/등록/수정/삭제 칸이 JPA save(MERGE)를 세지 않음 | "저장" 칸 추가 (CLI·웹) |
| petclinic: DAO 0개 | Spring Data 저장소 인터페이스는 @Repository가 없음 | `JpaRepository<…>`를 상속하면 DAO로 봄 |
| petclinic: 화면이 안 이어짐 | 뷰 이름을 상수로 반환 (`return VIEWS_OWNER_FORM;`), 화면이 JSP가 아니라 Thymeleaf 템플릿 | 클래스 문자열 상수를 반환값으로 풀고, `templates/<뷰>.html`로 연결 |
| petclinic: `/owners/new`가 "부르는 곳 없음" 오탐 | Thymeleaf 링크 식 `th:href="@{/owners/new}"`를 못 읽음 | `@{…}` 링크 인식 |
| jpetstore: "부르는 곳이 없는 SQL" 4건 오탐 | Maven 사이트 문서(`src/site/**`)의 설명용 예제 Mapper를 실제 코드로 분석 | `src/site` 건너뜀 |
| jpetstore: URL 10건 "부르는 곳 없음" 오탐 | `${pageContext.request.contextPath}/catalog/viewCategory?…`처럼 `.do` 없는 JSP 링크를 못 읽음 | contextPath 뒤 경로 인식 |
| jpetstore: `/`가 "부르는 곳 없음" | 첫 화면은 링크로 부르지 않음 | 루트 URL은 후보에서 뺌 |
| petclinic: 질문 0개, 아무 설명 없음 | 분기·TODO가 거의 없는 깔끔한 예제 | 0개일 때 이유를 알려 주는 안내 문구 |

## 고친 뒤 결과

**spring-petclinic**
- `/owners/{ownerId}/edit` → `OwnerController.processUpdateOwnerForm` → `MERGE JPA Owner` (테이블 OWNERS) → 화면 `templates/owners/createOrUpdateOwnerForm.html`
- 정리 후보 0건 (전에는 URL 2건 오탐)

**mybatis/jpetstore-6**
- 매퍼 인터페이스 호출 7종(AccountMapper 등)이 XML 쿼리 25개와 모두 이어짐, 부르는 곳이 없는 SQL 0건 (전에는 문서 예제 4건 오탐)
- 부르는 곳을 못 찾은 URL: 10건 → 1건. 남은 `/cart/checkout`은 실제로 화면에서 부르지 않는다 (장바구니 화면은 주문서로 바로 넘어감). 사람이 확인할 만한 진짜 후보다
- 질문 2개: `OrderController.newOrder`의 복잡한 분기(순환 복잡도 17), `CartController.updateCartQuantities`가 `NumberFormatException`을 잡고 무시하는 곳

**기존 예제 (회귀 확인)**
- eGovFrame: URL→SQL 83.3%, 질문 99개, 정리 후보 수 그대로
- 새 기능 덕분에 eGovFrame에서 JDBC로 직접 쓰는 SQL 7개(`SmsBasicDAO` 등)를 새로 찾음

## 아직 못 해 본 것

- 실제 SI 사내 코드 (보안상 불가). 공개된 전자정부 2.x iBatis 프로젝트는 테스트용 예제로만 확인했다.
- 사람(주니어 개발자)이 직접 써 보는 사용성 시험.
