"""可观测性的请求上下文。

为什么用 contextvar 而不是往 ``request.state`` 上挂
---------------------------------------------------
本项目**所有端点都是 ``def``**（不是 ``async def``），FastAPI 会把它们丢进
线程池执行。所以埋点数据必须能穿过线程边界：

- 中间件在事件循环线程里建立记录
- 端点在线程池的工作线程里执行、并往里追加 LLM 调用
- 中间件在响应结束后读回汇总

``contextvars`` 会被 ``anyio`` 的线程池调用正确复制到工作线程（已实测）。
**但有个关键细节**：必须往里放一个**可变对象**并原地修改它，
不能在端点里 ``set()`` 一个新值——那样只改了工作线程的副本，
中间件读到的仍是原对象（已实测确认）。

也就是说：``record.llm_calls.append(...)`` 可以，``ctx.set(new_record)`` 不行。
本模块的 API 就是按这个约束设计的（只提供读与原地修改，不提供替换）。
"""

from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field

# 当前请求的记录。中间件设置，端点与回调读取。
_current: contextvars.ContextVar["RequestRecord | None"] = contextvars.ContextVar(
    "observability_record", default=None
)


@dataclass
class LlmCall:
    """一次模型调用。字段与 ``LlmCallRecord`` 对应。"""

    agent: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0
    structured: bool = False
    usage_missing: bool = False


@dataclass
class RequestRecord:
    """一次请求的可观测数据。

    **只放结构性信息**：路由模板、方法、状态、耗时、用户名。
    刻意不放到这里的东西：query string、请求体、header、token。
    """

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    route: str = ""
    method: str = ""
    status: int = 0
    username: str = ""
    duration_ms: float = 0.0
    error: str = ""
    # 可变列表：工作线程里 append，中间件能读到（见模块说明）
    llm_calls: list[LlmCall] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(call.total_tokens for call in self.llm_calls)


def new_record() -> RequestRecord:
    return RequestRecord()


def set_record(record: RequestRecord | None):
    """设置当前请求的记录，返回用于重置的 token。"""
    return _current.set(record)


def reset_record(token) -> None:
    """恢复上一个上下文。**必须放在 finally 里**，否则线程复用时
    上一个请求的残留记录会被下一个请求读到。"""
    _current.reset(token)


def current_record() -> RequestRecord | None:
    """取当前请求的记录。不在请求上下文里（如脚本、后台任务）返回 ``None``。"""
    return _current.get()


def record_llm_call(call: LlmCall) -> None:
    """往当前请求追加一次模型调用。

    **原地 append**，不 ``set()`` 新对象——原因见模块说明。
    没有当前记录时静默丢弃：脚本或测试里直接调模型不该报错。
    """
    record = _current.get()
    if record is None:
        return
    record.llm_calls.append(call)


def set_username(username: str) -> None:
    """把已鉴权的用户名写进当前记录。

    由 ``security.get_current_user`` 调用——那里是**唯一**能确定身份的地方
    （身份只来自校验过的 JWT）。中间件本身不解析 token：那会把鉴权逻辑
    复制成两份，早晚不一致。
    """
    record = _current.get()
    if record is not None and username:
        record.username = username
