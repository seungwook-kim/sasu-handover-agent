# 사수 설치

필요한 것: Python 3.12 이상, git(커밋 기록을 근거로 쓸 때). GPU는 필요 없습니다.

## 1. 인터넷이 되는 PC

[uv](https://docs.astral.sh/uv/)로 명령어 하나만 설치합니다.

```bash
uv tool install "sasu[local-embed] @ git+https://github.com/seungwook-kim/sasu-handover-agent"
sasu setup      # LLM 연결·검색 방식을 묻고 설정 파일을 만든다
sasu doctor     # 무엇이 되고 안 되는지 점검
sasu index C:\work\my-project
sasu serve      # 브라우저로 화면 열기
```

벡터 검색이 필요 없으면 `[local-embed]`를 빼세요. 설치가 가벼워지고, 검색은 BM25와 코드 그래프로 합니다.

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

LLM이 없어도 분석·질문 생성·경로 추적·근거 모으기는 모두 됩니다. "물어보기"에서 근거만 보여 주고 답 문장을 만들지 않을 뿐입니다.

## 4. 저장 위치

| 무엇 | 어디 |
| --- | --- |
| 설정 | `~/.sasu/.env` (`sasu setup`이 만든다. 지금 폴더에 `.env`가 있으면 그것이 먼저) |
| 분석 파일 | `~/.sasu/projects/<프로젝트 이름>.db` — 인수인계 질문과 답도 여기 |
| 임베딩 캐시·모델 | `~/.sasu/cache`, `~/.sasu/models` |

`SASU_HOME` 환경 변수로 위치를 바꿀 수 있습니다. 인수인계 답은 분석 파일에 들어 있으니, 다음 담당자에게 넘길 때는 `projects/<이름>.db` 파일을 함께 넘기거나 엑셀 질문지로 내보내세요.

## 5. 개발용 (이 저장소에서 직접)

```bash
uv sync --extra local-embed
cp .env.example .env    # SASU_HOME=data로 두면 저장소의 data/ 폴더를 쓴다
uv run sasu --help
uv run pytest
```

## 6. Docker

`Dockerfile`이 있지만 **아직 실제로 빌드해 보지 못했습니다** (개발 PC에 Docker가 없음). 위의 1·2번 방법을 먼저 쓰세요.

```bash
docker build -t sasu .
docker run -p 8765:8765 -v C:\work:/repos -v sasu-data:/root/.sasu sasu
docker exec -it <컨테이너> sasu index /repos/my-project
```
