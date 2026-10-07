"""请求与响应模型。

统一响应约定：**HTTP 状态码恒为 200，业务状态放在 body 的 ``code`` 字段**，
失败也返回 200 而不是抛 ``HTTPException``。

例外只有鉴权：401（未登录 / 凭据无效）与 403（已登录但权限不够）走真正的
HTTP 状态码。这样客户端能一眼分辨"凭据有问题，该重新登录"、"这个账号不该做
这件事"与"业务逻辑报错"三种情况。

**请求模型里刻意没有身份字段。** ``username`` / ``role`` / ``owner`` / ``scope``
一律不存在——不是"忽略"，而是删掉：签名里没有这个字段，伪造就无处可传。
身份统一由服务端从 JWT 解出（见 ``app/security.py``）。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

CODE_SUCCESS = 200
CODE_BAD_REQUEST = 400
# 登录失败用这个业务码。它与"HTTP 401"不是一回事：HTTP 401 表示 token 缺失或无效。
CODE_UNAUTHORIZED = 401
CODE_FORBIDDEN = 403
CODE_NOT_FOUND = 404
CODE_INTERNAL_ERROR = 500


class BaseResponse(BaseModel, Generic[T]):
    code: int = Field(CODE_SUCCESS, description="业务状态码，200 表示成功")
    msg: str = Field("success", description="提示信息")
    data: T | None = Field(None, description="业务数据")

    @classmethod
    def success(cls, data: Any = None, msg: str = "success") -> "BaseResponse":
        return cls(code=CODE_SUCCESS, msg=msg, data=data)

    @classmethod
    def error(
        cls, msg: str, code: int = CODE_INTERNAL_ERROR, data: Any = None
    ) -> "BaseResponse":
        return cls(code=code, msg=msg, data=data)


# --------------------------------------------------------------------------- 用户


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    display_name: str = Field("")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ReviewUserRequest(BaseModel):
    username: str = Field(..., min_length=1)
    status: str = Field(..., description="pending / approved / rejected")


class DeleteUserRequest(BaseModel):
    username: str = Field(..., min_length=1)


class SetRoleRequest(BaseModel):
    username: str = Field(..., min_length=1)
    role: str = Field(..., description="admin / user")


# --------------------------------------------------------------------------- 对话


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    category: str = Field("全部", description="分类过滤，“全部”表示不过滤")
    stream: bool = Field(False, description="true 时返回 SSE 流")
    # 联网检索开关。**默认关**：Tavily 按次计费，且联网内容是外部不可信数据。
    # 用户在界面上显式打开（高亮 = 会联网，不亮 = 不联网）。
    use_web: bool = Field(False, description="本轮是否允许联网检索")
    # 刻意没有 username：提问者由后端从 JWT 解出。它决定检索范围与 qa_logs 归属。


class DeleteSessionRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class FeedbackRequest(BaseModel):
    qa_log_id: str = Field(..., min_length=1)
    rating: str = Field("unrated", description="unrated / helpful / needs_improvement")
    comment: str = Field("")
    session_id: str = Field("")


# --------------------------------------------------------------------------- 知识库


class DeleteDocumentRequest(BaseModel):
    document_id: str = Field(..., min_length=1)


class UploadFileResult(BaseModel):
    file_name: str
    status: str = Field(..., description="success / duplicate / failed")
    message: str
    document_id: str | None = None
