"""T-RC-14/15/16 测试：策略控制台运行守护面（运行日志 / 市场闸门 / 热更新 / 风控同源）。

覆盖：
1. **运行日志流**（T-RC-14）：来源推断前缀歧义（``hosted_sim_`` vs ``hosted_``）、
   自镜像递归守卫、跳过原因去重、游标过滤不卡死、身份缺失不写；
2. **市场闸门**（T-RC-15）：活跃快照市场解析**必须同时给出出处**（否则「没写市场」
   会被伪装成「确实是 A 股」）、无声明不判定、页签不一致话术；
3. **风控口径分裂诊断**（T-RC-16／D9）：一致/分裂/无从比对三态，缺失侧不得臆造一致；
4. **接线源守卫**：启动写回策略参数、热更新端点存在且不碰运行身份、
   市场一致性校验、模拟账户重置清运行日志——漏接即红（防静默失效）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_ROUTER_SRC = _BACKEND / "services/live_trading/routers/real_trading_lifecycle.py"


# ── 1. 运行日志流 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_infer_source_prefix_disambiguation():
    """``hosted_sim_`` 必须比 ``hosted_`` 先判——否则托管模拟被误标成容器 runner。"""
    from backend.services.live_trading.services.runtime_log_stream import (
        SOURCE_HOSTED_RUNNER,
        SOURCE_HOSTED_SIM,
        SOURCE_MANUAL,
        infer_source,
    )

    assert infer_source("hosted_sim_abc123") == SOURCE_HOSTED_SIM
    assert infer_source("hosted_abc123") == SOURCE_HOSTED_RUNNER
    assert infer_source("manual_xyz") == SOURCE_MANUAL
    assert infer_source("") == SOURCE_MANUAL
    assert infer_source(None) == SOURCE_MANUAL


@pytest.mark.unit
def test_runtime_scope_defaults_tenant_but_keeps_user():
    from backend.services.live_trading.services.runtime_log_stream import runtime_scope

    assert runtime_scope("acme", "10000001") == "acme:10000001"
    assert runtime_scope("", "10000001") == "default:10000001"
    assert runtime_scope(None, "10000001") == "default:10000001"


@pytest.mark.unit
def test_runtime_stream_does_not_mirror_itself():
    """递归守卫：父类 append_log 会回调 _mirror_to_runtime，运行流覆写为 no-op。

    这条一旦被删掉，运行流写自己会无限递归（或在 maxlen 内自我放大）。
    """
    from backend.services.live_trading.services.manual_execution_log_stream import (
        ManualExecutionLogStream,
    )
    from backend.services.live_trading.services.runtime_log_stream import RuntimeLogStream

    stream = RuntimeLogStream()
    # 不抛出、不调用父类实现（父类会去 import 运行流并写一条）
    assert stream._mirror_to_runtime(
        tenant_id="t", user_id="u", line="x", level="info"
    ) is None
    # 父类本身必须**仍然**有镜像实现（防有人顺手清掉镜像）
    assert (
        ManualExecutionLogStream._mirror_to_runtime
        is not RuntimeLogStream._mirror_to_runtime
    )


@pytest.mark.unit
def test_runtime_keys_are_scoped_not_task_scoped():
    """运行流键含 ``{tenant}:{user}``，且与任务流前缀不同（两条通道互不覆盖）。"""
    from backend.services.live_trading.services.runtime_log_stream import RuntimeLogStream

    stream = RuntimeLogStream()
    assert stream._stream_key("t:u").endswith(":logs:t:u")
    assert stream._state_key("t:u").endswith(":state:t:u")
    assert stream._last_skip_key("t:u").endswith(":lastskip:t:u")
    assert "runtime" in stream.stream_prefix
    assert "manual-execution" not in stream.stream_prefix


class _TinyRedis:
    """极简替身：只实现运行流用到的 get/set/delete。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        return True

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)


@pytest.mark.unit
def test_skip_once_dedup_keeps_stream_legible():
    """30s 一跳的调度器不能把「非交易日」灌满整条流：原因不变只记一次。"""
    from backend.services.live_trading.services import runtime_log_stream as mod

    stream = mod.RuntimeLogStream()
    stream._client = _TinyRedis()
    written: list[str] = []

    def _record(**kw):
        written.append(kw["line"])
        return True  # 返回值参与去重判定：写失败不落标记（见 log_skip_once docstring）

    stream.log = _record  # type: ignore[method-assign]

    first = stream.log_skip_once(
        tenant_id="t", user_id="u", reason="non_trading_day", line="非交易日"
    )
    second = stream.log_skip_once(
        tenant_id="t", user_id="u", reason="non_trading_day", line="非交易日"
    )
    changed = stream.log_skip_once(
        tenant_id="t", user_id="u", reason="outside_session", line="不在时段内"
    )
    # 跳出去后复位，再次进入同一跳过态应照常记录
    stream.clear_skip_marker(tenant_id="t", user_id="u")
    again = stream.log_skip_once(
        tenant_id="t", user_id="u", reason="non_trading_day", line="非交易日"
    )

    assert (first, second, changed, again) == (True, False, True, True)
    assert written == ["非交易日", "不在时段内", "非交易日"]


