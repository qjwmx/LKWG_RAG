"""攻略模式 Checkpointer 与多轮追问的测试。

这些用例覆盖的每一个点都对应一个**不报错但结果错**的坑：

1. ``operator.add`` 跨轮累积 —— 上一轮的研究发现混进本轮，Analyst 张冠李戴。
   而且输入传 ``[]`` **清不掉**（实测 ``[] + 旧值 = 旧值``）。
2. ``revision_count`` 跨轮累加 —— 第二轮就误判"已达上限"，Reviewer 直接放行。
3. ``turn_history`` 无界增长 —— 上下文越跑越长。
4. ``thread_id`` 不按用户隔离 —— 传别人的 session_id 就能读到别人的追问上下文。
5. 联网结果跨轮累积 —— 上一轮抓的网页被当成这一轮的证据。
"""

from __future__ import annotations

import pytest

from app.services import checkpoint, strategy_graph

REPORT = "### 结论\n测试报告内容。\n\n### 风险提示\n阵容来自玩家投稿。"


# --------------------------------------------------------------------------- reducer 单元


def test_reset_sentinel_clears():
    """★ sentinel 是唯一能清空 ``operator.add`` 字段的手段。"""
    assert checkpoint.reset_add(["旧1", "旧2"], checkpoint.RESET) == []


def test_plain_empty_list_does_not_clear():
    """★ 反面用例：这就是必须引入 sentinel 的原因。

    传 ``[]`` 得到 ``旧值 + [] = 旧值``——**看起来重置了，其实一个字节都没清**。
    这个 bug 不报错，只会让 Analyst 拿到上一轮的研究发现。
    """
    assert checkpoint.reset_add(["旧1", "旧2"], []) == ["旧1", "旧2"]


def test_reset_add_accumulates_normally():
    """不带 sentinel 时行为要和 ``operator.add`` 一致（扇出靠它）。"""
    assert checkpoint.reset_add(["a"], ["b"]) == ["a", "b"]
    assert checkpoint.reset_add(["a"], "b") == ["a", "b"]
    assert checkpoint.reset_add(None, ["b"]) == ["b"]
    assert checkpoint.reset_add(["a"], None) == ["a"]


def test_bounded_add_keeps_last_n():
    """★ 有界累积：历史不能无上限增长。"""
    reducer = checkpoint.bounded_add(3)
    value: list = []
    for i in range(10):
        value = reducer(value, [f"t{i}"])
    assert value == ["t7", "t8", "t9"]


def test_bounded_add_respects_sentinel():
    reducer = checkpoint.bounded_add(3)
    assert reducer(["a", "b"], checkpoint.RESET) == []


# --------------------------------------------------------------------------- thread_id 隔离


def test_thread_id_includes_username():
    """★ 不同用户即使 session_id 相同，也是不同线程。"""
    assert checkpoint.thread_id_for("alice", "s1") != checkpoint.thread_id_for("bob", "s1")


def test_thread_id_rejects_path_traversal():
    """★ session_id 来自客户端，必须消毒。

    不消毒的话，``../../`` 这类值会进 checkpoint 表当键，
    虽不直接读文件，但会污染数据、让裁剪逻辑失效。
    """
    tid = checkpoint.thread_id_for("alice", "../../etc/passwd")
    assert ".." not in tid
    assert "/" not in tid


def test_normalize_session_id_generates_when_invalid():
    """非法 session_id **不抛异常**，生成临时 id 继续跑。"""
    generated = checkpoint.normalize_session_id("")
    assert generated.startswith("strat_")
    assert generated != checkpoint.normalize_session_id("")

    # 合法值原样保留
    assert checkpoint.normalize_session_id("rag_abc-123") == "rag_abc-123"


def test_thread_id_sanitizes_username():
    """用户名里的特殊字符不能破坏前缀边界。"""
    tid = checkpoint.thread_id_for("ali:ce", "s1")
    assert tid.count(":") == 1, f"用户名里的冒号必须被替换掉：{tid}"


# --------------------------------------------------------------------------- 跨轮状态


def _patch(monkeypatch, script):
    from tests.test_strategy import FakeModel, FakeStructured

    strategy_graph.reset_graph()
    monkeypatch.setattr(
        strategy_graph, "_structured", lambda schema, tags=None: FakeStructured(schema, script)
    )
    monkeypatch.setattr(
        strategy_graph, "_model", lambda tags=None, temperature=0.2: FakeModel(script)
    )


