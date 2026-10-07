"""对话：问答（含 SSE 流式）、历史、会话、反馈。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.schemas import (
    CODE_BAD_REQUEST,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    BaseResponse,
    ChatRequest,
    DeleteSessionRequest,
    FeedbackRequest,
)
from app.security import CurrentUser, get_current_user, require_admin
from app.services import chat as chat_service
from app.services import storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["对话"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 挡在中间的 Nginx 默认会缓冲整个响应，不关掉流式就没有"逐字吐出"的效果
    "X-Accel-Buffering": "no",
}


def _sse_stream(request: ChatRequest, user: CurrentUser):
    """把 service 的事件流转成 SSE 帧。

    同步生成器交给 StreamingResponse，Starlette 会自动放进线程池迭代，
    不会阻塞事件循环（底层 openai SDK 调用是同步的）。
    """
    try:
        for event in chat_service.answer_question_stream(
            question=request.question,
            session_id=request.session_id,
            category=request.category,
            username=user.username,
            is_admin=user.is_admin,
            use_web=request.use_web,
        ):
            yield chat_service.sse_frame(event)
    except Exception as exc:  # noqa: BLE001 - 流已开始，无法再改 HTTP 状态码
        logger.exception("流式问答失败（session_id=%s）", request.session_id)
        yield chat_service.sse_frame(
            {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
        )


@router.post("/chat", summary="提问（stream=true 返回 SSE 流）")
def chat(
    request: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
):
    if request.stream:
        return StreamingResponse(
            _sse_stream(request, user),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    try:
        result = chat_service.answer_question(
            question=request.question,
            session_id=request.session_id,
            category=request.category,
            username=user.username,
            is_admin=user.is_admin,
            use_web=request.use_web,
        )
    except Exception as exc:  # noqa: BLE001 - 缺 Key / 服务不可达要转成可读错误而不是 500
        logger.exception("问答失败（session_id=%s）", request.session_id)
        return BaseResponse.error(
            "回答失败，请检查 LLM_API_KEY、嵌入服务与数据库连接。"
            f"原始错误：{type(exc).__name__}: {exc}"
        )
    return BaseResponse.success(data=result)


@router.get("/history", summary="按会话 ID 取历史消息")
def history(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """把问答记录还原成 ``[{role, content, ...}]`` 的消息序列，供前端直接渲染。

    **归属校验**：管理员可看任意会话，普通用户只能看自己的。
    """
    owner = storage.get_session_owner(session_id)
    if owner and owner != user.username and not user.is_admin:
        logger.warning("用户 %s 试图读取 %s 的历史（归属 %s）", user.username, session_id, owner)
        return BaseResponse.error("该会话不属于你。", code=CODE_FORBIDDEN)

    messages = []
    for log in storage.list_session_logs(session_id):
        messages.append(
            {
                "role": "user",
                "content": log["question"],
                "created_at": log.get("created_at", ""),
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": log["answer"],
                "qa_log_id": log["id"],
                "question_type": log.get("question_type", ""),
                "question": log.get("question", ""),
                "category": log.get("category", ""),
                "created_at": log.get("created_at", ""),
                # 恢复历史会话时用它还原引用卡片——否则刷新后引用就没了
                "source_docs": log.get("source_docs") or [],
                # 网络来源同样要还原：否则重新打开会话时只剩本地引用，
                # 用户会以为当初的联网根本没生效
                "web_sources": log.get("web_sources") or [],
            }
        )
    return BaseResponse.success(data=messages)


@router.get("/sessions", summary="会话列表（按 session 聚合）")
def sessions(
    limit: int = 20,
    offset: int = 0,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """最近的在前。每行含首个问题（当标题）、起止时间、轮数与分类。

    **管理员看全部会话；普通用户只看自己的**——过滤依据是 JWT 里的身份。
    """
    return BaseResponse.success(
        data=storage.list_sessions(
            limit=limit,
            offset=offset,
            username=user.username,
            include_all=user.is_admin,
        )
    )


@router.post("/delete_session", summary="删除整个会话")
def delete_session(
    payload: DeleteSessionRequest,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """删除某会话的全部问答记录，反馈随外键级联删除。"""
    owner = storage.get_session_owner(payload.session_id)
    if not user.is_admin and (not user.username or owner != user.username):
        logger.warning("用户 %s 试图删除 %s（归属 %s）", user.username, payload.session_id, owner)
        return BaseResponse.error("该会话不属于你，无权删除。", code=CODE_BAD_REQUEST)

    deleted = storage.delete_session(payload.session_id)
    if not deleted:
        return BaseResponse.error("未找到该会话。", code=CODE_NOT_FOUND)
    return BaseResponse.success(data={"session_id": payload.session_id, "deleted": deleted})


@router.post("/feedback", summary="提交或更新反馈")
def save_feedback(
    payload: FeedbackRequest,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    if payload.rating not in ("unrated", "helpful", "needs_improvement"):
        return BaseResponse.error(
            "rating 只能是 unrated / helpful / needs_improvement", code=CODE_BAD_REQUEST
        )
    record = storage.upsert_feedback(
        qa_log_id=payload.qa_log_id,
        rating=payload.rating,
        comment=payload.comment,
        session_id=payload.session_id,
    )
    return BaseResponse.success(data=record)


@router.get("/feedback", summary="查询单条问答的反馈")
def get_feedback(
    qa_log_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    record = storage.get_feedback(qa_log_id)
    if not record:
        return BaseResponse.error("尚未提交反馈。", code=CODE_NOT_FOUND)
    return BaseResponse.success(data=record)


@router.get(
    "/logs",
    summary="全部问答记录（管理员）",
    dependencies=[Depends(require_admin)],
)
def logs(limit: int | None = None) -> BaseResponse:
    return BaseResponse.success(data=storage.list_qa_logs(limit=limit))


@router.get(
    "/feedback_list",
    summary="全部反馈记录（管理员）",
    dependencies=[Depends(require_admin)],
)
def feedback_list() -> BaseResponse:
    return BaseResponse.success(data=storage.list_feedback())
