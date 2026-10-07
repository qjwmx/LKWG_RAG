"""嵌入提供者。

统一实现 LangChain 的 ``Embeddings`` 接口，于是检索链路里拿到的是标准
``embed_documents`` / ``embed_query``，换后端不影响调用方。

三种实现，由 ``EMBEDDING_PROVIDER`` 选择：

- ``ollama`` —— 本地常驻，**默认**。走 Ollama 的 OpenAI 兼容端点
  （``/v1/embeddings``），因此复用 ``langchain-openai``，不需要额外依赖。
- ``openai`` —— 任意 OpenAI 兼容的嵌入服务。
- ``hash``   —— 确定性**伪**嵌入。它不做任何语义编码，只是把文本映射成一个
  固定维度的向量。**它的存在只为了一件事：在完全没有外部服务时让界面能跑通**
  （上传、切分、入库、检索、流式渲染的链路都是真的，只有"检索得准不准"是假的）。
  ``/system/health`` 会把它标成 degraded，前端顶部也会提示。
"""

from __future__ import annotations

import hashlib
import logging
import struct

import numpy as np
from langchain_core.embeddings import Embeddings

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """嵌入失败，消息可直接展示给用户。"""


class _DimensionGuard:
    """维度校验。

    维度不符时数据库写入会被拒绝，但错误信息非常晦涩。这里提前转成一句
    能直接照做的提示。
    """

    def __init__(self, dimension: int, label: str) -> None:
        self.dimension = dimension
        self.label = label

    def check(self, vectors: list[list[float]]) -> list[list[float]]:
        if not vectors:
            return vectors
        actual = len(vectors[0])
        if actual != self.dimension:
            raise EmbeddingError(
                f"嵌入维度不符：{self.label} 输出 {actual} 维，"
                f"但配置 EMBEDDING_DIM={self.dimension}。"
                f"请把 EMBEDDING_DIM 改成 {actual}，并清空 document_chunks 重新入库。"
            )
        return vectors


class OpenAICompatibleEmbedding(Embeddings):
    """OpenAI 兼容的嵌入（含 Ollama 的 /v1 端点）。

    ``trust_env=False`` 的等价物：显式传入 ``http_client``，避免 httpx 读取系统代理
    （Windows 上来自注册表，环境变量里看不到）把 127.0.0.1 的回环请求也发进代理，
    拿到 502。``requests`` 默认绕过 localhost，httpx 不会——这是两者最坑的一处差异。
    """

    def __init__(self, *, base_url: str, api_key: str, model: str, label: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.label = label
        self._guard = _DimensionGuard(settings.embedding_dim, label)
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise EmbeddingError(
                    f"{self.label} 需要 API Key，请在 backend/.env 里配置 "
                    f"EMBEDDING_API_KEY（或 LLM_API_KEY）。"
                )
            import httpx
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                # 回环地址必须绕过系统代理，否则会拿到 502
                http_client=httpx.Client(trust_env=False, timeout=120.0),
            )
        return self._client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._get_client().embeddings.create(model=self.model, input=texts)
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 - 转成可读提示
            raise EmbeddingError(
                f"{self.label} 嵌入失败（{self.base_url}）：{type(exc).__name__}: {exc}"
            ) from exc
        return self._guard.check([item.embedding for item in response.data])

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class OllamaEmbedding(OpenAICompatibleEmbedding):
    """本地 Ollama。走它的 OpenAI 兼容端点，因此复用同一个实现。"""

    def __init__(self) -> None:
        base = settings.ollama_base_url.rstrip("/")
        super().__init__(
            base_url=f"{base}/v1",
            # Ollama 的兼容端点不校验 key，但 SDK 要求非空
            api_key=settings.embedding_api_key or "ollama",
            model=settings.embedding_model,
            label=f"ollama · {settings.embedding_model}",
        )


class HashEmbedding(Embeddings):
    """确定性伪嵌入。**没有语义能力**，仅用于让链路在无外部服务时跑通。"""

    def __init__(self) -> None:
        self.dimension = settings.embedding_dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        rows = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            # 用字符 n-gram 的哈希铺开，保证同一文本永远得到同一向量
            # （否则入库与检索两次算出的向量不同，什么都搜不到）
            for token in _tokens(text):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                for offset in range(0, 16, 4):
                    (value,) = struct.unpack_from("<I", digest, offset)
                    rows[row, value % self.dimension] += 1.0
            norm = float(np.linalg.norm(rows[row]))
            if norm > 0:
                rows[row] /= norm
        return rows.tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _tokens(text: str) -> list[str]:
    """中文按 2-gram、英文按空白切词。"""
    tokens: list[str] = []
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    for word in cleaned.split():
        tokens.append(word)
        if len(word) > 2:
            for i in range(len(word) - 1):
                tokens.append(word[i : i + 2])
    return tokens


_provider: Embeddings | None = None
_provider_name: str = ""


def get_provider() -> Embeddings:
    """按配置返回单例提供者。"""
    global _provider, _provider_name
    if _provider is not None:
        return _provider

    kind = (settings.embedding_provider or "ollama").strip().lower()
    if kind == "ollama":
        _provider = OllamaEmbedding()
        _provider_name = "ollama"
    elif kind == "openai":
        _provider = OpenAICompatibleEmbedding(
            base_url=settings.embedding_base_url or settings.llm_base_url,
            api_key=settings.embedding_api_key or settings.llm_api_key,
            model=settings.embedding_model,
            label=f"openai-compatible · {settings.embedding_model}",
        )
        _provider_name = "openai"
    elif kind == "hash":
        _provider = HashEmbedding()
        _provider_name = "hash"
    else:
        logger.warning("未知的 EMBEDDING_PROVIDER=%r，回退到 hash", kind)
        _provider = HashEmbedding()
        _provider_name = "hash"
    return _provider


def provider_name() -> str:
    get_provider()
    return _provider_name


def describe_provider() -> str:
    kind = provider_name()
    if kind == "ollama":
        return f"ollama · {settings.embedding_model}"
    if kind == "openai":
        return f"openai-compatible · {settings.embedding_model}"
    return "hash（伪嵌入，仅用于跑通界面，检索结果无语义意义）"


def reset_provider() -> None:
    """测试用：清掉单例以便换配置重建。"""
    global _provider, _provider_name
    _provider = None
    _provider_name = ""


def is_degraded() -> bool:
    """hash 提供者意味着检索没有语义意义，健康检查据此标 degraded。"""
    return provider_name() == "hash"
