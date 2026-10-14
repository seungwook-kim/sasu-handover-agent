# 사수 웹 화면 디자인 (리디자인 확정안)

- 회의: 2026-10-14, 7명(UX 리드·비주얼·데이터 시각화·프론트·SI 현장 도메인·UX 라이터·PO) — 1·2라운드 글, 토론 1~3, 회의실 실시간 토론, PO 목적 판정
- 회의 기록(라운드별 글·토론·회의실 기록)은 작업용 폴더에 두었고, 이 문서는 그 결론만 담는다. 구현은 `src/sasu/web/static/`.
- 판정 기준: docs/prd.md의 목표 G1~G5, 시나리오 S1~S5, 비목표. 모든 결정에 돕는 목표를 붙였다.

## 0. 바꿀 수 없는 제약
1. 인터넷 없이 동작. CDN·외부 링크 금지. 자산은 `src/sasu/web/static/` 안에.
2. 재배포 가능한 라이선스만(OFL·ISC·MIT), 라이선스 파일 동봉 (`static/licenses/`).
3. 빌드 없음. 순수 HTML·CSS·JS. app.js의 `h()` 구조 유지. 최신 CSS 금지: `@layer`, 중첩, `:has()`, `color-mix()`, `light-dark()`, 컨테이너 쿼리, popover. 기준선 Chrome/Edge 87.
4. 웹 API 모양과 동작은 그대로. 바꿀 수 있는 서버 쪽은 **표시 문자열만**(사회자 결정 1): 질문 종류 이름 `KIND_LABEL`(questions.py), 정리 제목 `_UNUSED_TITLES`(server.py와 cli.py 사본), 메시지 초안 `draft_message`(handover.py), 엑셀 상태 표기(handover_sheet.py), CLI 도움말 문구. 그리고 server.py에 `mimetypes.add_type("font/woff2", ".woff2")` 한 줄.
   - 메시지 초안의 `■ 질문`·`■ 코드:` 머리표는 남긴다(테스트와 코드 줄 토글이 쓴다).
   - unused.py·questions.py가 로직에 쓰는 `"죽은 코드"` 문자열은 **바꾸지 않는다.** 화면에서만 "(URL에서 이어지는 경로를 못 찾음)"으로 바꿔 보여 준다.
5. 밝게/어둡게 두 테마. 기존 `prefers-color-scheme` + `data-theme` 방식 유지, `<head>` 앞쪽에서 저장된 테마를 먼저 적용(깜빡임 방지), `color-scheme` 지정.
6. 해시 화면 id는 그대로: `overview`·`questions`·`trace`·`ask`·`cleanup` (북마크 유지).

## 1. 브랜드
- 인상: "옆자리에서 이 줄을 짚어 주는 차분한 선배". 남색 강조색 하나 + 푸른 기 무채색. 빨강은 진짜 위험(없는 SQL 호출)에만. 이모지·◐·▸▾·🎉·✉ 쓰지 않음(아이콘으로).
- 로고 A안("사" 선 모노그램) — `static/favicon.svg`와 같은 선 모양. 머리글: 마크 24px + "사수" 18px/700. `static/favicon.svg`(타일형, 같은 절 SVG). PO 판정: 로고는 곁가지라 맨 마지막에, 밀리면 먼저 뺀다.
- 그림·일러스트 없음. 빈 상태는 Lucide 아이콘 40px(muted) + 한 줄 제목 + 누를 수 있는 실제 예시.

## 2. 디자인 토큰 (밝게 / 어둡게)
| 토큰 | 밝게 | 어둡게 |
|---|---|---|
| --bg | #f5f6f8 | #0e1116 |
| --panel | #ffffff | #151920 |
| --panel-2 | #eef1f5 | #1c212a |
| --line | #dfe3ea | #2a303b |
| --control (입력·체크 테두리, 3:1) | #8a93a4 | #69738a |
| --text | #151a24 | #e6e9ef |
| --muted | #586172 | #9ba4b4 |
| --accent | #2c44c2 | #94a4ff |
| --accent-hover | #2337a6 | #aab7ff |
| --on-accent | #ffffff | #0b1022 |
| --accent-soft | #eceffd | #1e2546 |
| --ok / --ok-soft | #17763f / #e5f4eb | #5cc58a / #12291c |
| --warn / --warn-soft | #9a4f06 / #fdf0e0 | #f0a54e / #2f2315 |
| --bad / --bad-soft | #c0271f / #fdebea | #ff8077 / #3a1b1a |
| --code-bg | #f7f8fa | #0b0e12 |
| --hl-line (짚은 줄) | #fff1b0 | #3b3414 |
| --scrim | rgb(15 20 30 / .32) | rgb(0 0 0 / .55) |
| --ring | = --accent, 2px + offset 2px | 같음 |
| **근거 등급 표식**(아이콘·왼쪽 띠·요약 막대에만, 글자에는 안 씀) | | |
| --g-fact (사실) | #14936a | #199e70 |
| --g-answer (답변) | #c056c8 | #b75bb8 |
| --g-guess (추정) | #d9561f | #d95926 |
| **순서형**(URL 연결률 막대 3칸, 연한→진한) | #aab3c2 / #6f7a8f / #3a4458 | #4a5468 / #7d879a / #b8c0cd |

