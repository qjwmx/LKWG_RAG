"""LLM 调用指标：LangChain 回调。

为什么用回调而不是在每个节点里手写埋点
--------------------------------------
五 Agent 流水线里模型调用散落在 6+ 个地方（Planner / Researcher / Analyst /
Writer / Reviewer / 结构化输出）。在每处手写计时代码有两个问题：

1. **会漏**。新增一个节点时没人记得加埋点，而漏掉的表现是"统计少了一块"，
   不报错，很难发现。
2. **要重复处理 usage 解析**。不同调用路径（``invoke`` / ``stream`` /
   ``with_structured_output``）返回结构不同。

LangChain 的回调机制在**模型构造处**挂一次就全覆盖，而且能拿到
``metadata.langgraph_node``——于是"哪个 Agent 最贵"这个问题自然有答案。

实测确认（本项目环境）：
- 回调能在 LangGraph 节点内触发
- ``metadata.langgraph_node`` 有值，可归因到具体 Agent
- ``usage_metadata`` 在 ``invoke`` 与 ``stream`` 下都能拿到
- **构造期**绑定 ``callbacks=[handler]`` 即可，不必每次传 config

拿不到用量时不猜
----------------
上游（某些兼容端点）可能不返回 usage。这时**记 0 并置 usage_missing**，
而不是估算 token 数——估算值会污染成本统计，让"到底花了多少"变成
一个看起来精确、实际虚构的数字。
"""

from __future__ import annotations

import logging
import time

from langchain_core.callbacks import BaseCallbackHandler

from app.observability.context import LlmCall, record_llm_call

logger = logging.getLogger(__name__)


class LlmMetricsHandler(BaseCallbackHandler):
    """把每次模型调用的耗时与 token 用量记进当前请求。

    **绝不能抛异常**：这是旁路观测，任何失败都不该影响问答本身。
    每个回调都包了 try/except。
    """

    def __init__(self) -> None:
        super().__init__()
        # run_id -> 起始时间。用 dict 而不是单个变量：Researcher 是**并行**的，
        # 同一时刻可能有多个调用在飞，用单变量会互相覆盖（耗时算成负数或错值）。
        self._started: dict[str, float] = {}
        # run_id -> 该次调用的元信息（节点名、是否结构化）
        self._meta: dict[str, dict] = {}

    # ---------------------------------------------------------------- 开始

    def on_llm_start(self, serialized, prompts, **kwargs):  # noqa: ANN001
        try:
            run_id = str(kwargs.get("run_id") or "")
            metadata = kwargs.get("metadata") or {}
            tags = kwargs.get("tags") or []

            self._started[run_id] = time.monotonic()
            self._meta[run_id] = {
                # LangGraph 会给每次节点内调用打上节点名，这是"按 Agent 归因"的依据
                "agent": str(metadata.get("langgraph_node") or "") or "unknown",
                # nostream 标签 = 结构化输出调用（不推给前端）
                "structured": "nostream" in tags,
                "model": str((serialized or {}).get("name") or ""),
            }
        except Exception:  # noqa: BLE001 - 埋点失败绝不影响业务
            logger.debug("on_llm_start 埋点失败", exc_info=True)

    def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: ANN001
        """``ChatOpenAI`` 走的是这个钩子，不是 ``on_llm_start``。

        **两个都要实现**：只实现 ``on_llm_start`` 的话，对话模型（本项目
        全部调用都是对话模型）一次都记不到，而统计页面看起来只是"没有数据"。
        """
        self.on_llm_start(serialized, messages, **kwargs)

    # ---------------------------------------------------------------- 结束

    def on_llm_end(self, response, **kwargs):  # noqa: ANN001
        try:
            run_id = str(kwargs.get("run_id") or "")
            started = self._started.pop(run_id, None)
            meta = self._meta.pop(run_id, {})
            duration_ms = (time.monotonic() - started) * 1000 if started else 0.0

            input_tokens = output_tokens = total = 0
            missing = True

            # 优先取 message.usage_metadata（新式、结构清晰）
            usage = None
            try:
                generations = getattr(response, "generations", None) or []
                if generations and generations[0]:
                    message = getattr(generations[0][0], "message", None)
                    usage = getattr(message, "usage_metadata", None)
            except Exception:  # noqa: BLE001
                usage = None

            if usage:
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                total = int(usage.get("total_tokens") or (input_tokens + output_tokens))
                missing = False
            else:
                # 回退到 llm_output.token_usage（老式 OpenAI 形状）
                llm_output = getattr(response, "llm_output", None) or {}
                token_usage = llm_output.get("token_usage") or {}
                if token_usage:
                    input_tokens = int(token_usage.get("prompt_tokens") or 0)
                    output_tokens = int(token_usage.get("completion_tokens") or 0)
                    total = int(
                        token_usage.get("total_tokens") or (input_tokens + output_tokens)
                    )
                    missing = False

            model = meta.get("model") or ""
            try:
                llm_output = getattr(response, "llm_output", None) or {}
                model = llm_output.get("model_name") or model
            except Exception:  # noqa: BLE001
                pass

            record_llm_call(
                LlmCall(
                    agent=meta.get("agent") or "unknown",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total,
                    duration_ms=duration_ms,
                    structured=bool(meta.get("structured")),
                    usage_missing=missing,
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("on_llm_end 埋点失败", exc_info=True)

    # ---------------------------------------------------------------- 错误

    def on_llm_error(self, error, **kwargs):  # noqa: ANN001
        """模型调用失败。

        **不记录 token**（没有），但要清掉 ``_started``，否则并行调用多时
        这个 dict 会一直增长（内存泄漏）。
        """
        try:
            run_id = str(kwargs.get("run_id") or "")
            self._started.pop(run_id, None)
            self._meta.pop(run_id, None)
        except Exception:  # noqa: BLE001
            logger.debug("on_llm_error 埋点失败", exc_info=True)


# 模块级单例。**在模型构造处绑定**（见 rag_graph.get_chat_model /
# strategy_graph._model），这样所有调用路径自动覆盖，不必在每个节点里传 config。
#
# 必须是单例：每次 new 一个 handler，`_started` 就无法把 start 与 end 配对，
# 耗时与 token 都会算错。
HANDLER = LlmMetricsHandler()