@pytest.mark.unit
def test_skip_once_and_log_are_noop_without_user():
    """无身份不写（运行流按 user 归档，空 user 会写出一个全局垃圾键）。"""
    from backend.services.live_trading.services import runtime_log_stream as mod

    stream = mod.RuntimeLogStream()
    stream._client = _TinyRedis()
    assert stream.log_skip_once(tenant_id="t", user_id="", reason="r", line="l") is False
    assert stream._client.store == {}


@pytest.mark.unit
def test_fetch_scope_entries_filters_but_advances_cursor(monkeypatch):
    """过滤在读取后做，``next_id`` 必须取原始值——否则游标卡在最后一条不匹配记录上。"""
    from backend.services.live_trading.services import runtime_log_stream as mod

    stream = mod.RuntimeLogStream()
    raw_entries = [
        {"id": "1-0", "level": "info", "stage": "no_order", "source": "hosted_sim"},
        {"id": "2-0", "level": "error", "stage": "cycle_error", "source": "hosted_sim"},
        {"id": "3-0", "level": "info", "stage": "order", "source": "manual"},
    ]

    def _fake_fetch(scope, *, after_id="0-0", limit=200, latest=False):
        assert scope == "t:u"
        assert latest is True  # 首屏取最近 N 条（maxlen 截断下 0-0 会拿到最旧的）
        return {"entries": raw_entries, "next_id": "3-0", "snapshot": {"status": "running"}}

    monkeypatch.setattr(stream, "fetch_entries", _fake_fetch)

    only_errors = stream.fetch_scope_entries(tenant_id="t", user_id="u", level="error")
    assert [e["id"] for e in only_errors["entries"]] == ["2-0"]
    assert only_errors["next_id"] == "3-0"  # 未被过滤结果带偏
    assert only_errors["snapshot"] == {"status": "running"}

    by_source = stream.fetch_scope_entries(tenant_id="t", user_id="u", source="manual")
    assert [e["id"] for e in by_source["entries"]] == ["3-0"]

    by_stage = stream.fetch_scope_entries(tenant_id="t", user_id="u", stage="no_order")
    assert [e["id"] for e in by_stage["entries"]] == ["1-0"]

    unfiltered = stream.fetch_scope_entries(tenant_id="t", user_id="u")
    assert len(unfiltered["entries"]) == 3


@pytest.mark.unit
def test_read_state_is_state_only():
    """``/status`` 每 10s 轮询，不得顺带 xrevrange 拉 200 条日志。"""
    from backend.services.live_trading.services import runtime_log_stream as mod

    stream = mod.RuntimeLogStream()
    stream._client = _TinyRedis()
    key = stream.stream_prefix + ":state:t:u"
    stream._client.store[key] = '{"status": "running", "stage": "order"}'
    assert stream.read_state(tenant_id="t", user_id="u") == {
        "status": "running",
        "stage": "order",
    }
    # 坏 JSON 不抛，返回 None（面板显示「无周期信息」而不是崩）
    stream._client.store[key] = "{oops"
    assert stream.read_state(tenant_id="t", user_id="u") is None


@pytest.mark.unit
def test_log_never_raises_on_broken_client():
    """日志失败不能拖垮交易循环。"""
    from backend.services.live_trading.services import runtime_log_stream as mod

    class _Boom:
        def get(self, *_a, **_kw):
            raise RuntimeError("redis down")

        def set(self, *_a, **_kw):
            raise RuntimeError("redis down")

        def xadd(self, *_a, **_kw):
            raise RuntimeError("redis down")

    stream = mod.RuntimeLogStream()
    stream._client = _Boom()
    # 不抛出即通过；返回 False 表示「确实没落盘」，不能让调用方以为写了
    assert stream.log(tenant_id="t", user_id="u", line="x") is False
    assert stream.log_skip_once(tenant_id="t", user_id="u", reason="r", line="l") is False


