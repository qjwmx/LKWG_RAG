"""文档入库与删除。

写入链路（``ingest_bytes`` 是**唯一写入口**）：

1. sha256 去重 —— 命中且已成功则直接返回 duplicate；
2. 解析（含 ``.md``）→ 归一化文本；
3. 原文落盘，写 documents 行，status = processing；
4. 切分 → 嵌入 → 写 chunks；
5. 回写 chunk_count / status = success。

失败路径的补偿清理
------------------
``add_chunks`` 自己是一个事务（要么全写要么全不写），但它**提交之后**，
回写 ``status='success'`` 仍可能失败。此时切片已落库、文档却是 failed。
检索侧靠 ``WHERE status='success'`` 让它查不到，**但行还在**（白占空间，
且 ``chunk_count`` 与实际不符会让排查者困惑）。所以失败分支显式清一次切片。

清理本身失败**不能**掩盖原始异常，因此单独 ``try`` 且只 ``logger.exception``。

删除顺序
--------
**必须先删库、再删文件。** 文件系统不在数据库事务里，反序会留下
"记录还在、原文已没了"的悬空引用。删文件失败只记日志、不报错——
库里记录已删，最坏只是磁盘上多一个无人引用的文件，不该让用户以为删除失败而重试
（重试只会得到 not_found）。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.config import (
    DEFAULT_VERSION,
    ROLE_ADMIN,
    SAMPLE_DOC_DIR,
    SAMPLE_DOCS,
    SCOPE_PRIVATE,
    SCOPE_PUBLIC,
    SUPPORTED_FILE_TYPES,
    RAW_DOCUMENT_DIR,
)
from app.services import storage, vector_store
from app.services.chunker import split_text
from app.services.embeddings import EmbeddingError, get_provider
from app.services.parser import ParseError, parse

logger = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """入库失败，消息可直接展示给用户。"""


def can_manage(document: dict, username: str, is_admin: bool) -> bool:
    """能否删除这份文档。

    root 可删任意文档；普通用户只能删**自己上传的私有文档**——
    公共库归 root 维护。

    前端只负责不显示无权操作的按钮，**真正的拦截在服务端**（调用点必须调它）。
    """
    if is_admin:
        return True
    return bool(
        username
        and document.get("scope") == SCOPE_PRIVATE
        and document.get("owner") == username
    )


def ingest_bytes(
    file_name: str,
    file_bytes: bytes,
    category: str,
    title: str = "",
    version: str = "",
    source_label: str = "manual_upload",
    owner: str = "",
    scope: str = SCOPE_PUBLIC,
) -> dict:
    """把一份文件写入知识库。返回 ``{status, message, document}``。

    ``status`` 为 ``success`` / ``duplicate`` / ``failed``。
    """
    file_name = Path(file_name).name or "未命名文件"
    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_FILE_TYPES:
        return {
            "status": "failed",
            "message": f"不支持的文件类型：.{suffix or '(无扩展名)'}。"
            f"支持：{'、'.join(SUPPORTED_FILE_TYPES)}",
            "document": None,
        }
    if not file_bytes:
        return {"status": "failed", "message": "文件内容为空。", "document": None}

    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # 1. 去重。命中且已成功 -> duplicate；命中但上次失败 -> 复用该行重试。
    existing = storage.get_document_by_hash(file_hash)
    if existing and existing.get("status") == "success":
        return {
            "status": "duplicate",
            "message": "该文档已存在（内容完全相同），已跳过。",
            "document": existing,
        }

    # 2. 解析
    try:
        text, parsed_suffix = parse(file_name, file_bytes)
    except ParseError as exc:
        return {"status": "failed", "message": str(exc), "document": None}

    document_id = existing["id"] if existing else None
    raw_path = _store_raw_file(file_hash, file_name, file_bytes)

    if existing:
        document = storage.update_document(
            existing["id"],
            {
                "file_name": file_name,
                "file_type": parsed_suffix,
                "title": title or Path(file_name).stem,
                "category": category,
                "version": version or DEFAULT_VERSION,
                "source_label": source_label,
                "owner": owner,
                "scope": scope,
                "raw_path": raw_path,
                "text_length": len(text),
                "status": "processing",
                "error": "",
            },
        )
    else:
        document = storage.add_document(
            {
                "file_hash": file_hash,
                "file_name": file_name,
                "file_type": parsed_suffix,
                "title": title or Path(file_name).stem,
                "category": category,
                "version": version or DEFAULT_VERSION,
                "source_label": source_label,
                "owner": owner,
                "scope": scope,
                "raw_path": raw_path,
                "text_length": len(text),
                "chunk_count": 0,
                "status": "processing",
                "error": "",
            }
        )
        document_id = document["id"]

    # 3. 切分 + 嵌入 + 写切片
    try:
        chunks = split_text(text)
        if not chunks:
            raise IngestError("文档切分后没有可用内容。")

        embeddings = get_provider().embed_documents(chunks)
        # 重新入库时切片数可能变少，先删旧切片避免留下长尾
        vector_store.delete_chunks(document_id)
        vector_store.add_chunks(document_id, chunks, embeddings)

        document = storage.update_document(
            document_id, {"chunk_count": len(chunks), "status": "success", "error": ""}
        )
    except EmbeddingError as exc:
        document = _fail(document_id, str(exc))
        return {"status": "failed", "message": str(exc), "document": document}
    except Exception as exc:  # noqa: BLE001 - 任何失败都要落成 failed 而不是 500
        logger.exception("文档入库失败（%s）", file_name)
        message = f"{type(exc).__name__}: {exc}"
        document = _fail(document_id, message)
        return {"status": "failed", "message": message, "document": document}

    return {
        "status": "success",
        "message": f"已入库，共 {document.get('chunk_count', 0)} 个切片。",
        "document": document,
    }


def _fail(document_id: str, message: str) -> dict | None:
    """把文档标为 failed，并补偿清理已落库的切片。

    清理单独 try：它失败不能掩盖原始异常。
    """
    try:
        vector_store.delete_chunks(document_id)
    except Exception:  # noqa: BLE001
        logger.exception("补偿清理切片失败（document_id=%s）", document_id)
    try:
        return storage.update_document(
            document_id, {"status": "failed", "error": message[:2000], "chunk_count": 0}
        )
    except Exception:  # noqa: BLE001
        logger.exception("回写 failed 状态失败（document_id=%s）", document_id)
        return None


def _store_raw_file(file_hash: str, file_name: str, file_bytes: bytes) -> str:
    """原文落盘。路径带 hash 前缀，避免同名文件互相覆盖。"""
    try:
        RAW_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
        target = RAW_DOCUMENT_DIR / f"{file_hash[:12]}_{file_name}"
        target.write_bytes(file_bytes)
        return str(target)
    except OSError:
        # 落盘失败不该阻断入库：原文只是"留档"，检索靠的是切片表。
        logger.exception("原文落盘失败（%s）", file_name)
        return ""


def delete_document(document_id: str, requester: str, role: str) -> dict:
    """删除文档及其向量。

    返回 ``{status: success|not_found|forbidden, message, ...}``。
    """
    is_admin = role == ROLE_ADMIN
    document = storage.get_document(document_id)
    if not document:
        return {"status": "not_found", "message": "未找到该文档。"}
    if not can_manage(document, requester, is_admin):
        logger.warning("用户 %s 试图删除文档 %s（归属 %s）", requester, document_id, document.get("owner"))
        return {"status": "forbidden", "message": "你无权删除这份文档（公共库由管理员维护）。"}

    # 先删库：向量随 CASCADE 在同一事务里清除。
    # 刻意不调 vector_store.delete_chunks——那会变成两条删除路径，反而容易漏。
    deleted = storage.delete_document(document_id)
    if not deleted:
        return {"status": "not_found", "message": "未找到该文档。"}

    # 再删文件，best-effort。失败只记日志：库里记录已删，最坏是磁盘多个孤儿文件。
    raw_path = document.get("raw_path") or ""
    if raw_path:
        try:
            Path(raw_path).unlink(missing_ok=True)
        except OSError:
            logger.exception("删除原始文件失败（%s），已忽略", raw_path)

    return {
        "status": "success",
        "message": f"已删除《{document.get('title') or document_id}》及其向量索引。",
        "document_id": document_id,
    }


def seed_sample_documents(owner: str) -> list[dict]:
    """导入示例文档。一律进公共库——示例就是给大家看的。"""
    results: list[dict] = []
    if not SAMPLE_DOC_DIR.exists():
        raise IngestError(f"示例文档目录不存在：{SAMPLE_DOC_DIR}")

    for item in SAMPLE_DOCS:
        path = SAMPLE_DOC_DIR / item["file_name"]
        if not path.exists():
            results.append(
                {
                    "status": "failed",
                    "message": f"示例文件缺失：{item['file_name']}",
                    "document": None,
                }
            )
            continue
        results.append(
            ingest_bytes(
                file_name=item["file_name"],
                file_bytes=path.read_bytes(),
                category=item["category"],
                title=item["title"],
                version=item["version"],
                source_label="sample_seed",
                owner=owner,
                scope=SCOPE_PUBLIC,
            )
        )
    return results
