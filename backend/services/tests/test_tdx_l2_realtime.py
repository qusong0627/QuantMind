"""TDX L2 实时推理单元测试：截面标准化 / 信号合成 / 触发 / 冷却。

纯函数级测试（Redis 用 mock，不触真实库）：
- _z_score: 截面标准化与极端值 clip
- compute_signal_scores: ICIR 加权 + sigmoid 单调性
- compute_realtime_score: 日频分 × 权重 + 信号分 × 权重
- load/save_l2_config: Redis 读写与非法值保护
- is_cooldown / set_cooldown: 冷却窗口
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.live_trading.services.tdx_l2_capture_task import FACTOR_ICIR
from backend.services.live_trading.services.tdx_l2_realtime import (
    _z_score,
    compute_realtime_score,
    compute_signal_scores,
    is_cooldown,
    load_l2_config,
    save_l2_config,
    set_cooldown,
)
from backend.services.trade_shared.redis_client import RedisClient


def _factors(**overrides) -> dict:
    """13 因子基值（中性）。"""
    f = dict.fromkeys(FACTOR_ICIR, 0.0)
    f.update(overrides)
    return f


class TestZScore:
    def test_positive_outliers_get_positive_z(self):
        # Arrange
        values = {"A": 0.9, "B": 0.5, "C": 0.5, "D": 0.5, "E": 0.4}
        # Act
        z = _z_score(values)
        # Assert
        assert z["A"] > 0
        assert z["E"] < 0

    def test_clips_at_3_sigma(self):
        # Arrange: 20 个样本中一个极端离群值 → z≈4.2 被剪到 3
        values = {"A": 100.0, **{f"S{i}": 0.0 for i in range(19)}}
        # Act
        z = _z_score(values)
        # Assert
        assert z["A"] == 3.0
        assert z["S0"] >= -3.0

    def test_degenerate_returns_zero(self):
        # Arrange: 全同值 → 零标准差
        values = {"A": 0.5, "B": 0.5}
        # Act
        z = _z_score(values)
        # Assert
        assert z["A"] == 0.0


class TestComputeSignalScores:
    def test_bullish_stock_scores_higher_than_bearish(self):
        # Arrange: 同一池内 A 全面强于 B
        pool = {
            "SH600000": _factors(
                micro_vpin_vol_ratio=0.6, micro_vpin_amount_ratio=0.5,
                micro_zone_distribution=0.4, micro_open_gap=0.03,
            ),
            "SH600519": _factors(
                micro_vpin_vol_ratio=0.1, micro_vpin_amount_ratio=0.1,
                micro_zone_distribution=-0.3, micro_open_gap=-0.03,
            ),
            "SZ000001": _factors(),
            "SZ000002": _factors(),
            "SZ300750": _factors(),
            "SH601318": _factors(),
        }
        # Act
        signal = compute_signal_scores(pool, None)
        # Assert
        assert signal["SH600000"] > signal["SH600519"]
        assert all(0 <= v <= 100 for v in signal.values())

    def test_symmetric_pool_scores_near_midpoint(self):
        # Arrange: 全部中性 → sigmoid(0) ≈ 50
        pool = {f"SH{s:06d}": _factors() for s in range(10)}
        # Act
        signal = compute_signal_scores(pool, None)
        # Assert
        for v in signal.values():
            assert 40 <= v <= 60

    def test_custom_factor_weights_override(self):
        # Arrange: 只用 vpin 权重
        pool = {
            "A": _factors(micro_vpin_vol_ratio=0.9),
            "B": _factors(micro_vpin_vol_ratio=0.0),
            "C": _factors(micro_vpin_vol_ratio=0.5),
            "D": _factors(micro_vpin_vol_ratio=0.4),
            "E": _factors(micro_vpin_vol_ratio=0.3),
        }
        # Act
        signal = compute_signal_scores(pool, {"micro_vpin_vol_ratio": 1.0})
        # Assert
        assert signal["A"] > signal["C"] > signal["B"]


class TestComputeRealtimeScore:
    def test_fusion_and_signal_blend(self):
        # Arrange: 日频 3.0(=100分) + 信号 80 → 0.6×100 + 0.4×80 = 92
        # Act
        score = compute_realtime_score(fusion_score=3.0, signal_score=80.0)
        # Assert
        assert score == 92.0

    def test_missing_fusion_uses_neutral_50(self):
        # Arrange: 无日频分时按中性 50 处理
        # Act
        score = compute_realtime_score(fusion_score=None, signal_score=50.0)
        # Assert
        assert score == 50.0

    def test_weight_changes_blend(self):
        # Act
        score = compute_realtime_score(3.0, 100.0, daily_weight=0.8, signal_weight=0.2)
        # Assert
        assert score == 100.0


class _FakeRedis:
    """内存 raw 客户端替身（模拟 redis-py）：包装层的 json 序列化在外面完成。"""

    def __init__(self) -> None:
        self.store: dict = {}
        self.expired_at: dict = {}

    def set(self, key: str, value, ex=None, **kwargs) -> None:
        # 如实记录 TTL：旧版把 **kwargs 整个吞掉，于是「写实时键从不带过期」
        # 这个缺陷在测试里**永远不可见**——325 个键 ttl=-1 累积了一个月陈料。
        self.store[key] = value
        if ex is None:
            # 与真 Redis 一致：SET 不带 EX 会**清掉**该键既有的 TTL
            self.expired_at.pop(key, None)
        else:
            self.expired_at[key] = ex

    def get(self, key):
        return self.store.get(key)

    def scan_iter(self, match: str = "*"):
        # 仅支持单 "*" 通配（项目内用法）
        prefix, suffix = match.split("*", 1)
        for k in list(self.store.keys()):
            if k.startswith(prefix) and k.endswith(suffix):
                yield k

    def expire(self, key: str, ttl: int) -> None:
        self.expired_at[key] = ttl

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


def _patched_redis() -> RedisClient:
    """真实 RedisClient 包装层 + 内存 raw 替身（与生产一致：set 存 json 串）。"""
    rc = RedisClient()
    rc.client = _FakeRedis()
    return rc


class TestL2Config:
    def test_defaults_are_conservative(self):
        # Act: 无 Redis 连接时兜底默认值
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", RedisClient()):
            cfg = load_l2_config()
        # Assert
        assert cfg["enabled"] is False
        assert cfg["buy_trigger"] == 65.0
        assert cfg["sell_trigger"] == 45.0
        assert cfg["cooldown_min"] == 30
        assert cfg["factor_weights"] is None

    def test_save_restricts_pool_size(self):
        # Arrange
        rc = _patched_redis()
        # Act
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            save_l2_config({"pool_size": 999, "buy_trigger": 70, "enabled": True})
            cfg = load_l2_config()
        # Assert
        assert cfg["pool_size"] == 50
        assert cfg["buy_trigger"] == 70.0
        assert cfg["enabled"] is True

    def test_save_ignores_unknown_keys(self):
        # Arrange
        rc = _patched_redis()
        # Act
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            cfg = save_l2_config({"hack": "x", "buy_trigger": 66})
        # Assert
        assert "hack" not in cfg
        assert cfg["buy_trigger"] == 66.0


class TestCooldown:
    def test_cooldown_blocks_within_window(self):
        # Arrange
        redis = MagicMock()
        redis.get.return_value = str(time.time())
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", redis):
            # Act
            blocked = is_cooldown("SH600000", cooldown_min=30)
        # Assert
        assert blocked is True

    def test_cooldown_expired_allows(self):
        # Arrange
        redis = MagicMock()
        redis.get.return_value = str(time.time() - 3600)
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", redis):
            # Act
            blocked = is_cooldown("SH600000", cooldown_min=30)
        # Assert
        assert blocked is False

    def test_set_cooldown_writes_timestamp(self):
        # Arrange
        redis = MagicMock()
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", redis):
            # Act
            set_cooldown("SH600000", 30)
        # Assert
        redis.set.assert_called_once()
        assert "tdx:l2:cooldown:sh600000" in redis.set.call_args[0][0]


# ============ 委托时点行情落盘（"什么点买的"数据源） ============

class TestOrderQuotePersistence:
    def test_save_and_load_order_quote(self):
        # Arrange
        rc = _patched_redis()
        # Act
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            from backend.services.live_trading.services.tdx_l2_realtime import (
                load_order_quotes,
                load_symbol_quotes,
                save_order_quote,
            )

            save_order_quote(
                symbol="SH600000", order_id="1001", plan_id="p1", side="buy",
                volume=100, amount=1050.0, quote_price=10.5,
                market_detail="上证 3512.34 vs MA20 3400 (之上)", index_above=True,
            )
        # Assert: 每笔委托一条 + 每只最新一条
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            quotes = load_order_quotes()
            sym_quotes = load_symbol_quotes()
        assert "1001" in quotes
        assert quotes["1001"]["quote_price"] == 10.5
        assert quotes["1001"]["index_above"] is True
        assert "上证 3512.34" in quotes["1001"]["market_detail"]
        assert sym_quotes["SH600000"]["order_id"] == "1001"
        assert rc.client.expired_at["tdx:l2:order_quote:1001"] == 7 * 3600
        assert rc.client.expired_at["tdx:l2:quotes:sh600000"] == 24 * 3600

    def test_merge_order_states_adds_filled_price(self):
        # Arrange
        rc = _patched_redis()
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            from backend.services.live_trading.services.tdx_l2_realtime import (
                load_order_quotes,
                merge_order_states,
                save_order_quote,
            )

            save_order_quote(
                symbol="SH600000", order_id="2001", plan_id="p2", side="buy",
                volume=200, amount=2100.0, quote_price=10.5,
            )
            # Act: 成交后把桥的实际成交均价并进时点记录
            merge_order_states(
                [{"order_id": "2001", "status": "filled", "filled_price": 10.42, "filled_volume": 200}]
            )
        # Assert: 决策时行情与成交均价双口径并存
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            rec = load_order_quotes()["2001"]
        assert rec["quote_price"] == 10.5
        assert rec["filled_price"] == 10.42
        assert rec["status"] == "filled"

    def test_merge_ignores_unknown_order(self):
        # Arrange
        rc = _patched_redis()
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            from backend.services.live_trading.services.tdx_l2_realtime import (
                merge_order_states,
            )

            merge_order_states([{"order_id": "9999", "status": "filled"}])
        # Assert: 无异常且无写入
        assert not rc.client.store


# ============ 在途单注册表（不能多/不能漏） ============

class TestInflightRegistry:
    def test_save_load_clear_roundtrip(self):
        # Arrange
        rc = _patched_redis()
        from backend.services.live_trading.services.tdx_l2_realtime import (
            clear_inflight,
            list_inflight,
            load_inflight,
            save_inflight,
        )

        # Act
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            save_inflight("SH600000", {"side": "buy", "volume": 100, "order_id": "3001", "plan_id": "p3", "ts": 1.0, "retries": 0})
            rec = load_inflight("SH600000")
            all_records = list_inflight()
            clear_inflight("SH600000")
        # Assert
        assert rec["order_id"] == "3001"
        assert all_records["SH600000"]["plan_id"] == "p3"
        assert load_inflight("SH600000") is None
        assert rc.client.expired_at["tdx:l2:inflight:sh600000"] == 4 * 3600

    def test_list_inflight_returns_all_symbols(self):
        # Arrange
        rc = _patched_redis()
        from backend.services.live_trading.services.tdx_l2_realtime import (
            list_inflight,
            save_inflight,
        )

        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            save_inflight("SH600000", {"side": "buy", "volume": 100, "order_id": "a", "ts": 1.0, "retries": 0})
            save_inflight("SZ000001", {"side": "sell", "volume": 100, "order_id": "b", "ts": 1.0, "retries": 0})
            records = list_inflight()
        # Assert
        assert set(records.keys()) == {"SH600000", "SZ000001"}
        assert records["SZ000001"]["side"] == "sell"


# ============ 未成交重挂状态机 ============

class TestRetryInflightOrders:
    def _svc_and_orders(self, orders: list[dict]):
        from backend.services.live_trading.services.tdx_rolling_trade_service import (
            TdxRollingTradeService,
        )

        svc = MagicMock(spec=TdxRollingTradeService)
        svc.cancel_order = AsyncMock(return_value={"success": True, "message": "ok"})
        svc.pull_today_orders = AsyncMock(return_value=orders)
        return svc

    def _run_retry(self, rc, svc, *, inflight, signal_scores=None, pool=None, today_orders=None):
        from backend.services.live_trading.services.tdx_l2_realtime import (
            _retry_inflight_orders,
            save_inflight,
        )

        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            for sym, rec in inflight.items():
                save_inflight(sym, rec)
        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc), \
             patch("backend.services.live_trading.services.tdx_l2_realtime.tdx_pusher") as pusher:
            pusher.place_order = AsyncMock(
                return_value={"orders": [{"status": "submitted", "order_id": "9001"}]}
            )
            return run_retry_sync(
                _retry_inflight_orders(
                    svc,
                    signal_scores=signal_scores or {},
                    pool_data=pool or {"SH600000": {"now": 10.5}},
                    fixed_buy_amount=10000.0,
                    cooldown_min=30,
                    today_orders=today_orders or [],
                    market_detail="上证 3512.34",
                    index_above=True,
                ),
                pusher,
            )

    def test_filled_clears_and_sets_cooldown(self):
        # Arrange
        rc = _patched_redis()
        svc = self._svc_and_orders(
            [{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "filled", "filled_volume": 100, "total_volume": 100}]
        )
        # Act
        stats, _ = self._run_retry(
            rc, svc,
            inflight={"SH600000": {"side": "buy", "volume": 100, "order_id": "3001", "plan_id": "p3", "ts": time.time() - 10, "retries": 0}},
            signal_scores={"SH600000": 90.0},
            today_orders=[{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "filled", "filled_volume": 100}],
        )
        # Assert: 已成交 → 清档 + 冷却（从成交起算防反复触发）
        assert stats["cleared"] == ["SH600000"]
        assert rc.get("tdx:l2:cooldown:sh600000") is not None
        assert "tdx:l2:inflight:sh600000" not in rc.client.store

    def test_working_young_waits(self):
        # Arrange
        rc = _patched_redis()
        svc = self._svc_and_orders(
            [{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "submitted"}]
        )
        # Act
        stats, _ = self._run_retry(
            rc, svc,
            inflight={"SH600000": {"side": "buy", "volume": 100, "order_id": "3001", "plan_id": "p3", "ts": time.time() - 10, "retries": 0}},
            signal_scores={"SH600000": 90.0},
            today_orders=[{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "submitted"}],
        )
        # Assert: 挂单未超时 → 等撮合, 不撤不重挂
        assert stats["waiting"] == 1
        svc.cancel_order.assert_not_awaited()

    def test_rejected_resubmits_with_new_plan_id(self):
        # Arrange
        rc = _patched_redis()
        svc = self._svc_and_orders(
            [{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "rejected"}]
        )
        # Act
        stats, pusher = self._run_retry(
            rc, svc,
            inflight={"SH600000": {"side": "buy", "volume": 900, "order_id": "3001", "plan_id": "p3", "ts": time.time() - 200, "retries": 0}},
            signal_scores={"SH600000": 90.0},
            today_orders=[{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "rejected"}],
        )
        # Assert: 废单 → 换新 plan_id 按最新实时价重挂, 并保存时点行情
        assert stats["resubmitted"] == ["SH600000"]
        args, kwargs = pusher.place_order.await_args
        assert kwargs["plan_id"].startswith("rolling_l2_SH600000_buy_")
        assert kwargs["plan_id"] != "p3"
        assert kwargs["price"] == 10.5
        rec = rc.get("tdx:l2:inflight:sh600000")
        assert rec["order_id"] == "9001"
        assert rec["retries"] == 1
        quote = rc.get("tdx:l2:order_quote:9001")
        assert quote["quote_price"] == 10.5
        assert quote["market_detail"] == "上证 3512.34"

    def test_signal_gone_cancels_and_clears(self):
        # Arrange
        rc = _patched_redis()
        svc = self._svc_and_orders(
            [{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "submitted"}]
        )
        # Act: 信号消失（不在分数池）→ 撤单收掉
        stats, _ = self._run_retry(
            rc, svc,
            inflight={"SH600000": {"side": "buy", "volume": 100, "order_id": "3001", "plan_id": "p3", "ts": time.time() - 10, "retries": 0}},
            signal_scores={},
            today_orders=[{"order_id": "3001", "stock_code": "SH600000", "side": "buy", "status": "submitted"}],
        )
        # Assert
        svc.cancel_order.assert_awaited_once_with("SH600000", "3001")
        assert stats["cancelled"] == ["SH600000"]
        assert "tdx:l2:inflight:sh600000" not in rc.client.store

    def test_retries_exhausted_gives_up(self):
        # Arrange
        rc = _patched_redis()
        svc = self._svc_and_orders([])
        # Act: 重挂次数用尽 → 放弃, 不再发单
        stats, pusher = self._run_retry(
            rc, svc,
            inflight={"SH600000": {"side": "buy", "volume": 900, "order_id": "3001", "plan_id": "p3", "ts": time.time() - 500, "retries": 10}},
            signal_scores={"SH600000": 90.0},
        )
        # Assert
        assert stats["given_up"] == ["SH600000"]
        pusher.place_order.assert_not_called()


def run_retry_sync(coro, pusher):
    """同步执行重挂协程并返回 (stats, pusher)。"""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro), pusher
    finally:
        loop.close()


# ============ 主循环稳定性（桥断/engine断/采集陈旧都不停评分） ============

class _SpyNotifier:
    """通知替身：记调用（形状与 ``notification_publisher`` 的通知器一致）。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple] = []

    async def __call__(self, user_id, title, content, level="info") -> bool:
        self.calls.append((user_id, title, content, level))
        return self.ok


