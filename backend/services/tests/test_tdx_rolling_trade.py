"""TdxRollingTradeService 单元测试。

- compute_rolling_signals 纯函数级测试：买卖信号计算、大盘 MA20 过滤、持仓滚动。
- 执行模式（off/tdx/paper）配置读写与兼容。
- 模拟盘直接下单：持仓来源、会员门控、paper 成交路径。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.live_trading.services.tdx_rolling_trade_service import (
    DEFAULT_EXECUTE_MODE,
    DEFAULT_SCORE_THRESHOLD,
    TdxRollingTradeService,
    load_rolling_config,
    save_rolling_config,
)


@pytest.fixture
def svc() -> TdxRollingTradeService:
    return TdxRollingTradeService()


def _pos(symbol, volume=1000, available_volume=None, name=""):
    return {
        "symbol": symbol,
        "name": name or symbol,
        "volume": volume,
        "available_volume": available_volume if available_volume is not None else volume,
        "cost_price": 10.0,
        "market_value": 10000.0,
    }


class TestComputeRollingSignals:
    def test_buys_stocks_above_threshold_when_index_above_ma20(self, svc):
        # Arrange: 000001.SZ 11.25 元/股，1 万元可买一手
        score_map = {"000001.SZ": 2.8, "000002.SZ": 1.5}
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=[], index_above_ma20=True
        )
        # Assert
        assert [b["symbol"] for b in result["buys"]] == ["000001.SZ"]
        assert result["sells"] == []

    def test_no_buys_when_index_below_ma20(self, svc):
        # Arrange
        score_map = {"600519.SH": 2.8}
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=[], index_above_ma20=False
        )
        # Assert
        assert result["buys"] == []

    def test_sells_held_stock_dropping_below_threshold(self, svc):
        # Arrange
        score_map = {"600519.SH": 1.9}
        positions = [_pos("600519.SH")]
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=positions, index_above_ma20=True
        )
        # Assert
        assert [s["symbol"] for s in result["sells"]] == ["600519.SH"]

    def test_sells_held_stock_missing_from_new_run(self, svc):
        # Arrange
        score_map = {}  # 最新推理无该股
        positions = [_pos("600519.SH")]
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=positions, index_above_ma20=True
        )
        # Assert
        assert [s["symbol"] for s in result["sells"]] == ["600519.SH"]
        assert "最新推理已无该股" in result["sells"][0]["reason"]

    def test_holds_stock_still_above_threshold(self, svc):
        # Arrange
        score_map = {"600519.SH": 2.5}
        positions = [_pos("600519.SH")]
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=positions, index_above_ma20=True
        )
        # Assert
        assert result["sells"] == []
        assert result["buys"] == []
        assert [h["symbol"] for h in result["holds"]] == ["600519.SH"]

    def test_sells_still_happen_when_index_below_ma20(self, svc):
        # Arrange
        score_map = {"600519.SH": 1.5}
        positions = [_pos("600519.SH")]
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=positions, index_above_ma20=False
        )
        # Assert
        assert [s["symbol"] for s in result["sells"]] == ["600519.SH"]

    def test_buys_ranked_by_score_desc(self, svc):
        # Arrange: 三只低价股均可买一手
        score_map = {"000001.SZ": 2.3, "000002.SZ": 3.0, "000063.SZ": 2.7}
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=[], index_above_ma20=True
        )
        # Assert
        assert [b["symbol"] for b in result["buys"]] == [
            "000002.SZ",
            "000063.SZ",
            "000001.SZ",
        ]

    def test_no_buy_for_already_held_stock(self, svc):
        # Arrange
        score_map = {"600519.SH": 2.8}
        positions = [_pos("600519.SH")]
        # Act
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=positions, index_above_ma20=True
        )
        # Assert
        assert result["buys"] == []

    def test_score_equal_to_threshold_sells(self, svc):
        # 规则边界: 分数 <= 阈值 应卖出（"低于2.2分，第二天要卖出"）
        score_map = {"600519.SH": DEFAULT_SCORE_THRESHOLD}
        positions = [_pos("600519.SH")]
        result = svc.compute_rolling_signals(
            score_map=score_map, positions=positions, index_above_ma20=True
        )
        assert [s["symbol"] for s in result["sells"]] == ["600519.SH"]

    def test_custom_threshold_from_config(self, svc):
        # 阈值可配置: 3.0 时 2.8 分应卖出而不是买入
        score_map = {"600519.SH": 2.8}
        positions = [_pos("600519.SH")]
        result = svc.compute_rolling_signals(
            score_map=score_map,
            positions=positions,
            index_above_ma20=True,
            score_threshold=3.0,
        )
        assert [s["symbol"] for s in result["sells"]] == ["600519.SH"]
        assert result["score_threshold"] == 3.0

    def test_custom_threshold_buys_lower_score(self, svc):
        # 阈值调低到 1.0 时, 1.5 分也应买入
        score_map = {"000001.SZ": 1.5}
        result = svc.compute_rolling_signals(
            score_map=score_map,
            positions=[],
            index_above_ma20=True,
            score_threshold=1.0,
        )
        assert [b["symbol"] for b in result["buys"]] == ["000001.SZ"]


GET_REDIS_PATH = "backend.services.trade_shared.redis_client.get_redis"
ROLLING_MODULE = "backend.services.live_trading.services.tdx_rolling_trade_service"


def _redis_holding(saved: dict | None):
    mock_redis = MagicMock()
    mock_redis.get.return_value = saved
    return mock_redis


class TestRollingConfigExecuteMode:
    def test_default_off(self):
        with patch(GET_REDIS_PATH, return_value=_redis_holding(None)):
            _, _, mode = load_rolling_config("default", "00000001")
        assert mode == DEFAULT_EXECUTE_MODE

    def test_legacy_auto_place_true_maps_to_tdx(self):
        with patch(GET_REDIS_PATH, return_value=_redis_holding({"auto_place": True})):
            _, _, mode = load_rolling_config("default", "00000001")
        assert mode == "tdx"

    def test_redis_execute_mode_wins(self):
        with patch(GET_REDIS_PATH, return_value=_redis_holding({"execute_mode": "paper"})):
            _, _, mode = load_rolling_config("default", "00000001")
        assert mode == "paper"

    def test_invalid_execute_mode_falls_back_off(self):
        with patch(GET_REDIS_PATH, return_value=_redis_holding({"execute_mode": "hack"})):
            _, _, mode = load_rolling_config("default", "00000001")
        assert mode == "off"

    def test_save_and_reload_round_trip(self):
        mock_redis = MagicMock()
        with patch(GET_REDIS_PATH, return_value=mock_redis):
            save_rolling_config(
                "default", "00000001",
                score_threshold=2.5, fixed_buy_amount=20000, execute_mode="paper",
            )
        saved = mock_redis.set.call_args.args[1]
        assert saved["execute_mode"] == "paper"
        assert saved["auto_place"] is True
        # 旧读取方（只看 auto_place）仍兼容
        with patch(GET_REDIS_PATH, return_value=_redis_holding(saved)):
            _, _, mode = load_rolling_config("default", "00000001")
        assert mode == "paper"


class TestLoadPositionsFromPaper:
    @pytest.mark.asyncio
    async def test_normalizes_long_positions_and_skips_short(self):
        svc = TdxRollingTradeService()
        account = {
            "positions": {
                "SH600519": {"volume": 100, "cost": 1500.0, "market_value": 150000.0},
                "SH600000::short": {"volume": 500, "cost": 10.0, "market_value": 5000.0},
                "empty": {"volume": 0, "cost": 0.0},
            }
        }
        fake_manager = MagicMock()
        fake_manager.get_account = AsyncMock(return_value=account)
        with patch(
            "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
            return_value=fake_manager,
        ):
            positions, error = await svc.load_positions_from_paper("default", "00000001")

        assert error == ""
        assert len(positions) == 1
        assert positions[0]["symbol"] == "600519.SH"
        assert positions[0]["volume"] == 100
        assert positions[0]["cost_price"] == 1500.0
        assert positions[0]["raw_symbol"] == "SH600519"

    @pytest.mark.asyncio
    async def test_missing_account_reports_error(self):
        svc = TdxRollingTradeService()
        fake_manager = MagicMock()
        fake_manager.get_account = AsyncMock(return_value=None)
        with patch(
            "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
            return_value=fake_manager,
        ):
            _, error = await svc.load_positions_from_paper("default", "00000001")
        assert "未初始化" in error


class TestRunRollingPushExecuteMode:
    @pytest.mark.asyncio
    async def test_paper_mode_places_paper_orders_without_bridge(self):
        svc = TdxRollingTradeService()
        fake_tdx = MagicMock()
        fake_tdx.enabled = False
        buys = [{"symbol": "600519.SH", "score": 2.8, "volume": 100, "close": 1500.0}]
        sells = []
        with (
            patch(f"{ROLLING_MODULE}.load_rolling_config", return_value=(2.2, 10000.0, "paper")),
            patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx),
            patch(
                "backend.services.trade.services.member_gate.is_paid_member",
                new=AsyncMock(return_value=True),
            ),
            patch.object(svc, "load_latest_scores", new=AsyncMock(
                return_value=("run1", {"600519.SH": 2.8}, "2026-08-25")
            )),
            patch.object(svc, "load_positions_from_paper", new=AsyncMock(return_value=([], ""))),
            patch.object(svc, "load_positions_from_tdx") as load_tdx,
            patch.object(svc, "is_index_above_ma20", new=AsyncMock(return_value=(True, "ok"))),
            patch.object(svc, "compute_rolling_signals", new=MagicMock(
                return_value={"buys": buys, "sells": sells, "holds": []}
            )),
            patch(
                "backend.services.live_trading.services.tdx_signal_push_service._batch_lookup_names",
                new=MagicMock(return_value={}),
            ),
            patch.object(svc, "place_paper_orders", new=AsyncMock(return_value=(buys, []))),
            patch.object(svc, "place_rolling_orders") as place_tdx_orders,
        ):
            result = await svc.run_rolling_push(tenant_id="default", user_id="00000001")

        assert result["success"] is True
        assert result["execute_mode"] == "paper"
        assert result["positions_source"] == "paper"
        assert result["placed_orders"] == buys
        load_tdx.assert_not_called()
        place_tdx_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_tdx_mode_requires_bridge(self):
        svc = TdxRollingTradeService()
        fake_tdx = MagicMock()
        fake_tdx.enabled = False
        with (
            patch(f"{ROLLING_MODULE}.load_rolling_config", return_value=(2.2, 10000.0, "tdx")),
            patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx),
            patch(
                "backend.services.trade.services.member_gate.is_paid_member",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await svc.run_rolling_push(tenant_id="default", user_id="00000001")

        assert result["success"] is False
        assert "TDX_BRIDGE_URL" in result["error"]

    @pytest.mark.asyncio
    async def test_warning_only_mode_needs_no_member_and_no_bridge(self):
        svc = TdxRollingTradeService()
        fake_tdx = MagicMock()
        fake_tdx.enabled = False
        with (
            patch(f"{ROLLING_MODULE}.load_rolling_config", return_value=(2.2, 10000.0, "off")),
            patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx),
            patch(
                "backend.services.trade.services.member_gate.is_paid_member"
            ) as check_member,
            patch.object(svc, "load_latest_scores", new=AsyncMock(
                return_value=("run1", {"600519.SH": 2.8}, "2026-08-25")
            )),
            patch.object(svc, "load_positions_from_tdx", new=AsyncMock(return_value=([], ""))),
            patch.object(svc, "is_index_above_ma20", new=AsyncMock(return_value=(True, "ok"))),
            patch.object(svc, "compute_rolling_signals", new=MagicMock(
                return_value={"buys": [], "sells": [], "holds": []}
            )),
            patch(
                "backend.services.live_trading.services.tdx_signal_push_service._batch_lookup_names",
                new=MagicMock(return_value={}),
            ),
        ):
            result = await svc.run_rolling_push(tenant_id="default", user_id="00000001")

        assert result["success"] is True
        assert result["execute_mode"] == "off"
        check_member.assert_not_called()


# ============ 桥交互方法回归（曾因方法插入错位导致 body 错乱） ============

class TestTdxBridgeInteraction:
    @pytest.mark.asyncio
    async def test_load_positions_from_tdx_normalizes_bridge_payload(self):
        svc = TdxRollingTradeService()
        fake_tdx = MagicMock()
        fake_tdx.pull_positions = AsyncMock(return_value=[
            {"stock_code": "600206.SH", "total_volume": 2400, "available_volume": 1000,
             "cost_price": 50.745, "market_value": 126048.0, "stock_name": "有研新材"},
            {"stock_code": "688783.SH", "total_volume": 0, "available_volume": 0,
             "cost_price": 0.0, "market_value": 0.0, "stock_name": "空仓残留"},
        ])
        with patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            positions, error = await svc.load_positions_from_tdx()

        assert error == ""
        assert len(positions) == 1  # 已清仓残留(total_volume=0)被过滤
        assert positions[0]["symbol"] == "600206.SH"
        assert positions[0]["volume"] == 2400

    @pytest.mark.asyncio
    async def test_load_positions_from_tdx_returns_error_on_bridge_failure(self):
        svc = TdxRollingTradeService()
        fake_tdx = MagicMock()
        fake_tdx.pull_positions = AsyncMock(side_effect=Exception("bridge down"))
        with patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            positions, error = await svc.load_positions_from_tdx()

        assert positions == []
        assert "通达信桥持仓拉取失败" in error

    @pytest.mark.asyncio
    async def test_cancel_order_proxies_bridge_and_returns_result(self):
        svc = TdxRollingTradeService()
        fake_tdx = MagicMock()
        fake_tdx.cancel_order = AsyncMock(
            return_value={"success": True, "message": "已撤"}
        )
        with patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.cancel_order("SH600206", "160356")

        assert result["success"] is True
        fake_tdx.cancel_order.assert_awaited_once_with("SH600206", "160356")

    @pytest.mark.asyncio
    async def test_pull_today_orders_proxies_bridge(self):
        svc = TdxRollingTradeService()
        fake_tdx = MagicMock()
        fake_tdx.pull_orders = AsyncMock(return_value=[{"order_id": "160356"}])
        with patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            orders = await svc.pull_today_orders("SH600206")

        assert orders == [{"order_id": "160356"}]
        fake_tdx.pull_orders.assert_awaited_once_with("SH600206")


# ============ 执行闸门：持仓读不到 / 非交易时段（真单） ============

class _SpyNotifier:
    """通知替身：记调用、可注入失败（本类用例断言"真的推出去了"）。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple] = []

    async def __call__(self, user_id, title, content, level="info") -> bool:
        self.calls.append((user_id, title, content, level))
        return self.ok


