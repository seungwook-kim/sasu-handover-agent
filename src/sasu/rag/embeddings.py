"""임베딩: 코드 조각과 질문을 벡터로 바꾼다.

- api: OpenAI 호환 /embeddings 주소 (개발용 API, 현장의 사내 임베딩 서버)
- local: fastembed(ONNX)로 작은 다국어 모델을 CPU에서 돌린다. GPU 없이 한 번 색인하면 된다.
  설치: uv sync --extra local-embed
- none: 벡터 검색을 쓰지 않는다 (BM25 + 그래프만)
"""

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

from sasu.config import Settings

DEFAULT_LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    pass


class ApiEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str, batch: int = 64, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.batch = batch
        self.timeout = timeout
        self.name = f"api:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            body = json.dumps({"model": self.model, "input": texts[i:i + self.batch]}).encode("utf-8")
            request = urllib.request.Request(
                f"{self.base_url}/embeddings", data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raise EmbeddingError(f"임베딩 요청 실패 ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                raise EmbeddingError(f"임베딩 서버에 연결할 수 없음: {e}") from e
            items = sorted(data["data"], key=lambda d: d["index"])
            vectors.extend(item["embedding"] for item in items)
        return vectors


class LocalEmbedder:
    def __init__(self, model: str = DEFAULT_LOCAL_MODEL):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingError("로컬 임베딩을 쓰려면 먼저 설치하세요: uv sync --extra local-embed") from e
        self._model = TextEmbedding(model_name=model)
        self.name = f"local:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts, batch_size=64)]


def make_embedder(settings: Settings) -> Embedder | None:
    provider = os.environ.get("SASU_EMBED_PROVIDER", "none").lower()
    model = os.environ.get("SASU_EMBED_MODEL", "")
    if provider == "local":
        return LocalEmbedder(model or DEFAULT_LOCAL_MODEL)
    if provider == "api":
        base_url = os.environ.get("SASU_EMBED_BASE_URL", settings.llm_base_url)
        api_key = os.environ.get("SASU_EMBED_API_KEY", settings.llm_api_key)
        if not (api_key and model):
            raise EmbeddingError("SASU_EMBED_PROVIDER=api에는 SASU_EMBED_MODEL과 API 키가 필요합니다.")
        return ApiEmbedder(base_url, api_key, model)
    return None
