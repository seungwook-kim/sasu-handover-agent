"""인수인계 문서(전임자가 남긴 문서)를 읽어 근거로 쓴다.

- readers: 형식별로 글자와 제목 구조를 꺼낸다 (md·txt·docx·xlsx·pptx·hwpx는 표준 라이브러리·openpyxl,
  pdf는 pypdf, hwp 5.0은 olefile — 둘은 선택 의존성 `docs`).
- redact: 넣기 전에 비밀(비밀번호·키·토큰·주민번호·카드번호·IP·접속 문자열의 비밀번호)을 가린다.
- chunker: 제목 단위로 자르고, 길면 문단으로 나눈다. 문서 날짜를 정한다.
- linking: 조각에 나온 URL·테이블·클래스·메서드·업무명을 코드와 잇는다 (이름으로 저장해 다시 분석해도 남는다).
- store: 분석 파일의 documents·doc_chunks·doc_links 테이블 (questions처럼 다시 분석해도 지우지 않는다).
- search: 조각 검색 (BM25 + 벡터).
- conflicts: 문서와 코드가 다른 값을 말하는 곳 (규칙 기반) → '문서 확인' 질문.
- judge: 규칙이 못 보는 불일치(설정 값·상태값 뜻·동작)를 LLM으로 판정 (선택: --llm-judge·SASU_DOC_JUDGE=on, 결과 캐시).
- configs: 설정 파일 키=값 (설정 키·파일 이름 연결과 LLM 판정 근거).
- evidence: 묻기 근거에 [문서] 등급으로 붙인다.
"""
