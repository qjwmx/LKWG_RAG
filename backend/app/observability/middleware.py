"""请求轨迹中间件（纯 ASGI）。

为什么不用 ``BaseHTTPMiddleware``
--------------------------------
它是纯 ASGI 中间件：直接包 ``send``，不经过 Starlette 的 ``Request`` 包装层。
理由：

1. **少一层开销**。``BaseHTTPMiddleware`` 会为每个请求建 ``anyio`` 任务组与
   内存对象流。埋点本来就该尽量轻。
2. **拿到的是最原始的响应帧**，抓状态码更直接。

（注：``BaseHTTPMiddleware`` 在当前版本**不会**破坏 SSE，已实测确认。
选纯 ASGI 是为了开销，不是因为 SSE 有问题。）

关键设计
--------
- **只记路由模板**（``scope["route"].path``），不记原始路径。否则
  ``/pets/火苗``、``/pets/水灵`` 会各自变成一行指标，聚合页被打散成几千条。
  404 时 ``scope["route"]`` 是 ``None``，此时回落原始路径并**只保留前两段**，
  防止把任意 id 灌进指标。
- **绝不读 query string / 请求体 / header**。那些里面有口令、token、
  文档内容与提问原文。可观测性要回答"慢在哪"，不是留一份用户行为副本。
- 用户名由 ``security.get_current_user`` 写进 contextvar（那里是唯一能确定
  身份的地方）。中间件**不解析 token**——那会把鉴权逻辑复制成两份。
- 整段包 ``try/except``：**埋点失败绝不能让请求失败**。
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.observability import context as ctx
from app.observability.writer import get_writer

logger = logging.getLogger(__name__)

# 状态码 >= 这个值算错误（用于总览里的错误率）
ERROR_STATUS_THRESHOLD = 400
# 404 回落时保留的路径段数。``/api/v1`` 这样的前缀本身没有信息量，
# 保留它 + 下一段既能归类又不会泄露具体 id。
_FALLBACK_SEGMENTS = 2
# 错误信息截断长度。异常信息可能很长，也可能带敏感内容，只留开头。
_MAX_ERROR_LEN = 300


def _route_label(scope: Scope) -> str:
    """取用于聚合的路由标签。

    优先用 FastAPI 匹配到的**路由模板**（``/api/v1/pets/{name}``），
    这是让指标可聚合的关键——已实测两个不同的 id 会收敛成同一个模板。

    没匹配到路由（404）时回落：取路径前两段。这样
    ``/api/v1/whatever/123`` 归到 ``/api/v1/whatever``，
    而不是每个不存在的路径都变成一行。
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path

    raw = scope.get("path") or ""
    parts = [p for p in raw.split("/") if p]
    if not parts:
        return "/"
    return "/" + "/".join(parts[:_FALLBACK_SEGMENTS])


class ObservabilityMiddleware:
    """记录每个 HTTP 请求的路由、状态、耗时与用户名。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 只处理 HTTP。websocket / lifespan 直接透传——
        # 对 lifespan 包一层会让启动事件也产生一条"请求"记录。
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 关闭时直接透传，零开销。排查性能问题时不该让埋点成为变量。
        if not settings.observability_enabled:
            await self.app(scope, receive, send)
            return

        record = ctx.new_record()
        record.method = scope.get("method") or ""
        record.route = _route_label(scope)
        started = time.perf_counter()

        # 设进 contextvar：端点（线程池里）与 LLM 回调都会往里写。
        # 注意端点里是**原地改**这个对象，不是 set() 新值（见 context.py）。
        token = ctx.set_record(record)

        async def send_wrapper(message: Message) -> None:
            # 抓状态码。不缓冲 body——那会破坏流式响应。
            if message["type"] == "http.response.start":
                try:
                    record.status = int(message.get("status") or 0)
                except Exception:  # noqa: BLE001
                    pass
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # 业务异常要**继续抛**（让 FastAPI 的异常处理器照常工作），
            # 但先把轨迹记下来——异常请求恰恰是最需要被观测到的。
            record.error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
            if not record.status:
                record.status = 500
            raise
        finally:
            try:
                record.duration_ms = (time.perf_counter() - started) * 1000
                # 路由模板可能是在请求处理过程中才确定的（Starlette 匹配发生在
                # 中间件之后），所以这里**再取一次**，覆盖 __call__ 开头那次。
                # 不这么做的话，所有请求的 route 都会是回落值，聚合页全乱。
                resolved = _route_label(scope)
                if resolved:
                    record.route = resolved
                get_writer().submit(record)
            except Exception:  # noqa: BLE001 - 埋点绝不能影响响应
                logger.debug("请求轨迹投递失败", exc_info=True)
            finally:
                # 必须重置：线程/任务复用时，下一个请求会读到上一个的残留记录
                ctx.reset_record(token)
