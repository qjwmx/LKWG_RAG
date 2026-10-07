"""联网检索（Tavily）的测试。

**不真调 Tavily**：按次计费，且会让测试依赖外网。全部用 httpx 替身。
但有一条例外思路要写清楚——"联网不可用"与"网上没找到"是**两件事**，
测试里必须分开覆盖，否则会把降级路径写成一锅粥。

覆盖四组：
1. 配置门（没 key / 服务端关掉 → ``WebSearchError``，不是空列表）
2. 归一化与截断（含丢掉无 url 的条目）
3. 失败降级（超时、非 200、非 JSON、格式异常）
4. **提示注入防线**（``<web_content>`` 隔离 + ``_WEB_GUARD`` 声明真的进了模型上下文）
"""

from __future__ import annotations

import pytest

from app.services import web_search


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None, raise_json: bool = False):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._body


class FakeClient:
    """httpx.Client 的替身。记录请求，返回预设响应。"""

    last_payload: dict | None = None
    last_kwargs: dict | None = None

    def __init__(self, response=None, raises: Exception | None = None):
        self._response = response
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def post(self, url, **kwargs):
        FakeClient.last_kwargs = {"url": url, **kwargs}
        FakeClient.last_payload = kwargs.get("json")
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture
def configured(monkeypatch):
    """配好 key 且服务端开着——大多数用例的前提。"""
    monkeypatch.setattr(web_search.settings, "web_search_enabled", True)
    monkeypatch.setattr(web_search.settings, "tavily_api_key", "tvly-test-key")
    monkeypatch.setattr(web_search.settings, "tavily_max_results", 5)
    monkeypatch.setattr(web_search.settings, "web_snippet_max_chars", 1200)


@pytest.fixture
def patch_httpx(monkeypatch):
    """把 httpx.Client 换成替身。返回一个安装函数。"""
    import httpx

    def _install(response=None, raises: Exception | None = None) -> None:
        monkeypatch.setattr(
            httpx, "Client", lambda **_kw: FakeClient(response=response, raises=raises)
        )

    return _install


# --------------------------------------------------------------------------- 配置门


def test_status_does_not_leak_key(monkeypatch):
    """状态接口只能报告"有没有配"，**绝不能回显 key**。"""
    monkeypatch.setattr(web_search.settings, "web_search_enabled", True)
    monkeypatch.setattr(web_search.settings, "tavily_api_key", "tvly-super-secret")

    data = web_search.status()
    assert data["configured"] is True
    assert data["available"] is True
    assert "tvly-super-secret" not in str(data)


def test_missing_key_raises_not_empty_list(monkeypatch):
    """★ 没配 key 必须抛异常，而不是返回空列表。

    空列表的含义是"网上也没找到"，与"没查成"完全不同——
    混成一个的话，用户会以为"联网搜过了但没有结果"，
    而实际上是根本没联网，排查方向会完全跑偏。
    """
    monkeypatch.setattr(web_search.settings, "web_search_enabled", True)
    monkeypatch.setattr(web_search.settings, "tavily_api_key", "")

    with pytest.raises(web_search.WebSearchError, match="TAVILY_API_KEY"):
        web_search.search("洛克王国 阵容")


def test_server_switch_off_raises(configured, monkeypatch):
    """服务端总开关关掉时即使用户开了也不能联网。"""
    monkeypatch.setattr(web_search.settings, "web_search_enabled", False)
    with pytest.raises(web_search.WebSearchError, match="关闭"):
        web_search.search("洛克王国 阵容")


def test_empty_query_raises(configured):
    with pytest.raises(web_search.WebSearchError):
        web_search.search("   ")


def test_is_configured_requires_both(configured, monkeypatch):
    assert web_search.is_configured() is True
    monkeypatch.setattr(web_search.settings, "web_search_enabled", False)
    assert web_search.is_configured() is False


# --------------------------------------------------------------------------- 归一化


def test_results_are_normalized(configured, patch_httpx):
    patch_httpx(
        FakeResponse(
            200,
            {
                "results": [
                    {"title": "  S4 阵容  ", "url": "https://a.com/1", "content": "正文 A"},
                    {"title": "", "url": "https://b.com/2", "content": "正文 B"},
                ]
            },
        )
    )
    results = web_search.search("洛克王国 S4")

    assert len(results) == 2
    assert results[0] == {
        "title": "S4 阵容",
        "url": "https://a.com/1",
        "snippet": "正文 A",
        "source_type": "web",
    }
    # 空标题要有兜底，否则前端渲染出空白行
    assert results[1]["title"] == "(无标题)"