class _FakeRedis:
    """原生形状的去重设施替身（``set(k, v, ex=)``）。"""

    def __init__(self) -> None:
        self.store: dict = {}

    def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)


def _pusher(enabled: bool = True):
    """通达信推送服务替身：**所有被 await 的方法都必须是 AsyncMock**。

    漏一个就红在 ``object MagicMock can't be used in 'await' expression``——
    报错点落在服务代码里，看起来像实现坏了（预警段走 ``push_message``，
    不是 ``push_warnings``，两条路都要铺）。故集中一处造。
    """
    pusher = MagicMock()
    pusher.enabled = enabled
    pusher.push_message = AsyncMock(return_value={"success": True})
    pusher.push_warnings = AsyncMock(return_value={"success": True})
    return pusher


def _patch_stack(*, mode: str, in_session: bool, notifier, redis):
    """rolling 运行路径上的旁路替身（不碰真 Redis/真通知表）。

    ``is_trading_time`` **注入**：真单闸门读它，靠墙上钟的话用例白天绿、收盘后红。
    """
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(f"{ROLLING_MODULE}.load_rolling_config", return_value=(2.2, 10000.0, mode))
    )
    stack.enter_context(patch(f"{ROLLING_MODULE}.is_trading_time", return_value=in_session))
    stack.enter_context(patch(f"{ROLLING_MODULE}.trade_redis", redis))
    stack.enter_context(
        patch(
            "backend.services.live_trading.services.tdx_exec_alerts.default_notifier",
            new=lambda: notifier,
        )
    )
    stack.enter_context(
        patch(
            "backend.services.trade.services.member_gate.is_paid_member",
            new=AsyncMock(return_value=True),
        )
    )
    stack.enter_context(
        patch(
            "backend.services.live_trading.services.tdx_signal_push_service._batch_lookup_names",
            new=MagicMock(return_value={}),
        )
    )
    return stack