- 등급 색 검증(데이터 시각화 검증기, OKLab·Machado 2009): 모든 쌍 색각 이상 ΔE 8 이상·정상 15 이상, 강조색과 정상 15 이상, 표면 대비 3:1 이상 — 통과.
- 지금 화면의 AA 미달 5곳(파란·초록·주황 칩, 표 머리 글자, 입력 테두리)은 위 토큰으로 풀린다. 옅은 색 바탕 위 색 글자는 쓰지 않는다.

## 3. 글자·간격
- 글꼴: Pretendard 1.3.9 정적 subset **Regular(400)·Bold(700) 두 개만** (`Pretendard-Regular.subset.woff2` 267,096B, `Pretendard-Bold.subset.woff2` 270,784B, OFL-1.1). `@font-face`: `src: local("Pretendard Regular"), url(...)`, `font-display: fallback`, Regular만 preload. 600을 쓰지 말고 400/700만 쓴다.
- `--sans: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif;`
- `--mono: "D2Coding", Consolas, "Malgun Gothic", monospace;` 고정폭 글꼴은 번들하지 않음. 코드에 `font-variant-ligatures: none`.
- 크기 (html 100%, 1rem = 16px, 모두 rem):
  - 읽는 글(설명문·사수의 답 문장·메시지 초안·묻는 이유) 15px = .9375rem
  - 펼친 질문 문장·카드 제목 16px = 1rem
  - 표·트리·입력칸·접힌 질문 행 14px = .875rem
  - `파일:줄`·칩 12px = .75rem (12px에 --muted를 겹치지 않음)
  - 화면 제목 20px = 1.25rem / 700, 통계 큰 숫자 28px = 1.75rem / 700
  - 머리글 "글자 크게" 토글 = `html{font-size:125%}` (회의실·빔프로젝터용, localStorage 기억)
  - 인쇄 본문 10pt 이상
- `tabular-nums`는 표의 숫자 열에만(큰 통계 숫자에는 쓰지 않음). 한국어 본문 `word-break: keep-all`. 경로·식별자는 `.`·`/` 뒤에서만 줄바꿈(`<wbr>` 또는 `overflow-wrap:anywhere`는 `.loc`에만).
- 간격 2·4·8·12·16·24·32. 컨트롤 높이 28/32/40. 표 행 32px. 접힌 질문 행 36px.
- 모서리: 칩 4px 사각(등급 칩 포함), 버튼·입력·코드 6px, 카드·서랍·모달 8px. 999px는 진행 막대·개수 표시만.
- 그림자: 카드에는 없음(테두리만). 자동완성·토스트만 `0 4px 16px rgb(15 20 30/.12)`, 서랍만 `-12px 0 32px rgb(15 20 30/.18)`.

