"""实盘模型对话轮的提示词降级与截断纪律——审计 C5/H13 的回归闸。

跑法（scripts/ 是 bind mount，宿主/容器都可；容器：``docker exec -w /app``）::

    python3 -m pytest scripts/tests/ -q

盯三件事，错了都不会报错、只会让「模型对话」里的结论变假：

* **桥挂 → 无账户模板**：提示词不许要求「逐一点评持仓」，且要逐字禁止编造
  持仓/账户数字；``data_gaps=['account_unreachable']`` 必须随正文一起返回
  （前端横幅与日志标记全靠它，靠文本刮擦不算数）。
* **空仓 ≠ 桥挂**：账户在、仓位空——不是数据缺口（无横幅），但也不许点评持仓。
* **截断轮绝不落盘**：``finish_reason=='length'`` 重试 1 次后仍截断即抛
  ``TruncatedOutputError``，``main`` 不会调 ``append_log``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根

import live_model_analysis as lma  # noqa: E402


def _pos(code: str, cost: float = 10.0, volume: float = 100) -> dict:
    return {
        "code": code,
        "name": "测试",
        "volume": volume,
        "cost_price": cost,
    }


def _ledger(*positions: dict, quota: float = 100_000.0, known: bool = True) -> dict:
    used = round(sum(p["volume"] * p["cost_price"] for p in positions), 2)
    return {
        "quota": quota,
        "used": used,
        "remaining": round(quota - used, 2),
        "known": known,
        "positions": list(positions),
    }


def _build(
    ledger,
    *,
    price: float = 12.0,
    availability: dict[str, int] | None = None,
    mode: str = "intraday",
):
    return lma.build_user_content(
        {},
        ledger=ledger,
        availability={"600036.SH": 100} if availability is None else availability,
        price_reader=lambda env, code: price,
        mode=mode,
    )


class TestDataGapTemplate:
    def test_ledger_down_forbids_position_commentary_and_flags_gap(self):
        """验收（C5）：无账户数据 → 模板无「逐一点评持仓」+ 逐字禁止编造 + 缺口标记。"""
        content, gaps = _build(None)

        assert gaps == ["account_unreachable"]
        assert "逐一点评持仓" not in content  # 要求短语必须消失
        assert "**禁止**点评持仓" in content  # 只有禁止式表达允许存在
        assert "禁止编造" in content
        assert "分账账本不可达" in content

    def test_account_with_positions_keeps_the_normal_template(self):
        content, gaps = _build(_ledger(_pos("600036.SH", cost=10.0)), price=12.0)

        assert gaps == []
        assert "逐一点评持仓" in content
        assert "| 600036.SH | 测试 | 100 | 100 | 10.000 | 12.00 | +20.00% |" in content

    def test_prompt_shows_granted_quota_and_never_the_account_total(self):
        """用户拍板（2026-10-19）：只出现授予额度/已用/剩余，不出现账户总额。"""
        content, gaps = _build(_ledger(_pos("600036.SH", cost=10.0)))

        assert gaps == []
        assert "用户授予 ¥100,000 ｜ 已用（持仓成本） ¥1,000 ｜ 剩余 ¥99,000" in content
        assert "账户总资产、可用资金与其他 agent 的额度不是你的可见范围" in content
        assert content.count("总资产") == 1  # 只出现在上面那句否定里
        for leaked in ("可用资金 20", "持仓市值", "1,015,484", "500,000"):
            assert leaked not in content

    def test_empty_positions_is_not_a_gap_but_still_no_commentary_ask(self):
        """空仓 = 账户数据在（无横幅），但空仓点评无从谈起——模板要换掉。"""
        content, gaps = _build(_ledger())

        assert gaps == []
        assert "逐一点评持仓" not in content
        assert "空仓" in content
        assert "禁止编造" in content

    def test_unknown_agent_renders_empty_ledger_not_a_gap(self):
        """账本里查无此 agent = 名下没有货（ok），不是读失败（缺口）。"""
        content, gaps = _build(_ledger(known=False))

        assert gaps == []
        assert "分账账本里暂无你的成交记录" in content

    def test_missing_availability_renders_dash_not_volume(self):
        """桥不可达时可卖量显示 —，绝不拿账本持仓量冒充（会把 T+1 锁仓说成可卖）。"""
        content, _ = _build(_ledger(_pos("600036.SH", volume=200)), availability={})
        assert "| 600036.SH | 测试 | 200 | — | 10.000 | 12.00 | +20.00% |" in content

    def test_bridge_availability_is_clamped_to_the_agents_own_volume(self):
        """桥的可卖量是真实账户整只的量（别家也持有），不得大于本账持仓。

        2026-10-19 实测：本 agent 持中国石油 700 股，桥报可用 1500（共用账户）。
        照抄会让模型以为自己能卖 1500 股。
        """
        content, _ = _build(
            _ledger(_pos("600036.SH", volume=700)),
            availability={"600036.SH": 1500},
        )
        assert "| 600036.SH | 测试 | 700 | 700 | 10.000 | 12.00 | +20.00% |" in content

    def test_bridge_zero_availability_still_renders_zero(self):
        """T+1 锁仓是真实约束：桥说 0 就显示 0，不能被夹取逻辑吞掉。"""
        content, _ = _build(
            _ledger(_pos("600036.SH", volume=700)),
            availability={"600036.SH": 0},
        )
        assert "| 600036.SH | 测试 | 700 | 0 | 10.000 | 12.00 | +20.00% |" in content

    def test_news_block_absence_does_not_change_the_gap_list(self, monkeypatch):
        monkeypatch.setattr(lma, "_news_block", lambda: "")  # 不读真实 latest.json

        content, gaps = _build(None)

        assert gaps == ["account_unreachable"]  # 新闻缺失不是账户缺口（本项范围外）
        assert "【当日新闻分子】" not in content


class TestLedgerRead:
    """分账账本是 agent 可见面的唯一账户口径：脏行跳过、失败即降级、不含总额。"""

    def test_maps_rows_and_computes_grant_usage(self):
        view = lma._ledger_view(
            ["a", "b"],
            [("a", "600036.SH", 100, 10.0, "招商银行")],
            [("b",)],
            100_000.0,
        )

        assert view["a"]["used"] == 1000.0
        assert view["a"]["remaining"] == 99_000.0
        assert view["a"]["positions"] == [
            {
                "code": "600036.SH",
                "name": "招商银行",
                "volume": 100.0,
                "cost_price": 10.0,
            }
        ]
        assert view["a"]["known"] is True
        assert view["b"]["known"] is True and view["b"]["positions"] == []
        # 视图只带额度与持仓：账户总额三数一个都不许出现（用户拍板 2026-10-19）
        assert set(view["a"]) == {"quota", "used", "remaining", "known", "positions"}

    def test_dirty_rows_are_skipped_not_turned_into_nan(self):
        view = lma._ledger_view(
            ["a"],
            [
                ("a", "600036.SH", None, 10.0, ""),
                ("a", "600036.SH", "oops", 10.0, ""),
                ("a", "600036.SH", 0, 10.0, ""),
                ("a", "600036.SH", -100, 10.0, ""),
                ("a", "600036.SH", float("nan"), 10.0, ""),
                ("a", "600036.SH", float("inf"), 10.0, ""),
                ("a", "600036.SH", 100, float("nan"), ""),
                ("a", "600036.SH", 100, 10.0, "招商银行"),
            ],
            [],
            100_000.0,
        )

        assert view["a"]["used"] == 1000.0  # 只有最后一行是干净的
        assert len(view["a"]["positions"]) == 1
        assert view["a"]["remaining"] == 99_000.0

    def test_read_failure_returns_none_for_every_agent(self, monkeypatch):
        """读失败 = 降级模板；**绝不回退到桥账户**（回退就是泄漏账户总额的路）。"""

        def boom(env):
            raise RuntimeError("db down")

        monkeypatch.setattr(lma, "_pg_connect", boom)
        monkeypatch.setattr(
            lma, "_bridge_account", lambda env: pytest.fail("不许回退到桥账户")
        )

        assert lma.read_agent_ledgers({}, ["a", "b"]) == {"a": None, "b": None}

    def test_no_agents_means_no_database_roundtrip(self, monkeypatch):
        monkeypatch.setattr(lma, "_pg_connect", lambda env: pytest.fail("不该连库"))
        assert lma.read_agent_ledgers({}, []) == {}

    def test_quota_comes_from_trading_policy_per_agent(self, monkeypatch):
        """额度逐家取 ``TradingPolicy``（本脚本曾是阶段 1 改造漏网）。

        2026-10-30 实测：决策轮提示词 ¥200,000，同一天 15:30 复盘轮仍写
        「用户授予 ¥100,000」——模型拿半额额度做次日计划。
        """
        import backend.shared.trading_policy as tp
        from backend.shared.trading_policy import AgentPolicy, TradingPolicy

        policy = TradingPolicy(
            default=AgentPolicy(quota=100_000.0),
            agents={
                "a": AgentPolicy(quota=200_000.0),
                "b": AgentPolicy(quota=150_000.0),
            },
        )
        monkeypatch.setattr(tp, "load_trading_policy", lambda: policy)

        assert lma._policy_quotas(["a", "b", "c"]) == {
            "a": 200_000.0,
            "b": 150_000.0,
            "c": 100_000.0,
        }

    def test_policy_failure_degrades_instead_of_using_a_constant(self, monkeypatch):
        """策略文件坏 → 本轮无账户上下文；**不许**回退成某个固定额度。"""
        import backend.shared.trading_policy as tp

        def boom():
            raise tp.TradingPolicyError("bad file")

        monkeypatch.setattr(tp, "load_trading_policy", boom)
        monkeypatch.setattr(
            lma, "_pg_connect", lambda env: pytest.fail("策略坏时不该读库")
        )

        assert lma.read_agent_ledgers({}, ["a"]) == {"a": None}

    def test_ledger_view_accepts_per_agent_quota_map(self):
        view = lma._ledger_view(
            ["a", "b"],
            [("a", "600036.SH", 100, 10.0, "招商银行")],
            [],
            {"a": 200_000.0, "b": 100_000.0},
        )

        assert view["a"]["quota"] == 200_000.0
        assert view["a"]["remaining"] == 199_000.0
        assert view["b"]["quota"] == 100_000.0


class TestBridgeAvailability:
    def test_extracts_only_code_and_available_volume_never_totals(self, monkeypatch):
        monkeypatch.setattr(
            lma,
            "_bridge_account",
            lambda env: {
                "asset": {
                    "asset": 1_015_484,
                    "cash": 20_000,
                    "market_value": 995_484,
                },
                "positions": [
                    {"stock_code": "600036.SH", "available_volume": "100"},
                    {"stock_code": "601000.SH", "available_volume": None},
                ],
            },
        )

        assert lma._bridge_availability({}) == {"600036.SH": 100}

    def test_bridge_down_means_empty_map(self, monkeypatch):
        monkeypatch.setattr(lma, "_bridge_account", lambda env: None)
        assert lma._bridge_availability({}) == {}


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _resp(finish: str, content: str = "分析正文") -> _FakeResp:
    return _FakeResp(
        {
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


ENV = {"OPENAI_API_BASE": "http://fake", "OPENAI_API_KEY": "k"}
SIG = "deepseek-v4-flash"


def _patch_post(monkeypatch, responses: list[_FakeResp]) -> list[str]:
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr("requests.post", fake_post)
    return calls


class TestCallModelTruncation:
    def test_length_finish_retries_once_then_raises_with_content(self, monkeypatch):
        """验收（H13）：截断 → 重试 1 次；仍截断 → 抛错（调用方据此拒落盘）。"""
        calls = _patch_post(monkeypatch, [_resp("length"), _resp("length")])

        with pytest.raises(lma.TruncatedOutputError) as ei:
            lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")

        assert len(calls) == 2  # 恰好重试一次，不是三次五次
        assert ei.value.content == "分析正文"  # 原文随异常携带，供人工审计
        assert ei.value.usage == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }

    def test_truncated_then_complete_retry_recovers(self, monkeypatch):
        calls = _patch_post(monkeypatch, [_resp("length"), _resp("stop", "完整分析")])

        content, usage = lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")

        assert content == "完整分析"
        assert len(calls) == 2
        assert usage["total_tokens"] == 15

    def test_truncated_reasoning_fallback_is_not_salvaged(self, monkeypatch):
        """reasoning 兜底捞出的思考草稿若被截断，必须同样作废（2026-09-08 实录教训）。"""
        truncated = _FakeResp(
            {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "思考到一半…"},
                        "finish_reason": "length",
                    }
                ]
            }
        )
        _patch_post(monkeypatch, [truncated, truncated])

        with pytest.raises(lma.TruncatedOutputError):
            lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")

    def test_empty_content_with_stop_finish_is_empty_reply(self, monkeypatch):
        _patch_post(monkeypatch, [_resp("stop", ""), _resp("stop", "")])

        with pytest.raises(RuntimeError, match="空回复"):
            lma.call_model(ENV, SIG, "deepseek-v4-flash", "prompt")


class TestAppendLogGaps:
    def test_gaps_field_lands_in_the_jsonl_entry(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lma, "LOG_DIR", tmp_path)

        path = lma.append_log("提示词", "正文", SIG, None, ["account_unreachable"])

        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["data_gaps"] == ["account_unreachable"]
        assert [m["role"] for m in entry["new_messages"]] == ["user", "assistant"]

    def test_no_gaps_means_no_field(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lma, "LOG_DIR", tmp_path)

        path = lma.append_log("提示词", "正文", SIG, None, [])

        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert "data_gaps" not in entry  # 无缺口不落字段（前端横幅判据=字段存在）


class TestMainRejectsTruncatedRound:
    def test_truncated_round_is_never_written(self, monkeypatch, tmp_path):
        """验收：截断 → main 报失败且 append_log 一次都不被调用（拒落盘）。"""
        monkeypatch.setattr(lma, "LOCK", tmp_path / "lock")
        monkeypatch.setattr(lma, "_load_env", lambda: {})
        monkeypatch.setattr(lma, "build_user_content", lambda env, **kw: ("提示词", []))
        monkeypatch.setattr(lma, "ALERT_STATE_FILE", tmp_path / ".alert_state.json")
        # 账本/桥是外部依赖：测试绝不连真 PG/桥
        monkeypatch.setattr(
            lma, "read_agent_ledgers", lambda e, agents: dict.fromkeys(agents)
        )
        monkeypatch.setattr(lma, "_bridge_availability", lambda e: {})
        written: list = []
        monkeypatch.setattr(lma, "append_log", lambda *a, **kw: written.append(a))

        def boom(env, sig, model, user_content):
            raise lma.TruncatedOutputError("半截", None)

        monkeypatch.setattr(lma, "call_model", boom)
        monkeypatch.setattr(sys, "argv", ["live_model_analysis.py", "--agents", SIG])

        assert lma.main() == 2  # 全部失败
        assert written == []