@pytest.mark.unit
def test_skip_reason_text_passthrough():
    from backend.services.simulation.services.simulation_hosted_scheduler import (
        SKIP_REASON_TEXT,
        skip_reason_text,
    )

    for code in (
        "non_trading_day",
        "outside_session",
        "weekday_skip",
        "interval_skip",
        "before_window",
        "sell_only_window",
        "lock_held",
        "lock_error",
    ):
        assert SKIP_REASON_TEXT[code] != code, f"{code} 未配中文文案"
        assert skip_reason_text(code) != code
    # 未知码原样透出，不静默吞掉（新加 reason 时面板不会显示空白）
    assert skip_reason_text("brand_new_reason") == "brand_new_reason"
    assert skip_reason_text("") == "未知原因"


# ── 2. 市场闸门 ────────────────────────────────────────────────────


@pytest.mark.unit
def test_active_strategy_market_reports_source():
    """只给市场不给出处，会把「载荷没写市场」伪装成「确实是 A 股」。"""
    from backend.shared.active_strategy_market import (
        active_strategy_market,
        resolve_active_strategy_market,
    )

    market, source = resolve_active_strategy_market(
        {"live_trade_config": {"market": "hk"}}
    )
    assert (market, source) == ("HK", "live_trade_config")

    market, source = resolve_active_strategy_market(
        {"execution_config": {"market": "US"}}
    )
    assert (market, source) == ("US", "execution_config")

    # live 优先于 exec（与启动链路 deployment_market 的推导顺序一致）
    market, source = resolve_active_strategy_market(
        {"live_trade_config": {"market": "HK"}, "execution_config": {"market": "US"}}
    )
    assert (market, source) == ("HK", "live_trade_config")

    # 都没写 → 兜底 CN，但出处必须诚实标记为 default
    assert resolve_active_strategy_market({}) == ("CN", "default")
    assert resolve_active_strategy_market(None) == ("CN", "default")
    assert active_strategy_market({"live_trade_config": {"market": "hk"}}) == "HK"


@pytest.mark.unit
def test_strategy_declared_market_and_gate():
    from backend.shared.active_strategy_market import (
        market_gate,
        strategy_declared_market,
    )

    assert strategy_declared_market({"parameters": {"market": "hk"}}) == "HK"
    assert strategy_declared_market({"parameters": {}}) is None
    assert strategy_declared_market({"parameters": "not-a-dict"}) is None
    assert strategy_declared_market(None) is None

    # 无声明不判定（调用方没说要看哪个市场，不该凭空判不匹配）
    assert market_gate(None, "CN") is None
    assert market_gate("", "CN") is None

    matched = market_gate("CN", "CN")
    assert matched == {"declared": "CN", "active": "CN", "matched": True, "reason": ""}

    mismatched = market_gate("HK", "CN")
    assert mismatched and mismatched["matched"] is False
    assert "HK" in mismatched["reason"] and "CN" in mismatched["reason"]


# ── 3. 风控口径分裂诊断（D9）───────────────────────────────────────


@pytest.mark.unit
def test_execution_config_divergence_matrix():
    from backend.services.live_trading.routers.real_trading_lifecycle import (
        _execution_config_divergence,
    )

    active = {"execution_config": {"stop_loss": -0.08, "max_buy_drop": -0.03}}
    aligned = {"parameters": {"execution_config": {"stop_loss": -0.08, "max_buy_drop": -0.03}}}
    result = _execution_config_divergence(active_data=active, strategy=aligned)
    assert result == {"diverged": False, "fields": {}}

    drifted = {"parameters": {"execution_config": {"stop_loss": -0.05, "max_buy_drop": -0.03}}}
    result = _execution_config_divergence(active_data=active, strategy=drifted)
    assert result is not None and result["diverged"] is True
    assert result["fields"] == {"stop_loss": {"active": -0.08, "strategy": -0.05}}
    assert "stop_loss" in result["message"]
    # max_buy_drop 一致 → 不进 fields（避免把一致项也说成不一致）
    assert "max_buy_drop" not in result["fields"]

    # 无从比对 → None（不臆造「一致」）
    assert _execution_config_divergence(active_data=active, strategy=None) is None
    assert (
        _execution_config_divergence(active_data=active, strategy={"parameters": {}})
        is None
    )
    assert (
        _execution_config_divergence(
            active_data={"execution_config": "broken"},
            strategy={"parameters": {"execution_config": {}}},
        )
        is None
    )


@pytest.mark.unit
def test_risk_sync_merges_and_preserves_exit_rules():
    """同步只碰风控键，策略作者的退出规则（止盈/最长持有/移动止损）必须保留。"""
    from backend.services.live_trading.routers.real_trading_lifecycle import (
        _RISK_EXEC_KEYS,
    )

    assert set(_RISK_EXEC_KEYS) == {"max_buy_drop", "stop_loss"}
    src = _ROUTER_SRC.read_text(encoding="utf-8")
    # 合并（而非整体覆盖）是这条契约的实现形态：只覆盖风险键，其余原样带入
    assert "merged = {**current_exec, **risk_values}" in src, (
        "风控同步必须合并进已有 execution_config，整体覆盖会抹掉 take_profit/max_hold_days"
    )
    # 失败如实回传（f-string 形态），不静默
    assert '"reason": f"同步失败：{exc}"' in src
    # 参数被并发修改时不得覆盖，提示刷新重试
    assert "StrategyLockedError, VersionConflictError" in src


