"""极小的 OpenAI 兼容对话服务，仅用于本地冒烟测试。

为什么需要它：验证"逐字流式"必须有一个真的会分块吐字的服务端。
如果只测错误路径，LangGraph 的 generate + persist 节点等于没被跑过——
而那正是最容易写错的地方（流式事件形状、落库时机）。

它实现两个端点：
- ``POST /v1/chat/completions``：支持 ``stream=true``（返回 SSE）与 false。
- 其余请求一律 404，方便发现意外的调用。

只依赖标准库，不引入额外依赖。
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ANSWER = (
    "### 结论\n"
    "以寂灭骨龙为核心，可以走「三首领放空大脑队」的思路："
    "利用它的龙/幽双属性与高物攻，配合队友处理它的幽、草、武、地四个弱点。\n\n"
    "### 推荐阵容\n"
    "- **三首领放空大脑队**（作者：小火炉）：白金独角兽、冰钻布鲁斯、"
    "黑猫巫师、寂灭骨龙、音速犬、圆号鱼\n"
    "- 这套阵容里寂灭骨龙走物攻向（性格平和，个体生命/物防/魔防），"
    "技能带隼鳞、吓退、偷袭、先发制人。\n\n"
    "### 属性与克制分析\n"
    "寂灭骨龙是龙/幽属性，被幽、草、武、地克制。"
    "队伍的攻击覆盖了光、冰、地、幻、幽、恶、机械、火、翼、草、虫、龙，"
    "但打不动武、毒、水、电、萌这五个属性，需要注意。\n\n"
    "### 使用要点\n"
    "1. 先用圆号鱼或黑猫巫师处理对手的草、地属性精灵；\n"
    "2. 寂灭骨龙避免直接对上武系与幽系的强攻手；\n"
    "3. 轮转时保留音速犬作为火系收割手。\n\n"
    "### 风险提示\n"
    "以上阵容来自 BWIKI 玩家投稿，**不代表对局使用率或强度排名**。"
    "赛季环境会变化，请结合当前版本实际对局判断。"
)

# 切成小片模拟真实模型逐 token 输出
PIECES = [ANSWER[i : i + 6] for i in range(0, len(ANSWER), 6)]


def _wants_json(body: dict) -> bool:
    """请求是不是结构化输出。"""
    fmt = body.get("response_format") or {}
    if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "json_object"):
        return True
    # 有些客户端用 tool calling 实现结构化输出
    return bool(body.get("tools"))


def _schema_name(body: dict) -> str:
    """尽量取出 schema 名，取不到返回空串。"""
    fmt = body.get("response_format") or {}
    if isinstance(fmt, dict):
        js = fmt.get("json_schema") or {}
        if js.get("name"):
            return str(js["name"])
        schema = js.get("schema") or {}
        if schema.get("title"):
            return str(schema["title"])
        # 有些实现把 schema 直接放 response_format 下
        if fmt.get("schema", {}).get("title"):
            return str(fmt["schema"]["title"])
    tools = body.get("tools") or []
    if tools:
        fn = tools[0].get("function") or {}
        if fn.get("name"):
            return str(fn["name"])
    return ""


def _schema_properties(body: dict) -> dict:
    """取出 JSON schema 的 properties，用于**按结构而不是按名字**识别。"""
    fmt = body.get("response_format") or {}
    if isinstance(fmt, dict):
        js = fmt.get("json_schema") or {}
        schema = js.get("schema") or fmt.get("schema") or {}
        if isinstance(schema, dict) and schema.get("properties"):
            return schema["properties"]
    tools = body.get("tools") or []
    if tools:
        params = (tools[0].get("function") or {}).get("parameters") or {}
        if params.get("properties"):
            return params["properties"]
    return {}


def _json_for_schema(name: str, properties: dict | None = None) -> dict:
    """按 schema 名**或字段结构**给出合规的假数据。

    优先看字段结构：LangChain 给 schema 起的名字随版本变
    （有时是类名、有时带前缀），只靠名字匹配会在升级后静默失配——
    表现成"规划失败：AssertionError"，排查方向完全错。
    """
    props = properties or {}
    lowered = (name or "").lower()
    keys = set(props.keys())

    # 按字段结构识别（最可靠）
    if {"brief", "units"} <= keys:
        return {
            "brief": "研究寂灭骨龙的 PvP 阵容搭配",
            "units": [
                {"topic": "寂灭骨龙适合什么阵容", "reason": "核心问题"},
                {"topic": "寂灭骨龙怕什么属性", "reason": "风险分析"},
            ],
        }
    if {"unit_id", "summary"} <= keys:
        return {
            "unit_id": "u0",
            "topic": "寂灭骨龙适合什么阵容",
            "summary": "寂灭骨龙在投稿阵容中常与黑猫巫师、圆号鱼同队。",
            "evidence": ["三首领放空大脑队（作者：小火炉）"],
            "lineup_ids": [],
        }
    if "verdict" in keys:
        return {"verdict": "pass", "notes": "", "evidence_gaps": []}

    # 退回到按名字匹配
    if "plan" in lowered:
        return {
            "brief": "研究寂灭骨龙的 PvP 阵容搭配",
            "units": [
                {"topic": "寂灭骨龙适合什么阵容", "reason": "核心问题"},
                {"topic": "寂灭骨龙怕什么属性", "reason": "风险分析"},
            ],
        }
    if "finding" in lowered:
        return {
            "unit_id": "u0",
            "topic": "寂灭骨龙适合什么阵容",
            "summary": "寂灭骨龙在投稿阵容中常与黑猫巫师、圆号鱼同队。",
            "evidence": ["三首领放空大脑队（作者：小火炉）"],
            "lineup_ids": [],
        }
    if "review" in lowered or "verdict" in lowered:
        return {"verdict": "pass", "notes": "", "evidence_gaps": []}
    # 兜底：给一个空对象，让调用方报出清晰的校验错误而不是静默通过
    return {}




class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: ANN002 - 静音访问日志
        return

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return

        if os.environ.get("FAKE_LLM_DEBUG"):
            keys = sorted(body.keys())
            props = sorted(_schema_properties(body).keys())
            print(
                f"[fake-llm] json={_wants_json(body)} name={_schema_name(body)!r} "
                f"props={props} top_keys={keys}",
                flush=True,
            )

        # 结构化输出请求：LangChain 会带 response_format=json_schema。
        # 不识别它的话，Planner/Reviewer 会拿到 markdown 然后 JSON 解析失败——
        # 表现成"规划失败"，而其实是假模型不会返回 JSON。
        #
        # **注意 stream 与 response_format 可能同时出现**：LangChain 的结构化输出
        # 内部走流式（``_stream`` + ``get_final_completion``）。这时必须返回 SSE，
        # 返回普通 JSON 会让客户端在拼装最终结果时断言失败
        # （``AssertionError`` 且没有消息，极难定位）。
        if _wants_json(body):
            payload = _json_for_schema(_schema_name(body), _schema_properties(body))
            text = json.dumps(payload, ensure_ascii=False)
            if body.get("stream"):
                self._stream_text(text)
            else:
                self._json_once(text)
            return

        if body.get("stream"):
            self._stream()
        else:
            self._once()

    def _json_once(self, text: str) -> None:
        """返回普通（非流式）JSON 响应。"""
        data = json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_text(self, text: str) -> None:
        """把一段文本按 SSE 分块吐出（供结构化输出使用）。"""
        self._begin_stream()
        step = 24
        for index, i in enumerate(range(0, len(text), step)):
            self._frame_text(text[i : i + step], first=(index == 0))
            time.sleep(0.002)
        self._end_stream()

    # ------------------------------------------------------------------ 流式辅助

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # 长度未知，用 chunked 传输
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, payload: dict) -> None:
        data = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _frame_text(self, piece: str, first: bool = False) -> None:
        # 第一个 chunk 必须带 role：LangChain 会用累积的 delta 拼出最终
        # completion，缺 role 时在 ``_convert_dict_to_message`` 抛
        # ValidationError（role 不能是 None），且报错位置离真正原因很远。
        delta: dict = {"content": piece}
        if first:
            delta["role"] = "assistant"
        self._chunk(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "fake",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": None}
                ],
            }
        )

    def _end_stream(self) -> None:
        self._chunk(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "fake",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):X}\r\n".encode("ascii") + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _stream(self) -> None:
        """正文流式：逐片吐 ANSWER。"""
        self._begin_stream()
        for index, piece in enumerate(PIECES):
            self._frame_text(piece, first=(index == 0))
            time.sleep(0.005)
        self._end_stream()

    def _once(self) -> None:
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "fake",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ANSWER},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8021), Handler).serve_forever()
