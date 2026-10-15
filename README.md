# 사수 (SASU) · 떠난 개발자 대신 답하는 SI 인수인계 에이전트

떠나는 개발자에게 무엇을 물어야 할지 코드 분석으로 먼저 찾아 주는 SI 인수인계 에이전트입니다. 받은 답변을 코드 위치에 연결해 두고, 신규 투입자의 "왜?"에 근거와 함께 답합니다. 웹 화면은 **지금 어느 단계인지(투입 첫날 · 인수인계 중 · 전임자가 떠난 뒤 · 고도화 준비)를 고르면 그때 할 일 하나만** 보여 줍니다.

**Java(Spring · MyBatis · iBatis · JPA · JSP)와 Python(FastAPI · Flask · Django)** 프로젝트를 분석합니다.

> **상태: 개발 중 (다듬는 단계).** 코드 분석(경로 추적, 프로젝트 지도, 공통화 진단, 불필요한 소스, 기존 동작 목록), 인수인계 질문 생성·엑셀 질문지, RAG 기반 "왜?" 질의응답(일상어 질문 넓히기 포함), 단계별 웹 화면, 에이전트 연결(MCP·Claude Code 플러그인), 설치 꾸러미까지 동작합니다. 테스트 142개. 웹 화면은 AI가 신입·주니어 개발자 페르소나를 연기한 사용성 시험으로 고쳤고, 실제 사람 시험은 아직입니다.

![시작 화면에서 단계 고르기 → 브라우저 주소 붙여 넣어 경로 추적 → 트리의 질문 배지 → 받은 답 적기](docs/images/demo-trace-questions.gif)

문제 정의, 설계 결정과 이유, 결과 수치, 한계를 한 장으로 정리한 [프로젝트 요약](docs/portfolio.md)이 있습니다. 지금까지 무엇이 얼마나 나아졌는지는 [개선 이력](docs/improvements.md)에 숫자로 정리했습니다. 요구사항·설계·데이터 모델은 [PRD](docs/prd.md) · [SDD](docs/sdd.md) · [ERD](docs/erd.md)에 정리했습니다.

## 사용해 보기