# ── 4. 接线源守卫（漏接即红）────────────────────────────────────────


@pytest.mark.unit
def test_start_syncs_effective_risk_into_strategy_params():
    """D9：启动时生效风控必须回写策略参数，否则退出规则读到的仍是旧值。"""
    src = _ROUTER_SRC.read_text(encoding="utf-8")
    assert "_sync_execution_config_to_strategy(" in src
    # 必须在 /start 内被调用（而非只定义）
    start_idx = src.index('@router.post("/start")')
    stop_idx = src.index('@router.post("/stop")')
    assert "_sync_execution_config_to_strategy(" in src[start_idx:stop_idx]
    # 结果必须回传，否则同步静默失败用户无从知晓
    assert '"execution_config_sync": execution_config_sync' in src


@pytest.mark.unit
def test_start_validates_market_consistency():
    """港股策略不得被 A 股页签启动（此前完全不校验）。"""
    src = _ROUTER_SRC.read_text(encoding="utf-8")
    start_idx = src.index('@router.post("/start")')
    stop_idx = src.index('@router.post("/stop")')
    body = src[start_idx:stop_idx]
    assert "declared_market" in body
    assert "declared_market != deployment_market" in body
    # 市场必须回显，前端页签闸门才有依据
    assert '"market": deployment_market' in body


@pytest.mark.unit
def test_runtime_config_endpoint_protects_runtime_identity():
    """热更新只改配置：run_id/code_str 等身份键必须原样保留且拒绝客户端提交。"""
    from backend.services.live_trading.routers import real_trading_lifecycle as mod

    paths = {r.path for r in mod.router.routes}
    assert "/runtime-config" in paths

    src = _ROUTER_SRC.read_text(encoding="utf-8")
    for identity_key in ("run_id", "started_at", "code_str", "code_sha"):
        assert f'"{identity_key}"' in src.split("_RUNTIME_IDENTITY_KEYS = (")[1].split(")")[0]
    assert "patch_exec.pop(identity_key, None)" in src
    assert "patch_live.pop(identity_key, None)" in src
    # 乐观并发：版本不符必须 409 而不是静默覆盖
    assert "expected_config_version" in src
    # REAL/SHADOW 显式拒绝并告知需重建容器（不谎称零中断）
    assert "X-Requires-Restart" in src
    # 身份键原样保留地合并回快照（新配置只覆盖配置键，run_id/code_str 原样带回）
    assert "new_snapshot = {" in src and "**snapshot," in src
    assert '"position_untouched": True' in src


@pytest.mark.unit
def test_engine_logs_silent_early_returns():
    """模拟引擎的 early-return 分支此前静默返回 report.error——正是「日志空白」的一半原因。"""
    src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "log_source" in src
    for stage in ('stage="no_order"', 'stage="order"', 'stage="cycle_error"'):
        assert stage in src, f"{stage} 未落日志"
    # 逐单必须带序号，否则面板无法把「第几单被拒」对上
    assert "for index, order in enumerate(orders)" in src
    # dry-run/每日批处理（log_source=None）不得写运行日志
    assert "if log_source is None" in src or "if not log_source" in src


@pytest.mark.unit
def test_hosted_scheduler_logs_skips_and_cycles():
    src = (
        _BACKEND / "services/simulation/services/simulation_hosted_scheduler.py"
    ).read_text(encoding="utf-8")
    for token in (
        "skip_reason_text",
        "hosted_sim_skip_once",
        "hosted_sim_clear_skip_marker",
        "cycle_start",
        "cycle_end",
    ):
        assert token in src, f"托管调度日志缺 {token}"
    # 心跳必须在每轮**开始**写：写在结尾会让卡死的一轮看起来「新鲜」
    run_idx = src.index("async def _run(self)")
    body = src[run_idx : run_idx + 1200]
    assert body.index("_sched_heartbeat") < body.index("await self.run_once()")


