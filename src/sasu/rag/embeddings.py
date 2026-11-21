"""임베딩: 코드 조각과 질문을 벡터로 바꾼다.

- api: OpenAI 호환 /embeddings 주소 (개발용 API, 현장의 사내 임베딩 서버)
- voyage: Voyage AI (api와 같은 형식, 주소·기본 모델만 정해 둔 것). 코드 검색 평가에서 가장 강했다
  (템플릿 56문항 벡터만 R@10: MiniLM 39%, voyage-3.5 73%, voyage-code-3 79%)
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
VOYAGE_URL = "https://api.voyageai.com/v1"
DEFAULT_VOYAGE_MODEL = "voyage-code-3"


def is_voyage(base_url: str) -> bool:
    return "voyageai.com" in (base_url or "")


def voyage_key() -> str:
    """Voyage 키: SASU_EMBED_API_KEY, 없으면 VOYAGE_API_KEY."""
    return os.environ.get("SASU_EMBED_API_KEY") or os.environ.get("VOYAGE_API_KEY", "")


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.lower() != "none":  # 키 없이 여는 사내 서버
        headers["Authorization"] = f"Bearer {api_key}"
    return headers

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
        return self._embed(texts, None)

    def embed_query(self, text: str) -> list[float]:
        """질문 임베딩. Voyage는 input_type=query를 붙인다 (템플릿 평가 벡터만 R@5 71% → 73%, MRR 0.544 → 0.557).
        다른 OpenAI 호환 서버는 모르는 필드를 거부할 수 있어 그대로 보낸다."""
        return self._embed([text], "query" if is_voyage(self.base_url) else None)[0]

    def _embed(self, texts: list[str], input_type: str | None) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            payload: dict = {"model": self.model, "input": texts[i:i + self.batch]}
            if input_type:
                payload["input_type"] = input_type
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                f"{self.base_url}/embeddings", data=body,
                headers=_headers(self.api_key),
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raise EmbeddingError(f"임베딩 요청 실패 ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                raise EmbeddingError(f"임베딩 서버에 연결할 수 없음: {e}") from e
            items = sorted(data["data"], key=lambda d: d.get("index", 0))  # index를 빼는 서버도 있다 (그때는 받은 순서)
            vectors.extend(item["embedding"] for item in items)
        return vectors


class LocalEmbedder:
    def __init__(self, model: str = DEFAULT_LOCAL_MODEL):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingError("로컬 임베딩을 쓰려면 먼저 설치하세요: uv sync --extra local-embed") from e
        from sasu.config import home

        # 모델 파일을 SASU_HOME/models에 둔다. 인터넷이 없는 PC에는 이 폴더를 복사해 오면 된다
        self._model = TextEmbedding(model_name=model, cache_dir=str(home() / "models"))
        self.name = f"local:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts, batch_size=64)]


def embed_query(embedder, text: str) -> list[float]:
    """질문 하나를 벡터로 (embed_query가 있으면 그것, 없으면 embed)."""
    if hasattr(embedder, "embed_query"):
        return embedder.embed_query(text)
    return embedder.embed([text])[0]


def embed_with_code(embedder) -> bool:
    """임베딩 조각에 메서드 원문 코드를 붙일지. SASU_EMBED_CODE = auto(기본: API 임베딩일 때만) | on | off.
    작은 로컬 모델은 입력 한도(약 512토큰)가 짧아 코드를 붙이면 요약이 잘린다.
    voyage-code-3 템플릿 평가: 벡터만 R@5 71% → 75%, 섞기(0.8) R@5 75% → 79%, MRR 0.566 → 0.611."""
    value = os.environ.get("SASU_EMBED_CODE", "auto").strip().lower()
    if value in ("on", "1", "true"):
        return True
    if value in ("off", "0", "false"):
        return False
    return isinstance(embedder, ApiEmbedder)


def hybrid_defaults(embedder) -> tuple[str, float]:
    """BM25와 벡터를 섞는 방식과 벡터 비중. SASU_HYBRID_FUSION(linear|minmax), SASU_VECTOR_WEIGHT로 바꾼다.
    - linear: BM25/최고점 + w × 코사인. 약한 로컬 MiniLM에 맞춰 w=0.3으로 정했다 (rag.md 실험 6)
    - minmax: 둘 다 0~1로 맞춘 뒤 (1-w)·BM25 + w·벡터. 강한 API 임베딩은 이쪽이 낫다
      (voyage-code-3 템플릿 56문항: linear 0.3 R@5 61%·MRR 0.416 → minmax 0.8 R@5 75%·MRR 0.566)"""
    api = isinstance(embedder, ApiEmbedder)
    fusion = os.environ.get("SASU_HYBRID_FUSION", "").strip().lower() or ("minmax" if api else "linear")
    if fusion not in ("linear", "minmax"):
        fusion = "minmax" if api else "linear"
    try:
        weight = float(os.environ.get("SASU_VECTOR_WEIGHT", "") or (0.8 if fusion == "minmax" else 0.3))
    except ValueError:
        weight = 0.8 if fusion == "minmax" else 0.3
    return fusion, weight


def make_embedder(settings: Settings) -> Embedder | None:
    provider = os.environ.get("SASU_EMBED_PROVIDER", "none").lower()
    model = os.environ.get("SASU_EMBED_MODEL", "")
    if provider == "voyage":
        key = voyage_key()
        if not key:
            raise EmbeddingError("SASU_EMBED_PROVIDER=voyage에는 SASU_EMBED_API_KEY(또는 VOYAGE_API_KEY)가 필요합니다.")
        return ApiEmbedder(os.environ.get("SASU_EMBED_BASE_URL") or VOYAGE_URL, key, model or DEFAULT_VOYAGE_MODEL)
    if provider == "local":
        return LocalEmbedder(model or DEFAULT_LOCAL_MODEL)
    if provider == "api":
        base_url = os.environ.get("SASU_EMBED_BASE_URL", settings.llm_base_url)
        api_key = os.environ.get("SASU_EMBED_API_KEY", settings.llm_api_key)
        if not model:
            raise EmbeddingError("SASU_EMBED_PROVIDER=api에는 SASU_EMBED_MODEL이 필요합니다 (키는 서버가 요구할 때만).")
        return ApiEmbedder(base_url, api_key, model)
    return None