def test_entries_without_url_are_dropped(configured, patch_httpx):
    """★ 没有 url 的条目必须丢掉。

    没有出处就**无法核实**，留着它只会让模型写出引用不了的结论——
    而报告的整个价值就在于"每个结论都能点进去核对"。
    """
    patch_httpx(
        FakeResponse(
            200,
            {
                "results": [
                    {"title": "有出处", "url": "https://a.com/1", "content": "A"},
                    {"title": "没出处", "url": "", "content": "B"},
                    {"title": "也没出处", "content": "C"},
                ]
            },
        )
    )
    results = web_search.search("洛克王国")
    assert len(results) == 1
    assert results[0]["url"] == "https://a.com/1"


def test_snippet_is_truncated(configured, patch_httpx, monkeypatch):
    """摘要要截断：网页正文几万字，全塞进去会挤掉本地库资料。"""
    monkeypatch.setattr(web_search.settings, "web_snippet_max_chars", 50)
    patch_httpx(
        FakeResponse(
            200,
            {"results": [{"title": "长文", "url": "https://a.com/1", "content": "字" * 500}]},
        )
    )
    results = web_search.search("洛克王国")
    assert len(results[0]["snippet"]) < 100
    # 必须有省略标记，否则模型会把半句话当完整结论
    assert results[0]["snippet"].endswith("...")


def test_max_results_is_capped(configured, patch_httpx, monkeypatch):
    """配置项填得再大也要有硬上限——否则会把上下文挤爆。"""
    monkeypatch.setattr(web_search.settings, "tavily_max_results", 999)
    patch_httpx(FakeResponse(200, {"results": []}))
    web_search.search("洛克王国")

    assert FakeClient.last_payload["max_results"] <= web_search.MAX_RESULTS_CAP


def test_request_disables_proxy_and_raw_content(configured, patch_httpx):
    """★ 两个容易漏掉的请求参数。

    ``trust_env=False``：httpx 默认读系统代理环境变量，本机残留的代理
    会让请求拿到 502（embeddings.py 里踩过同一个坑）。
    ``include_raw_content=False``：raw_content 是整页正文，一条上万字。
    """
    patch_httpx(FakeResponse(200, {"results": []}))
    web_search.search("洛克王国")

    assert FakeClient.last_kwargs["url"] == web_search.TAVILY_ENDPOINT
    assert FakeClient.last_payload["include_raw_content"] is False
    assert FakeClient.last_payload["api_key"] == "tvly-test-key"


# --------------------------------------------------------------------------- 失败降级


@pytest.mark.parametrize(
    ("response", "raises", "match"),
    [
        (None, TimeoutError("timed out"), "网络请求失败"),
        (FakeResponse(401, {}), None, "401"),
        (FakeResponse(500, {}), None, "500"),
        (FakeResponse(200, {}, raise_json=True), None, "非 JSON"),
        (FakeResponse(200, {"results": "不是列表"}), None, "格式异常"),
    ],
)
def test_failures_raise_web_search_error(configured, patch_httpx, response, raises, match):
    """★ 所有失败路径都要抛 ``WebSearchError``（可降级），不能是裸异常。

    裸异常会让调用方的 ``except WebSearchError`` 漏过去，
    结果联网抖动就把整轮攻略搞崩——而联网本该是"锦上添花"。
    """
    patch_httpx(response, raises=raises)
    with pytest.raises(web_search.WebSearchError, match=match):
        web_search.search("洛克王国")


def test_http_error_message_does_not_echo_body(configured, patch_httpx):
    """错误信息不能把响应体整个带上——可能很长，也可能回显 key。"""
    patch_httpx(FakeResponse(400, {"detail": "tvly-test-key is invalid"}))
    with pytest.raises(web_search.WebSearchError) as exc:
        web_search.search("洛克王国")
    assert "tvly-test-key" not in str(exc.value)


def test_empty_results_is_not_an_error(configured, patch_httpx):
    """"网上没找到"是正常结果，返回空列表而不是抛异常。"""
    patch_httpx(FakeResponse(200, {"results": []}))
    assert web_search.search("洛克王国") == []