@pytest.mark.unit
def test_read_heartbeats_matches_health_check_semantics():
    """守护条心跳判定必须与体检 C07 同口径——两处各写一遍必然漂移。"""
    from backend.shared.scheduler_registry import read_heartbeats

    class _FakeClient:
        def __init__(self, values):
            self.values = values

        def get(self, key):
            return self.values.get(key)

    now = 1_700_000_000.0
    entries = read_heartbeats(
        ("sim_hosted", "manual_execution"),
        now_ts=now,
        env={},
        redis_client=_FakeClient(
            {
                "qm:sched:hb:sim_hosted": str(now - 10),  # TTL 300 → ok
                "qm:sched:hb:manual_execution": str(now - 9999),  # → stale
            }
        ),
    )
    by_key = {e["key"]: e for e in entries}
    assert by_key["sim_hosted"]["state"] == "ok"
    assert by_key["sim_hosted"]["age"] == 10
    assert by_key["manual_execution"]["state"] == "stale"

    # 无心跳 → missing（不是 ok，也不抛）
    missing = read_heartbeats(
        ("sim_hosted",), now_ts=now, env={}, redis_client=_FakeClient({})
    )
    assert missing[0]["state"] == "missing"
    assert missing[0]["age"] is None

    # 开关关掉 → off（运行态未知，但至少不是「死了」）
    off = read_heartbeats(
        ("sim_hosted",),
        now_ts=now,
        env={"ENABLE_SIMULATION_HOSTED_SCHEDULER": "0"},
        redis_client=_FakeClient({"qm:sched:hb:sim_hosted": str(now)}),
    )
    assert off[0]["state"] == "off"

    # 脏值 → missing，不因为解析失败就报 ok
    dirty = read_heartbeats(
        ("sim_hosted",),
        now_ts=now,
        env={},
        redis_client=_FakeClient({"qm:sched:hb:sim_hosted": "not-a-number"}),
    )
    assert dirty[0]["state"] == "missing"

    # 未注册 key 跳过（不静默当成正常）
    assert read_heartbeats(("not_a_job",), now_ts=now, env={}, redis_client=_FakeClient({})) == []


@pytest.mark.unit
def test_status_exposes_guardian_scheduler_heartbeats():
    """守护条数据源：/status 必须回传托管循环心跳，前端才判得出「服务端还在跑」。"""
    src = _ROUTER_SRC.read_text(encoding="utf-8")
    assert '"schedulers"' in src
    assert "_GUARDIAN_JOB_KEYS" in src
    keys_block = src.split("_GUARDIAN_JOB_KEYS = (")[1].split(")")[0]
    for key in ("sim_hosted", "manual_execution"):
        assert f'"{key}"' in keys_block
    # 体检失败不得让 /status 500：采集函数必须把异常兜成空列表
    collector = src.split("def _build_scheduler_health_block")[1].split("\ndef ")[0]
    assert "except Exception" in collector and "return []" in collector


@pytest.mark.unit
def test_risk_status_reports_effective_values_and_locks():
    """L4 风控层数据源：生效值 + 风险锁 + 口径分裂，一处读全。

    「现在的止损到底是多少」此前散在三处（快照/策略参数/界面），用户无法自证；
    本端点把三者放在同一个响应里，并显式标注 `source`，谁是真值一目了然。
    """
    from backend.services.live_trading.routers import real_trading_lifecycle as mod

    paths = {r.path for r in mod.router.routes}
    assert "/risk-status" in paths

    src = _ROUTER_SRC.read_text(encoding="utf-8")
    body = src.split('@router.get("/risk-status")')[1].split("\n@router.")[0]
    # 风险锁按交易日维度读（与下单时的判定同源）
    assert "load_risk_locks" in body
    assert "trade_date" in body
    # 生效值必须来自运行快照，且与 /status 用同一个分裂诊断（不另写一套）
    assert "_execution_config_divergence" in body or "execution_config_divergence" in body
    # 兜底：Redis 挂了也要给出「读不到」而不是「无锁」
    assert "available" in body


@pytest.mark.integration
def test_risk_status_handler_executes(redis_env):
    """真调一次 `/risk-status`——源码断言过不了 NameError（本次就抓到两个）。

    这条防的是「字符串在、函数不在」的空转测试：`_read_active_strategy` 与
    `date` 都曾是我凭印象写下的名字，只有真跑才发现它们不存在。
    """
    import asyncio

    from backend.services.live_trading.routers import real_trading_lifecycle as mod

    client, auth = redis_env
    payload = asyncio.run(
        mod.get_risk_status(
            user_id=None,
            tenant_id=None,
            trade_date=None,
            auth=auth,
            redis=client,
        )
    )
    assert payload["status"] == "success"
    assert payload["source"] == "runtime_snapshot"
    # 无活跃策略 → 未运行，且生效值为空 dict（不是 None，前端不必再判两种空）
    assert payload["running"] is False
    assert payload["effective_execution_config"] == {}
    # 锁块必须给出 available，且读得到时带交易日
    assert "locks" in payload
    if payload["locks"]["available"]:
        assert payload["locks"]["trade_date"]
        assert payload["locks"]["symbols"] == []


