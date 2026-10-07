"""向量检索。

**过滤下推到 SQL，不是"检索完再筛"。** 这是本模块最关键的一处设计：

如果先把全库 top-k 取回来、再在应用层按可见范围过滤，top-k 会被筛空——
表现成"知识库明明有资料，却回答未找到明确依据"，而且**不报错**。
所以可见范围（``scope='public' OR owner=:me``）与分类过滤都写在 WHERE 里，
让候选集在取回前就已经是对的。

检索本身用 numpy 做精确余弦相似度。没有 ANN 索引，但：
- 免去 pgvector 等数据库扩展依赖，SQLite 上也能跑；
- 精确 KNN 的召回优于近似索引；
- 万级切片内耗时可忽略。

数据量涨到十万级时，把 ``_score`` 换成 pgvector 的 ``<=>`` 运算符即可，
过滤逻辑不需要动。
"""

from __future__ import annotations

import logging

import numpy as np
from sqlalchemy import or_, select

from app.cache import (
    bump_corpus_version,
    cache_embedding,
    cache_search,
    get_cached_embedding,
    get_cached_search,
)
from app.config import SCOPE_PRIVATE, SCOPE_PUBLIC, settings
from app.db.models import DocumentChunk, DocumentRecord
from app.db.session import transaction
from app.services.embeddings import EmbeddingError, get_provider

logger = logging.getLogger(__name__)

# 向量检索 fail-open：异常时返回空结果并记日志，页面表现为"未找到明确依据"，
# 而不是把堆栈糊在用户脸上。改成 False 可以把异常抛出去（排查时用）。
VECTOR_FAIL_OPEN = True


class SearchHit(dict):
    """一条命中结果。字典形状，便于直接拼进 SSE 帧。"""


def _embedding_model_label() -> str:
    """缓存键里的模型标识。

    必须包含 provider 与模型名：``hash`` 伪嵌入与 ``ollama`` 真实嵌入
    对同一文本会给出**完全不同**的向量。只用文本做键的话，切换 provider
    后会命中另一个 provider 的向量，检索结果变得莫名其妙且不报错。
    """
    kind = (settings.embedding_provider or "ollama").strip().lower()
    return f"{kind}:{settings.embedding_model}:{settings.embedding_dim}"


def _embed_query_cached(query: str) -> np.ndarray:
    """算查询向量，优先走缓存。

    **这是五 Agent 流水线里最值得缓存的一步**：每个研究单元都要对
    自己的 topic 做一次 ``embed_query``，一次问答可能有 2–4 个单元。
    同一话题反复问（或同一 session 里追问）时命中率很高。

    命中缓存省掉的是一次网络往返（实测 Ollama 冷启 3.3s、热态 0.36s；
    远端嵌入服务则是一次几十到几百毫秒的 HTTP 请求）。

    **缓存的是纯函数结果**：同样的文本 + 同样的模型必然得到同样的向量，
    所以这里没有正确性风险，TTL 也可以给得很长（见 EMBED_CACHE_TTL）。
    """
    label = _embedding_model_label()
    cached = get_cached_embedding(label, query)
    if cached is not None:
        return np.asarray(cached, dtype=np.float32)

    vector = get_provider().embed_query(query)
    cache_embedding(label, query, vector)
    return np.asarray(vector, dtype=np.float32)


def _visibility_clause(requester: str, include_all: bool):
    """可见范围条件。

    root（``include_all=True``）**不加任何条件**——这正是 root 的语义：
    它的检索覆盖所有人上传的文档，包括他人的私有材料。
    这是刻意的行为，不是漏洞。
    """
    if include_all:
        return None
    return or_(
        DocumentRecord.scope == SCOPE_PUBLIC,
        (DocumentRecord.scope == SCOPE_PRIVATE) & (DocumentRecord.owner == (requester or "")),
    )


