# 사수 웹 화면을 컨테이너로 띄운다. 분석할 저장소는 /repos로, 분석 파일은 /root/.sasu 볼륨으로 둔다.
# 주의: 개발 PC에 Docker가 없어 아직 실제로 빌드해 보지 못했다 (docs/install.md).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system ".[local-embed,mcp]"

# 임베딩 모델을 이미지에 넣어 둔다 (실행 중에는 인터넷이 없어도 된다)
ENV SASU_HOME=/root/.sasu
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', cache_dir='/opt/sasu-models')" \
    && mkdir -p /root/.sasu && ln -s /opt/sasu-models /root/.sasu/models

ENV SASU_EMBED_PROVIDER=local
EXPOSE 8765
VOLUME ["/root/.sasu"]
CMD ["sasu", "serve", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