설치는 명령 하나입니다 ([uv](https://docs.astral.sh/uv/) 필요). 인터넷이 막힌 PC용 설치 꾸러미(Python 포함)와 사내 LLM 서버 연결은 [설치 안내](docs/install.md)에 있습니다.

```bash
uv tool install "sasu[local-embed,mcp] @ git+https://github.com/seungwook-kim/sasu-handover-agent"
sasu setup                                             # LLM 연결·검색 방식 설정 (질문에 답하면 끝)
sasu doctor                                            # 무엇이 되고 안 되는지 점검
```

```bash
sasu fetch-sample                                      # 예제: 전자정부 표준프레임워크 공통컴포넌트 (~/.sasu/repos)
sasu index ~/.sasu/repos/egovframe-common-components   # 분석 (약 5초, 벡터 색인도 함께)
sasu serve                                             # 웹 화면 (브라우저가 열림)
```

이 저장소에서 개발할 때는 `uv sync --all-extras` 뒤 `uv run sasu ...`로 실행합니다 (`.env`에 `SASU_HOME=data`).

### 단계별 사용법

`sasu serve`로 웹 화면을 열면 시작 화면에서 **지금 어느 단계인지** 고릅니다. 카드마다 할 일 한 줄, 핵심 숫자, 주 버튼 하나가 있고, 누르면 그 단계의 화면으로 갑니다. 마지막으로 고른 단계는 "지난번"으로 표시만 하고 자동으로 넘어가지는 않습니다.

![시작 화면: 투입 첫날 · 인수인계 중 · 전임자가 떠난 뒤 · 고도화 준비 단계 카드](docs/images/web-home.jpg)

| 단계 | 웹 화면에서 하는 일 | 같은 일을 CLI로 |
| --- | --- | --- |
| **투입 첫날** | [화면 경로 보기] → **경로 추적**. 브라우저 주소를 그대로 붙여 넣어도 됩니다(`http://개발서버/egov/cop/bbs/selectArticleList.do?bbsId=…` → 컨텍스트 경로와 쿼리스트링을 떼고 `/cop/bbs/selectArticleList.do`로 찾음). URL 일부나 한글 업무명("게시물")으로도 찾습니다. 결과는 Controller(C)→Service(S)→DAO(D)→SQL→테이블 트리와 "이 URL이 쓰는 테이블" CRUD 표. 프로젝트 전체 구조는 카드의 [프로젝트 지도] 링크로 봅니다 | `sasu trace <URL>` · `sasu map` · `sasu map cop.bbs` |
| **인수인계 중** | [질문 보기] → **인수인계 질문**. "먼저 물을 20개"부터 행을 펼쳐 답한 사람과 답을 적고 [답변 저장 (Ctrl+Enter)]을 누르면 다음 미답변 질문이 열립니다. 답을 못 받았으면 [건너뛰기] + 이유. 엑셀은 [질문지 ▾] 메뉴 하나에 모았습니다(전체 / 보이는 질문만 / 골라서 내려받기, 답변 엑셀 올리기, 질문 다시 만들기). 전임자에게 보낼 메시지는 펼친 질문의 [⋯] → 메시지 만들기 | `sasu questions --export` · `sasu answers` · `sasu answer` · `sasu draft` |
| **전임자가 떠난 뒤** | [사수에게 묻기]. "로그인 5번 틀리면 잠겨?"처럼 일상어로 물으면, 답을 만들기 전에 **"이 코드에 대해 물을까요?"** 카드로 검색이 고른 코드와 다른 후보(고른 이유 한 줄씩)를 보여 줍니다. 맞으면 [이 코드로 묻기], 아니면 후보나 화면(URL)으로 고릅니다. 답은 문장마다 사실·답변·추정·모름 등급과 근거 번호(E1…)가 붙고, 모르면 "전임자에게 물을 것"을 줍니다 | `sasu ask "<질문>"` (`--target`으로 대상 지정) |
| **고도화 준비** | [기존 동작 목록 받기]. "기능 찾기"에 업무명(휴가)이나 모듈(uss.ion)을 넣으면 그 기능의 URL만, 비우면 전체를 엑셀로 받습니다. "고도화 후 확인" 열 채우는 법은 버튼 아래 3줄과 엑셀 '사용법' 시트에 있습니다. 없는 SQL 호출·미사용 코드·복사한 코드는 [정리할 코드 후보 보기] 안에 접어 두었습니다 | `sasu behaviors --export` (모듈 단위까지) · `sasu unused` · `sasu common` |

화면마다 **주 버튼은 하나**이고 나머지는 접어 둡니다(질문 화면의 "종류별로 보기", 경로 추적의 "보기 옵션", 묻기의 "고급"과 "자세히", 고도화 준비의 "정리할 코드 후보"). 기능을 뺀 것이 아니라 처음에 보이는 것을 줄였습니다. 머리글에는 프로젝트 선택과 [보기 설정](밝게·어둡게, 글자 크게)만 있습니다.

어느 화면에서든 `파일:줄`을 누르면 코드가 열립니다(넓은 화면에서는 본문을 가리지 않는 옆 패널). 화면 파일·글꼴·아이콘이 모두 패키지 안에 있고 외부 CDN을 쓰지 않아 인터넷이 막힌 PC에서도 그대로 열립니다. 디자인 결정은 [웹 화면 디자인](docs/ui-design.md)에 정리했습니다.

![경로 추적: /cop/bbs/selectArticleList.do가 거치는 메서드·SQL과 읽는 테이블 8개, 노드에 걸린 인수인계 질문 Q126](docs/images/web-trace.jpg)

![인수인계 질문: 같은 메서드의 질문 2개(휴가 '09' 분기, 복잡한 분기)를 펼쳐 답한 사람과 답을 적는 화면](docs/images/web-questions.jpg)

**개선 이력 (숫자로).** 웹 화면은 AI가 인터넷 자료를 근거로 만든 신입·주니어 개발자 페르소나를 연기하며 실제 브라우저를 조작하는 사용성 시험(시뮬레이션)으로 고쳤습니다. 첫 시험(20명, 할 일 60개)은 성공 58%, 틀렸는데 성공했다고 믿은 경우 11건이었고, 가장 큰 원인은 일상어 질문에 엉뚱한 코드를 고르는 검색과 시작점을 알 수 없는 화면이었습니다.

| 지표 | 처음 | 지금 |
| --- | --- | --- |
| 일상어 질문의 묻기 대상: 상위 6 후보 안에 정답 (16문항) | 25% | 69~81% |
| 버그 증상으로 고칠 메서드 찾기: Recall@10 (173문항) | 51% | 63% |
| 첫 화면 도구 버튼: 인수인계 질문 / 고도화 준비 | 22 / 14 | 8 / 4 |
| 같은 페르소나·같은 할 일: 게시판 목록이 읽는 테이블 찾기 (N01) | 7동작 | 3동작 |
| 같은 페르소나·같은 할 일: 답 못 받은 질문 건너뛰기 (J05) | 포기 | 성공 |
| 시연 GIF 2개 용량 | 6.13MB | 0.67MB |

정리한 화면으로 시험한 4명(할 일 16개)은 시작 화면에서 갈 곳을 16번 모두 헤매지 않았고 성공 11 · 부분 5 · 실패 0이었습니다. 첫 시험과 할 일이 달라 나란히 비교하지는 않습니다. 부분 5개 중 4개였던 "휴가관리 기능만 받기"는 시험 뒤 "기능 찾기"로 고쳤지만 다시 시험하지는 않았습니다. **실제 사람이 아니라 AI가 연기한 결과라 실제 신입보다 끈기 있고 꼼꼼할 수 있습니다.** 실제 사용자 확인은 남은 일입니다. 커밋별 수치, 측정 방법, 다시 재지 않은 것은 [개선 이력](docs/improvements.md)에 정리했습니다.

분석 결과는 프로젝트마다 `~/.sasu/projects/<폴더 이름>.db`에 따로 저장됩니다. 마지막으로 분석한 프로젝트가 지금 프로젝트가 되고, 인수인계 질문과 답은 다른 프로젝트를 분석해도 섞이거나 지워지지 않습니다.

### 명령 모음

```bash
sasu map                                               # 프로젝트 지도: 기술 스택, 모듈, 핵심 테이블
sasu map cop.bbs                                       # 모듈 하나: URL마다 읽고 쓰는 테이블
sasu trace /cop/bbs/selectArticleList.do               # URL 하나의 경로 추적
sasu common                                            # 공통 코드 사용 현황, 복사해 붙인 코드, 중복 SQL
sasu unused                                            # 끊어진 쿼리 참조, 안 쓰는 SQL·URL·메서드·JSP
sasu behaviors --export 기존동작목록.xlsx               # 고도화 전 URL마다 지금 하는 일 (체크리스트)
sasu questions --export 인수인계질문.xlsx                # 인수인계 질문 만들기 + 엑셀 질문지
sasu answers 인수인계질문.xlsx                           # 답을 적은 질문지 가져오기
sasu ask "휴가 신청할 때 연차가 없으면 어떻게 돼?"         # RAG: 근거를 모아 "왜?"에 답하기
sasu answer Q12 "받은 답" --by 김개발                    # 떠난 뒤 연락해서 받은 답 기록
sasu draft Q12                                         # 떠난 개발자에게 보낼 질문 초안
sasu projects                                          # 분석한 프로젝트 목록 (*: 지금 프로젝트)
sasu use full-stack-fastapi-template                   # 다른 프로젝트로 바꾸기
sasu eval-everyday                                     # 일상어 질문으로 묻기 대상 검색 평가
```

### 기존 동작 목록 (고도화 전후 확인)

고도화·리뉴얼에서 흔한 사고는 "원래 되던 것이 조용히 빠지는 것"입니다. `sasu behaviors --export`는 URL마다 지금 하는 일을 한 줄로 적은 엑셀 체크리스트를 만듭니다: 처리 메서드, 화면·이동, 읽는 테이블, **쓰는 테이블(등록·수정·삭제)**, 업무 코드 분기(`'09' ← sTemp.equals("09")`, 크기 비교는 `기준 20`), 주의 주석, 받은 인수인계 답변, 저장소 밖 빈처럼 확인할 연결. 고도화가 끝나면 "고도화 후 확인" 열(같음 / 달라짐(의도) / 달라짐(확인 필요) / 없어짐)을 채웁니다.

eGovFrame 공통컴포넌트에서는 URL 1,182개 중 데이터를 바꾸는 URL 451개, 업무 코드로 나뉘는 URL 134개가 나옵니다 (1초). 웹 화면 "고도화 준비"에서는 업무명(예: 휴가관리)이나 모듈로 골라 받을 수 있습니다. 엑셀에는 주석에서 뽑은 "기능(한글)" 열과, "고도화 후 확인" 열 채우는 법을 적은 "사용법" 시트가 들어갑니다.

### 에이전트에 연결 (MCP)

`sasu mcp`는 MCP 서버입니다. Claude Code·Cursor 같은 에이전트가 경로 추적, 근거 모으기, 인수인계 질문·답 기록을 도구로 씁니다. 에이전트의 LLM이 답을 쓰므로 사수 쪽 LLM 설정 없이도 됩니다.

Claude Code에는 **플러그인**(MCP 서버 + 사수 스킬)으로 한 번에 붙입니다. 스킬이 "코드가 지금 폴더에 없어도 분석해 둔 프로젝트를 먼저 확인하고, 근거를 모아 [사실]/[답변]/[추정]으로 답하라"는 흐름을 알려 줍니다.

```bash
claude plugin marketplace add seungwook-kim/sasu-handover-agent
claude plugin install sasu@sasu
```

도구 목록과 MCP만 붙이는 방법은 [설치 안내](docs/install.md#5-에이전트에-연결-claude-code-플러그인mcp)에 있습니다.

Claude Code에 붙여 "게시판 글 목록 화면은 어떤 테이블을 읽고, 그중 여러 모듈이 같이 쓰는 건?"이라고 물었을 때, 에이전트가 `trace_url`과 `project_overview`를 스스로 골라 불러 테이블 8개와 공유 테이블 3개(COMVNUSERMASTER 24개 모듈 등)를 답했습니다.

### Java 데이터 접근 방식

SI 프로젝트는 세대마다 SQL을 부르는 방식이 다릅니다. 사수는 다음을 모두 테이블까지 잇습니다.

| 방식 | 코드 | 잇는 방법 |
| --- | --- | --- |
| MyBatis (문자열 ID) | `selectList("BBSArticle.selectArticleList", vo)` | Mapper XML의 `namespace.id` |
| iBatis | `list("selectBoardList", vo)` + `<sqlMap>` | 네임스페이스가 없어도 하나뿐인 ID면 잇는다. `<isNotEmpty>`·`<statement>`도 읽음 |
| MyBatis 매퍼 인터페이스 | `memberMapper.selectMembers()` | XML `namespace`가 인터페이스 이름, `id`가 메서드 이름 |
| MyBatis 어노테이션 | `@Select("SELECT … FROM TB_NOTICE")` | 메서드에 붙은 SQL |
| JPA (Spring Data) | `orderRepository.save(o)`, `findByStatus(…)`, `@Query` | `JpaRepository<Order, …>`의 엔티티 → `@Table` 또는 기본 이름(`OrderItem` → `ORDER_ITEM`). 상속 메서드(save·findById·deleteById)는 이름으로 조회·저장·삭제를 정하고, JPQL의 엔티티 이름은 테이블로 바꾼다 |
| JDBC·JPQL 문자열 | `jdbcTemplate.update("INSERT INTO …")`, `em.createQuery("select i from OrderItem i")` | 코드 안의 SQL 모양 문자열 (로그 문구는 거른다) |

eGovFrame에서는 MyBatis 외에 `SmsBasicDAO`가 JDBC로 직접 쓰는 SQL 7개를 새로 찾았습니다.

개발 중에 보지 않은 공개 프로젝트(spring-petclinic: JPA·Thymeleaf, mybatis/jpetstore-6: 매퍼 인터페이스)로 써 보고 막힌 곳 8가지를 고쳤습니다. [처음 보는 프로젝트로 써 보기](docs/field-test.md)에 정리했습니다.

### Python 프로젝트

```bash
sasu fetch-sample --python                             # 예제: FastAPI 풀스택 템플릿
sasu index ~/.sasu/repos/full-stack-fastapi-template
sasu trace "/{settings.API_V1_STR}/users/signup"
```

```
URL /{settings.API_V1_STR}/users/signup
└── users.register_user  backend/app/api/routes/users.py:147
    ├── crud.get_user_by_email  backend/app/crud.py:34
    │   └── SELECT ORM User  backend/app/models.py:52
    │       테이블 USER
    └── crud.create_user  backend/app/crud.py:10
        ├── security.get_password_hash  backend/app/core/security.py:35
        └── INSERT ORM User  backend/app/models.py:52  모델 추정
            테이블 USER
```

| 대상 | 읽는 것 |
| --- | --- |
| URL | FastAPI `@router.get`와 `include_router(prefix=...)`, Flask `@bp.route`와 `register_blueprint(url_prefix=...)`, Django `urlpatterns`·`include()`·클래스 뷰(`get`/`post`...) |
| 호출 | import 별칭·상대 import, `self.repo = Repo()` 같은 주입, 모듈 함수(`crud.get_user`). 확실하지 않으면 프로젝트에 하나뿐인 이름으로 잇고 "이름으로 추정"이라고 표시 |
| DB | Django ORM(`Post.objects.filter`), SQLAlchemy·SQLModel(`select(User)`, `session.get`, `session.add(obj)`), 코드 안의 SQL 문자열 → 테이블 (`__tablename__`, `Meta.db_table`, Django 기본 `앱_모델`) |
| 화면 | `render_template`, `render`, `TemplateResponse`의 템플릿 파일 |
| 질문 | TODO·주의 주석, 드문 값 분기(`status == "partial_refund"`처럼 Python은 소문자 코드 값도), 복잡한 분기, 복사한 코드 |
| 용어 | 모델 필드의 `verbose_name`·`description`·`comment`를 검색 용어집으로 |

소스 루트(`pyproject.toml`·`manage.py`가 있는 폴더, `src/`)를 찾아 import 경로와 파일을 맞춥니다. `tests`·`migrations`·가상환경 폴더는 건너뜁니다.

### "왜?" 질의응답 (RAG)

질문에 맞는 코드를 **BM25 + 벡터(sqlite-vec) + 코드 그래프**로 찾고, 코드·SQL·호출 관계·주석과 수정 이력·인수인계 답변·커밋 기록을 근거로 모아 LLM에 넘깁니다. LLM은 모든 문장을 [사실]/[답변]/[추정]으로 시작하고 근거 번호를 달아야 하며, 코드가 이를 검사해 근거가 약한 문장은 [추정]으로 낮춥니다. 포맷 정리·릴리스 커밋은 건너뛰고 그 줄을 실제로 바꾼 커밋 메시지를 찾습니다.

질문은 한국어 일상어인데 코드는 영어 약어(`EgovVcatnManageController`, `processLoginIncorrect`)라서, 두 가지를 더했습니다.

- **질문 넓히기:** 검색 전에 LLM(claude CLI면 haiku)이 질문을 코드에 나올 말로 넓힙니다("잠겨" → lock, incorrect, 잠금). 프롬프트에는 코드에서 뽑은 "업무명=클래스 조각" 힌트를 줍니다. LLM이 없거나 실패하면 원래 검색 그대로입니다 (`SASU_QUERY_EXPAND`).
- **한글 이름표:** 클래스·메서드 주석에서 업무명("휴가관리")과 메서드 설명("휴가관리정보를 신규로 등록한다")을 뽑아 검색·URL 찾기·질문 목록·엑셀에 붙입니다. 주석에 없으면 비워 두고 지어내지 않습니다 (eGovFrame 컨트롤러 164개 중 156개, URL 1,182개 중 1,153개에 붙음).

![사수에게 묻기: "로그인 5번 틀리면 잠겨?" → 검색이 고른 코드를 확인 → 등급과 근거가 붙은 답(실제 LLM 답)](docs/images/demo-ask.gif)

위 화면에서 사수는 질문을 넓혀(Login, incorrect, lock…) 로그인 실패를 처리하는 `EgovLoginServiceImpl.processLoginIncorrect`를 고르고, 답을 쓰기 전에 그 코드가 맞는지 묻습니다. 답은 "실패 횟수 + 1이 설정값 `getLockCount()` 이상이면 잠근다"를 [사실]로 쓰고, "5번"은 코드에 박혀 있지 않고 설정값이라 **[모름]**으로 남긴 뒤 전임자에게 그 설정값을 물을 질문을 줍니다.

LLM은 OpenAI 호환 주소(개발: OpenRouter, 현장: 사내 LLM 서버) 또는 이 PC에 로그인된 claude CLI(`SASU_LLM_PROVIDER=claude-code`, 개발용)로 연결합니다.

- **검색:** 버그 증상 설명으로 고칠 메서드를 찾는 평가(질문 173개)에서 상위 10개 안에 정답이 있는 비율 63%. 인수인계 답변이 질문과 맞으면 그 답을 단 메서드를 끌어올립니다.
- **일상어 검색:** 사용성 시험에서 나온 일상어 질문 16개("로그인 5번 틀리면 잠겨?" 등)로 묻기 확인 카드에 나오는 상위 6 후보 안에 정답이 있는 비율이 25% → 69~81%(질문 넓히기, 같은 설정으로 3번 잰 범위)로 올랐습니다. 대신 질문당 약 5초가 더 듭니다. 코드에 답이 없는 일반 질문 3개는 모두 "딱 맞는 코드를 못 찾았어요"로 경고했습니다. 질문이 적고 같은 세트로 가중치를 정해 과적합 가능성이 있습니다 (`sasu eval-everyday`).
- **답변:** 커밋 메시지를 숨기고 "이 줄은 왜 이렇게 돼 있나?"를 물었을 때(질문 15개), 실제 이유를 맞힌 답이 사수 7개 vs LLM만 2개, 틀린 이유를 단정한 답이 사수 3개 vs LLM만 8개.

구조, 평가 방법과 한계, 벡터 검색을 섞는 방법을 정한 실험은 [RAG 구조와 평가](docs/rag.md)에 정리했습니다.

### 인수인계 질문

코드만 봐서는 이유를 알 수 없는 곳을 찾아 질문으로 만듭니다. 끊어진 쿼리·동적 쿼리 ID·드문 업무 코드 값 분기(`USR03`, `BBST04`)·TODO와 주의 주석·설정에만 있는 빈·무시하는 예외·복잡한 분기·상속 대신 복사한 코드가 대상입니다. 엑셀 질문지로 내보내 인수인계 회의에서 답을 적고 다시 가져오면, 답은 질문(코드 위치)에 연결되어 다시 분석해도 지워지지 않습니다.

과거 기록으로 채점하면 질문 상위 10개의 30%가 이후 버그 수정이 들어간 메서드를 가리킵니다 (무작위 3%). 다만 버그 위치만 맞히는 데는 복잡도순이 더 강합니다. 방법과 한계는 [4단계 평가](docs/gate4-evaluation.md)에 정리했습니다.

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

eGovFrame 공통컴포넌트(URL 1,182개)에서 URL의 83.3%가 SQL까지 이어집니다. 나머지는 화면만 보여 주는 URL(9.9%)과 LDAP·메시지·검증처럼 DB를 쓰지 않는 URL(6.8%)입니다.

문자열을 더해 만드는 동적 쿼리 ID(`"LoginUsr.updateLoginIncorrect" + userSe`), static 유틸 호출, XML 설정에만 있는 저장소 밖 빈도 따라가거나 표시합니다. 기본 보기는 Controller·Service·DAO만 펼치고, `--full`을 붙이면 유틸 내부까지 모두 펼칩니다. 검증 과정은 [관문 1 검증](docs/gate1-verification.md)에 정리했습니다.

### eGovFrame에서 찾은 것

- **실제 버그 2건:** 당직관리 목록(`/uss/ion/bnt/selectBndtManageList.do`)이 Mapper에 없는 개수 쿼리를 부르고, 인쇄 출력 DAO는 네임스페이스 오타(`PrntngOutptDA`)로 쿼리를 찾을 수 없습니다.
- **사람의 메모와 일치:** `BBSArticle.selectMaxNttId`를 안 쓰는 SQL로 찾았고, Mapper 수정 이력의 "selectMaxNttId문은 실제 사용하지 않음"을 근거로 붙입니다.
- **상속 대신 복사:** 보안 조치로 공통 부모 클래스에 넣은 권한 검사(`egovAssertAdminOrOwner`)를 47개 컨트롤러가 복사해 씁니다.

검증과 고친 오탐은 [3단계 검증](docs/gate3-verification.md)에 정리했습니다.

## 구조

```mermaid
flowchart LR
    subgraph 입력
        SRC["Java·Python 소스"]
        SQLX["MyBatis·iBatis XML"]
        VIEW["JSP·템플릿·설정"]
        GIT["git 기록"]
    end
    SRC & SQLX & VIEW --> PARSE["파서<br/>tree-sitter·정규식"]
    PARSE --> GRAPH[("코드 그래프 · SQLite<br/>URL → 메서드 → SQL → 테이블")]
    GRAPH --> QGEN["질문 생성<br/>끊어진 쿼리·업무 코드 분기·TODO"]
    QGEN --> SHEET["엑셀 질문지"] --> ANS["떠나는 개발자의 답"]
    ANS --> GRAPH
    GRAPH --> SEARCH["검색<br/>BM25 + 벡터 + 그래프 + 답변"]
    SEARCH --> EVID["근거 모으기<br/>확인된 사실 · 인수인계 답변 · 기록"]
    GIT --> EVID
    EVID --> LLM["LLM<br/>사내 서버 또는 claude"] --> CHECK["등급 검사<br/>[사실] [답변] [추정]"]
    GRAPH --> OUT["CLI · 웹 화면 · MCP · Claude Code 플러그인<br/>지도 · 경로 추적 · 정리 후보 · 기존 동작 목록"]
```

확정할 수 있는 것(호출 관계, SQL, 테이블, 참조 여부)은 정적 분석이 정하고, LLM은 모은 근거로 설명만 합니다. LLM이 쓴 문장은 코드가 다시 검사해, 근거와 등급이 맞지 않으면 [추정]으로 낮춥니다.

## 왜 만드나

SI 고도화·유지보수 프로젝트에서 인수인계는 보통 1주 정도입니다. 문서와 말로 설명을 듣고 질문할 기회는 있지만, **무엇을 물어봐야 할지 모르는 채로** 기간이 끝납니다. 기존 개발자가 떠난 뒤에야 처음 보는 로직과 버그를 만나고, 결국 다시 연락하게 됩니다.

코드는 "무엇을 하는지"는 보여 주지만 "왜 그렇게 했는지"는 보여 주지 않습니다. 그 "왜"는 떠나는 개발자의 머릿속에 있고, 인수인계 기간이 그것을 붙잡을 수 있는 유일한 때입니다.

## 사수가 돕는 네 순간

웹 화면 시작 화면의 단계 카드와 같은 순서입니다.

| 순간 | 사수가 하는 일 |
| --- | --- |
| 투입 첫날 | 맡은 화면(URL)이 어떤 코드·SQL·테이블을 거치는지 **경로 추적**과 프로젝트 지도로 보여 준다 |
| 인수인계 중 | 코드 위험 분석으로 **"먼저 물을 질문 20개"**를 만들고, 떠나는 개발자의 짧은 답변을 **해당 코드 위치에 연결**해 저장한다 |
| 전임자가 떠난 뒤 | 신규 투입자의 "왜?"에 코드 근거와 인수인계 답변으로 답한다. 답이 없으면 떠난 개발자에게 보낼 질문 초안을 만들고, 받은 답도 저장한다 |
| 고도화 준비 | 바꾸기 전 동작을 URL마다 **기존 동작 목록** 엑셀로 남기고, 없는 SQL 호출·안 쓰는 코드·복사한 코드를 정리 후보로 보여 준다 |

## 설계 원칙

- **확정할 수 있는 건 정적 분석으로 확정한다.** 호출 관계, 참조 여부, 위험 패턴은 코드 분석으로 찾고, LLM은 설명과 질의응답만 맡는다.
- **답변의 근거를 구분해 표시한다.** 모든 답에 "확인된 사실 / 인수인계자 답변 / 추정"을 붙이고, 근거가 없으면 "모름"이라고 답한다.
- **LLM은 주소 하나로 연결한다.** OpenAI 호환 주소만 바꾸면 개발용 API에서 사내 LLM 서버(키 없이 여는 서버 포함)로 옮겨 갈 수 있다. LLM이 없어도 분석·질문·근거 모으기는 모두 된다.
- **사람의 답이 가장 값진 근거다.** 인수인계 답변은 검색에서 끌어올리고 [답변]으로 인용하되, 코드와 어긋나면 그 점을 짚는다.
- **가볍게 돈다.** 서버 없는 파일형 DB를 쓰고, 개발 PC에서 모델을 직접 돌리지 않는다.

## 기술 구성

| 역할 | 도구 |
| --- | --- |
| 코드 분석 | tree-sitter (Java, Python), MyBatis XML 파서 |
| 엑셀 | openpyxl (인수인계 질문지, 기존 동작 목록) |
| 저장 | SQLite + sqlite-vec (벡터 색인) |
| 검색 | BM25(한국어 글자 2-gram) + 벡터 + 코드 그래프 전파 |
| 임베딩 | fastembed 로컬 모델 또는 OpenAI 호환 `/embeddings` |
| LLM | OpenAI 호환 API (개발: OpenRouter / 현장: 사내 LLM 서버) |
| API · 화면 | FastAPI, 웹 화면(CDN 없는 HTML·JS), MCP 서버 |
| 평가 | git(archive·log·blame)으로 공개 프로젝트의 과거 시점을 재현해 채점 |
| 설치 | uv tool, 오프라인 꾸러미(휠·Python·임베딩 모델), `sasu setup`·`sasu doctor` |

## 평가 방법

실제 회사 코드 대신 **공개 프로젝트의 과거 기록**을 정답지로 씁니다.

1. **질문 생성:** 과거 시점의 코드로 질문 Top 20을 만들고, 이후 실제로 버그가 수정된 위치를 얼마나 미리 짚었는지 채점합니다. 무작위 선택, 복잡도 순 선택과 비교합니다.
2. **"왜?" 답변:** 커밋 메시지를 숨긴 채 "이 줄은 왜 이렇게 돼 있나"를 묻고, 숨긴 메시지와 비교합니다. 근거가 없을 때 추정이라고 표시하는지도 봅니다.

"나중에 버그가 난 곳 = 인수인계 때 물어봤어야 할 곳"은 완벽한 등식이 아니므로, 1번은 참고 지표로 다룹니다.

분석·평가 대상은 [전자정부 표준프레임워크 공통컴포넌트](https://github.com/eGovFramework/egovframe-common-components)(Java · Spring · MyBatis · JSP, Apache-2.0)입니다.

## 진행 단계

날짜 대신 관문을 통과해야 다음 단계로 넘어갑니다.

- [x] 준비: 대상 프로젝트를 사수 없이 직접 읽고 기능 3개의 흐름 정답 메모 작성
- [x] 기반 그래프: Java + MyBatis 연결 (관문 1: 정답 메모의 흐름이 그대로 나옴 → [검증 기록](docs/gate1-verification.md))
- [x] 보조 기능: 지도, 공통화, 불필요한 소스 → [검증 기록](docs/gate3-verification.md)
- [x] 질문 생성 → [과거 기록 평가](docs/gate4-evaluation.md)
- [x] 답변 연결 + "왜?" 질의응답 (관문 2: 폴더 하나로 질문 → 답 입력 → "왜?" 답변까지) → [RAG 구조와 평가](docs/rag.md)
- [x] 평가와 개선 (관문 3: 비교표 완성, 약한 결과도 기록)
- [x] Python 프로젝트 지원 (FastAPI · Flask · Django), 프로젝트별 분석 파일
- [x] 다듬기: 웹 화면, 엑셀 내보내기·가져오기
- [x] 설치: `uv tool install`, 인터넷이 막힌 PC용 꾸러미(Python·모델 포함), `sasu setup`·`sasu doctor`
- [x] 사용 흐름 점검: 답 기록 → 말로 묻기 → [답변] 근거로 답하기 (실제 LLM), 답과 코드가 어긋나면 짚기
- [x] 확장: MCP 서버 (도구 10개), Claude Code 플러그인 (사수 스킬 + MCP)
- [x] 확장: 기존 동작 목록 (고도화 전후 확인 체크리스트)
- [x] 사용성 시험(AI가 연기한 페르소나 20명 → 고친 뒤 4명)과 반영: 묻기 전 대상 확인, 한글 업무명, 건너뛰기, 일상어 질문 넓히기, 단계별 시작 화면

## 라이선스

MIT