def search(
    query: str,
    category: str = "全部",
    requester: str = "",
    include_all: bool = False,
    top_k: int = 4,
) -> list[SearchHit]:
    """检索相关切片。

    返回 ``[{document_id, chunk_index, content, score, title, category, version,
    file_name, owner, scope}, ...]``，按相似度降序。

    结果会进缓存（见 ``_search_cached``）：一次检索要读**全部**候选切片的
    向量（实测 3000 切片 × 2560 维 = 7.3 MB、67ms），而同一问题常被反复问。
    """
    try:
        return _search_cached(query, category, requester, include_all, top_k)
    except EmbeddingError:
        raise
    except Exception:  # noqa: BLE001
        if not VECTOR_FAIL_OPEN:
            raise
        logger.exception("向量检索失败，降级为空结果")
        return []


def _search_cached(
    query: str,
    category: str,
    requester: str,
    include_all: bool,
    top_k: int,
) -> list[SearchHit]:
    """带缓存的检索。

    ★ **缓存键必须包含可见性**（``requester`` / ``include_all``）——
    见 ``cache.store._search_key`` 的说明。只按查询文本做键会让 root 的
    全量检索结果被普通用户命中，等于泄露他人私有文档。

    **只缓存成功的结果**：``_search`` 抛异常时不写缓存，否则一次偶发的
    嵌入服务抖动会把"空结果"固化下来，表现为"这个问题永远搜不到"。
    """
    cached = get_cached_search(query, category, requester, include_all, top_k)
    if cached is not None:
        return [SearchHit(**hit) for hit in cached]

    hits = _search(query, category, requester, include_all, top_k)
    cache_search(
        query, category, requester, include_all, top_k, [dict(hit) for hit in hits]
    )
    return hits


def _search(
    query: str,
    category: str,
    requester: str,
    include_all: bool,
    top_k: int,
) -> list[SearchHit]:
    query_vector = _embed_query_cached(query)

    stmt = (
        select(
            DocumentChunk.document_id,
            DocumentChunk.chunk_index,
            DocumentChunk.content,
            DocumentChunk.embedding,
            DocumentRecord.title,
            DocumentRecord.category,
            DocumentRecord.version,
            DocumentRecord.file_name,
            DocumentRecord.owner,
            DocumentRecord.scope,
        )
        .join(DocumentRecord, DocumentRecord.id == DocumentChunk.document_id)
        # 只检索成功入库的文档：失败/处理中的文档不该被引用
        .where(DocumentRecord.status == "success")
    )

    if category and category != "全部":
        stmt = stmt.where(DocumentRecord.category == category)

    visibility = _visibility_clause(requester, include_all)
    if visibility is not None:
        stmt = stmt.where(visibility)

    with transaction() as session:
        rows = session.execute(stmt).all()

    if not rows:
        return []

    # 相似度在应用层算：先把 BLOB 堆成矩阵，一次矩阵乘法算完全部候选，
    # 而不是逐行循环（逐行在几千切片时就能明显感觉到慢）。
    vectors = []
    valid_rows = []
    for row in rows:
        embedding = row.embedding
        if embedding is None or len(embedding) == 0:
            continue
        vectors.append(np.asarray(embedding, dtype=np.float32))
        valid_rows.append(row)

    if not vectors:
        return []

    matrix = np.vstack(vectors)
    query_norm = float(np.linalg.norm(query_vector))
    if query_norm == 0:
        return []

    # 向量在入库时已归一化，这里再除一次以防外部数据未归一化
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    scores = (matrix @ query_vector) / (norms * query_norm)

    order = np.argsort(-scores)[: max(top_k, 0)]
    hits: list[SearchHit] = []
    for index in order:
        row = valid_rows[int(index)]
        hits.append(
            SearchHit(
                document_id=row.document_id,
                chunk_index=int(row.chunk_index),
                content=row.content,
                score=round(float(scores[int(index)]), 4),
                title=row.title or row.file_name or "未命名文档",
                category=row.category or "未分类",
                version=row.version or "未填写",
                file_name=row.file_name or "",
                owner=row.owner or "",
                scope=row.scope or "",
            )
        )
    return hits