_BUY = {"symbol": "600519.SH", "score": 2.8, "volume": 100, "close": 1500.0}
_SELL = {"symbol": "000001.SZ", "score": 1.5, "volume": 200, "available_volume": 200,
         "close": 12.0, "name": "平安银行", "reason": "掉下阈值"}


def _arm(svc, *, sells=None, buys=None):
    """铺好「有买卖信号」的一次运行所需的全部旁路（执行段之前）。"""
    svc.load_latest_scores = AsyncMock(
        return_value=("run1", {"600519.SH": 2.8, "000001.SZ": 1.5}, "2026-08-25")
    )
    svc.is_index_above_ma20 = AsyncMock(return_value=(True, "ok"))
    svc.compute_rolling_signals = MagicMock(
        return_value={"buys": buys if buys is not None else [dict(_BUY)],
                      "sells": sells if sells is not None else [dict(_SELL)],
                      "holds": []}
    )
    svc.place_rolling_orders = AsyncMock(return_value=([], []))
    svc.place_paper_orders = AsyncMock(return_value=([], []))


class TestRollingExecutionGates:
    @pytest.mark.asyncio
    async def test_unreadable_positions_block_orders_and_warnings(self):
        """持仓读不到 ⇒ fail-closed：不下单、不推预警，且把原因与条数登记在结果里。

        两个 loader 都是「出错返回 ([], 原因)」而**不抛**：此前那个原因只落一行
        warning，流程照走 —— held={} 让卖出腿全空（想卖的卖不掉）、买入腿把每只
        达标标的当成新仓买满一篮子。持仓未知时算出来的信号不是"少几条"，是**算错**。
        """
        svc = TdxRollingTradeService()
        _arm(svc)
        svc.load_positions_from_tdx = AsyncMock(return_value=([], "通达信桥持仓拉取失败"))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        svc.place_rolling_orders.assert_not_awaited()
        fake_tdx.push_warnings.assert_not_awaited()          # 预警也是按 held 算的
        assert result["positions_error"] == "通达信桥持仓拉取失败"
        assert result["signals_suppressed"] == 2             # 1 买 + 1 卖
        assert result["results"]["warnings_skipped"]["success"] is False
        assert result["in_trading_hours"] is True
        # 告警面：交易时段内这条必须推出去（人不在场正是需要它的场合）
        assert len(notifier.calls) == 1
        assert "持仓读不到" in notifier.calls[0][1]

    @pytest.mark.asyncio
    async def test_unreadable_positions_also_block_paper_orders(self):
        """模拟盘同一把闸：持仓未知时算出来的买卖腿同样是错的。"""
        svc = TdxRollingTradeService()
        _arm(svc)
        svc.load_positions_from_paper = AsyncMock(return_value=([], "模拟盘账户不可用"))
        fake_tdx, notifier, redis = _pusher(enabled=False), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="paper", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        svc.place_paper_orders.assert_not_awaited()
        assert result["signals_suppressed"] == 2
        assert result["positions_error"] == "模拟盘账户不可用"

    @pytest.mark.asyncio
    async def test_out_of_hours_real_orders_are_held_back_and_recorded(self):
        """非交易时段的真单一律不提交，但**登记在案**（不是静默消失）。

        rolling 是「每次推理完跑一次」拉动的：推理完全可能在盘后完成（收盘重算/
        补跑），那一刻的真单只会被柜台拒、或被客户端挂成次日单。
        """
        svc = TdxRollingTradeService()
        _arm(svc)
        svc.load_positions_from_tdx = AsyncMock(return_value=([], ""))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="tdx", in_session=False, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        svc.place_rolling_orders.assert_not_awaited()
        assert result["session_suppressed"] == 2
        assert "非交易时段" in result["results"]["execute_skipped"]["error"]
        assert result["in_trading_hours"] is False
        # 预警照发（预警是建议、不是委托）：盘后仍可以给人看
        assert fake_tdx.push_warnings.await_count >= 1

    @pytest.mark.asyncio
    async def test_out_of_hours_paper_orders_still_simulate(self):
        """模拟盘不受时段闸门约束：盘后手动「立即执行」演练正需要它。"""
        svc = TdxRollingTradeService()
        _arm(svc)
        svc.load_positions_from_paper = AsyncMock(return_value=([], ""))
        fake_tdx, notifier, redis = _pusher(enabled=False), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="paper", in_session=False, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        assert svc.place_paper_orders.await_count == 1
        assert result["session_suppressed"] == 0

    @pytest.mark.asyncio
    async def test_the_outcome_carries_every_field_the_alert_judges_read(self):
        """夹具形状=生产形状：判据读的字段必须真的由 ``run_rolling_push`` 写。

        判据是「读字典 → 判断」，字段名漂移两边都不报错：结果里没有该字段 ⇒
        ``.get`` 得 None ⇒ 判据恒不触发 ⇒ 告警面静默消失（判据用例全绿）。
        两类字段分开钉——**每次运行都写**的事实（面板与判据共用）与
        **只在早退失败分支写**的 ``error``。
        """
        svc = TdxRollingTradeService()
        _arm(svc, buys=[], sells=[])
        svc.load_positions_from_tdx = AsyncMock(return_value=([], ""))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        for field in (
            "success", "positions_error", "signals_suppressed",
            "session_suppressed", "in_trading_hours", "failed_orders",
        ):
            assert field in result, f"结果字典里没有告警判据要读的字段：{field}"
        assert result["success"] is True
        assert "error" not in result, (
            "成功路径上带了 error 键——判据读法是 not success and err，"
            "键在而值为空是对的、值有内容就会把成功当失败"
        )

        # 另一条分支：早退型失败（无分数）必须带上原因，aborted 判据靠它
        svc2 = TdxRollingTradeService()
        svc2.load_latest_scores = AsyncMock(return_value=(None, {}, ""))
        notifier2, redis2 = _SpyNotifier(), _FakeRedis()
        with _patch_stack(mode="tdx", in_session=True, notifier=notifier2, redis=redis2), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", _pusher()):
            failed = await svc2.run_rolling_push(tenant_id="default", user_id="10000001")

        assert failed["success"] is False
        assert str(failed.get("error") or "").strip(), (
            "早退失败没有写 error——「一条都没执行」的告警就没有原因可讲"
        )

    @pytest.mark.asyncio
    async def test_a_failed_leg_is_pushed_with_its_reason(self):
        """真去下单但失败 ⇒ 推一条（一天一次），且点明"不会自动重发"。"""
        svc = TdxRollingTradeService()
        _arm(svc)
        svc.load_positions_from_tdx = AsyncMock(return_value=([], ""))
        svc.place_rolling_orders = AsyncMock(
            return_value=([], [{"symbol": "600519.SH", "side": "buy", "error": "风控拒单[L1]"}])
        )
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        assert result["failed_orders"]
        assert len(notifier.calls) == 1
        title, content = notifier.calls[0][1], notifier.calls[0][2]
        assert "下单失败" in title
        assert "风控拒单[L1]" in content
        assert "不会自动重发" in content


