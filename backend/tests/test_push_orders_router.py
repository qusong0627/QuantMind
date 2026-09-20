"""候选信号一键推送（``push_orders``）的路由层单测。

边界划在「本模块自己的算术与判据」上：镜像控制面（``status_snapshot``）用假体替换，
因为这里要验的是**配额推算与分组**，不是镜像服务怎么读 Redis。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.services.api.routers import push_orders as po


# --------------------------------------------------------------------------
# _channels_effective：实盘是叠加不是替代
# --------------------------------------------------------------------------


class TestChannelsEffective:
    def test_real_always_rides_on_a_sim_order(self):
        """只勾实盘时实际走两条路：本仓真单只有镜像链，必须先有模拟单。"""
        # Arrange / Act / Assert
        assert po._channels_effective(["real"]) == ["sim", "real"]

    def test_sim_only(self):
        # Arrange / Act / Assert
        assert po._channels_effective(["sim"]) == ["sim"]

    def test_both_collapses_to_the_same_pair(self):
        """``["sim","real"]`` 与 ``["real"]`` 等价 —— 一笔单不可能建两次。"""
        # Arrange / Act / Assert
        assert po._channels_effective(["sim", "real"]) == po._channels_effective(["real"])

    def test_duplicate_real_does_not_duplicate_the_label(self):
        # Arrange / Act / Assert
        assert po._channels_effective(["real", "real"]) == ["sim", "real"]


# --------------------------------------------------------------------------
# _list_blocking：名单/新闻阻断（ack_risk 在调用点解，这里只报事实）
# --------------------------------------------------------------------------


class TestListBlocking:
    def test_clean_leg_is_not_blocked(self):
        # Arrange / Act / Assert
        assert po._list_blocking(None) == (False, "")

    def test_excluded_symbol_blocks_with_reasons(self):
        # Arrange
        risk = {
            "excluded": True,
            "hits": [{"source": "user_manual", "reason": "业绩暴雷"}, {"source": "fin"}],
        }

        # Act
        blocked, why = po._list_blocking(risk)

        # Assert：理由要带出来（用户得知道凭什么），且不能只显示 source
        assert blocked is True
        assert "业绩暴雷" in why

    def test_excluded_without_reason_still_blocks(self):
        # Arrange：机器名单条目可能只有 source 没有 reason
        # Act
        blocked, why = po._list_blocking({"excluded": True, "hits": []})

        # Assert
        assert blocked is True
        assert "名单命中" in why

    def test_news_risk_blocks_and_names_the_tags(self):
        # Arrange
        risk = {
            "excluded": False,
            "news": {"risk": [{"tag": "立案调查"}, {"tag": "财务造假"}]},
        }

        # Act
        blocked, why = po._list_blocking(risk)

        # Assert
        assert blocked is True
        assert "立案调查" in why and "财务造假" in why

    def test_news_warn_only_does_not_block(self):
        """警示档不拦买 —— 只有 risk 档才阻断（否则大半个市场都点不动）。"""
        # Arrange
        risk = {"excluded": False, "news": {"warn": [{"tag": "减持"}]}}

        # Act
        blocked, _why = po._list_blocking(risk)

        # Assert
        assert blocked is False

    def test_positive_news_never_blocks(self):
        # Arrange
        risk = {"excluded": False, "news": {"strong_pos": [{"tag": "业绩预增"}]}}

        # Act / Assert
        assert po._list_blocking(risk)[0] is False


# --------------------------------------------------------------------------
# _list_gate：买入阻断 / 卖出只提示（名单是买入纪律，不能把卖出一起锁死）
# --------------------------------------------------------------------------


class TestListGate:
    _EXCLUDED = {
        "excluded": True,
        "hits": [{"source": "fundamental_flags", "reason": "连续4年亏损（2022-2025）"}],
    }

    def test_buy_hit_blocks(self):
        # Arrange / Act
        blocked, problem, note = po._list_gate("buy", self._EXCLUDED, False)

        # Assert
        assert blocked is True
        assert "连续4年亏损" in problem
        assert note == ""

    def test_buy_hit_with_ack_is_released_without_note(self):
        """勾了「已知悉」就放行 —— 此时不再重复提示（用户已经表过态）。"""
        # Arrange / Act
        blocked, problem, note = po._list_gate("buy", self._EXCLUDED, True)

        # Assert
        assert (blocked, problem, note) == (False, "", "")

    def test_sell_hit_is_never_blocked_and_says_why(self):
        """实测账户持有 27 只、其中两只全在名单上：卖出若也按买入判，卖出推送恒 0 成交。"""
        # Arrange / Act
        blocked, problem, note = po._list_gate("sell", self._EXCLUDED, False)

        # Assert
        assert blocked is False
        assert problem == ""
        assert "连续4年亏损" in note and "卖出不受名单限制" in note

    def test_sell_hit_still_annotated_even_when_acked(self):
        """卖出侧的提示与 ack 无关：ack 是「允许买」，不是「别告诉我它利空」。"""
        # Arrange / Act
        blocked, _problem, note = po._list_gate("sell", self._EXCLUDED, True)

        # Assert
        assert blocked is False
        assert note != ""

    def test_sell_news_risk_is_only_a_note(self):
        # Arrange
        risk = {"excluded": False, "news": {"risk": [{"tag": "立案调查"}]}}

        # Act
        blocked, _problem, note = po._list_gate("sell", risk, False)

        # Assert
        assert blocked is False
        assert "立案调查" in note

    def test_clean_sell_is_silent(self):
        """没命中就不该多出一句话（干净的腿多一段提示 = 噪声）。"""
        # Arrange / Act / Assert
        assert po._list_gate("sell", None, False) == (False, "", "")


# --------------------------------------------------------------------------
# _find_position：四种键形
# --------------------------------------------------------------------------


class TestFindPosition:
    @pytest.mark.parametrize(
        "key", ["600036.SH", "SH600036", "sh600036", "600036"]
    )
    def test_all_key_forms_resolve(self, key):
        """持仓缓存的键形随写入路径而变，四种都要认 —— 认不出会让卖出恒判「无持仓」。"""
        # Arrange
        positions = {key: {"available_volume": 700, "market_value": 28000.0}}

        # Act
        hit = po._find_position(positions, "600036.SH")

        # Assert
        assert hit is not None
        assert hit["available_volume"] == 700

    def test_missing_symbol_returns_none(self):
        # Arrange / Act / Assert
        assert po._find_position({"000001.SZ": {"available_volume": 1}}, "600036.SH") is None

    def test_empty_positions_returns_none(self):
        # Arrange / Act / Assert
        assert po._find_position({}, "600036.SH") is None

    def test_long_short_list_form_picks_first_bucket(self):
        """多空双边持仓是 list 形态，取其中一档而不是当作缺失。"""
        # Arrange
        positions = {"600036.SH": [{"available_volume": 300}]}

        # Act
        hit = po._find_position(positions, "600036.SH")

        # Assert
        assert hit == {"available_volume": 300}


# --------------------------------------------------------------------------
# _mirror_plan：只读配额推算
# --------------------------------------------------------------------------


def _mirror_status(**over) -> dict:
    base = {
        "enabled": True,
        "kill_switch": False,
        "trading_time": True,
        "real_trading_ready": True,
        "blocked_reason": "",
        "broker_selected": "qmt_exec",
        "whitelist": ["default:10000001"],
        "blacklist": [],
        "queue_length": 0,
        "config": {
            "max_order_value": 10000.0,
            "max_daily_value": 50000.0,
            "max_daily_symbols": 5,
            "max_daily_orders": 20,
            "max_slippage_pct": 0.01,
            "max_consecutive_rejects": 3,
            "queue_outside_hours": True,
            "markets": ["CN"],
        },
        "quota": {"date": "2026-09-20", "daily_value": 0.0, "daily_orders": 0, "daily_symbols": 0},
    }
    base.update(over)
    return base


def _legs(n: int, *, amount: float = 1000.0) -> list[dict]:
    return [
        {"symbol": f"60000{i}.SH", "amount": amount, "executable": True}
        for i in range(n)
    ]


class TestMirrorPlan:
    def test_sim_only_does_not_touch_the_mirror(self, monkeypatch):
        # Arrange：即便镜像服务会抛异常，sim-only 也不该去碰它
        def _boom(_redis):
            raise AssertionError("sim-only 不应查询镜像控制面")

        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot", _boom
        )

        # Act
        out = po._mirror_plan(object(), real=False, legs=_legs(2))

        # Assert
        assert out == {"requested": False}

    def test_mirror_unreadable_is_reported_not_faked(self, monkeypatch):
        # Arrange
        def _boom(_redis):
            raise RuntimeError("redis down")

        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot", _boom
        )

        # Act
        out = po._mirror_plan(object(), real=True, legs=_legs(1))

        # Assert：不可读要如实说，不能返回一个「配额充足」的假象
        assert out["requested"] is True
        assert out["available"] is False
        assert "redis down" in out["reason"]

    def test_within_quota_leaves_every_leg_executable(self, monkeypatch):
        # Arrange：5 只、上限 5 只
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: _mirror_status(),
        )
        legs = _legs(5)

        # Act
        out = po._mirror_plan(object(), real=True, legs=legs)

        # Assert
        assert all(x["executable"] for x in legs)
        assert out["quota"]["remaining_symbols"] == 5

    def test_sixth_symbol_is_marked_will_skip(self, monkeypatch):
        """选 6 只推实盘而日上限 5 只 → 第 6 只必须在下单前就标出来。

        镜像侧只会计一次 ``skipped(max_daily_symbols)`` 然后静默丢弃；
        用户若在确认面板上看不到，就会以为 6 只都发出去了。
        """
        # Arrange
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: _mirror_status(),
        )
        legs = _legs(6)

        # Act
        out = po._mirror_plan(object(), real=True, legs=legs)

        # Assert
        assert [x["executable"] for x in legs] == [True] * 5 + [False]
        assert legs[5]["mirror_precheck"] == {
            "will_skip": True,
            "reason": "max_daily_symbols",
        }
        assert "配额" in legs[5]["problem"]
        assert out["quota"]["remaining_symbols"] == 5

    def test_already_consumed_quota_counts_against_the_batch(self, monkeypatch):
        # Arrange：今天已用 4 个标的，本批 3 只 → 第 2 只起超限
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: _mirror_status(
                quota={
                    "date": "2026-09-20",
                    "daily_value": 0.0,
                    "daily_orders": 4,
                    "daily_symbols": 4,
                }
            ),
        )
        legs = _legs(3)

        # Act
        po._mirror_plan(object(), real=True, legs=legs)

        # Assert
        assert [x["executable"] for x in legs] == [True, False, False]

    def test_single_order_value_cap_marks_only_the_oversized_leg(self, monkeypatch):
        # Arrange：单笔上限 1 万，三只里中间那只 2 万
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: _mirror_status(),
        )
        legs = _legs(3)
        legs[1]["amount"] = 20000.0

        # Act
        po._mirror_plan(object(), real=True, legs=legs)

        # Assert
        assert [x["executable"] for x in legs] == [True, False, True]
        assert legs[1]["mirror_precheck"]["reason"] == "max_order_value"

    def test_already_blocked_legs_do_not_consume_quota(self, monkeypatch):
        """被风控/名单阻断的腿不会走到镜像，因此不能占用配额试算。"""
        # Arrange：3 只里第 1 只已被阻断，剩 2 只应在 5 只额度内
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: _mirror_status(),
        )
        legs = _legs(3)
        legs[0]["executable"] = False

        # Act
        po._mirror_plan(object(), real=True, legs=legs)

        # Assert
        assert [x["executable"] for x in legs] == [False, True, True]

    def test_daily_value_cap_stops_the_batch_midway(self, monkeypatch):
        """日累计金额超限：前两只放行、第 3 只起跳过。

        单笔上限必须**调高**（3 万）才能只考日累计那条 —— 若沿用缺省的 1 万，
        每只 2 万会先撞单笔上限，三条腿全挂，测不到日累计。
        """
        # Arrange：日上限 5 万、单笔上限 3 万，每只 2 万
        status = _mirror_status()
        status["config"] = {**status["config"], "max_order_value": 30000.0}
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: status,
        )
        legs = _legs(3, amount=20000.0)

        # Act
        po._mirror_plan(object(), real=True, legs=legs)

        # Assert
        assert [x["executable"] for x in legs] == [True, True, False]
        assert legs[2]["mirror_precheck"]["reason"] == "max_daily_value"

    def test_per_order_cap_beats_daily_cap_in_the_reason(self, monkeypatch):
        """单笔超限优先于日累计报出 —— 否则用户会以为「今天额度用完了」而不是「这只买太多」。"""
        # Arrange：单笔上限 1 万，一笔 2 万
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: _mirror_status(),
        )
        legs = _legs(1, amount=20000.0)

        # Act
        po._mirror_plan(object(), real=True, legs=legs)

        # Assert
        assert legs[0]["mirror_precheck"]["reason"] == "max_order_value"

    def test_kill_switch_is_surfaced_for_the_confirmation_panel(self, monkeypatch):
        # Arrange
        monkeypatch.setattr(
            "backend.services.live_trading.services.real_mirror_service.status_snapshot",
            lambda _r: _mirror_status(kill_switch=True, enabled=False, blocked_reason="kill_switch"),
        )

        # Act
        out = po._mirror_plan(object(), real=True, legs=_legs(1))

        # Assert：急停必须并列展示（前端硬确认要看到这三态）
        assert out["kill_switch"] is True
        assert out["blocked_reason"] == "kill_switch"


# --------------------------------------------------------------------------
# _mirror_receipt：五类回执不合并
# --------------------------------------------------------------------------


class TestMirrorReceipt:
    def test_sim_only_has_no_receipt(self):
        # Arrange / Act / Assert
        assert po._mirror_receipt(SimpleNamespace(mirror=None), real=False) is None

    def test_missing_payload_is_fail_closed(self):
        # Arrange：勾了实盘却没有回执载荷
        # Act
        out = po._mirror_receipt(SimpleNamespace(mirror=None), real=True)

        # Assert：绝不当成功
        assert out["class"] == "failed"
        assert out["status"] == "unknown"

    def test_skipped_is_not_rendered_as_success(self):
        # Arrange
        outcome = SimpleNamespace(
            mirror={"status": "skipped", "reason": "whitelist", "symbol": "600036.SH"}
        )

        # Act
        out = po._mirror_receipt(outcome, real=True)

        # Assert：这是本次实测到的真实阻断（白名单键形），必须原样透出
        assert out["class"] == "skipped"
        assert out["reason"] == "whitelist"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("submitted", "success"),
            ("ok", "success"),
            ("queued", "queued"),
            ("duplicate", "duplicate"),
            ("failed", "failed"),
            ("error", "failed"),
        ],
    )
    def test_each_status_keeps_its_own_class(self, status, expected):
        # Arrange
        outcome = SimpleNamespace(mirror={"status": status, "reason": "r"})

        # Act / Assert
        assert po._mirror_receipt(outcome, real=True)["class"] == expected

    def test_drain_counters_are_passed_through(self):
        # Arrange
        outcome = SimpleNamespace(
            mirror={"status": "ok", "submitted": 3, "failed": 1, "requeued": 2, "dropped": 0}
        )

        # Act
        out = po._mirror_receipt(outcome, real=True)

        # Assert
        assert out["detail"] == {"submitted": 3, "failed": 1, "requeued": 2, "dropped": 0}


# --------------------------------------------------------------------------
# 载荷校验
# --------------------------------------------------------------------------


class TestPushInValidation:
    """非法载荷必须落在 ``ValidationError`` 上（FastAPI 据此回 422）。

    盯的是**异常类型本身**：若哪天把这些校验从模型搬到函数体里、随手抛个
    ``ValueError``，FastAPI 会把它变成 500 而不是 422，前端拿到的就不再是
    「你的入参不合法」而是「服务端崩了」——那正是这里要钉住的差别。
    """

    def test_batch_id_is_required(self):
        """幂等键依赖 batch_id；缺了就没法保证重复点击不重复下单 → 必须 422。"""
        # Arrange / Act / Assert
        with pytest.raises(ValidationError):
            po.PushIn(symbols=["600036.SH"], side="buy", channels=["sim"])

    def test_short_batch_id_rejected(self):
        # Arrange / Act / Assert
        with pytest.raises(ValidationError):
            po.PushIn(symbols=["600036.SH"], side="buy", channels=["sim"], batch_id="abc")

    def test_batch_over_fifty_symbols_rejected(self):
        # Arrange
        symbols = [f"6000{i:02d}.SH" for i in range(po.MAX_BATCH_SYMBOLS + 1)]

        # Act / Assert
        with pytest.raises(ValidationError):
            po.PushIn(symbols=symbols, side="buy", channels=["sim"], batch_id="b" * 10)

    def test_empty_symbols_rejected(self):
        # Arrange / Act / Assert
        with pytest.raises(ValidationError):
            po.PushIn(symbols=[], side="buy", channels=["sim"], batch_id="b" * 10)

    def test_unknown_channel_rejected(self):
        # Arrange / Act / Assert
        with pytest.raises(ValidationError):
            po.PushIn(
                symbols=["600036.SH"], side="buy", channels=["paper"], batch_id="b" * 10
            )

    def test_unknown_side_rejected(self):
        # Arrange / Act / Assert
        with pytest.raises(ValidationError):
            po.PushIn(
                symbols=["600036.SH"], side="hold", channels=["sim"], batch_id="b" * 10
            )

    def test_minimal_valid_payload_defaults(self):
        # Arrange / Act
        body = po.PushIn(
            symbols=["600036.SH"], side="buy", channels=["sim"], batch_id="batch-0001"
        )

        # Assert：默认不 ack、不 dry_run，且 quantities 为 None（走服务端算量）
        assert body.ack_risk is False
        assert body.dry_run is False
        assert body.quantities is None


# --------------------------------------------------------------------------
# 幂等键
# --------------------------------------------------------------------------


class TestCandidateClientOrderId:
    def test_same_batch_symbol_side_is_stable(self):
        # Arrange
        from backend.shared.order_contract import build_candidate_client_order_id as build

        # Act
        a = build("batch-0001", "600036.SH", "buy")
        b = build("batch-0001", "600036.SH", "buy")

        # Assert：同键才能让重复点击命中唯一索引而不重复下单
        assert a == b
        assert a.startswith("cand-")

    def test_different_side_gets_a_different_key(self):
        # Arrange
        from backend.shared.order_contract import build_candidate_client_order_id as build

        # Act / Assert：同批次买卖要能各下一单
        assert build("b" * 10, "600036.SH", "buy") != build("b" * 10, "600036.SH", "sell")

    def test_different_batch_gets_a_different_key(self):
        # Arrange
        from backend.shared.order_contract import build_candidate_client_order_id as build

        # Act / Assert：重新打开面板 = 新批次 = 可以再下一单
        assert build("batch-a", "600036.SH", "buy") != build("batch-b", "600036.SH", "buy")

    def test_key_fits_the_column(self):
        # Arrange：batch_id 用满上限
        from backend.shared.order_contract import (
            MAX_CLIENT_ORDER_ID_LEN,
            build_candidate_client_order_id as build,
        )

        # Act
        key = build("x" * 64, "688981.SH", "sell")

        # Assert
        assert len(key) <= MAX_CLIENT_ORDER_ID_LEN

    def test_empty_batch_id_still_produces_a_key(self):
        # Arrange：Pydantic 已挡 min_length=8，这里是纯函数层的兜底
        from backend.shared.order_contract import build_candidate_client_order_id as build

        # Act
        key = build("", "600036.SH", "buy")

        # Assert
        assert "nobatch" in key