def _run(query: str, session_id: str, **kwargs):
    from app.services import strategy_service

    return list(
        strategy_service.strategy_stream(
            query=query,
            target_pet="寂灭骨龙",
            max_revisions=kwargs.pop("max_revisions", 3),
            username="alice",
            session_id=session_id,
            **kwargs,
        )
    )


def _done(events: list[dict]) -> dict:
    for event in events:
        if event["type"] == "done":
            return event
    raise AssertionError("没有 done 事件")


def test_findings_reset_between_turns(monkeypatch, seeded_domain):
    """★ 核心用例：第二轮 ``findings`` 不能带上第一轮的。

    不带 sentinel 的话第二轮会变成 2 条（上轮 1 条 + 本轮 1 条），
    Analyst 会把上一轮的研究结论当成这一轮的证据——
    **不报错**，只是报告开始张冠李戴。
    """
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)

    first = _done(_run("推荐一套阵容", "sess-reset"))
    second = _done(_run("那换成水系呢", "sess-reset"))

    assert len(first["findings"]) == 1, f"第一轮应 1 条，实际 {len(first['findings'])}"
    assert len(second["findings"]) == 1, (
        f"第二轮 findings 累积了：{len(second['findings'])} 条（应为 1）"
    )


def test_revision_count_resets_between_turns(monkeypatch, seeded_domain):
    """★ ``revision_count`` 跨轮累加会让第二轮误判"已达上限"。

    第二轮拿到上一轮的 ``revision_count``（比如 1），Reviewer 里
    ``revision + 1`` 就变成 2；跑几轮之后直接 >= max_revisions，
    于是 Reviewer **不再审阅就放行**——表现为"第二轮开始质量明显下降"。
    """
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1, review_rounds=0)
    _patch(monkeypatch, script)

    for turn in range(4):
        result = _done(_run(f"第 {turn} 轮", "sess-rev"))
        assert result["revisions"] == 1, (
            f"第 {turn + 1} 轮 revisions={result['revisions']}（应为 1，跨轮累加了）"
        )

    # 每轮都要真的调 Reviewer，不能因为计数累加而跳过
    assert script.reviews == 4, f"应审 4 次，实际 {script.reviews}"


def test_turn_history_is_kept_and_bounded(monkeypatch, seeded_domain):
    """★ 多轮记忆：保留最近几轮，且有界。

    上限是必须的——不设的话跑几十轮之后上下文和 checkpoint 体积都会失控。
    """
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)

    for turn in range(checkpoint.MAX_TURN_HISTORY + 2):
        _run(f"第 {turn} 轮问法", "sess-hist")

    state = _read_state("alice", "sess-hist")
    history = state.get("turn_history") or []
    assert len(history) == checkpoint.MAX_TURN_HISTORY, (
        f"turn_history 应被裁到 {checkpoint.MAX_TURN_HISTORY}，实际 {len(history)}"
    )
    # 留下的必须是最新的那几轮（顺序也要对，否则"上一轮"指代错轮）
    assert history[-1]["query"] == f"第 {checkpoint.MAX_TURN_HISTORY + 1} 轮问法"
    assert history[-1]["summary"], "记忆里要有结论摘要"


def test_history_is_visible_to_planner(monkeypatch, seeded_domain):
    """★ 多轮记忆要真的**送进模型**，而不是只存在状态里。

    只存不喂的话，Planner 看到的仍然是一句没有主语的追问，
    这个功能就是白做的——而且不报错。
    """
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)

    _run("推荐寂灭骨龙的阵容", "sess-feed")
    script.seen.clear()  # 只看第二轮
    _run("那换成水系呢", "sess-feed")

    text = script.all_text()
    assert "推荐寂灭骨龙的阵容" in text, "上一轮的问题应进入本轮上下文"
    assert "之前的对话" in text, "应带上历史区块的标题"


def test_different_sessions_do_not_share_history(monkeypatch, seeded_domain):
    """★ 会话隔离：另一个 session_id 读不到这个会话的记忆。"""
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)

    _run("推荐寂灭骨龙的阵容", "sess-A")
    script.seen.clear()
    _run("那换成水系呢", "sess-B")

    assert "推荐寂灭骨龙的阵容" not in script.all_text(), "B 会话不该看到 A 会话的历史"