class TestSessionRefusalProducer:
    """生产者侧：券商回执里「被时段闸挡下」这件事怎么变成失败行上的标记。

    上一条纪律（``TestAlertDiscipline``）测的是消费方——它喂进去的是**已经带标记**
    的行。标记本身由提交器从信封里翻译过来：``{status:error, skipped:out_of_session}``
    按 ``status`` 判就是"下单被拒"，会被推成「订单失败，请核对 orders 补单」，
    而 orders 里根本没有这些单。这条把信封形状与告警口径钉在一起。
    """

    @pytest.mark.asyncio
    async def test_a_refusal_from_the_bridge_is_recorded_as_not_submitted(self):
        from types import SimpleNamespace

        from backend.services.live_trading.services.trading_session import (
            OUT_OF_SESSION_MESSAGE,
        )

        svc = TdxRollingTradeService()
        _arm(svc)
        svc.__dict__.pop("place_rolling_orders", None)  # 走真的提交器（_arm 铺的是替身）
        svc.load_positions_from_tdx = AsyncMock(return_value=([], ""))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()
        fake_tdx.place_order = AsyncMock(
            return_value={
                "status": "error",
                "skipped": "out_of_session",
                "message": OUT_OF_SESSION_MESSAGE,
                "orders": [],
            }
        )

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx), patch(
                    "backend.services.trade.services.risk_gate_service.check_direct_order",
                    new=AsyncMock(return_value=SimpleNamespace(passed=True, rule_id=None, reason=None)),
                ):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        assert fake_tdx.place_order.await_count == 2, "两条腿都要真的走到提交器"
        assert result["failed_orders"] == []
        assert result["session_suppressed"] == 2
        assert "非交易时段" in result["results"]["execute_skipped"]["error"]
        assert notifier.calls == [], "被闸门挡下不是故障，不该推告警"


