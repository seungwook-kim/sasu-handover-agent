# 사수 설치

필요한 것: Python 3.12 이상, git(커밋 기록을 근거로 쓸 때). GPU는 필요 없습니다.

## 1. 인터넷이 되는 PC

[uv](https://docs.astral.sh/uv/)로 명령어 하나만 설치합니다.

```bash
uv tool install "sasu[local-embed,mcp] @ git+https://github.com/seungwook-kim/sasu-handover-agent"
sasu setup      # LLM 연결·검색 방식을 묻고 설정 파일을 만든다
sasu doctor     # 무엇이 되고 안 되는지 점검
sasu index C:\work\my-project
sasu serve      # 브라우저로 화면 열기
```

벡터 검색이 필요 없으면 `local-embed`를, 에이전트 연결이 필요 없으면 `mcp`를 빼세요. `local-embed`를 빼면 설치가 가벼워지고, 검색은 BM25와 코드 그래프로 합니다. 인수인계 문서 중 PDF·HWP를 읽거나 인수인계서를 Word로 받으려면 `docs`·`docx`를 더합니다 ([8절](#8-선택-기능-extra)).

## 2. 인터넷이 막힌 PC

인터넷이 되는 PC(**설치할 PC와 같은 OS·Python 버전**)에서 꾸러미를 만들어 옮깁니다.

```bash
git clone https://github.com/seungwook-kim/sasu-handover-agent
cd sasu-handover-agent
uv run python scripts/make_offline_bundle.py --local-embed
# → dist/sasu-offline-windows-py312.zip (모델 파일 포함)
```

설치할 PC에서 압축을 풀고:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1   # Windows
sh install.sh                                          # Linux·macOS
```

`~/.sasu/app`에 설치되고, 임베딩 모델 파일은 `~/.sasu/models`로 복사됩니다. 설치 중 인터넷에 접속하지 않습니다 (`pip --no-index`).

## 3. LLM 연결

사수는 LLM을 **OpenAI 호환 주소 하나**로 연결합니다. 사내 LLM 서버(vLLM, Ollama, TGI 등 OpenAI 호환 API를 여는 서버)가 있으면 그 주소를 넣으면 됩니다.

```bash
sasu setup --llm openai --base-url http://llm.사내주소:8000/v1 --api-key none --model qwen2.5-32b-instruct
sasu doctor --ping    # 실제로 짧은 요청을 보내 연결 확인
```

LLM이 없어도 분석·질문 생성·경로 추적·근거 모으기는 모두 됩니다. "사수에게 묻기"에서 근거만 보여 주고 답 문장을 만들지 않을 뿐입니다.

**질문 넓히기 (선택).** LLM이 연결돼 있으면 "사수에게 묻기"가 검색 전에 일상어 질문("로그인 5번 틀리면 잠겨?")을 코드에 나올 말(login, incorrect, lock…)로 넓혀 맞는 코드를 더 잘 찾습니다. 질문마다 짧은 LLM 호출이 한 번 더 들어 claude CLI면 약 5초, Claude API면 약 1.5초가 늘고(재순위를 켜면 약 1.9초 더), 같은 질문은 `~/.sasu/cache/query-expand`에 저장해 다시 부르지 않습니다. 실패하면 조용히 원래 검색으로 돌아갑니다.

| 환경 변수 | 기본값 | 뜻 |
| --- | --- | --- |
| `SASU_QUERY_EXPAND` | `auto` | `auto`(LLM이 설정돼 있으면 켬) · `on` · `off`. 사내 LLM 서버가 느리거나 호출 수를 줄여야 하면 `off` |
| `SASU_EXPAND_MODEL` | claude CLI: `haiku` / Claude API: `claude-haiku-5-5` / OpenAI 호환: `SASU_LLM_MODEL`과 같음 | 넓히기에 쓸 모델. 사내 서버에 작고 빠른 모델이 따로 있으면 그 이름을 넣으세요 |
| `SASU_EXPAND_PROVIDER` | `auto` | 넓히기(와 재순위)만 다른 공급자로: `auto`(`SASU_LLM_PROVIDER`와 같게) · `anthropic` · `claude-code` · `openai`. 개발 PC에서 답변은 claude CLI로, 넓히기는 `anthropic`으로 두면 넓히기가 5.17초 → 1.46초 (같은 질문 10개 평균) |
| `SASU_RERANK` | `auto` | LLM 재순위: 검색 상위 후보를 넓히기와 같은 빠른 LLM이 다시 골라 6개를 둔다. `auto`(넓히기 공급자가 Claude API일 때만 켬) · `llm`(공급자와 상관없이 켬) · `off`. 질문당 호출 1번, 약 +1.9초 |
| `SASU_RERANK_POOL` | `40` | 재순위에 보여 줄 후보 수 (6~60, 같은 클래스는 3개까지) |

`~/.sasu/.env`에 `SASU_QUERY_EXPAND=off`처럼 한 줄을 더하면 됩니다. 넓히기 결과는 `~/.sasu/cache/query-expand`, 재순위 결과는 `~/.sasu/cache/rerank`에 질문 해시로 둡니다. 넓히기 LLM은 같은 호출에서 "일반 개념 질문인지"도 판정해, 코드에 답이 없는 질문에 "딱 맞는 코드를 못 찾았어요" 경고를 띄웁니다.

**Claude API (개발용, 선택).** 인터넷이 되는 개발 PC에서는 Claude API를 직접 쓸 수 있습니다. `uv sync --extra anthropic`(전역 설치는 `sasu[anthropic]`)으로 공식 SDK를 설치하고 `.env`에 아래를 넣습니다. 키 값은 문서·화면·로그에 남기지 않습니다(설정 객체의 repr에서도 뺌). 키 이름을 `ANTHROPIC_API_KEY`로 바꾸지 마세요: 사수는 키를 SDK에 직접 넘기고 그 이름으로 내보내지 않는데, 같은 PC의 claude CLI가 `ANTHROPIC_API_KEY`를 보면 구독 로그인 대신 API 요금으로 바뀌기 때문입니다.

| 환경 변수 | 기본값 | 뜻 |
| --- | --- | --- |
| `SASU_ANTHROPIC_API_KEY` | (없음) | Claude API 키. 사수가 SDK에 직접 넘기고 출력·기록하지 않습니다 |
| `SASU_LLM_PROVIDER` | `openai` | `anthropic`이면 답변도 Claude API로 (모델은 `SASU_LLM_MODEL`, 비우면 `claude-opus-5-5`, `sonnet`·`haiku`처럼 짧게 써도 됨) |

- **temperature를 보내지 않습니다.** Claude Haiku 5.5·Sonnet 5.5·Opus 5.5는 기본값이 아닌 temperature·top_p·top_k를 400으로 거절합니다. 넓히기 출력의 흔들림은 프롬프트와 캐시로 줄이고, 평가는 캐시 없이 5번씩 재서 흔들림을 함께 적습니다.
- 넓히기·재순위는 `claude-haiku-5-5`에 effort low로 부릅니다. `sasu doctor`가 "Claude API · 모델"과 고치는 법(키·SDK)을 보여 줍니다.
- 웹 화면(`sasu serve`)은 묻기 검색 색인과 넓히기를 메모리에 두고, 분석 파일이 바뀌면(다시 분석·답변 저장·프로젝트 바꾸기) 다시 만듭니다. `.env`를 바꾸면 서버를 다시 켜세요.

**인수인계 문서 불일치 LLM 판정 (선택).** 문서와 코드가 다른 곳은 규칙으로 찾고(LLM 없이), 규칙이 못 보는 설정 값·상태값 뜻·동작 차이는 LLM 판정으로 더할 수 있습니다 ([인수인계 문서](documents.md#7-문서와-코드가-다를-때)).

| 환경 변수 | 기본값 | 뜻 |
| --- | --- | --- |
| `SASU_DOC_JUDGE` | `off` | `on`이면 문서 넣기·불일치 보기에서 LLM 판정도 쓴다 (명령마다 `--llm-judge`로도 켬) |
| `SASU_JUDGE_PROVIDER` | `anthropic` | `anthropic`(Claude API, `SASU_ANTHROPIC_API_KEY`) · `expand`(넓히기와 같은 공급자) |
| `SASU_JUDGE_MODEL` | `claude-haiku-5-5` | 판정 모델. 결과는 `~/.sasu/cache/doc-judge`에 둔다 |

## 4. 저장 위치

| 무엇 | 어디 |
| --- | --- |
| 설정 | `~/.sasu/.env` (`sasu setup`이 만든다. 지금 폴더에 `.env`가 있으면 그것이 먼저) |
| 분석 파일 | `~/.sasu/projects/<프로젝트 이름>.db` — 인수인계 질문과 답, 넣은 인수인계 문서의 조각(비밀을 가린 것)도 여기. 원본 문서 파일은 복사하지 않는다 |
| 임베딩·질문 넓히기·재순위·문서 판정 캐시, 모델 | `~/.sasu/cache`, `~/.sasu/models` |

`SASU_HOME` 환경 변수로 위치를 바꿀 수 있습니다. 인수인계 답과 문서 조각은 분석 파일에 들어 있으니, 다음 담당자에게 넘길 때는 `projects/<이름>.db` 파일을 함께 넘기거나 엑셀 질문지·인수인계서(`sasu handover-doc`)로 내보내세요.

## 5. 에이전트에 연결 (Claude Code 플러그인·MCP)

Claude Code·Cursor 같은 에이전트가 사수를 도구로 씁니다. 답은 에이전트의 LLM이 쓰고, 사수는 분석 결과와 근거를 돌려주므로 사수 쪽 LLM 설정이 없어도 됩니다. 먼저 1번처럼 `sasu` 명령을 설치해 두세요.

**Claude Code: 플러그인 (스킬 + MCP 한 번에, 권장)**

```bash
claude plugin marketplace add seungwook-kim/sasu-handover-agent
claude plugin install sasu@sasu
```

플러그인에는 MCP 서버와 **사수 스킬**이 함께 들어 있습니다. 스킬은 에이전트에게 언제 사수를 쓰고 어떻게 답할지 알려 줍니다: 코드가 지금 폴더에 없어도 분석해 둔 프로젝트를 먼저 확인하기, "왜?"에는 근거를 모아 문장마다 [사실]/[답변]/[추정]과 근거 번호를 붙이기, 받은 답만 기록하기, 정리 후보는 확인 없이 지우지 않기. 인터넷이 막힌 PC에서는 오프라인 꾸러미의 `claude-plugin` 폴더를 `claude plugin marketplace add <그 폴더>`로 추가합니다.

도구 이름 없이 "휴가 신청 처리에서 결과 코드가 09면 왜 따로 처리해?"라고만 물었을 때, 에이전트가 스킬을 따라 `list_projects` → `gather_evidence`를 불러 "09는 연차 연도 체크 실패 코드"라고 코드 근거와 함께 답하고, 이유는 [추정]으로 남긴 채 떠난 개발자에게 물을 질문을 제안했습니다.

**Claude Code: MCP만**

```bash
claude mcp add sasu -- sasu mcp
```

다른 에이전트는 설정 파일에 다음을 넣습니다.

```json
{"mcpServers": {"sasu": {"command": "sasu", "args": ["mcp"]}}}
```

| 도구 | 하는 일 |
| --- | --- |
| `list_projects` | 분석한 프로젝트 목록 |
| `project_overview` | 기술 스택, 모듈별 URL·SQL·테이블, 여러 모듈이 함께 쓰는 테이블 |
| `trace_url` | URL → 메서드 → SQL → 테이블 |
| `search_code` | 질문으로 관련 메서드 찾기 (BM25 + 벡터 + 그래프 + 인수인계 답변) |
| `gather_evidence` | "왜?"의 근거를 등급(확인된 사실·인수인계 답변·기록)과 번호를 붙여 모음 |
| `read_code` | 분석한 저장소 안의 파일 읽기 (저장소 밖은 거부) |
| `handover_questions` | 떠나는 개발자에게 물어야 할 질문 |
| `record_answer` | 받은 답을 질문에 기록 (이후 `gather_evidence`의 [답변] 근거) |
| `find_unused` | 끊어진 쿼리, 부르는 곳이 없는 SQL·URL·메서드·JSP |
| `existing_behaviors` | URL마다 지금 하는 일 (화면, 읽고 쓰는 테이블, 업무 코드 분기, 주석, 인수인계 답변) |

## 6. 개발용 (이 저장소에서 직접)

```bash
uv sync --all-extras
cp .env.example .env    # SASU_HOME=data로 두면 저장소의 data/ 폴더를 쓴다
uv run sasu --help
uv run pytest
```

개발하면서 쓰려면 `uv tool install --editable ".[local-embed,mcp]"`로 설치합니다. 코드를 고치면 `sasu` 명령과 MCP 서버에 바로 반영됩니다. 스킬(`plugin/skills/sasu/SKILL.md`)을 고쳤다면 `plugin/.claude-plugin/plugin.json`의 `version`을 올리고 푸시한 뒤 `claude plugin marketplace update sasu && claude plugin update sasu@sasu`를 실행하세요. 버전이 같으면 새 내용을 받지 않습니다.

## 7. Docker

`Dockerfile`이 있지만 **아직 실제로 빌드해 보지 못했습니다** (개발 PC에 Docker가 없음). 위의 1·2번 방법을 먼저 쓰세요.

```bash
docker build -t sasu .
docker run -p 8765:8765 -v C:\work:/repos -v sasu-data:/root/.sasu sasu
docker exec -it <컨테이너> sasu index /repos/my-project
```

## 8. 선택 기능 (extra)

기본 의존성만으로 분석·질문·웹·OpenAI 호환 LLM·인수인계 문서(md·txt·docx·xlsx·pptx·hwpx)·인수인계서(md)가 됩니다. 나머지는 필요한 것만 더합니다.

| extra | 들어가는 것 | 쓰는 곳 |
| --- | --- | --- |
| `local-embed` | fastembed | 로컬 임베딩으로 벡터 검색 (`SASU_EMBED_PROVIDER=local`) |
| `mcp` | mcp 2.x | `sasu mcp` (Claude Code·Cursor 연결) |
| `docs` | pypdf, olefile | 인수인계 문서 중 PDF와 HWP(한글 5.0) 읽기. 없으면 그 형식만 "설치하세요"라고 알린다 |
| `docx` | python-docx | `sasu handover-doc`의 Word(.docx) 출력, 웹 [인수인계서 만들기 ▾] → Word. 없으면 CLI는 md만 만들고 웹은 이유를 알린다 |
| `anthropic` | anthropic SDK | `SASU_LLM_PROVIDER=anthropic`·`SASU_EXPAND_PROVIDER=anthropic`, 재순위, 문서 불일치 LLM 판정 (개발 PC용) |

```bash
uv tool install "sasu[local-embed,mcp,docs,docx] @ git+https://github.com/seungwook-kim/sasu-handover-agent"   # 전역 설치
uv sync --extra docs --extra docx      # 이 저장소에서 개발할 때 (모두: uv sync --all-extras)
```

인터넷이 막힌 PC용 꾸러미(`scripts/make_offline_bundle.py`)는 `mcp`·`docs`·`docx`를 늘 넣고, `--local-embed`를 주면 `local-embed`도 넣습니다. 그래서 꾸러미로 설치한 PC에서도 PDF·HWP 문서를 읽고 Word 인수인계서를 만들 수 있습니다. `anthropic`(Claude API)은 인터넷이 필요해 넣지 않습니다.