@pytest.mark.integration
def test_status_handler_without_active_strategy_does_not_raise(redis_env, monkeypatch):
    """无活跃策略时 `GET /status` 必须正常返回——这是控制台最常走的那条路。

    实测抓到过 `UnboundLocalError: local variable 'active_data' referenced before
    assignment`：`active_data` 只在「读得到快照」的分支里绑定，没启动策略时压根
    不执行，而市场/配置块又无条件引用它 → 500。前端此时会拿缺省值照常渲染，
    表现成「界面像在跑、心跳与版本全空」——正是最该被拦住的那类静默故障。

    这里不 mock `_build_market_and_config_block`：崩溃就发生在**它的实参表达式**上，
    把它换掉等于把被测点删了。只桩掉真会碰库/网络的三个外呼。
    """
    import asyncio
    from unittest.mock import AsyncMock

    from backend.services.live_trading.routers import real_trading_lifecycle as mod

    client, auth = redis_env
    monkeypatch.setattr(
        mod, "_build_signal_source_status", AsyncMock(return_value=(None, {}))
    )
    monkeypatch.setattr(
        mod, "_fetch_active_portfolio_snapshot", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        mod.manual_execution_service,
        "get_latest_hosted_task",
        AsyncMock(return_value=None),
    )

    payload = asyncio.run(
        mod.get_status(
            user_id=None,
            tenant_id=None,
            trading_mode="REAL",
            market="CN",
            auth=auth,
            redis=client,
            db=None,
        )
    )

    assert payload["status"] == "not_running"
    # 市场/配置块必须真的合并进来了（缺了它守护条就没有心跳可显示）
    assert payload["market"] == "CN"
    assert "schedulers" in payload
    assert "config_version" in payload


@pytest.mark.integration
def test_logs_handler_returns_cursor_entries_for_frontend(redis_env):
    """真调一次 `GET /logs`（运行维度日志）——前端日志面板的数据契约就在这条上。

    面板读的是 ``entries`` + ``next_id`` 游标，还要按 ``stage`` 过滤；这三样任一
    写错，界面表现都是同一句「暂无运行日志」，排查时看不出区别，故在契约层锁死。
    """
    import asyncio

    from backend.services.live_trading.routers import real_trading_lifecycle as mod
    from backend.services.live_trading.services.runtime_log_stream import log_runtime

    client, auth = redis_env
    log_runtime(
        tenant_id=_TENANT,
        user_id=_USER,
        line="周期开始：第 3 个交易日",
        level="info",
        stage="cycle_start",
        source="hosted_sim",
    )
    log_runtime(
        tenant_id=_TENANT,
        user_id=_USER,
        line="本轮无信号，跳过",
        level="warning",
        stage="no_order",
        source="hosted_sim",
    )

    def fetch(**kw):
        return asyncio.run(
            mod.get_logs(
                auth=auth,
                **{"tail": 100, "after_id": "0-0", "limit": 200,
                   "level": None, "stage": None, "source": None,
                   "user_id": None, "tenant_id": None, **kw},
            )
        )

    data = fetch()
    assert [e["line"] for e in data["entries"]] == [
        "周期开始：第 3 个交易日",
        "本轮无信号，跳过",
    ]
    # 前端按 source 打好来源标签，字段不能丢
    assert data["entries"][0]["source"] == "hosted_sim"
    assert data["next_id"] and data["next_id"] != "0-0"

    # stage 过滤：被滤掉的条目仍推进游标，否则轮询会卡死在最后一条不匹配记录上
    only_skip = fetch(stage="no_order")
    assert [e["line"] for e in only_skip["entries"]] == ["本轮无信号，跳过"]
    assert only_skip["next_id"] == data["next_id"]

    # 游标增量：从 next_id 之后读，不应重复拿到已读条目
    assert fetch(after_id=data["next_id"])["entries"] == []

    # 旧前端拿的是拼好的文本（<pre>{logs}</pre>），这条不能省
    assert "本轮无信号，跳过" in data["logs"]