class TestAlertDiscipline:
    """告警面的三条纪律（exec-leg 复审 2026-09-24）：被自家闸门压住的一轮不冒充
    「已恢复」；跨时段的拒单不算下单失败；坐标取账户，不取「谁跑的推理」。"""

    @staticmethod
    def _reported_today(redis) -> None:
        """让「今天报过」成立：告警键只在送达后写，这里直接铺一条。"""
        from backend.services.live_trading.services import tdx_exec_alerts as X

        redis.store[X.alert_key(X.FAMILY_ROLLING, X.KIND_ORDERS_FAILED,
                                X.now_shanghai().date())] = "1"

    @pytest.mark.asyncio
    async def test_a_suppressed_round_does_not_announce_recovery(self):
        """10:00 报过故障、20:00 补跑被时段闸全压住 ⇒ **不许**推「已恢复」。

        那一轮一条委托都没提交过：上午的故障（比如桥断了）可能还在，而值班
        收到「已恢复」后就再没人看它。恢复要等一个真能下单的轮次来宣布。
        """
        svc = TdxRollingTradeService()
        _arm(svc)                       # 有 1 买 1 卖 ⇒ 盘外必进 session_suppressed
        svc.load_positions_from_tdx = AsyncMock(return_value=([], ""))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()
        self._reported_today(redis)

        with _patch_stack(mode="tdx", in_session=False, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        assert result["session_suppressed"] == 2, "夹具没造出「被压住的一轮」"
        assert notifier.calls == []

    @pytest.mark.asyncio
    async def test_a_clean_in_session_round_announces_recovery(self):
        """反向对照：同一份「今天报过」的铺垫 + 盘中干净的一轮 ⇒ 「已恢复」必须推。

        没有它，上面那条 ``calls == []`` 在「恢复通知链路整个断了」时也是绿的。
        """
        svc = TdxRollingTradeService()
        _arm(svc, buys=[], sells=[])
        svc.load_positions_from_tdx = AsyncMock(return_value=([], ""))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()
        self._reported_today(redis)

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        assert len(notifier.calls) == 1
        assert "已恢复" in notifier.calls[0][1]

    @pytest.mark.asyncio
    async def test_a_session_refusal_at_the_gate_is_not_a_failed_order(self):
        """跨 11:35/15:05 的运行：提交时被闸门拒 ⇒ 记「未提交」，不推「下单失败」。

        混进失败清单会推「N 条腿下单失败，请核对 orders 补单」——而去核对的话
        orders 里根本没有这些单（它们压根没出过门）。
        """
        svc = TdxRollingTradeService()
        _arm(svc)
        svc.load_positions_from_tdx = AsyncMock(return_value=([], ""))
        svc.place_rolling_orders = AsyncMock(
            return_value=(
                [],
                [
                    {
                        "symbol": "600519.SH",
                        "side": "buy",
                        "error": "非交易时段（A 股 09:15–11:35 / 12:55–15:05），委托未提交",
                        "session_refused": True,
                    }
                ],
            )
        )
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            result = await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        assert result["failed_orders"] == []
        assert result["session_suppressed"] == 1
        assert "未提交" in result["results"]["execute_skipped"]["error"]
        assert notifier.calls == []

    @pytest.mark.asyncio
    async def test_alerts_go_to_the_account_not_to_whoever_ran_the_inference(
        self, monkeypatch
    ):
        """定时全局任务以 ``user_id="system"`` 触发推理——那不是能收通知的坐标
        （``notifications.user_id`` 有 FK 指向 ``users(user_id)``，插不进去 ⇒
        被判「未送达」⇒ 去重键不写 ⇒ 每次运行重试、每次都失败）。回退到账户坐标。
        """
        monkeypatch.setenv("TDX_ACCOUNT_USER_ID", "00000001")  # 老口径 → 规范名
        svc = TdxRollingTradeService()
        _arm(svc)
        svc.load_positions_from_tdx = AsyncMock(return_value=([], "通达信桥持仓拉取失败"))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            await svc.run_rolling_push(tenant_id="default", user_id="system")

        assert len(notifier.calls) == 1
        assert notifier.calls[0][0] == "10000001"

        # 反向对照：本来就是账户坐标的调用方不被改写（否则这条规则会把所有人的
        # 告警都并到管理员账户上）
        notifier2, redis2 = _SpyNotifier(), _FakeRedis()
        with _patch_stack(mode="tdx", in_session=True, notifier=notifier2, redis=redis2), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            await svc.run_rolling_push(tenant_id="default", user_id="00002002")

        assert notifier2.calls[0][0] == "00002002"

    @pytest.mark.asyncio
    async def test_a_crash_mid_run_is_still_visible(self):
        """运行中途抛异常（DB/数据源）也必须有人看见——此前只有 script_runner 的
        一行 warning，而告警面是「人不在场时唯一会响的东西」。异常照旧上抛。"""
        svc = TdxRollingTradeService()
        svc.load_latest_scores = AsyncMock(side_effect=RuntimeError("db down"))
        fake_tdx, notifier, redis = _pusher(), _SpyNotifier(), _FakeRedis()

        with _patch_stack(mode="tdx", in_session=True, notifier=notifier, redis=redis), \
                patch(f"{ROLLING_MODULE}.tdx_pusher", fake_tdx):
            with pytest.raises(RuntimeError):
                await svc.run_rolling_push(tenant_id="default", user_id="10000001")

        assert len(notifier.calls) == 1
        assert "异常" in notifier.calls[0][1]
        assert "db down" in notifier.calls[0][2]