## 4. 아이콘
- Lucide 1.57.0 (ISC + MIT 일부) — `index.html` `<body>` 첫머리 숨긴 `<svg>` 안 `<symbol id="i-이름">` 스프라이트. app.js에 `icon(name)` 헬퍼(`createElementNS`로 svg+use, `aria-hidden`, `focusable=false`). `stroke=currentColor`. 아이콘만 있는 버튼엔 aria-label.
- 쓰는 이름(1.57.0에서 확인된 이름, 옛 이름 circle-help·alert-triangle·filter는 없음):
  - 탭: 한눈에 보기 `map`, 인수인계 질문 `message-circle-question-mark`, 경로 추적 `route`, 사수에게 묻기 `message-square-quote`, 고도화 준비 `brush-cleaning`
  - 머리글: `folder-git-2`, 테마 `sun`/`moon`, 글자 크게 `a-large-small`(없으면 `type`)
  - 질문: `file-down`(질문지 내려받기), `file-up`(답변 엑셀 올리기), `file-spreadsheet`(기존 동작 목록), `refresh-cw`, `save`, `mail`, `search`, `list-filter`, `user-round`, `ellipsis`
  - 공통: `copy`, `code-xml`, `panel-right-open`, `x`, `chevron-right`/`chevron-down`, `loader-circle`
  - 상태: `triangle-alert`, `circle-alert`, `info`, `unlink`, `circle-check`
  - 등급: 사실 `circle-check`, 답변 `user-round-check`, 추정 `circle-dashed`, 모름 `circle-question-mark`, 물어볼 질문 `message-circle-question-mark`
  - 경로·정리: `link`, `database`, `table-2`, `app-window`, `git-commit-horizontal`, `network`, `files`, `circle-slash`, `corner-down-right`(이동 URL), `repeat`(반복 노드)
  - 질문 종류 아이콘(색 없음): 없는 SQL 호출 `unlink`, 동적 SQL ID `braces`, 미사용 SQL `database`(+ `circle-slash` 대신 하나만), 설정에만 있는 빈 `package`, 업무 코드 분기 `hash`, 주의 주석 `sticky-note`, 복잡한 분기 `git-branch`, 복사한 코드 `copy`, 무시하는 예외 `shield-off`
- 등급 아이콘·트리 C/S/D 배지·질문 종류는 색이 아니라 모양·글자로 구분한다.

## 5. 등급·근거 표시 (G3 — 이 제품의 핵심 약속)
- 등급 칩: 4px 사각, `--control` 1px 외곽선, `--panel` 바탕, 글자 `--text`(대괄호 없이 "사실·답변·추정·모름·물어볼 질문"), 칩 안 아이콘 14px은 등급 색(`--g-*`). 모름 = 회색 실선 외곽선 + 글자 `--text`, 물어볼 질문 = 잉크 외곽선, 색 없음.
- 답 문장: 왼쪽 3px 띠(등급 색), **추정만 점선 띠**. 점선은 추정과 "저장소 밖"에만 쓴다.
- E칩: 보이는 글자 "E1"만, 글자 `--accent`, 안에 12px 아이콘 — 근거 종류 색: 코드·SQL = `--g-fact`, 받은 답변 = `--g-answer`, 주석·커밋 = `--muted`. aria-label "E1 코드·SQL 근거 보기". 근거 카드 머리 "E1 · 코드·SQL · BndtManageDAO.java:43", 역링크 "문장 1·3에서 씀".
- 근거 종류 표시 이름(API level 값 → 화면): 확인된 사실 → **코드·SQL**, 인수인계 답변 → **받은 답변**, 기록 → **주석·커밋**.
- 추정으로 낮춤: 원래 등급을 회색 취소선 칩으로 + 추정 칩, 아래 12px "낮춘 이유: 코드·SQL 근거 없음". 하단 ※ 목록에서는 "추정으로 낮춤" 항목을 빼고 나머지 경고만.
- 모름: 칩 바로 아래에 물어볼 질문 + [이 질문 복사] [메시지 만들기] — 모름이 막다른 길이 아니라 다음 행동이 되게(PO).
- "사수의 답" 머리는 12px + 회색 부제 "근거로 만든 답이에요. 추정·모름은 전임자에게 확인하세요." 큰 굵은 요약·강조 상자·그림자 금지. 모든 문장 같은 크기(PO 위험 ④).
- 범례 한 줄 설명(라이터): 사실 "코드·SQL에서 직접 확인한 내용이에요." / 답변 "전임자가 준 답변에 있는 내용이에요. 기억에 기댄 것이라 코드와 다를 수 있어요." / 추정 "사수가 근거를 보고 짐작한 내용이에요. 고치기 전에 확인하세요." / 모름 "근거가 없어 답하지 않았어요. 아래 질문을 전임자에게 물어보세요." / 물어볼 질문 "전임자에게 그대로 보낼 수 있는 질문이에요."