@pytest.mark.unit
def test_stop_records_reason_in_audit_and_runtime_log():
    """停止原因必须落两份：返回值（前端留痕）+ 运行日志（用户可见）。

    此前 ``/stop`` 不留原因，事后只能看到「某时刻停了」——分不清人工、换策略
    还是风控告警，而这三者的处置完全不同。
    """
    src = _ROUTER_SRC.read_text(encoding="utf-8")
    stop_idx = src.index('@router.post("/stop")')
    stop_body = src[stop_idx : stop_idx + 4000]
    # 必须接收原因（Form 参数，与其余可选身份参数同口径）
    assert "reason: Optional[str] = Form(None)" in stop_body
    # 未填写时如实记 unspecified，不编造一个默认原因
    assert '"unspecified"' in stop_body
    assert '"reason": stop_reason' in stop_body
    # 写进运行日志流，用户能在面板上看到「为什么停了」
    assert "runtime_log_stream" in stop_body
    assert 'stage="stop"' in stop_body
    # 通知正文带上原因（否则通知与日志两处口径不一致）
    assert "{stop_reason}" in src


@pytest.mark.unit
def test_simulation_reset_clears_runtime_logs():
    """账户重置后日志面板不能还挂着上一轮策略的周期记录。"""
    src = (_BACKEND / "services/simulation/routers/simulation.py").read_text(
        encoding="utf-8"
    )
    assert "qm:real-trading:runtime:" in src
    for kind in ("logs", "state", "lastskip"):
        assert f'"{kind}"' in src.split("for _kind in (")[1].split(")")[0]


# ── 5. 热更新真机链路（真 Redis，命名空间隔离 + 用后即清）────────────────


_TENANT = "pytest_t_rc16"
_USER = "99999999"
_KEY = f"trade:active_strategy:{_TENANT}:{_USER}"


def _snapshot(**over) -> dict:
    base = {
        "strategy_id": "sys_pytest_tpl",
        "run_id": "run-pytest-001",
        "mode": "SIMULATION",
        "strategy_name": "pytest 冒烟",
        "execution_config": {"stop_loss": -0.08, "max_buy_drop": -0.03},
        "live_trade_config": {
            "rebalance_days": 3,
            "sell_time": "14:45",
            "buy_time": "14:50",
            "market": "CN",
        },
        "started_at": "2026-09-20T01:00:00+00:00",
        "code_sha": "deadbeef1234",
        "code_str": "STRATEGY_CONFIG = {}",
        "config_version": 1,
        "config_updated_at": "2026-09-20T01:00:00+00:00",
        "config_history": [],
    }
    base.update(over)
    return base


@pytest.fixture()
def redis_env():
    """真 Redis 客户端 + 身份命名空间清理（前清后清，失败也不留脏键）。"""
    from types import SimpleNamespace

    from backend.services.trade_shared.redis_client import RedisClient

    client = RedisClient()
    client.connect()
    if client.client is None:
        pytest.fail("Redis 不可用：热更新链路无法验证（不跳过——跳过会假绿）")

    def _purge():
        keys = list(client.client.keys(f"{_KEY}*"))
        keys += list(client.client.keys(f"qm:real-trading:runtime:*:{_TENANT}:{_USER}"))
        keys += list(client.client.keys(f"qm:hosted:simulation:{_TENANT}:{_USER}:*"))
        if keys:
            client.client.delete(*keys)

    _purge()
    auth = SimpleNamespace(user_id=_USER, tenant_id=_TENANT)
    yield client, auth
    _purge()


# FastAPI 的 Form(...) 默认值是「描述符对象」而非 None：直接调用处理函数时
# 未显式传参会拿到恒真/非 None 的占位值（本项目已踩过一次的坑），故显式铺默认。
_FORM_DEFAULTS = {
    "execution_config": None,
    "live_trade_config": None,
    "expected_config_version": None,
    "user_id": None,
    "tenant_id": None,
    "dry_run": False,
    "force": False,
    "operator": None,
    "change_reason": None,
}