class TestLoopStability:
    def _seed_pool(self, rc, n: int = 6):
        """写 n 只**新鲜**因子（与生产同形：ts = 当前上海墙钟）。

        旧夹具写死 ``2026-08-25T10:00:00``（陈旧）而回环仍照常评分——**等于把
        「不筛时间戳」这个缺陷固化成前提**。生产写的是当前钟，夹具须同形。
        """
        from datetime import datetime

        from backend.services.live_trading.services.tdx_l2_realtime import _REALTIME_KEY

        ts = datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005 — naive 上海墙钟正是契约本身（写侧写它、读侧同钟比较）
        for i in range(n):
            sym = f"SH60000{i}"
            rc.set(
                _REALTIME_KEY.format(symbol=sym),
                {
                    "symbol": sym,
                    "ts": ts,
                    "factors": _factors(micro_vpin_vol_ratio=0.5, micro_open_gap=0.02),
                    "now": 10.0 + i,
                },
            )

    def _bootstrap(self, rc, svc):
        from datetime import datetime

        from backend.services.live_trading.services import tdx_l2_capture_task as cap
        from backend.services.live_trading.services.tdx_l2_realtime import (
            save_l2_config,
        )

        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc):
            save_l2_config({
                "enabled": True, "interval_sec": 1,
                "buy_trigger": 65, "sell_trigger": 45, "cooldown_min": 0,
            })
        cap.l2_status.update({
            "running": True,
            "last_cycle_at": datetime.now().isoformat(),
        })
        svc.load_latest_scores = AsyncMock(return_value=(None, {f"SH60000{i}": 3.0 for i in range(6)}, "run-1"))
        svc.is_index_above_ma20 = AsyncMock(return_value=(True, "上证 3512.34 vs MA20 3400.00 (之上)"))
        svc.load_positions_from_tdx = AsyncMock(return_value=([], None))
        svc.pull_today_orders = AsyncMock(return_value=[])
        svc.cancel_order = AsyncMock(return_value={"success": True})
        svc.place_rolling_orders = AsyncMock(return_value=([], []))
        return cap.l2_status

    def _run_loop(
        self, rc, svc, *, in_trading_hours=True, notifier=None, mode="tdx", stop_after=2
    ):
        """跑 ``stop_after - 1`` 个**跑满**的周期（第 ``stop_after`` 周期注入
        CancelledError 退出）。

        停止钩子挂在**持仓查询**（step 6）上：它之前的评分已落盘、之后的执行与
        状态镜像尚未发生——要跑满 N 轮就传 ``N + 1``。

        **时段与执行模式一律注入**（``in_trading_hours`` / ``mode``）：真单闸门读
        它们，靠墙上钟或真配置的话同样的用例白天绿、收盘后红——「机构级」的测试
        不能在收盘后变色。
        ``notifier`` 传替身时随告警面一起装上（``tdx_exec_alerts.default_notifier``
        是惰性工厂，只有真有事要推时才会被调用）。
        """
        import asyncio

        from backend.services.live_trading.services import tdx_l2_capture_task as cap
        from backend.services.live_trading.services.tdx_l2_realtime import (
            realtime_status,
            run_tdx_l2_realtime_task,
        )

        cycles = {"n": 0}
        loader = (
            svc.load_positions_from_paper if mode == "paper" else svc.load_positions_from_tdx
        )

        async def stop_after_cycles(*args, **kwargs):
            cycles["n"] += 1
            if cycles["n"] >= stop_after:
                raise asyncio.CancelledError()
            return await loader(*args, **kwargs)

        if mode == "paper":
            svc.load_positions_from_paper = AsyncMock(side_effect=stop_after_cycles)
        else:
            svc.load_positions_from_tdx = AsyncMock(side_effect=stop_after_cycles)
        notifier_patch = (
            patch(
                "backend.services.live_trading.services.tdx_exec_alerts.default_notifier",
                new=lambda: notifier,
            )
            if notifier is not None
            else patch("backend.services.live_trading.services.tdx_exec_alerts.default_notifier")
        )

        with patch("backend.services.live_trading.services.tdx_l2_realtime.trade_redis", rc), \
             patch("backend.services.live_trading.services.tdx_l2_realtime.tdx_pusher") as pusher, \
             patch("backend.services.live_trading.services.tdx_l2_realtime.asyncio.sleep", AsyncMock()), \
             notifier_patch, \
             patch("backend.services.live_trading.services.tdx_l2_realtime.is_trading_time", return_value=in_trading_hours), \
             patch("backend.services.live_trading.services.tdx_rolling_trade_service.TdxRollingTradeService", return_value=svc), \
             patch("backend.services.live_trading.services.tdx_rolling_trade_service.load_rolling_config", return_value=("tdx", 10000.0, mode)), \
             patch("backend.services.trade.services.member_gate.is_paid_member", AsyncMock(return_value=True)), \
             patch("backend.services.live_trading.services.tdx_l2_capture_task.l2_status", cap.l2_status):
            with pytest.raises(asyncio.CancelledError):
                run_loop_sync(run_tdx_l2_realtime_task(interval_sec=1))
        return pusher, realtime_status

    def test_bridge_down_still_scores_and_skips_execution(self):
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        svc.load_positions_from_tdx = AsyncMock(side_effect=Exception("bridge unreachable"))
        # Act: 桥断 — 循环仍评分, 不触发执行
        pusher, status = self._run_loop(rc, svc)
        # Assert
        score_keys = [k for k in rc.client.store if k.startswith("tdx:l2:score:")]
        assert len(score_keys) >= 6
        assert status["bridge_ok"] is False
        assert "持仓查询失败" in (status["last_error"] or "")
        pusher.place_order.assert_not_called()

    def test_engine_fusion_failure_degrades_to_neutral(self):
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        svc.load_latest_scores = AsyncMock(side_effect=Exception("engine down"))
        # Act: engine 断 → 降级中性分, 评分照常落盘
        pusher, status = self._run_loop(rc, svc)
        # Assert
        score_keys = [k for k in rc.client.store if k.startswith("tdx:l2:score:")]
        assert len(score_keys) >= 6
        payload = rc.get("tdx:l2:score:SH600000")
        assert payload["fusion_score"] is None or payload["realtime_score"] >= 0
        assert payload["realtime_score"] < 65  # 中性分不致触发买入
        pusher.place_order.assert_not_called()

    def test_capture_stale_blocks_trigger_but_not_scoring(self):
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        # 采集链路陈旧（上一周期 1 小时前）
        from datetime import datetime, timedelta

        from backend.services.live_trading.services import tdx_l2_capture_task as cap

        cap.l2_status["last_cycle_at"] = (
            datetime.now() - timedelta(hours=1)
        ).isoformat()
        # Act
        pusher, status = self._run_loop(rc, svc)
        # Assert: 评分照常, 但禁止触发执行
        score_keys = [k for k in rc.client.store if k.startswith("tdx:l2:score:")]
        assert len(score_keys) >= 6
        assert status["capture_stale"] is True
        pusher.place_order.assert_not_called()

    def test_execution_path_actually_reaches_broker(self):
        """有买卖项且 execute_mode≠off 时，执行段必须真正走到券商调用。

        回归 70c538c9 引入的元组数不匹配：``_execute_signals`` 返回
        ``(placed, failed, error)`` **三元组**（docstring 亦然），调用点却按两元组
        解包 → 每轮抛 ``ValueError`` → 被外层兜住记「实时推理异常」
        → **L2 实时自动交易从未下过单**（分数照写、界面看着是活的）。

        ⚠️ 本类既有三个用例断言 ``pusher.place_order`` 未被调用，**恰好因这个异常
        而通过** —— 空参与。本用例断言执行段被触达，异常一回来它就红。
        """
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        # Act
        _pusher, status = self._run_loop(rc, svc)
        # Assert
        assert svc.place_rolling_orders.await_count >= 1, (
            "执行段未触达券商调用（execute_mode=tdx 且有买卖项）"
        )
        assert "unpack" not in (status["last_error"] or ""), (
            f"执行段抛异常：{status['last_error']}"
        )

    def test_loop_mirrors_status_to_redis(self):
        """循环必须把状态镜像到 `tdx:l2:realtime:status`（运维脚本/设置页读它）。

        该键**此前从未被写过**（`_STATUS_KEY` 定义了但零引用）→ 监控恒显示 {}。
        """
        # Arrange
        from backend.services.live_trading.services.tdx_l2_realtime import _STATUS_KEY

        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        # Act
        self._run_loop(rc, svc)
        # Assert
        mirrored = rc.get(_STATUS_KEY)
        assert isinstance(mirrored, dict), "状态键未被写入——监控面板会一直空着"
        assert mirrored.get("running") is True
        assert mirrored.get("last_cycle_at"), "镜像里必须带 last_cycle_at（活性判据）"
        assert rc.client.expired_at.get(_STATUS_KEY), "活性键必须有 TTL，否则死循环留下假活状态"

    def test_status_key_never_enters_factor_pool(self):
        """状态键与因子池**同前缀**（`tdx:l2:realtime:*`），不得被当成一只标的进池。

        ⚠️ 用的 ``ts`` 必须**新鲜**：陈旧时间戳会被新鲜度闸门顺手挡掉，那样测的是
        闸门而不是本守卫 —— 本用例初版就栽在这里（探针验出的空参与）。
        """
        # Arrange
        from datetime import datetime

        from backend.services.live_trading.services.tdx_l2_realtime import _STATUS_KEY

        rc = _patched_redis()
        self._seed_pool(rc)
        # 故意给状态键塞一个 symbol 字段（最坏情况：未来有人给状态加了这个字段）
        rc.set(_STATUS_KEY, {
            "symbol": "SH999999",
            "factors": _factors(),
            "ts": datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005 — 新鲜 → 只能靠守卫挡
        })
        svc = MagicMock()
        self._bootstrap(rc, svc)
        # Act
        _pusher, _status = self._run_loop(rc, svc)
        # Assert
        scored = {k.removeprefix("tdx:l2:score:") for k in rc.client.store
                  if k.startswith("tdx:l2:score:")}
        assert "SH999999" not in scored, "状态键被当成了标的进池"

    def test_stale_pool_entries_excluded_from_cross_section(self):
        """陈旧键必须**出池**：截面标准化的分母只能是同一时刻的那批标的。

        2026-09-23 实测：325 键中 212 个是 08-25~09-22 的陈料且 ttl=-1，
        采集停过的标的会一直留在池里，把今日每只标的的 z 都拉偏。
        """
        # Arrange: 6 只新鲜 + 4 只陈旧（模拟采集停摆后残留）
        from datetime import datetime, timedelta

        from backend.services.live_trading.services.tdx_l2_realtime import _REALTIME_KEY

        rc = _patched_redis()
        self._seed_pool(rc, n=6)
        stale_ts = (datetime.now() - timedelta(days=29)).isoformat(timespec="seconds")  # noqa: DTZ005
        for i in range(4):
            sym = f"SH99999{i}"
            rc.set(
                _REALTIME_KEY.format(symbol=sym),
                {
                    "symbol": sym,
                    "ts": stale_ts,
                    "factors": _factors(micro_vpin_vol_ratio=99.0),  # 极端值：混入必拉偏截面
                    "now": 10.0,
                },
            )
        svc = MagicMock()
        self._bootstrap(rc, svc)
        # Act
        _pusher, status = self._run_loop(rc, svc)
        # Assert: 分数只落新鲜的 6 只，陈旧的 4 只一个都没有
        scored = {k.removeprefix("tdx:l2:score:") for k in rc.client.store
                  if k.startswith("tdx:l2:score:")}
        assert not {f"SH99999{i}" for i in range(4)} & scored, "陈旧标的混进了截面"
        assert len(scored & {f"SH60000{i}" for i in range(6)}) >= 6, "新鲜标的被误杀"
        assert status["pool_stale_skipped"] == 4

    def test_malformed_ts_excluded_fail_closed(self):
        """``ts`` 取不到 → 按陈旧处理（宁可少一只，不可偏一池）。"""
        # Arrange
        from backend.services.live_trading.services.tdx_l2_realtime import _REALTIME_KEY

        rc = _patched_redis()
        self._seed_pool(rc, n=6)
        for i, bad in enumerate(("", None, "not-a-time")):
            sym = f"SH88888{i}"
            rc.set(
                _REALTIME_KEY.format(symbol=sym),
                {"symbol": sym, "ts": bad, "factors": _factors(), "now": 10.0},
            )
        svc = MagicMock()
        self._bootstrap(rc, svc)
        # Act
        _pusher, status = self._run_loop(rc, svc)
        # Assert
        scored = {k.removeprefix("tdx:l2:score:") for k in rc.client.store
                  if k.startswith("tdx:l2:score:")}
        assert not {f"SH88888{i}" for i in range(3)} & scored
        assert status["pool_stale_skipped"] == 3

    # ── 执行闸门与失败可见性（循环级） ────────────────────────────────
    @pytest.fixture(autouse=True)
    def _reset_alert_seen(self):
        """清掉 ``_ALERTED_KEYS``：它是**模块级**的进程内去重集（生产里一天一进程）。

        不清的话用例之间会互相压制——前一个用例投递成功的告警键留在集合里，
        后一个用例的同类故障**静默不推**（判据对、投递对，红在别处）。
        """
        from backend.services.live_trading.services import tdx_l2_realtime as mod

        mod._ALERTED_KEYS.clear()
        yield
        mod._ALERTED_KEYS.clear()

    def _mirrored(self, rc):
        """最后一轮**跑满**的周期镜像（面板/运维脚本读的那份，也是喂给判据的那份）。

        不能直接读模块级 ``realtime_status``：停止钩子抛在下一周期的**清零块之后**，
        那面字典会停在半途（字段已清、本轮还没写回）。
        """
        from backend.services.live_trading.services.tdx_l2_realtime import _STATUS_KEY

        snap = rc.get(_STATUS_KEY)
        assert isinstance(snap, dict), "状态键未写入——面板会一直空着"
        return snap

    def test_unreadable_positions_skip_trigger_and_reach_the_human(self):
        """持仓读不到 ⇒ 本周期一个触发都不做，且这条**要推给人**。

        循环 24/7 常驻、无人值守：把「少做了一整轮判断」写成一行 warning，
        等于让它自然消失。状态里的 ``trigger_skipped_symbols`` 是说给人听的数字。
        """
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        notifier = _SpyNotifier()
        svc.load_positions_from_tdx = AsyncMock(return_value=([], "通达信桥持仓拉取失败"))
        # Act
        pusher, _status = self._run_loop(rc, svc, notifier=notifier)
        # Assert
        pusher.place_order.assert_not_called()
        mirrored = self._mirrored(rc)
        assert mirrored["positions_error"] == "通达信桥持仓拉取失败"
        assert mirrored["trigger_skipped_symbols"] == 6
        assert mirrored["session_skipped_symbols"] == 0
        assert mirrored["in_trading_hours"] is True
        assert len(notifier.calls) == 1, "持仓读不到在交易时段内必须推一条"
        assert "持仓读不到" in notifier.calls[0][1]

    def test_out_of_hours_tdx_holds_real_orders_without_alerting(self):
        """盘外（tdx 模式）：不下真单、不做触发判断，但**分数照写**、且不推告警。

        「盘外不发单」不是故障——把它报成告警，每天收盘后都会响一次，
        人就开始无视这一类通知（那才是不再看得见真故障的时刻）。
        """
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        notifier = _SpyNotifier()
        # Act
        pusher, _status = self._run_loop(rc, svc, in_trading_hours=False, notifier=notifier)
        # Assert
        pusher.place_order.assert_not_called()
        svc.place_rolling_orders.assert_not_awaited()
        mirrored = self._mirrored(rc)
        assert mirrored["session_skipped_symbols"] == 6
        assert mirrored["trigger_skipped_symbols"] == 0
        assert mirrored["in_trading_hours"] is False
        assert mirrored["positions_error"] is None
        score_keys = [k for k in rc.client.store if k.startswith("tdx:l2:score:")]
        assert len(score_keys) >= 6, "盘外仍要评分（信号面板与次日基线都读它）"
        assert notifier.calls == [], "盘外不发单不是故障，不该推告警"

    def test_out_of_hours_paper_still_runs_the_full_trigger(self):
        """盘外（paper 模式）：本地撮合不受时段闸门约束（盘后演练正需要它）。"""
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        svc.load_positions_from_paper = AsyncMock(return_value=([], None))
        svc.place_paper_orders = AsyncMock(return_value=([], []))
        # Act
        _pusher, _status = self._run_loop(
            rc, svc, in_trading_hours=False, mode="paper"
        )
        # Assert
        svc.place_paper_orders.assert_awaited()
        mirrored = self._mirrored(rc)
        assert mirrored["session_skipped_symbols"] == 0
        assert mirrored["in_trading_hours"] is False

    def test_a_cycle_error_is_mirrored_and_pushed(self):
        """本周期抛异常 ⇒ ``cycle_error`` 进状态镜像**并推一条**（带 exc_info 的日志不够）。

        「循环在跑但每轮都炸」在外表上是最安静的一种坏法：状态键仍被写、面板仍是绿的。
        """
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        notifier = _SpyNotifier()
        svc.is_index_above_ma20 = AsyncMock(side_effect=RuntimeError("指数读取炸了"))
        # Act
        _pusher, _status = self._run_loop(rc, svc, notifier=notifier)
        # Assert
        assert "指数读取炸了" in (self._mirrored(rc)["cycle_error"] or "")
        assert [c for c in notifier.calls if "本周期异常" in c[1]], "周期异常必须推给人"

    def test_a_fault_then_a_clean_cycle_pushes_exactly_one_recovery(self):
        """先坏一轮、再好一轮 ⇒ 推一条「已恢复」，且**只有一条**。

        恢复通知的前提是「今天报过 + 本轮干净」两个事实都在：只按"本轮干净"推
        会在每个正常周期都报一次平安；只按"今天报过"推则永远等不到干净的一轮。
        """
        # Arrange
        rc = _patched_redis()
        self._seed_pool(rc)
        svc = MagicMock()
        self._bootstrap(rc, svc)
        notifier = _SpyNotifier()
        faults = {"n": 0}

        async def first_cycle_faults():
            faults["n"] += 1
            return ([], "通达信桥持仓拉取失败") if faults["n"] == 1 else ([], None)

        svc.load_positions_from_tdx = AsyncMock(side_effect=first_cycle_faults)
        # Act：跑满两个周期，第三个周期的持仓查询处退出
        _pusher, _status = self._run_loop(rc, svc, notifier=notifier, stop_after=3)
        # Assert
        assert len(notifier.calls) == 2, (
            f"应恰好两条（一条故障 + 一条恢复），实际 {[c[1] for c in notifier.calls]}"
        )
        assert "持仓读不到" in notifier.calls[0][1]
        assert notifier.calls[1][3] == "success"
        assert "已恢复" in notifier.calls[1][1]
        assert self._mirrored(rc)["positions_error"] is None, "干净的那轮状态必须回到干净"


