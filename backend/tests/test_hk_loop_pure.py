"""港股分析循环 + 候选池脚本的纯函数/编排测试（2026-10-08 富途通道恢复，P4）。

两个宿主脚本（``scripts/hk_picks.py`` / ``scripts/live_hourly_analysis_hk.py``）
不进容器、只能靠这里兜住；钉的是**钱与口径**，不是覆盖率：

1. **取价与现金闸**：买单手数按限价成本折算（港股 buffer 0.5%）——
   按现价折会把单笔预算与账户现金闸双双顶穿（旧栈实测漂移过的坑）；
2. **卖出的 pct 三态**：缺比例=清仓、脏值=停手留痕、`{}`/全未知 action = 解析失败
   （「模型让卖、系统静默不卖」是历史事故）；
3. **fail-closed**：取不到价的单子不报出去；`configs/hk_exec.json` 不存在 ⇒ 永不执行；
4. **港股隔离**：交易日志文件名 `live_trade_hk_*` + `mode=execute_hk` 双保险，
   通知一律走 ``channel="hk"``；
5. **时段**：北京 09:30–12:00 / 13:00–16:00 的边界逐分钟钉住。

纪律：本文件的 QQ 外发**全部**换成记录器（autouse fixture），测试永不真发；
所有落盘路径（日志/候选池/交易日志）一律改写到 tmp_path，不碰仓库 data/。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _p in (str(SCRIPTS), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hk_picks  # noqa: E402
import live_hourly_analysis_hk as hk  # noqa: E402
from backend.shared.decision.contract import (  # noqa: E402
    SCHEMA_INTRADAY,
    STATUS_PARSE_FAILED,
    parse_decisions,
)

CN = timezone(timedelta(hours=8))


def _cn(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=CN)


def _decisions(text: str):
    batch = parse_decisions(text, schema=SCHEMA_INTRADAY)
    assert batch.ok, f"测试夹具必须是合法决策块，实际 {batch.status}"
    return list(batch.decisions)


def _bars(closes: list[float], start: str = "2026-01-01") -> list[dict]:
    base = datetime.strptime(start, "%Y-%m-%d")
    return [
        {"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "close": float(c)}
        for i, c in enumerate(closes)
    ]


# ---------------------------------------------------------------- 公共夹具


@pytest.fixture(autouse=True)
def no_real_qq(monkeypatch):
    """QQ 外发钉进记录器：宿主脚本用的是 scripts/push_notify（真实凭据在 keeper.env）。

    拦在 ``push_notify.notify`` 这一层而不是 ``hk.notify_hk``——真实的 notify_hk
    （通道选择 + 失败吞掉）本身也要被测，替身放低了会把被测量一起替掉。
    """
    import push_notify

    sent: list[tuple[str, str, str]] = []

    def fake_notify(title: str, content: str, channel: str = "default") -> None:
        sent.append((title, content, channel))

    monkeypatch.setattr(push_notify, "notify", fake_notify)
    return sent


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch, tmp_path):
    """落盘全部搬到 tmp：日志目录 / 交易日志 / 候选池 / 执行开关锁文件。"""
    monkeypatch.setattr(hk, "DATA_DIR", tmp_path / "agent_data_hk", raising=False)
    monkeypatch.setattr(hk, "TRADE_LOG_DIR", tmp_path / "logs", raising=False)
    monkeypatch.setattr(hk, "POOL_FILE", tmp_path / "hk_picks.json", raising=False)
    monkeypatch.setattr(hk, "EXEC_CONFIG", tmp_path / "hk_exec.json", raising=False)
    return tmp_path


# ---------------------------------------------------------------- 候选池（hk_picks）


class TestRanking:
    def test_ties_take_average_rank(self):
        assert hk_picks._ranks([1.0, 2.0, 2.0, 3.0]) == [0.0, 0.5, 0.5, 1.0]

    def test_single_and_empty(self):
        assert hk_picks._ranks([7.0]) == [0.5]
        assert hk_picks._ranks([]) == []

    def test_score_orders_by_momentum_and_drops_short_history(self):
        bars_map = {
            "UP.HK": _bars([100 + i for i in range(80)]),   # 单边上涨
            "FLAT.HK": _bars([100.0] * 80),                 # 完全横盘（波动 0）
            "DOWN.HK": _bars([200 - i for i in range(80)]),  # 单边下跌
            "SHORT.HK": _bars([100 + i for i in range(64)]),  # 差一根，剔除
        }
        stats = hk_picks.score_universe(bars_map)
        codes = [s["code"] for s in stats]
        assert "SHORT.HK" not in codes, "样本 <65 根的标的必须剔除，不参与分位"
        assert codes == ["UP.HK", "FLAT.HK", "DOWN.HK"], "动量高的排前（降序）"
        assert stats[0]["score"] > stats[-1]["score"]
        assert stats[1]["vol20"] == pytest.approx(0.0, abs=1e-9), "横盘波动=0（反向计分拿最高分位）"

    def test_market_direction_thresholds(self):
        assert "bullish" in hk_picks.market_direction([{"mom20": 1.01}])
        assert "中性" in hk_picks.market_direction([{"mom20": 1.0}])
        assert "bearish" in hk_picks.market_direction([{"mom20": -1.01}])
        assert "中性" in hk_picks.market_direction([{"mom20": -1.0}])
        assert "样本不足" in hk_picks.market_direction([])


class TestKlineParsing:
    def test_qfq_preferred_and_short_rows_skipped(self):
        payload = {
            "code": 0,
            "data": {"hk00700": {
                "qfqday": [["2026-10-06", "380", "385", "388", "379", "1200"],
                           ["2026-10-07", "386"],  # 字段不全 → 丢
                           ["2026-10-08", "386", "390", "392", "385", "900"]],
                "day": [["2026-10-08", "1", "1", "1", "1", "1"]],
            }},
        }
        bars = hk_picks.parse_kline_payload(payload, "00700.HK")
        assert [b["date"] for b in bars] == ["2026-10-06", "2026-10-08"]
        assert bars[-1]["close"] == "390", "腾讯原始值是字符串，转换在 score_universe 里做"

    def test_day_fallback_and_error_code(self):
        payload = {"code": 0, "data": {"hk00700": {"day": [["2026-10-08", "1", "2", "3", "4", "5"]]}}}
        assert len(hk_picks.parse_kline_payload(payload, "00700.HK")) == 1
        assert hk_picks.parse_kline_payload({"code": 1}, "00700.HK") == []
        assert hk_picks.parse_kline_payload("boom", "00700.HK") == []


class TestFreshness:
    def test_stale_days_skips_weekends(self):
        # 2026-10-01 周四 → 2026-10-05 周一：跨 10-02(五)/10-05(一) = 2 个工作日
        assert hk_picks.stale_days("2026-10-01", date(2026, 10, 5)) == 2
        assert hk_picks.stale_days("2026-10-05", date(2026, 10, 5)) == 0
        assert hk_picks.stale_days("2026-10-09", date(2026, 10, 5)) == 0, "未来日期不倒扣"
        assert hk_picks.stale_days("", date(2026, 10, 5)) == 0
        assert hk_picks.stale_days("坏值", date(2026, 10, 5)) == 0

    def test_build_doc_shape_and_blank_name_fallback(self):
        stats = [{"code": "00700.HK", "last_close": 385.0, "mom20": 3.0,
                  "mom60": 5.0, "trend": 1.0, "vol20": 1.5, "score": 88.0},
                 {"code": "09999.HK", "last_close": 200.0, "mom20": 1.0,
                  "mom60": 2.0, "trend": 0.5, "vol20": 2.0, "score": 70.0}]
        doc = hk_picks.build_doc(
            stats, top=20, names={"00700.HK": "腾讯控股"},
            latest_bar="2026-10-08", today=date(2026, 10, 8),
            generated_at="2026-10-08T09:20:00+08:00",
        )
        assert doc["date"] == "2026-10-08" and doc["data_date"] == "2026-10-08"
        assert doc["stale_days"] == 0 and doc["universe_size"] == 2
        assert doc["picks"][0]["name"] == "腾讯控股"
        assert doc["picks"][1]["name"] == "", "名称表没有的标的留空，不回落成代码"

    def test_build_doc_respects_top(self):
        stats = [{"code": f"{i:05d}.HK", "last_close": 1.0, "mom20": 0.0, "mom60": 0.0,
                  "trend": 0.0, "vol20": 0.0, "score": float(100 - i)} for i in range(30)]
        doc = hk_picks.build_doc(stats, top=7, names={}, latest_bar="2026-10-08",
                                 today=date(2026, 10, 8), generated_at="x")
        assert len(doc["picks"]) == 7


class TestBarFreshness:
    """单只 K 线自身的陈旧度：停牌/退市标的必须出池（2026-10-08 实盘发现）。

    恒生银行（00011.HK，HSBC 私有化退市）最后一个交易日停在 2026-01-14，
    但根数 90 ≥ MIN_BARS，旧口径下照样参与分位并冲到第 3 名——池级
    ``stale_days`` 取的是**全池最新**一根，被活的 29 只盖住，拦不住它。
    """

    @staticmethod
    def _bars_ending(closes_start: float, n: int, last: str) -> list[dict]:
        """以 ``last`` 收尾的连续日历日 K（周末不建模，口径同 _bars）。"""
        end = datetime.strptime(last, "%Y-%m-%d")
        return _bars([closes_start + i for i in range(n)],
                     start=(end - timedelta(days=n - 1)).strftime("%Y-%m-%d"))

    def test_stale_symbols_flags_a_stopped_ticker(self):
        bars_map = {
            "FRESH.HK": self._bars_ending(100.0, 80, "2026-10-08"),
            "DEAD.HK": self._bars_ending(100.0, 90, "2026-01-14"),
        }
        out = hk_picks.stale_symbols(bars_map)
        assert [e["code"] for e in out] == ["DEAD.HK"], "只报落后全池最新交易日的标的"
        assert out[0]["last_bar"] == "2026-01-14"
        assert out[0]["lag_days"] > hk_picks.MAX_BAR_LAG_WEEKDAYS

    def test_score_universe_drops_the_stale_one(self):
        bars_map = {
            "UP.HK": self._bars_ending(100.0, 80, "2026-10-08"),
            "FLAT.HK": [{"date": b["date"], "close": 100.0}
                        for b in self._bars_ending(1.0, 80, "2026-10-08")],
            "DEAD.HK": self._bars_ending(100.0, 90, "2026-01-14"),  # 停牌近半年
        }
        stats = hk_picks.score_universe(bars_map)
        assert [s["code"] for s in stats] == ["UP.HK", "FLAT.HK"], "退市股不进池、不参与分位"
        assert hk_picks.score_universe(bars_map) == stats, "纯函数：同输入同输出"

    def test_small_data_gap_is_tolerated(self):
        """差 2 个工作日（漏抓/临时停牌一日）仍在池内——阈值是 >3 个工作日。"""
        bars_map = {
            "A.HK": self._bars_ending(100.0, 80, "2026-08-21"),  # 周五
            "B.HK": self._bars_ending(100.0, 80, "2026-08-19"),  # 周三
        }
        assert hk_picks.stale_symbols(bars_map) == []
        assert len(hk_picks.score_universe(bars_map)) == 2

    def test_lag_threshold_boundary(self):
        """恰好 3 个工作日留下，第 4 个出池（边界钉死，别让改动悄悄挪阈值）。"""
        pool = {"A.HK": self._bars_ending(100.0, 80, "2026-08-21")}  # 周五，全池最新
        for last, expected in (("2026-08-18", []), ("2026-08-17", ["B.HK"])):
            lag3 = {**pool, "B.HK": self._bars_ending(100.0, 80, last)}
            assert [e["code"] for e in hk_picks.stale_symbols(lag3)] == expected, last
        assert hk_picks.MAX_BAR_LAG_WEEKDAYS == 3

    def test_short_history_is_not_reported_as_stale(self):
        """根数不足是另一条剔除线（MIN_BARS），别混进 excluded_stale 的台账。"""
        bars_map = {
            "FRESH.HK": self._bars_ending(100.0, 80, "2026-10-08"),
            "NEW.HK": self._bars_ending(100.0, 20, "2026-10-08"),
        }
        assert hk_picks.stale_symbols(bars_map) == []

    def test_all_stale_together_is_not_a_lag(self):
        """整池数据同一天停（长假后首次抓取）→ 谁都不算落后（池级 stale_days 管这个）。"""
        bars_map = {"A.HK": self._bars_ending(100.0, 80, "2026-09-30"),
                    "B.HK": self._bars_ending(100.0, 80, "2026-09-30")}
        assert hk_picks.stale_symbols(bars_map) == []
        assert len(hk_picks.score_universe(bars_map)) == 2

    def test_build_doc_records_excluded_stale(self):
        stats = [{"code": "00700.HK", "last_close": 385.0, "mom20": 3.0, "mom60": 5.0,
                  "trend": 1.0, "vol20": 1.5, "score": 88.0}]
        excluded = [{"code": "00011.HK", "last_bar": "2026-01-14", "lag_days": 185}]
        doc = hk_picks.build_doc(stats, top=20, names={}, latest_bar="2026-10-08",
                                 today=date(2026, 10, 8), generated_at="x",
                                 excluded_stale=excluded)
        assert doc["excluded_stale"] == excluded, "剔除台账要落盘，生产上要能复盘为什么少了某只"
        doc_default = hk_picks.build_doc(stats, top=20, names={}, latest_bar="2026-10-08",
                                         today=date(2026, 10, 8), generated_at="x")
        assert doc_default["excluded_stale"] == [], "不传就是空台账，不是缺键"


# ---------------------------------------------------------------- 时段 / 环境


class TestTradingWindow:
    @pytest.mark.parametrize("hh,mm,expected", [
        (9, 29, False), (9, 30, True), (11, 59, True), (12, 0, True), (12, 1, False),
        (12, 59, False), (13, 0, True), (15, 59, True), (16, 0, True), (16, 1, False),
    ])
    def test_boundaries(self, hh, mm, expected):
        assert hk.in_trading_window(_cn(2026, 10, 8, hh, mm)) is expected

    def test_weekend_closed(self):
        assert hk.in_trading_window(_cn(2026, 10, 10, 10, 0)) is False  # 周六
        assert hk.in_trading_window(_cn(2026, 10, 11, 14, 0)) is False  # 周日


class TestEnvLoading:
    def test_load_env_is_lazy_and_does_not_mutate_environ(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text(
            "# 注释\nOPENAI_API_BASE=https://example.invalid/v1\nGLM_API_KEY=\"quoted\"\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(hk, "ROOT", tmp_path, raising=False)
        before = dict(os.environ)
        env = hk._load_env()
        assert env["OPENAI_API_BASE"] == "https://example.invalid/v1"
        assert env["GLM_API_KEY"] == "quoted"
        assert dict(os.environ) == before, "读 .env 不得写回进程环境（kicker 靠 _env_get 兜底）"

    def test_env_get_falls_back_to_process_env(self, monkeypatch):
        monkeypatch.setenv("HK_TEST_KEY", "from-process")
        assert hk._env_get({"HK_TEST_KEY": "from-file"}, "HK_TEST_KEY") == "from-file"
        assert hk._env_get({}, "HK_TEST_KEY") == "from-process"
        assert hk._env_get({}, "HK_TEST_MISSING") == ""


# ---------------------------------------------------------------- 提示词 / 持仓行


class TestPrompt:
    SIM = {
        "total_asset": 250000.0, "cash": 80000.0,
        "positions": {
            "00700.HK": {"volume": 200, "cost": 350.0, "price": 380.0, "name": "腾讯控股"},
            "00001.HK": {"volume": 0, "cost": 60.0, "price": 60.0, "name": "长和"},
        },
    }

    def test_build_rows_snapshot_price_wins_and_zero_volume_dropped(self):
        rows = hk.build_rows(
            self.SIM,
            {"00700.HK": {"last_price": 385.5, "day_chg": 1.2}},
        )
        assert [r["code"] for r in rows] == ["00700.HK"], "0 股持仓行不进行"
        r = rows[0]
        assert r["price"] == 385.5 and r["day_chg"] == 1.2
        assert r["pnl"] == round((385.5 - 350.0) * 200, 2)
        assert r["pnl_pct"] == round((385.5 - 350.0) / 350.0 * 100, 2)

    def test_build_rows_without_snapshot_marks_day_chg_unknown(self):
        rows = hk.build_rows(self.SIM, {})
        assert rows[0]["price"] == 380.0 and rows[0]["day_chg"] is None

    def test_user_content_tables_and_decision_contract(self):
        rows = hk.build_rows(self.SIM, {"00700.HK": {"last_price": 385.5, "day_chg": 1.2}})
        pool = {"date": "2026-10-08", "market_direction": "bullish（权重股动量偏多）",
                "picks": [{"code": "09988.HK", "name": "阿里巴巴-W", "last_close": 120.0,
                           "mom20": 4.2, "mom60": 6.1, "score": 91.0}]}
        news = [{"title": "腾讯回购", "source": "财联社", "time": "10-08 10:00",
                 "sentiment": "bullish", "keyword": "腾讯"}]
        text = hk.build_user_content(
            rows, 250000.0, 80000.0, "deepseek-v4-flash", pool,
            news=news, notes=["候选池数据日 2026-10-06（陈旧 2 个交易日，分位按该日计）"],
        )
        assert "00700.HK" in text and "当前持仓" in text
        assert "09988.HK" in text and "bullish（权重股动量偏多）" in text
        assert "数据提示：候选池数据日 2026-10-06" in text
        assert "[利好] 腾讯回购" in text
        assert '"decisions"' not in text, "默认提示词不提 JSON 契约（对话轮只出观点）"

        with_json = hk.build_user_content(
            rows, 250000.0, 80000.0, "deepseek-v4-flash", {}, ask_decisions=True,
        )
        assert '"action": "hold|sell|buy|watch"' in with_json
        assert "只列你**现在就要执行**的动作" in with_json

    def test_system_prompt_carries_hk_rules(self):
        text = hk.system_prompt_for("deepseek-v4-flash", {"name": "基线模式", "prompt": "逐只简评"})
        assert "港股" in text and "T+0" in text and "基线模式" in text


# ---------------------------------------------------------------- 决策 → 计划（钱的口径）


class TestPlanHkOrders:
    HOLDINGS = {"00700.HK": 500}
    PRICES = {"00700.HK": 100.0}

    def test_sell_missing_pct_clears_position_at_sell_limit(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"sell","code":"HK.700","reason":"止损"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=0.0,
        )
        assert blocks == []
        assert orders == [{"code": "00700.HK", "side": "sell", "volume": 500,
                           "price": 99.0, "reason": "止损"}], "缺比例=清仓；HK.700 先归一到 00700.HK"

    def test_sell_partial_pct_rounds_down_to_lot(self):
        orders, _ = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"sell","code":"00700.HK","pct":0.3,"reason":"减"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=0.0,
        )
        assert orders[0]["volume"] == 100, "500×0.3=150 → 整手向下取 100"

    def test_sell_dirty_pct_blocks_and_keeps_evidence(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"sell","code":"00700.HK","pct":"三分之一","reason":"减"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=0.0,
        )
        assert orders == []
        assert blocks[0]["rule"] == "pct_dirty" and "停手留痕" in blocks[0]["reason"]

    def test_sell_not_held_and_below_lot(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"sell","code":"09988.HK","pct":1.0,"reason":"卖"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=0.0,
        )
        assert orders == [] and blocks[0]["rule"] == "sell_not_held"

        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"sell","code":"00700.HK","pct":0.1,"reason":"减"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=0.0,
        )
        assert orders == [] and blocks[0]["rule"] == "below_min_lot"

    def test_sell_without_quote_fails_closed(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"sell","code":"00700.HK","reason":"止损"}]}'),
            holdings=self.HOLDINGS, prices={}, cash=0.0,
        )
        assert orders == [] and blocks[0]["rule"] == "no_quote"

    def test_buy_new_position_blocked_in_v1(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"buy","code":"09988.HK","pct":0.2,"reason":"建仓"}]}'),
            holdings=self.HOLDINGS, prices={"09988.HK": 120.0}, cash=1_000_000.0,
        )
        assert orders == [] and blocks[0]["rule"] == "not_held_v1"

    def test_buy_without_pct_does_not_move(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"buy","code":"00700.HK","reason":"加"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=1_000_000.0,
        )
        assert orders == [] and blocks[0]["rule"] == "pct_missing"

    def test_buy_lots_by_limit_cost_and_caps_at_20pct(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"buy","code":"00700.HK","pct":1.0,"reason":"满仓加"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=100_000.0,
        )
        assert blocks == []
        # 单笔 ≤ 现金 20%（20000）× 限价 100.5 → 一手成本 10050 → 1 手
        assert orders == [{"code": "00700.HK", "side": "buy", "volume": 100,
                           "price": 100.5, "reason": "满仓加"}]
        assert 100 * 100.5 <= 100_000.0 * 0.2 + 0.5, "预算按限价口径不得顶穿 20% 上限"

    def test_buy_equity_below_one_lot_blocks(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions('{"decisions":[{"action":"buy","code":"00700.HK","pct":1.0,"reason":"加"}]}'),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=20_000.0,
        )
        assert orders == [] and blocks[0]["rule"] == "unaffordable"

    def test_second_buy_sees_cash_after_first(self):
        """同轮两笔买单按序扣减现金：首笔花掉 10050 后，二笔预算按 49950 计 → 不足一手。

        若实现忘了扣减（avail 恒为 60000），二笔会算出 12000 预算再买一手——
        这笔断言就是那条闸门的探测器。
        """
        orders, blocks = hk.plan_hk_orders(
            _decisions(
                '{"decisions":['
                '{"action":"buy","code":"00700.HK","pct":1.0,"reason":"一"},'
                '{"action":"buy","code":"00700.HK","pct":1.0,"reason":"二"}]}'
            ),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=60_000.0,
        )
        assert len(orders) == 1 and orders[0]["volume"] == 100
        assert blocks and blocks[0]["rule"] == "unaffordable"

    def test_hold_and_watch_are_not_orders(self):
        orders, blocks = hk.plan_hk_orders(
            _decisions(
                '{"decisions":['
                '{"action":"hold","code":"00700.HK","reason":"持有"},'
                '{"action":"watch","code":"00700.HK","stop_loss":95.0,"reason":"守护"}]}'
            ),
            holdings=self.HOLDINGS, prices=self.PRICES, cash=100_000.0,
        )
        assert orders == [] and blocks == [], "hold/watch 不是执行面动作（watch 由守护单链承载）"


class TestDecisionParsing:
    def test_prose_with_json_is_parsed(self):
        batch = hk.parse_agent_decisions(
            "腾讯仓位偏重。\n"
            '{"decisions":[{"action":"sell","code":"HK.700","pct":0.3,"reason":"止盈"}]}\n'
            "以上。"
        )
        assert batch.ok and batch.decisions[0].code == "HK.700"

    def test_empty_decisions_is_not_success(self):
        batch = hk.parse_agent_decisions('{"decisions":[]}')
        assert batch.status == STATUS_PARSE_FAILED and batch.failed


# ---------------------------------------------------------------- 执行（默认关）


class TestExecution:
    ORDERS = [{"code": "00700.HK", "side": "buy", "volume": 100, "price": 100.5, "reason": "加仓"}]

    def test_exec_disabled_when_config_missing(self):
        assert hk.exec_enabled() is False, "配置文件不存在 = 永不执行（fail-closed）"

    def test_exec_enabled_reads_flag(self, tmp_path):
        (tmp_path / "hk_exec.json").write_text('{"enabled": true}', encoding="utf-8")
        assert hk.exec_enabled() is True
        (tmp_path / "hk_exec.json").write_text("{坏 json", encoding="utf-8")
        assert hk.exec_enabled() is False

    def test_dry_run_writes_trade_log_and_never_calls_api(self, monkeypatch):
        def boom(*a, **kw):  # noqa: ANN001
            raise AssertionError("dry-run 不得发起任何下单请求")

        monkeypatch.setattr(hk, "_api_post", boom, raising=False)
        done, errors = hk.execute_plan("deepseek-v4-flash", self.ORDERS, dry_run=True)
        assert errors == [] and done[0]["dry_run"] is True

        files = list((hk.TRADE_LOG_DIR).glob("live_trade_hk_*.jsonl"))
        assert len(files) == 1, "交易日志文件名必须是 live_trade_hk_ 前缀（防串 A 股 feed）"
        rec = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert rec["mode"] == "execute_hk" and rec["dry_run"] is True
        assert rec["code"] == "00700.HK" and rec["volume"] == 100

    def test_execute_posts_to_place_and_records_result(self, monkeypatch):
        seen: list[dict] = []

        def fake_post(path, body, timeout=30):  # noqa: ANN001
            seen.append({"path": path, "body": body})
            return {"success": True, "data": {"success": True, "order_id": "O1",
                                              "status": "SUBMITTED", "message": ""}}

        monkeypatch.setattr(hk, "_api_post", fake_post, raising=False)
        done, errors = hk.execute_plan("deepseek-v4-flash", self.ORDERS, dry_run=False)
        assert errors == [] and done[0]["order_id"] == "O1"
        assert seen[0]["path"].endswith("/api/v1/agent-arena/futu/place")
        assert seen[0]["body"] == {
            "env": "SIMULATE", "market": "HK",
            "order": {"code": "00700.HK", "price": 100.5, "quantity": 100,
                      "order_type": "NORMAL", "trd_side": "BUY"},
        }
        rec = json.loads(
            next(hk.TRADE_LOG_DIR.glob("live_trade_hk_*.jsonl")).read_text(encoding="utf-8").strip()
        )
        assert rec["mode"] == "execute_hk" and rec["result"]["order_id"] == "O1"

    def test_rejection_is_reported_with_reason(self, monkeypatch):
        monkeypatch.setattr(
            hk, "_api_post",
            lambda *a, **kw: {"success": False, "data": {"success": False, "message": "现金不足"}},
            raising=False,
        )
        done, errors = hk.execute_plan("deepseek-v4-flash", self.ORDERS, dry_run=False)
        assert done == [] and "现金不足" in errors[0]

    def test_http_failure_does_not_raise(self, monkeypatch):
        def boom(*a, **kw):  # noqa: ANN001
            raise RuntimeError("opend down")

        monkeypatch.setattr(hk, "_api_post", boom, raising=False)
        done, errors = hk.execute_plan("deepseek-v4-flash", self.ORDERS, dry_run=False)
        assert done == [] and "opend down" in errors[0]


# ---------------------------------------------------------------- 候选池消费 / 通知 / 端到端


class TestPoolConsumption:
    def _write(self, doc: dict) -> None:
        hk.POOL_FILE.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def test_missing_file_degrades_with_note(self):
        doc, notes = hk.load_pool()
        assert doc == {} and "仅持仓复盘" in notes[0]

    def test_stale_pool_is_dropped(self):
        self._write({"date": "2026-09-30", "data_date": "2026-09-30", "stale_days": 6,
                     "picks": [{"code": "00700.HK"}]})
        doc, notes = hk.load_pool()
        assert doc == {} and "已陈旧 6 个交易日" in notes[0]

    def test_yesterday_dated_but_fresh_pool_is_kept(self):
        """港股假期次日：池子的 date 不是今天，但 stale_days=0 仍应可用。"""
        self._write({"date": "2026-10-07", "data_date": "2026-10-08", "stale_days": 0,
                     "picks": [{"code": "00700.HK"}]})
        doc, notes = hk.load_pool()
        assert doc["picks"] and notes == []

    def test_slightly_stale_pool_is_kept_with_note(self):
        self._write({"date": "2026-10-06", "data_date": "2026-10-06", "stale_days": 2,
                     "picks": [{"code": "00700.HK"}]})
        doc, notes = hk.load_pool()
        assert doc["picks"] and "2026-10-06" in notes[0]


class TestNotify:
    def test_notify_hk_targets_hk_channel(self, no_real_qq):
        hk.notify_hk("标题", "正文")
        assert no_real_qq == [("标题", "正文", "hk")]

    def test_notify_failure_never_breaks_the_round(self, monkeypatch, capsys):
        import push_notify

        def boom(*a, **kw):  # noqa: ANN001
            raise RuntimeError("keeper.env 缺 key")

        monkeypatch.setattr(push_notify, "notify", boom)
        hk.notify_hk("标题2", "正文2")  # 不抛
        assert "QQ 通知失败" in capsys.readouterr().out


class TestDockerFallback:
    def test_missing_opend_container_fails_fast_without_exec(self, monkeypatch):
        """OpenD 容器不在时要 1 秒内报根因，不许挂着等子进程 90s 超时（2026-10-08 实测）。"""
        calls: list[list[str]] = []

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "Error: No such object: futu-opend"

        def fake_run(argv, **kw):  # noqa: ANN001
            calls.append(list(argv))
            return _Proc()

        monkeypatch.setattr(hk.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as ei:
            hk._docker_account_both()
        assert "futu-opend 容器未运行" in str(ei.value)
        assert len(calls) == 1 and calls[0][:2] == ["docker", "inspect"], "容器不在就不该再 exec"

    def test_unlogged_opend_reports_handshake_stall(self, monkeypatch):
        """容器在跑但 OpenD 停在「请输入账号」：SDK 挂等握手 → 报根因，不甩超时命令。"""

        class _Up:
            returncode = 0
            stdout = "true\n"
            stderr = ""

        def fake_run(argv, **kw):  # noqa: ANN001
            if argv[1] == "inspect":
                return _Up()
            raise hk.subprocess.TimeoutExpired(cmd=argv, timeout=hk.EXEC_TIMEOUT_S)

        monkeypatch.setattr(hk.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError) as ei:
            hk._docker_account_both()
        assert "无响应" in str(ei.value) and "请输入账号" in str(ei.value)


class TestRunAnalysis:
    def test_account_unreachable_alerts_and_returns_1(self, monkeypatch, no_real_qq):
        monkeypatch.setattr(hk, "fetch_account_both", lambda: (None, "opend 未登录"), raising=False)
        monkeypatch.setattr(hk, "call_model", lambda *a, **kw: pytest.fail("账户不可达时不得调模型"))
        rc = hk.run_analysis(dry_run=True, agents=["deepseek-v4-flash"], ask_decisions=False)
        assert rc == 1
        assert no_real_qq and no_real_qq[0][2] == "hk" and "opend 未登录" in no_real_qq[0][1]
        assert not list(hk.DATA_DIR.glob("**/log.jsonl")), "中断轮不得留空日志"

    def test_full_round_writes_log_and_summarizes(self, monkeypatch, no_real_qq):
        sim = {"total_asset": 120000.0, "cash": 30000.0,
               "positions": {"00700.HK": {"volume": 100, "cost": 350.0,
                                          "price": 380.0, "name": "腾讯控股"}}}
        monkeypatch.setattr(hk, "fetch_account_both", lambda: (sim, ""), raising=False)
        monkeypatch.setattr(hk, "fetch_snapshot",
                            lambda codes: {"00700.HK": {"last_price": 385.0, "day_chg": 1.1}},
                            raising=False)
        monkeypatch.setattr(hk, "load_news", lambda names: [], raising=False)
        monkeypatch.setattr(
            hk, "call_model",
            lambda env, sig, model, user, system: (
                "腾讯今日 +1.1%，继续持有。\n"
                '{"decisions":[{"action":"hold","code":"00700.HK","reason":"持有"}]}',
                {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            ),
            raising=False,
        )
        rc = hk.run_analysis(dry_run=True, agents=["deepseek-v4-flash"], ask_decisions=True)
        assert rc == 0

        log = next(hk.DATA_DIR.glob("deepseek-v4-flash/log/*/log.jsonl"))
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["signature"] == "deepseek-v4-flash"
        assert [m["role"] for m in entry["new_messages"]] == ["user", "assistant"]
        assert entry["usage"]["total_tokens"] == 150
        assert "00700.HK" in entry["new_messages"][0]["content"]
        assert entry["new_messages"][1]["content"].startswith("腾讯今日")

        titles = [t for t, _, _ in no_real_qq]
        assert any("港股盘中分析" in t for t in titles), "每轮必发一条摘要（用户裁决的事件类型）"