## 6. 전체 틀
- **머리글 한 줄(약 52px, 고정):** 로고+사수 | 탭 5개(링크 `<a href="#…">` + `aria-current="page"`) | 오른쪽: "분석 기준 10-11 22:36 · 6241e39"(`/api/projects`의 indexed_at·head, 로컬 시각) · 프로젝트 선택 · 글자 크게 · 테마(sun/moon). 1366에서 넘치면 분석 기준은 툴팁으로 접는다. 화면별 필터 막대도 고정.
- 탭(순서·이름): 한눈에 보기(#overview) · 인수인계 질문 + 배지 "미답변 n"(#questions) · 경로 추적(#trace, 부제 "콜 트리") · 사수에게 묻기(#ask) · 고도화 준비(#cleanup). 탭 바꿀 때 `document.title = "탭 이름 · 사수"`. Alt+1~5(입력 중 무시)는 "좋음".
- 콘텐츠 폭 `min(100% - 48px, 1360px)`. 반응형: 1280px 이상 두 열 배치, 860 이하 한 열, 640 이하 위아래로 쌓기·가로 넘침 0(모바일 전용 화면은 만들지 않음).
- 해시 파서: `#trace?url=…` 같은 딥링크를 위해 화면 id와 인자를 나눠 읽는다(뒤로 가기는 pushState). 북마크 id는 그대로.

## 7. 화면별
### 7.1 한눈에 보기 (S1)
- 제목 = 프로젝트 이름, 부제 = 빌드 이름·버전·분석 기준·커밋.
- 첫 줄 **할 일 카드 4개**(누르면 이동, `<a href>`): ① 질문 답변 "먼저 물을 20개 중 n · 전체 99개 중 n" + 막대 → #questions ② URL→SQL 연결 **83%** + 순서형 3칸 막대(DB까지 연결 / 코드에서 끝남 / 컨트롤러에서 끝남, 조각 아래 직접 이름표, 툴팁 "동적 ID로 추정한 연결 포함") ③ 없는 SQL 호출 n → #cleanup ④ 여러 모듈이 함께 쓰는 테이블 n → 하위 탭.
- 그 아래 **등급 범례 한 줄**(칩 5개 + "사수는 코드·SQL·받은 답변만 근거로 답하고, 근거가 없으면 '모름'이라고 말합니다.") — PO 빠진 것 ③.
- 그 아래 **규모 흐름 띠** 한 줄(약 40px): `URL 1,182 → 컨트롤러 164 → 서비스 167 → DAO 151 → SQL 1,248 → 테이블 205 · 화면(JSP) 747 · 모듈 61`. 640 이하에서 2열 격자.
- 하위 탭: **모듈 | 함께 쓰는 테이블 | 기술 스택**.
  - 모듈 표: 열 머리 눌러 정렬(▲▼, `aria-sort`), 정렬 기준 열에만 셀 안 막대(6px, 순서형 가운데 색), 0은 "–" muted, 행 32px, 상위 20행 + "나머지 n개 더 보기", 표 위 찾기 칸, 주요 테이블 mono 칩(공유 테이블이면 앞에 표식, 툴팁 "n개 모듈이 씀"), 누를 수 있는 행 끝 ›. 행을 누르면 **그 행 아래 제자리에** "CRUD (URL × 테이블)" 표 + [이 모듈 기존 동작 목록(AS-IS) 내려받기](`/api/behaviors/export?module=`). 캡션 "58개 표시 · n개 제외".
  - 함께 쓰는 테이블: 테이블 | 쓰는 모듈 수(막대, 최대값 기준) | SQL | 읽기 | 쓰기(C·U·D 글자 칩, 0은 생략) | 모듈 칩(누르면 그 모듈 펼침). 쓰기가 없으면 "이 소스에서는 조회만"(배치·프로시저·연계는 못 봄 — NFR-06).
  - 기술 스택: 무채색 외곽선 mono 칩, 핵심 5줄(빌드·프레임워크·데이터 접근·DB 드라이버·화면) + "라이브러리 n개 더" 접기.

### 7.2 인수인계 질문 (S2, G1·G2)
- 진행: "먼저 물을 20개 중 n개 · 전체 99개 중 n개 답변 완료" + 막대(--ok), 옆에 **[질문지 내려받기]**(묶음으로 물을 길 = 엑셀, PO) + 안내 한 줄: "외부로 보낼 때는 '코드'·'위치' 열을 지우세요. '질문'·'묻는 이유'에도 코드 일부가 남을 수 있으니, 보내기 전에 사내 반출 규정을 확인해 주세요. 열을 지워도 답변은 그대로 가져와요."
- 종류별 작은 진행 막대 목록(아이콘·이름·답변/전체, 막대 길이 = 질문 수, 채움 = 답변 수). 누르면 그 종류로 거름(종류 select와 같은 상태). 0건 종류는 muted.
- 툴바(고정): 상태 분할 버튼 **미답변 n / 답변 완료 n / 전체 n**(`aria-pressed`), 종류 select, 검색(150ms 디바운스, `aria-label`), [질문지 내려받기](`<a download>`), [답변 엑셀 올리기](키보드로 열리는 버튼), [⋯] 메뉴 안 [질문 다시 만들기] + 확인 창 "분석 기준(10-11 22:36)으로 질문을 다시 만들어요. 받은 답변은 그대로예요. 코드가 바뀌었으면 먼저 터미널에서 `sasu index`를 실행하세요."
- 목록: 구분 머리 **먼저 물을 20 · 다음 30 · 나중**. 순위는 서버 순서(`priority DESC, id`)로 자르고 같은 점수로 경계가 갈리면 그 행에 "동점" 표시. 우선순위는 색·신호 막대 없이 구분 머리와 "우선 n위" 글자로만(비목표: 버그 예측).
- **접힌 한 줄 행 36px:** Q번호(mono) · 종류(아이콘+이름, 무채색 칩) · 질문 문장(한 줄 말줄임) · `클래스.메서드:줄` · 상태 칩(미답변/답변 완료/건너뜀) · "작성 중" 표시. 같은 target 질문이 2개 이상이면 묶음 머리(예: `insertVcatnManage · 2`), 묶음 순위는 묶음 안 최고 순위, 진행 개수는 질문 수로 셈. Top 20 밖 질문이 묶음에 섞이면 그 행에 "다음" 표시 유지.
- **제자리 펼침:** 펼치는 단위는 질문 1개 또는 같은 target 묶음 1개. 새 단위를 열면 앞 단위는 접히고, 누른 행의 화면 위치는 고정. 펼친 안: 질문 문장(16px) · 묻는 이유 · 코드 한 줄(pre) · 답변 입력칸 · 답한 사람(기억, 묶음은 칸 하나) · [답변 저장 (Ctrl+Enter)] · [답변 지우기](별도) · [메시지 만들기] · [사수에게 묻기] · `파일:줄`(서랍).
- Ctrl+Enter: 저장 후 다음 미답변 펼침. **빈 칸에서 Ctrl+Enter는 저장하지 않고 다음 질문으로 이동, 상태는 미답변 그대로**(화면 문구 "다음 질문으로"). 빈 값 저장으로 받은 답이 지워지던 버그를 막는다(app.js 425·430).
- 쓰던 답은 `drafts` Map에 보존(검색·필터·접기에도 유지), 접힌 행에 "작성 중".
- 저장 뒤: 토스트 "Q4 답변을 저장했어요. 이제 사수에게 물을 때 근거로 쓰여요." + 행에 [이 코드로 묻기] (G2→G3, PO 빠진 것 ①).
- J/K·↑↓는 목록에 포커스가 있을 때만, `/`는 검색으로(설정에서 끌 수 있게) — "좋음".
- 메시지 만들기(모달): 제목 "Q1 · 전임자에게 보낼 메시지", 체크박스 "코드 한 줄 넣기 (반출 규정 확인 후)" **기본 끔**(끄면 `■ 코드:` 줄 제거), [메시지 복사], [닫기].

### 7.3 경로 추적 (S1·S4, G4)
- 입력: combobox(`role=combobox`, `aria-expanded`, `aria-controls`, `aria-activedescendant`, listbox/option, Esc로 닫기), 체크 "공통 유틸 안쪽도 보기", 최근 추적 5개(localStorage).
- **붙여 넣은 URL 정리**(화면에서만, API 그대로): `new URL(v, location.origin).pathname` → `;jsessionid=` 제거 → 0건이면 첫 경로 조각(컨텍스트 경로)을 떼고 다시 찾기. 규칙: ① 입력이 바뀌면 결과 맨 위에 "붙여 넣은 주소를 `/…`로 바꿔 찾았어요" + 원래 입력을 늘 보여 줌 ② 컨텍스트 경로를 뗀 재시도는 정확히 같은 URL이 있을 때만 자동 추적, 부분 일치면 후보 목록 ③ 쿼리스트링은 버리지 않고 안내에 함께 표시.
- 요약 띠: "메서드 n · SQL n · 읽기 n · 이 소스에서 쓰기 없음(또는 쓰기 n) · 추정 연결 n" + 테이블 칩(쓰기 = 채운 칩, 읽기 = 외곽선, 추정 연결로만 닿은 테이블 = 점선). 칩에 포커스·마우스를 두면 그 테이블을 쓰는 SQL만 강조(나머지 40%).
- 1280 이상 두 열: 트리 65% | "이 URL이 쓰는 테이블" CRUD 35%. 미만은 요약 띠가 위.
- 트리 노드: URL(외곽선 알약) · Controller/Service/DAO는 **무채색 글자 배지 C/S/D**(툴팁에 전체 이름) · 유틸은 빈 원+muted, 같은 부모 아래 연속 유틸은 "유틸 n개"로 접기 · SQL은 `database` 아이콘 + 동사 태그(SELECT 외곽선, 쓰기는 채움) + 다음 줄 테이블 칩 · 화면 `app-window`("파일 없음"이면 위험 아이콘+글자) · 이동 URL `corner-down-right` · 저장소 밖 빈 = 점선 사각 + "저장소 밖 · 빈 'propertiesService'" · 반복 `repeat`(누르면 첫 위치로 이동·깜빡임) · 동적 ID 연결 = 점선 + "추정 연결 · 동적 ID "prefix"+…".
- 행 구조 `grid: auto 1fr auto`, `파일:줄`은 오른쪽 열, 앞부분을 줄인 경로 + 전체는 title. 접힌 노드 "▸ n개 더". 접기 버튼은 `aria-expanded` + 이름("EgovArticleController 아래 3개 펼치기"). `role=tree`는 나중.
- **노드 질문 배지(꼭):** 질문의 `target`이 노드 label과 같으면 무채색 칩 `Q4`·`Q5`(아이콘 `message-circle-question-mark` 12px, 답변 있으면 `check`). 누르면 #questions의 그 질문으로.

### 7.4 사수에게 묻기 (S3, G3)
- 입력: textarea(Ctrl+Enter로 묻기), 라벨 "대상 코드 (선택)" + placeholder "예: EgovBBSController.selectArticleList 또는 /cop/bbs/…do", 체크 "커밋 기록도 근거로 쓰기". 예시 줄 "인수인계 질문에서 골라 묻기:" + 질문만 30자 칩(대상은 title).
- 진행 단계 "① 코드 찾기 → ② 근거 모으기 → ③ 답 쓰기"(live 영역으로 읽힘, 결과 영역 `aria-busy`). [그만두기]는 넣지 않음(서버 LLM 호출은 멈추지 않음).
- "찾은 코드 (다른 코드면 고르세요):" 후보 칩.
- 1280 이상 두 열: 답 7 | 근거 5(sticky). E칩 누르면 오른쪽 근거만 강조(페이지 스크롤 없음).
- 답·근거 표시는 5절. 물어볼 질문 상자 "전임자에게 물을 것" + [이 질문 복사].
- LLM 없음 = 오류가 아닌 **안내 상자**(info 아이콘, 빨강 아님): "사수의 답을 쓰려면 LLM 연결이 필요해요. 근거는 아래에서 그대로 볼 수 있어요. 연결은 터미널에서 `sasu setup`으로 해요." [명령 복사]. LLM 연결 실패(401 등) = 사람 말 문구 + 원래 오류는 [자세히] 안에 접기.

### 7.5 고도화 준비 (S4, G4)
- 맨 위: [기존 동작 목록(AS-IS) 내려받기] + 한 줄 설명 "URL마다 화면·읽고 쓰는 테이블·분기 값 — 고도화 뒤 같은 목록으로 확인".
- 요약 타일 줄(누르면 그 섹션 열림, 막대그래프 아님): 없는 SQL 호출 n(위험 아이콘 "실행 오류 위험") · 미사용 SQL n · 부르는 곳 못 찾은 URL n · 미사용 메서드 n · 미사용 JSP n · 복사한 코드 n묶음 · 같은 SQL n. 개수 배지는 무채색, 위험색은 없는 SQL 호출에만.
- 섹션 머리에 판단을 한 번만: 없는 SQL 호출 "● Mapper에 없음 확인", 나머지 "○ 후보 — 동적 ID·외부 호출은 못 봄". 섞인 섹션만 열로.
- 이름은 `bndtManageDAO.`(muted) + `selectBndtManageListTotCnt`(굵게), `.` 뒤에서만 줄바꿈. 위치는 1줄 `BndtManageDAO.java:43` + 2줄 폴더(muted, 공통 앞부분은 계산해서 생략). 열 너비 이름 30% / 위치 26%.
- 화면 표시만: reason의 "(죽은 코드)" → "(URL에서 이어지는 경로를 못 찾음)".
- 아래 "공통 코드·복사 코드" 섹션: 공통 코드 / 복사한 코드("상속하지 않고 복사한 곳 n") / 같은 SQL.

### 7.6 코드 서랍 (모든 화면)
- 1280px 이상: **덮개 없는 옆 패널** `<aside aria-label="코드 보기">`(1440에서 560px, 1366에서 480px). 본문은 패널 폭만큼 줄고 계속 조작 가능. 열면 패널 제목으로 포커스, Esc로 닫으면 연 자리로 포커스 복귀, 포커스 가두기 없음.
- 1280px 미만: 모달(`role=dialog`, `aria-modal=true`, 이름) + 포커스 가두기 + 닫으면 복귀.
- 머리 "코드 보기 · `파일명:줄`", [위치 복사] [이 코드로 묻기] [닫기]. 짚은 줄 `--hl-line` + 줄 번호 옆 2px `--accent`. "좋음": "이 범위의 질문" 칩(질문 `file_path`·`line`이 서랍 범위 안).
- `파일:줄`(.loc)·E칩·표 행·추천 항목은 모두 `<button>`(모양만 초기화) — 키보드로 열 수 있게.

## 8. 접근성 "꼭" (WCAG 2.2 AA)
- 2.1.1: `.loc`·`.cite`·`tr.click`·추천 항목 → button. 엑셀 올리기는 `.visually-hidden` file input + 키보드로 열리는 버튼.
- 4.1.2: 탭 = 링크 + `aria-current`, 분할 버튼 `aria-pressed`(문자열 "true"/"false" — `h()`가 false 값을 건너뛰므로 문자열로), 검색칸 `aria-label`, 테마 버튼 aria-label "어둡게 보기"/"밝게 보기".
- 4.1.3: 상주 live 영역 `<div id="live" class="visually-hidden" role="status" aria-live="polite">` 하나 — 토스트·"질문 n개"·로딩 문구를 여기에도 넣음(중복 낭독 없게 토스트 요소 자체엔 aria-live 없음). 진행률 `role="progressbar" aria-valuenow`. 토스트 25자 안팎 또는 글자 수에 따라 표시 시간 늘림.
- 2.4.3: 서랍·모달 포커스(7.6). 카드를 다시 그린 뒤에는 같은 버튼에 포커스.
- 2.4.7/2.4.11: `:focus-visible{outline:2px solid var(--ring);outline-offset:2px}` 모든 컨트롤, `html{scroll-padding-top:머리글 높이}`.
- 1.4.3/1.4.11: 2절 토큰. 1.4.1: 색만으로 뜻을 전하지 않음(5절).
- 1.4.13: 추천 목록 Esc로 닫기. 2.5.8: 누름 영역 24px 이상(`.toggle-kids`, E칩).
- `@media (prefers-reduced-motion: reduce)`: 애니메이션·transition·smooth 스크롤 끔. `@media (forced-colors: active)`: 칩에 `border:1px solid`.

## 9. 문구·용어 (라이터 사전 + 토론 3 합의)
- 사람: **전임자**(화면·CLI·메시지). "인계자/인수자"는 화면·출력물 어디에도 쓰지 않음.
- 상태: **미답변 / 답변 완료 / 건너뜀**(화면·엑셀 드롭다운 handover_sheet.py 50행·CLI). 가져오기는 옛 값 "답변함"도 "답변 완료"로 받음(`_LABEL_STATUS` 별칭). DB 값 open/answered/skipped는 그대로.
- 사람이 준 답 = **답변**(답변 저장·답변 고치기), 사수가 만든 답 = **사수의 답**. why = **묻는 이유**(엑셀 열 "이유(코드 근거)" → "묻는 이유"). 엑셀의 "답변·답한 사람·상태·key" 열 이름은 그대로. 화면 입력칸도 "답한 사람".
- 질문 종류 이름(`KIND_LABEL`): broken_sql **없는 SQL 호출**, dynamic_sql **동적 SQL ID**, note **주의 주석**, magic_value **업무 코드 분기**, external_bean **설정에만 있는 빈**, empty_catch **무시하는 예외**, copied_code **복사한 코드**, complex **복잡한 분기**, unused_sql **미사용 SQL**.
- 정리 제목(`_UNUSED_TITLES`, server.py + cli.py 사본): broken_sql 없는 SQL 호출 / sql 미사용 SQL / url 부르는 곳 못 찾은 URL / method 미사용 메서드 / jsp 미사용 JSP.
- URL 분류: **DB까지 연결 / 코드에서 끝남 / 컨트롤러에서 끝남**. 모듈 상세·추적 표 제목 "CRUD (URL × 테이블)". 기존 동작 목록 = "기존 동작 목록(AS-IS)".
- 버튼: 질문지 내려받기 / 답변 엑셀 올리기 / 기존 동작 목록(AS-IS) 내려받기 / 메시지 만들기 / 메시지 복사 / 이 질문 복사 / 사수에게 묻기 / 이 코드로 묻기 / 위치 복사 / 닫기(기호 겹쳐 쓰지 않음).
- 오류·빈 상태 문구는 화면 코드(app.js)에 있는 문구를 따른다(서버 꺼짐 "사수 서버에 연결할 수 없어요. `sasu serve`를 실행한 터미널 창이 열려 있는지 확인하세요." 등). 가져오기 실패: "파일을 열 수 없어요. 문서 보안(DRM)이 걸린 파일일 수 있으니, 사내 보안 담당자에게 처리 방법을 문의해 주세요."
- 위 용어와 다른 문구가 있으면 위 용어가 우선.
- 문체: 화면 설명은 합니다체, 토스트·빈 상태·오류는 해요체, 표 안 데이터 설명만 메모체. 한다체 금지.

## 10. 메시지 초안 (handover.py `draft_message`, CLI와 함께 씀)
```
안녕하세요, {project} 업무를 이어받은 [이름]입니다.
코드 한 곳만 여쭤봐도 될까요?

■ 질문: {question}
■ 위치: {target} ({패키지 끝 두 단계}/{파일명}:{line})
■ 코드: {code}            ← code가 있을 때만. 화면 토글이 끄면 이 줄을 지움
■ 묻는 이유: {why}

한두 줄로 답해 주셔도 충분합니다. 기억나지 않으시면 "모름"이라고만 주셔도 됩니다.
주신 답변은 코드 위치에 기록해 다음 담당자도 볼 수 있게 하겠습니다. 감사합니다.
```

## 11. 인쇄 (꼭, CSS만)
- `@media print`: 밝은 테마 고정, 버튼·입력칸·서랍·머리글 숨김. 인수인계 질문 화면을 표로: 번호·상태·종류·위치·질문·답변·답한 사람·일자. 제목 "인수인계 질문·답변 목록", 머리 한 줄 "프로젝트 · 분석 기준 · 커밋 · 출력일 · 먼저 물을 20개 중 n · 전체 99개 중 n 답변 완료". **서명란·"확인서" 없음**(비목표: 산출물 자동 생성 — PO). 본문 10pt 이상.
- 접힌 행에도 답변 글자가 DOM에 있어야 하고(숨김만), `beforeprint`에서 필터를 "전체"로 바꿨다가 `afterprint`에서 되돌린다.

## 12. 범위
- **꼭:** 2~11절 전부(토큰·글꼴·아이콘·로고는 마지막), 접근성 "꼭", drafts·빈 값 저장 방지, URL 붙여넣기 정리, 트리 노드 질문 배지, Top 20 진행률, 한눈에 보기 범례 한 줄, 답변 저장 뒤 "근거로 쓰여요", 글자 크게 토글, 인쇄.
- **좋음(꼭이 끝난 뒤):** 서랍 "이 범위의 질문" 칩, `/`·Alt+1~5, 최근 추적·묻기 5개, 해시 딥링크.
- **나중:** 묶음 메시지(돌아오는 길과 함께 — PO 보류), Ctrl+K, 첫 방문 안내, 서버 외부 발송용 옵션, role=tree, stale 답변 표시(API 필요), 모바일 전용 배치.
- **뺌:** [그만두기].

## 13. 자산 (내려받아 넣음)
| 파일 | 출처 | 라이선스 | 넣을 곳 |
|---|---|---|---|
| Pretendard-Regular.subset.woff2, Pretendard-Bold.subset.woff2 | github.com/orioncactus/pretendard/releases/download/v1.3.9/Pretendard-1.3.9.zip → `web/static/woff2-subset/` | OFL-1.1 | static/fonts/ |
| OFL-Pretendard.txt | 같은 zip의 LICENSE.txt | — | static/licenses/ |
| Lucide 아이콘(4절 목록) | github.com/lucide-icons/lucide/releases/download/1.57.0/lucide-icons-1.57.0.zip | ISC (+MIT) | index.html 인라인 스프라이트 |
| LICENSE-lucide.txt | raw.githubusercontent.com/lucide-icons/lucide/1.57.0/LICENSE | — | static/licenses/ |
| favicon.svg | 직접 만든 로고 A안 | MIT(저장소) | static/ |
- `.gitattributes`에 `*.woff2 binary`.
