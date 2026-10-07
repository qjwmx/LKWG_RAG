"""可观测性：请求轨迹与 LLM 用量采集。

**数据只有管理员能看**（见 ``routes/admin.py`` 的 ``/observability/*``，
挂在 ``require_admin`` 依赖下）。

四个模块：

- ``context``    —— contextvar 请求上下文（穿过 FastAPI 线程池）
- ``middleware`` —— 纯 ASGI 中间件，记录路由/状态/耗时/用户名
- ``llm``        —— LangChain 回调，按 Agent 记录 token 与耗时
- ``writer``     —— 有界队列 + 后台线程批量落库

设计上的三条硬约束：

1. **不记录内容**。只记结构性信息（路由模板、状态码、耗时、用户名），
   绝不记 query string、请求体、header、token、文档内容或提问原文。
   可观测性要回答"慢在哪 / 错在哪"，不是留一份能还原用户行为的副本。
2. **不反压业务**。队列满了就丢弃并计数，埋点失败全部吞掉。
   拿可用性换指标是方向反了。
3. **开销要低**。入队实测 1.2µs；落库走 WAL + 批量，约 0.02ms/行。
   同步写 SQLite 单条要 2ms，会把 3ms 的接口拖慢一倍。
"""

from app.observability.context import (
    LlmCall,
    RequestRecord,
    current_record,
    new_record,
    record_llm_call,
    set_username,
)
from app.observability.llm import HANDLER as LLM_HANDLER
from app.observability.middleware import ObservabilityMiddleware
from app.observability.writer import get_writer

__all__ = [
    "LLM_HANDLER",
    "LlmCall",
    "ObservabilityMiddleware",
    "RequestRecord",
    "current_record",
    "get_writer",
    "new_record",
    "record_llm_call",
    "set_username",
]
