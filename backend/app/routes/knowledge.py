"""知识库管理：文档增删查、切片查看、示例导入。

**可见范围**：管理员（root）看全部（含他人私有），普通用户看公共库 + 自己的私有。
过滤下推到 SQL，不在这里取全量再筛。

**上传的归属与可见范围由服务端按角色决定，请求里说了不算**：
root 上传进公共库（所有人可检索），普通用户上传进私有库（只有自己可检索）；
``owner`` 取自 JWT。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.config import DOCUMENT_CATEGORIES, SUPPORTED_FILE_TYPES, settings
from app.schemas import (
    CODE_BAD_REQUEST,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    BaseResponse,
    DeleteDocumentRequest,
)
from app.security import CurrentUser, get_current_user, require_admin
from app.services import documents as document_service
from app.services import storage, vector_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge_base", tags=["知识库管理"])


@router.get("/list_files", summary="文档列表")
def list_files(
    category: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """文档列表，按可见范围过滤。"""
    return BaseResponse.success(
        data=storage.list_documents(
            requester=user.username,
            include_all=user.is_admin,
            category=category,
        )
    )


@router.get("/detail", summary="文档详情")
def detail(
    document_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    record = storage.get_document(document_id)
    if not record:
        return BaseResponse.error("未找到对应的知识文档。", code=CODE_NOT_FOUND)
    # 普通用户只能看自己可见的文档，否则 document_id 可被枚举出别人的私有文档元数据
    if not user.is_admin and not _visible(record, user.username):
        return BaseResponse.error("该文档不属于你。", code=CODE_FORBIDDEN)
    return BaseResponse.success(data=record)


@router.get("/chunks", summary="文档切片（供引用高亮）")
def chunks(
    document_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """取某文档的全部切片。

    前端"点开引用 → 高亮原文片段"用它：拿到 ``chunk_index`` 后滚动并高亮对应切片。
    **不含向量**——前端不需要，传过去只是白占带宽。
    """
    record = storage.get_document(document_id)
    if not record:
        return BaseResponse.error("未找到对应的知识文档。", code=CODE_NOT_FOUND)
    if not user.is_admin and not _visible(record, user.username):
        return BaseResponse.error("该文档不属于你。", code=CODE_FORBIDDEN)

    return BaseResponse.success(
        data={"document": record, "chunks": vector_store.get_chunks(document_id)}
    )


def _visible(record: dict, username: str) -> bool:
    if record.get("scope") == "public":
        return True
    return bool(username) and record.get("owner") == username


@router.post("/upload_docs", summary="上传文档（支持多文件）")
def upload_docs(
    files: list[UploadFile] = File(..., description="支持多文件"),
    category: str = Form(...),
    version: str = Form(""),
    title: str = Form("", description="自定义标题，仅单文件上传时生效"),
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """多文件上传。

    **部分失败仍返回 code=200**，逐项结果放在 ``data.uploaded``、
    失败项放在 ``data.failed_files``。整体失败才用非 200 的 business code。

    单文件时 ``title`` 生效；多文件时忽略它，各自用文件名去后缀作标题。
    """
    if not files:
        return BaseResponse.error("请至少选择一个文件。", code=CODE_BAD_REQUEST)
    if category not in DOCUMENT_CATEGORIES:
        return BaseResponse.error(
            f"未知的分类 {category!r}，可选：{'、'.join(DOCUMENT_CATEGORIES)}",
            code=CODE_BAD_REQUEST,
        )

    # 角色决定入库位置。身份来自 JWT，客户端改不了。
    scope = "public" if user.is_admin else "private"
    owner = user.username
    effective_title = title.strip() if len(files) == 1 else ""
    max_bytes = settings.upload_max_mb * 1024 * 1024

    uploaded: list[dict] = []
    failed_files: dict[str, str] = {}

    for upload in files:
        file_name = upload.filename or "未命名文件"
        suffix = Path(file_name).suffix.lower().lstrip(".")
        if suffix not in SUPPORTED_FILE_TYPES:
            failed_files[file_name] = (
                f"不支持的文件类型：.{suffix or '(无扩展名)'}。"
                f"支持：{'、'.join(SUPPORTED_FILE_TYPES)}"
            )
            continue

        try:
            payload = upload.file.read()
        except Exception as exc:  # noqa: BLE001
            failed_files[file_name] = f"读取文件失败：{type(exc).__name__}: {exc}"
            continue

        if len(payload) > max_bytes:
            failed_files[file_name] = f"文件超过 {settings.upload_max_mb}MB 上限。"
            continue

        try:
            result = document_service.ingest_bytes(
                file_name=file_name,
                file_bytes=payload,
                category=category,
                title=effective_title or Path(file_name).stem,
                version=version.strip(),
                source_label="manual_upload",
                owner=owner,
                scope=scope,
            )
        except Exception as exc:  # noqa: BLE001 - 单文件失败不该中断整批
            logger.exception("上传失败（%s）", file_name)
            failed_files[file_name] = f"{type(exc).__name__}: {exc}"
            continue

        document = result.get("document") or {}
        uploaded.append(
            {
                "file_name": file_name,
                "status": result["status"],
                "message": result["message"],
                "document_id": document.get("id"),
            }
        )
        if result["status"] == "failed":
            failed_files[file_name] = result["message"]

    if not uploaded and failed_files:
        return BaseResponse.error(
            "全部文件导入失败。", code=CODE_BAD_REQUEST, data={"failed_files": failed_files}
        )
    return BaseResponse.success(data={"uploaded": uploaded, "failed_files": failed_files})


@router.post("/delete_docs", summary="删除文档（向量级联删除）")
def delete_docs(
    payload: DeleteDocumentRequest,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """删除文档。

    权限在服务端判断：root 可删任意文档；普通用户只能删自己上传的私有文档
    ——公共库是 root 维护的。
    """
    result = document_service.delete_document(
        payload.document_id, requester=user.username, role=user.role
    )
    code = {
        "success": 200,
        "not_found": CODE_NOT_FOUND,
        "forbidden": CODE_FORBIDDEN,
    }.get(result["status"], CODE_BAD_REQUEST)

    if result["status"] == "success":
        return BaseResponse.success(data=result, msg=result["message"])
    return BaseResponse.error(result["message"], code=code, data=result)


@router.post(
    "/seed",
    summary="一键导入示例文档（管理员）",
    dependencies=[Depends(require_admin)],
)
def seed(user: CurrentUser = Depends(get_current_user)) -> BaseResponse:
    """导入示例制度文档。一律进公共库——示例就是给大家看的。"""
    try:
        results = document_service.seed_sample_documents(owner=user.username)
    except Exception as exc:  # noqa: BLE001 - 示例文件缺失等要转成可读错误
        logger.exception("示例文档导入失败")
        return BaseResponse.error(f"示例文档导入失败：{exc}")

    uploaded = [
        {
            "file_name": (r.get("document") or {}).get("file_name", ""),
            "status": r["status"],
            "message": r["message"],
            "document_id": (r.get("document") or {}).get("id"),
        }
        for r in results
    ]
    failed = {
        (r.get("document") or {}).get("file_name", f"item-{i}"): r["message"]
        for i, r in enumerate(results)
        if r["status"] == "failed"
    }
    return BaseResponse.success(data={"uploaded": uploaded, "failed_files": failed})