def test_different_users_do_not_share_history(monkeypatch, seeded_domain):
    """★ 跨用户隔离：这是**信息泄露**，不只是功能问题。

    thread_id 若只用 session_id，用户 A 猜到/拿到 B 的 session_id
    就能读到 B 的追问上下文。
    """
    from app.services import strategy_service
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)

    list(
        strategy_service.strategy_stream(
            query="推荐寂灭骨龙的阵容",
            target_pet="寂灭骨龙",
            username="alice",
            session_id="shared-id",
        )
    )
    script.seen.clear()
    list(
        strategy_service.strategy_stream(
            query="那换成水系呢",
            target_pet="寂灭骨龙",
            username="bob",
            session_id="shared-id",
        )
    )

    assert "推荐寂灭骨龙的阵容" not in script.all_text(), "bob 不该读到 alice 的上下文"


def _read_state(username: str, session_id: str) -> dict:
    """读 checkpoint 里的最新状态。"""
    saver = checkpoint.get_saver()
    assert saver is not None
    graph = strategy_graph.build_graph()
    snapshot = graph.get_state(
        {"configurable": {"thread_id": checkpoint.thread_id_for(username, session_id)}}
    )
    return dict(snapshot.values or {})


# --------------------------------------------------------------------------- 持久化与裁剪


def test_checkpoint_persists_across_graph_rebuild(monkeypatch, seeded_domain):
    """★ 数据必须落在**文件**里，不是只在内存。

    只缓存内存对象的话，进程重启（或多 worker）后追问上下文就没了，
    而"多轮追问"这个功能的前提恰恰是跨请求持久。
    """
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)
    _run("第一轮问题", "sess-persist")

    # 丢掉所有内存缓存，模拟进程重启
    checkpoint.reset_saver()
    strategy_graph.reset_graph()
    _patch(monkeypatch, script)

    state = _read_state("alice", "sess-persist")
    assert state.get("turn_history"), "重启后仍应读得到上一轮的记忆"
    assert state["turn_history"][-1]["query"] == "第一轮问题"


def test_prune_threads_keeps_newest():
    """★ 裁剪：checkpoint 表只增不减，必须能按最后活动时间裁掉旧的。"""
    saver = checkpoint.get_saver()
    assert saver is not None

    for i in range(6):
        graph = strategy_graph.build_graph()
        graph.update_state(
            {"configurable": {"thread_id": f"prune-{i}"}},
            {"query": f"q{i}"},
        )

    removed = checkpoint.prune_threads(keep=2)
    assert removed == 4, f"应删掉 4 个，实际 {removed}"

    stats = checkpoint.thread_stats()
    assert stats["threads"] == 2


def test_prune_threads_noop_when_under_limit():
    assert checkpoint.prune_threads(keep=100) == 0


def test_prune_does_not_collide_with_concurrent_writes():
    """★ 裁剪必须走 saver 的锁，不能裸操作连接。

    ``SqliteSaver`` 把每个 checkpoint 读写都包在 ``with self.lock`` 里，
    而裁剪直接操作**同一个** ``sqlite3.Connection``。不走同一把锁的话，
    两个线程会同时在这个连接上开事务：``BEGIN`` 嵌套报
    "cannot start a transaction within a transaction"，
    或者一个线程的 ROLLBACK 把另一个线程刚写的 checkpoint 吞掉——
    后者**不报错**，只是偶尔丢一轮记忆。

    这里真的起并发：一边不停写 checkpoint，一边反复裁剪。
    """
    import threading

    errors: list[BaseException] = []
    stop = threading.Event()

    def write_loop() -> None:
        graph = strategy_graph.build_graph()
        i = 0
        while not stop.is_set():
            try:
                graph.update_state(
                    {"configurable": {"thread_id": f"conc-{i % 8}"}},
                    {"query": f"q{i}"},
                )
            except BaseException as exc:  # noqa: BLE001 - 收集起来在主线程断言
                errors.append(exc)
                return
            i += 1

    def prune_loop() -> None:
        while not stop.is_set():
            try:
                checkpoint.prune_threads(keep=2)
                checkpoint.thread_stats()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    writers = [threading.Thread(target=write_loop) for _ in range(3)]
    pruners = [threading.Thread(target=prune_loop) for _ in range(2)]
    for thread in writers + pruners:
        thread.start()
    import time

    time.sleep(2.0)
    stop.set()
    for thread in writers + pruners:
        thread.join(timeout=10)

    assert errors == [], f"并发下不应报错：{errors[:3]}"
    # 裁剪仍在生效：线程数被压到上限附近（允许并发写入把数字顶上去）
    assert checkpoint.thread_stats()["threads"] <= 12


