"""FastAPI 应用总装。

启动::

    uvicorn app.main:app --reload --port 8000

所有端点都用 ``def`` 而不是 ``async def``：底层 openai SDK 与 SQLAlchemy 全是
同步的，FastAPI 会把 ``def`` 端点丢进线程池执行，既不阻塞事件循环，
也不用把整条 service 链改写成 async。
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.db.session import init_db
from app.observability import ObservabilityMiddleware
from app.routes import admin, auth, chat, knowledge, public, roco, system
from app.schemas import CODE_BAD_REQUEST, BaseResponse
from app.security import get_current_user
from fastapi import Depends

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _apply_langsmith_env()

    if settings.jwt_secret_auto_generated:
        logger.warning(
            "JWT_SECRET 未配置，已自动生成随机密钥。"
            "本次进程签发的 token 在重启后会全部失效（表现为需要重新登录）。"
            "对外部署请在 backend/.env 里显式设置 JWT_SECRET。"
        )
    if settings.admin_password == "admin":
        logger.warning("管理员口令仍是默认值 admin，对外提供服务前请务必修改。")
    # 建表放在启动期而不是 import 期：import 期连库会让数据库一抖动
    # 整个应用连页面都渲染不出来，且报错位置极具误导性。
    init_db()
    _log_cache_mode()
    _start_observability()
    _warm_up()
    yield
    _stop_observability()


def _start_observability() -> None:
    """启动轨迹写入器的后台线程。

    放在 lifespan 而不是 import 期：import 期起线程会让"导入模块"产生副作用，
    测试里 import 一次就多一个线程。
    """
    if not settings.observability_enabled:
        logger.info("可观测性已关闭（OBSERVABILITY_ENABLED=false），埋点零开销")
        return
    from app.observability import get_writer

    get_writer().start()


def _stop_observability() -> None:
    """停止写入器并尽量排空队列，避免丢掉最后一批轨迹。"""
    if not settings.observability_enabled:
        return
    try:
        from app.observability import get_writer

        get_writer().stop()
    except Exception:  # noqa: BLE001 - 关闭阶段的失败不该让进程退出码变脏
        logger.debug("停止可观测性写入器失败", exc_info=True)


def _log_cache_mode() -> None:
    """启动时说明缓存用的是 Redis 还是进程内实现。

    **必须显式说清楚**：多实例部署时进程内实现意味着登录限流与令牌吊销
    不跨实例共享（在 A 实例登出的令牌，B 实例仍然接受）。这是"看起来正常、
    实则有安全缺口"的情况，不能只靠读代码发现。
    """
    from app.cache import health as cache_health
    from app.cache import is_configured

    ok, detail = cache_health()
    if not is_configured():
        logger.info("Redis 未配置（REDIS_URL 为空），使用进程内缓存：%s", detail)
        logger.info(
            "单进程部署下这没有问题；多实例部署请配置 REDIS_URL，"
            "否则登录限流与令牌吊销不跨实例生效。"
        )
    elif ok:
        logger.info("Redis 已启用：%s", detail)
    else:
        logger.warning("Redis 配置了但不可用：%s", detail)


def _warm_up() -> None:
    """启动时预热嵌入模型。

    **这不是可选优化，而是首屏体验的关键。** 实测：Ollama 第一次
    ``embed_query`` 要 **3.3 秒**（把模型加载进显存），之后每次只要 0.36 秒。
    不预热的话，用户当天的第一个问题会莫名多等约 3 秒，而且现象是
    "有时快有时慢"，很难归因到嵌入服务上。

    预热放在**后台线程**里：加载模型要几秒，卡在 lifespan 里会让
    uvicorn 迟迟不开始监听，健康检查与反向代理会误判成启动失败。
    失败只记日志——嵌入服务没起来时问答本来就该给出可读错误，
    不能因为预热失败就让整个服务起不来。
    """
    import threading

    def _run() -> None:
        try:
            from app.services.embeddings import get_provider, is_degraded

            if is_degraded():
                # hash 伪嵌入没有模型可加载，跳过
                return
            started = time.monotonic()
            get_provider().embed_query("预热")
            logger.info("嵌入模型预热完成（%.2fs）", time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - 预热失败不该影响启动
            logger.warning(
                "嵌入模型预热失败（不影响启动，首个问题会慢一些）：%s: %s",
                type(exc).__name__,
                exc,
            )

    threading.Thread(target=_run, name="embedding-warmup", daemon=True).start()


def _apply_langsmith_env() -> None:
    """把 ``LANGCHAIN_*`` 配置转成 LangChain 认的环境变量。

    **不能只放在 ``Settings`` 里**：LangSmith 追踪是由 LangChain 自己读
    ``LANGCHAIN_TRACING_V2`` / ``LANGCHAIN_API_KEY`` **环境变量**决定的，
    它不认识我们的 pydantic 配置对象。只写进 ``.env`` 而不做这一步的话，
    用户填了 key 却什么都不会发生——而且不报错，最难排查的那类问题。

    注意变量名是 ``LANGCHAIN_TRACING_V2``（带 V2）：LangChain v1 认的是这个，
    旧的 ``LANGCHAIN_TRACING`` 已不生效。
    """
    if not settings.langchain_tracing:
        return
    if not settings.langchain_api_key:
        logger.warning("LANGCHAIN_TRACING 已开启但没填 LANGCHAIN_API_KEY，追踪不会生效。")
        return

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
    # 显式关掉旧的变量名，避免两套配置互相干扰
    os.environ.pop("LANGCHAIN_TRACING", None)
    logger.info("LangSmith 追踪已开启（project=%s）", settings.langchain_project)


def create_app() -> FastAPI:
    app = FastAPI(
        title="RAG 知识库 API",
        description=(
            "企业内部制度与流程问答服务。问答走 OpenAI 兼容的对话模型，"
            "嵌入可走本地 Ollama 或任意兼容服务，持久化默认 SQLite。"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    # 开发期前端走 Vite 代理，本不需要 CORS；这里放开是为了支持前后端分机部署。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 可观测性埋点。**必须最后添加**——Starlette 里后添加的中间件在最外层，
    # 这样它包住 CORS：响应经过本中间件时 CORS 头已由内层附加，
    # 我们只读状态码、不改响应，顺序上没有冲突，但放外层能观测到
    # 被内层中间件直接短路掉的请求（例如 CORS 预检被拒）。
    app.add_middleware(ObservabilityMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        """把 422 校验错误也纳入统一响应格式。

        否则客户端要同时处理两种错误形状：BaseResponse 和 FastAPI 默认的
        ``{"detail": [...]}``。
        """
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())[1:]) or "请求体"
        return JSONResponse(
            status_code=200,
            content=BaseResponse.error(
                f"参数校验失败：{location} {first.get('msg', '')}".strip(),
                code=CODE_BAD_REQUEST,
            ).model_dump(),
        )

    # 鉴权按 router 挂而不是挂在 FastAPI() 上：全局依赖会连 /docs 一起挡掉。
    auth_dep = [Depends(get_current_user)]
    app.include_router(system.router, dependencies=auth_dep)
    app.include_router(knowledge.router, dependencies=auth_dep)
    app.include_router(chat.router, dependencies=auth_dep)
    # 洛克王国领域接口（图鉴 / 阵容 / 五 Agent 攻略研究）
    app.include_router(roco.router, dependencies=auth_dep)
    # admin router 自带 require_admin，不再重复挂 get_current_user
    app.include_router(admin.router)
    # auth router 刻意不挂鉴权：注册与登录面向的是尚未登录的用户
    app.include_router(auth.router)
    # public router 同样不挂鉴权：登录页要拿展示数据。
    # **只暴露精灵图鉴的公开信息**，见其模块说明。
    app.include_router(public.router)

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse(url="/docs")

    return app


app = create_app()