@pytest.mark.integration
def test_runtime_config_hot_update_end_to_end(redis_env):
    """真 Redis 全链：预演不写 → 写入保身份 → 版本冲突 → 身份不可伪造 → 留痕。"""
    import asyncio
    import json

    from fastapi import HTTPException

    from backend.services.live_trading.routers import real_trading_lifecycle as mod

    client, auth = redis_env

    async def call(**kw):
        return await mod.update_runtime_config(
            auth=auth, redis=client, **{**_FORM_DEFAULTS, **kw}
        )

    def stored():
        raw = client.client.get(_KEY)
        return json.loads(raw) if raw else None

    async def scenario():
        # 1) 预演：不写盘，但如实报出节奏变化
        client.client.set(_KEY, json.dumps(_snapshot()))
        preview = await call(
            live_trade_config=json.dumps({"rebalance_days": 5}), dry_run=True
        )
        assert preview["status"] == "dry_run"
        assert preview["diff"]["live_trade_config"]["rebalance_days"] == {
            "from": 3,
            "to": 5,
        }
        assert stored()["config_version"] == 1, "预演写了盘"

        # 2) 真写：版本 +1，运行身份原样保留
        applied = await call(
            live_trade_config=json.dumps({"rebalance_days": 5}),
            execution_config=json.dumps({"stop_loss": -0.1}),
            expected_config_version=1,
            operator="pytest",
            change_reason="盘中调参",
        )
        assert applied["effective_at"] == "next_cycle"
        after = stored()
        assert after["config_version"] == 2
        assert after["run_id"] == "run-pytest-001"
        assert after["code_str"] == "STRATEGY_CONFIG = {}"
        assert after["code_sha"] == "deadbeef1234"
        assert after["live_trade_config"]["rebalance_days"] == 5
        # 合并语义：风险键被覆盖，策略原有退出规则不被抹掉
        assert after["execution_config"] == {"stop_loss": -0.1, "max_buy_drop": -0.03}
        assert after["config_history"][-1]["operator"] == "pytest"

        # 3) 乐观并发：基线过期 → 409（不静默覆盖）
        with pytest.raises(HTTPException) as conflict:
            await call(
                live_trade_config=json.dumps({"rebalance_days": 10}),
                expected_config_version=1,
            )
        assert conflict.value.status_code == 409

        # 4) 身份键不可由客户端伪造（否则 run_id 一改，账本与原运行实例断链）
        await call(live_trade_config=json.dumps({"run_id": "HACKED"}))
        assert stored()["run_id"] == "run-pytest-001"

        # 5) REAL 模式拒绝热更新并指明需重建容器
        client.client.set(_KEY, json.dumps(_snapshot(mode="REAL")))
        with pytest.raises(HTTPException) as real_reject:
            await call(live_trade_config=json.dumps({"rebalance_days": 4}))
        assert real_reject.value.status_code == 409
        assert (real_reject.value.headers or {}).get("X-Requires-Restart") == "true"
        assert stored()["config_version"] == 1, "被拒绝的请求不该改动快照"

        # 6) 未运行 → 409
        client.client.delete(_KEY)
        with pytest.raises(HTTPException) as idle:
            await call(live_trade_config=json.dumps({"rebalance_days": 4}))
        assert idle.value.status_code == 409

    asyncio.run(scenario())


@pytest.mark.integration
def test_same_day_refire_guard_requires_force(redis_env):
    """当日已触发过：改节奏必须 force；不改节奏的字段不受限；force 须留痕。"""
    import asyncio
    import json
    from datetime import datetime

    from fastapi import HTTPException

    from backend.services.live_trading.routers import real_trading_lifecycle as mod
    from backend.shared.market_sessions import market_timezone
    from backend.services.live_trading.services.runtime_log_stream import (
        runtime_log_stream,
    )

    client, auth = redis_env
    trade_date = datetime.now(market_timezone("CN")).date().isoformat()
    lock_key = (
        f"qm:hosted:simulation:{_TENANT}:{_USER}:sys_pytest_tpl:{trade_date}:BUY"
    )

    async def call(**kw):
        return await mod.update_runtime_config(
            auth=auth, redis=client, **{**_FORM_DEFAULTS, **kw}
        )

    async def scenario():
        client.client.set(_KEY, json.dumps(_snapshot()))
        client.client.set(lock_key, "fired", ex=3600)

        with pytest.raises(HTTPException) as guarded:
            await call(live_trade_config=json.dumps({"rebalance_days": 5}))
        assert guarded.value.status_code == 409
        assert "force" in guarded.value.detail
        assert json.loads(client.client.get(_KEY))["config_version"] == 1

        # 预演不受同日守卫拦截（不写盘，且「今天已跑过」正是操作者要看的）
        preview = await call(
            live_trade_config=json.dumps({"rebalance_days": 5}), dry_run=True
        )
        assert preview["already_fired_phases"] == ["BUY"]

        # 与节奏无关的风控字段直接放行
        risk_only = await call(execution_config=json.dumps({"stop_loss": -0.05}))
        assert risk_only["rhythm_changed"] is False

        forced = await call(
            live_trade_config=json.dumps({"rebalance_days": 5}),
            force=True,
            change_reason="紧急调整",
        )
        assert forced["forced"] is True
        history = json.loads(client.client.get(_KEY))["config_history"]
        assert history[-1]["forced"] is True
        assert history[-1]["already_fired_phases"] == ["BUY"]
        assert history[-1]["reason"] == "紧急调整"

        # 变更必须能在运行日志里查到（用户可见的证据链，而非只落快照）
        entries = runtime_log_stream.fetch_scope_entries(
            tenant_id=_TENANT, user_id=_USER, limit=50
        )["entries"]
        assert any(e.get("stage") == "config_update" for e in entries)

    asyncio.run(scenario())