def test_has_history_reflects_reality(monkeypatch, seeded_domain):
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)

    assert checkpoint.has_history("alice", "sess-has") is False
    _run("一个问题", "sess-has")
    assert checkpoint.has_history("alice", "sess-has") is True


def test_delete_thread_removes_history(monkeypatch, seeded_domain):
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)

    _run("一个问题", "sess-del")
    assert checkpoint.has_history("alice", "sess-del") is True

    checkpoint.delete_thread("alice", "sess-del")
    assert checkpoint.has_history("alice", "sess-del") is False


def test_disabled_checkpoint_still_runs(monkeypatch, seeded_domain):
    """``CHECKPOINT_ENABLED=false`` 时图要能**无状态**跑通，不能报 thread_id 错。"""
    from tests.test_strategy import FakeScript

    script = FakeScript(units=1)
    _patch(monkeypatch, script)
    monkeypatch.setattr(checkpoint.settings, "checkpoint_enabled", False)

    result = _done(_run("一个问题", "sess-off"))
    assert result["report"], "无状态模式仍应产出报告"


# --------------------------------------------------------------------------- 属性检索


def test_extract_type_mentions_finds_suffixed_types():
    """★ 属性识别必须要求「系 / 属性」后缀。

    裸匹配会把日常用词误判成属性：「普通玩家」「光看数据」「地"方"」
    里都有属性名，但都不是在问属性。中文没有词边界，
    所以用后缀做约束——这是指代属性的标准写法。
    """
    assert strategy_graph._extract_type_mentions("那换成冰系呢") == ["冰"]
    assert strategy_graph._extract_type_mentions("推荐火属性的精灵") == ["火"]
    assert strategy_graph._extract_type_mentions("冰系和草系哪个好") == ["草", "冰"]


def test_extract_type_mentions_ignores_plain_words():
    """反面用例：日常用词不能被当成属性。"""
    assert strategy_graph._extract_type_mentions("普通玩家怎么玩") == []
    assert strategy_graph._extract_type_mentions("光看数据不够") == []
    assert strategy_graph._extract_type_mentions("这地方不错") == []
    # 「无」属性不参与克制，不该被当成查询目标
    assert strategy_graph._extract_type_mentions("无系") == []


def test_attribute_followup_actually_retrieves(monkeypatch, seeded_domain):
    """★ 核心用例：追问「换成冰系」必须真的检索到冰系精灵。

    这是**多轮上下文真正生效**的分界线。checkpointer 把「那」正确补成
    寂灭骨龙之后，如果检索仍然只按精灵名找，这句话里唯一的实质信息
    （"冰系"）就被丢掉了——Researcher 一无所获，回答"资料不足"，
    而库里明明有 22 只冰系精灵。表现为「记住了上下文但答不出来」。

    这个 bug 只在真实端到端跑过才会暴露：单测上下文、单测检索都是绿的。
    """
    bundle = strategy_graph._gather_evidence(
        {"target_pet": "", "lineup_type": "pvp"}, "那换成冰系精灵呢"
    )
    assert bundle["pets"], "冰系属性查询应检索到精灵"
    assert any("冰" in (p.get("attributes") or []) for p in bundle["pets"]), (
        f"检索到的精灵应含冰属性，实际：{[p['attributes'] for p in bundle['pets']]}"
    )


def test_type_mentions_do_not_break_pet_only_queries(monkeypatch, seeded_domain):
    """普通查询（只有精灵名、没有属性词）不受影响。"""
    bundle = strategy_graph._gather_evidence(
        {"target_pet": "寂灭骨龙", "lineup_type": "pvp"}, "寂灭骨龙配什么队友"
    )
    assert bundle["teammates"] or bundle["lineups"], "原有检索路径不能退化"