class TestPayloadAge:
    """``_payload_age_sec``：池新鲜度判据（纯函数，读写同钟）。"""

    def _age(self, payload):
        from backend.services.live_trading.services.tdx_l2_realtime import (
            _payload_age_sec,
        )

        return _payload_age_sec(payload)

    def test_fresh_payload_is_near_zero(self):
        # Arrange
        from datetime import datetime

        payload = {"ts": datetime.now().isoformat(timespec="seconds")}  # noqa: DTZ005 — naive 上海墙钟正是契约本身（写侧写它、读侧同钟比较）
        # Act / Assert
        assert 0 <= self._age(payload) < 5

    def test_old_payload_age_tracks_wall_clock(self):
        # Arrange: 本仓实测的那批陈料是 29 天前
        from datetime import datetime, timedelta

        payload = {"ts": (datetime.now() - timedelta(days=29)).isoformat(timespec="seconds")}  # noqa: DTZ005
        # Act / Assert
        assert self._age(payload) == pytest.approx(29 * 86400, abs=5)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"ts": ""},
            {"ts": None},
            {"ts": "   "},
            {"ts": "not-a-time"},
            {"ts": "2026-13-45T99:99:99"},
        ],
    )
    def test_missing_or_malformed_ts_is_none(self, payload):
        assert self._age(payload) is None

    def test_aware_ts_normalized_to_local_clock(self):
        """aware 时间戳（防御性分支）不得因时区差算成十几个小时。"""
        from datetime import datetime, timezone

        payload = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        assert abs(self._age(payload)) < 5


def run_loop_sync(coro):
    """同步执行主循环协程（以 CancelledError 退出）。"""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()