def count_searchable(
    category: str = "全部",
    requester: str = "",
    include_all: bool = False,
) -> int:
    """数一下当前可见范围内有多少**可检索**的切片。

    这是给五 Agent 流水线做**廉价预检**用的：库里一条都没有时，
    没必要花 1 次 Planner + N 次 Researcher 的模型调用才得出"没资料"。
    纯 COUNT 查询，不加载向量、不做矩阵运算。

    过滤条件与 ``search`` **完全一致**（同一个 ``_visibility_clause``）：
    两处各写一份的话，预检说"有资料"而检索返回空，用户会看到
    "资料不足"却不知道为什么。
    """
    try:
        return _count_searchable(category, requester, include_all)
    except Exception:  # noqa: BLE001 - 预检失败不该让问答挂掉
        if not VECTOR_FAIL_OPEN:
            raise
        logger.exception("预检计数失败，按'有资料'处理（交给正常检索路径判断）")
        # 保守返回 1：让流程继续走到真正的检索，由它给出准确结果。
        # 返回 0 会让预检失败直接变成"资料不足"，把可答的问题答成不可答。
        return 1


def _count_searchable(category: str, requester: str, include_all: bool) -> int:
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(DocumentChunk)
        .join(DocumentRecord, DocumentRecord.id == DocumentChunk.document_id)
        .where(DocumentRecord.status == "success")
    )
    if category and category != "全部":
        stmt = stmt.where(DocumentRecord.category == category)

    visibility = _visibility_clause(requester, include_all)
    if visibility is not None:
        stmt = stmt.where(visibility)

    with transaction() as session:
        return int(session.execute(stmt).scalar() or 0)


def add_chunks(document_id: str, chunks: list[str], embeddings: list[list[float]]) -> None:
    """写入切片。**调用方负责先删旧切片**（见 documents.ingest_bytes）。

    用「先删后插」而不是 upsert：文档重新入库时切片数可能变少，
    upsert 会留下长尾旧切片，检索时命中已经不存在的内容。

    ``embeddings`` 是 LangChain ``Embeddings.embed_documents`` 的返回（list[list[float]]）。

    这里传 **ndarray 而不是 bytes**：序列化统一由 ``VectorBlob`` 类型负责，
    两处各转一次的话，第二处会把已经是 bytes 的值再当数组解析而报错。
    """
    vectors = np.asarray(embeddings, dtype=np.float32)
    with transaction() as session:
        for index, content in enumerate(chunks):
            session.add(
                DocumentChunk(
                    document_id=document_id,
                    chunk_index=index,
                    content=content,
                    embedding=vectors[index],
                )
            )
    # ★ 语料变了：让所有检索结果缓存立即失效。
    # 放在这里而不是调用方，是因为**任何**写切片的路径都必须失效缓存；
    # 靠调用方记得调，早晚会漏一处，而漏掉的表现是"上传成功但搜不到新内容"。
    bump_corpus_version()


def delete_chunks(document_id: str) -> int:
    """删除某文档的全部切片。返回删除行数。

    正常删除路径**不需要**调它——``document_chunks.document_id`` 是
    ON DELETE CASCADE，删 documents 行时向量在同一事务里自动清除。
    它用于**写入失败时的补偿清理**：切片已落库但文档状态回写失败，
    此时删文档行会连累已成功的记录，只能单独清切片。
    """
    from sqlalchemy import delete

    with transaction() as session:
        result = session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        removed = result.rowcount or 0
    # 删了切片也是语料变更（补偿清理、重新入库都会走到这里）
    if removed:
        bump_corpus_version()
    return removed


def get_chunks(document_id: str) -> list[dict]:
    """取某文档的全部切片（按 chunk_index 升序）。

    供前端"点开引用 → 高亮原文片段"使用。**不含向量**——前端不需要，
    传过去只是白占带宽。
    """
    stmt = (
        select(DocumentChunk.chunk_index, DocumentChunk.content)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index.asc())
    )
    with transaction() as session:
        rows = session.execute(stmt).all()
    return [{"chunk_index": int(r.chunk_index), "content": r.content} for r in rows]
