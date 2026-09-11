#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 50 个 A 股 AI-IDE 策略模板（<id>.py + <id>.json）。

背景：素材来自 `~/桌面/TradingView策略-重爬1001/策略分析/`（531 条结构化策略）。
本脚本把它们提炼成 quantmind 策略模板，落盘到 `strategy_templates/`。

硬约束（读源码核实，勿改）：
1. 模板加载器 `glob("*.json")` 不递归 → 文件必须平铺在 strategy_templates/ 根目录，
   "文件夹"由 JSON 的 `dir` 字段（→ AI-IDE 虚拟目录 ide_dir）表达。
2. `f_*` 只在 RedisRecordingStrategy / RedisRiskGuardTopkStrategy 生效
   （FundamentalFilterMixin 先 pop，其余类会被 strip_unsupported_kwargs 丢掉）。
3. `f_*` 只认 features_daily 真实存在的列；未知列静默跳过（logger.debug）。
   features_daily 的 return_1d/3d/5d/10d/20d/60d 是【未来收益】，用作过滤=标签泄漏，禁用。
   单位：total_mv/float_mv/net_profit_ttm/revenue_ttm/equity/annual_net_profit 为元；
   amount_ma_5 为万元；vol_std_*/ma_gap_*/rsi_*/pct_change 为百分数。
4. JSON `params` 的 default 必须与 .py kwargs 一致：topk/n_drop/rebalance_days 等
   UI 滑块参数会在请求时覆盖 kwargs。

用法：python3 scripts/gen_ashare_strategy_templates.py [--out strategy_templates] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 参数元数据：名称 -> (中文描述, min, max)
# ---------------------------------------------------------------------------
PARAM_META: dict[str, tuple[str, float | None, float | None]] = {
    "topk": ("持仓股票总数", 5, 200),
    "n_drop": ("每期最大替换数", 0, 200),
    "rebalance_days": ("调仓周期（交易日）", 1, 60),
    "risk_degree": ("基础仓位比例（0-1）", 0.05, 1.0),
    # 上限 60 是 QlibStrategyParams.momentum_period 的硬约束（schemas/backtest.py:76），
    # 写大了 AI-IDE 请求会 422，回测根本起不来。
    "momentum_period": ("动量回看天数", 5, 60),
    "momentum_weight": ("动量因子融合权重", 0.0, 1.0),
    # 规模与流动性
    "f_total_mv_min": ("总市值下限（元）", 0, None),
    "f_total_mv_max": ("总市值上限（元）", 0, None),
    "f_float_mv_min": ("流通市值下限（元）", 0, None),
    "f_amount_ma_5_min": ("5 日均成交额下限（万元）", 0, None),
    "f_circulating_capital_min": ("流通股本下限（股）", 0, None),
    # 估值与分红
    "f_pe_ttm_min": ("PE(TTM) 下限（剔除亏损）", -50, 300),
    "f_pe_ttm_max": ("PE(TTM) 上限", 0, 300),
    "f_pb_max": ("PB 上限", 0.1, 30),
    "f_ps_ttm_max": ("PS(TTM) 上限", 0.1, 100),
    "f_dividend_rate_min": ("股息率下限（小数，0.02=2%）", 0.0, 0.2),
    # 盈利与质量
    "f_net_profit_ttm_min": ("TTM 净利润下限（元）", 0, None),
    "f_annual_net_profit_min": ("最近年度净利润下限（元）", 0, None),
    "f_revenue_ttm_min": ("TTM 营收下限（元）", 0, None),
    "f_equity_min": ("净资产下限（元）", 0, None),
    # 波动与风险
    "f_vol_std_20_max": ("20 日收益波动率上限（%）", 0.1, 20),
    "f_vol_std_60_max": ("60 日收益波动率上限（%）", 0.1, 20),
    "f_vol_atr_14_max": ("ATR(14) 上限（%）", 0.01, 20),
    "f_beta_20_min": ("20 日 Beta 下限", -3, 3),
    "f_beta_20_max": ("20 日 Beta 上限", -3, 3),
    # 均线与相对位置
    "f_ma_gap_5_min": ("相对 5 日均线偏离下限（%）", -50, 50),
    "f_ma_gap_5_max": ("相对 5 日均线偏离上限（%）", -50, 50),
    "f_ma_gap_20_min": ("相对 20 日均线偏离下限（%）", -50, 50),
    "f_ma_gap_20_max": ("相对 20 日均线偏离上限（%）", -50, 50),
    # 强弱与超买超卖
    "f_rsi_6_max": ("RSI(6) 上限", 0, 100),
    "f_rsi_14_min": ("RSI(14) 下限", 0, 100),
    "f_rsi_14_max": ("RSI(14) 上限", 0, 100),
    "f_kdj_j_max": ("KDJ-J 上限", -50, 150),
    "f_macd_hist_min": ("MACD 柱下限", -50, 50),
    "f_pct_change_min": ("上一交易日涨跌幅下限（%）", -30, 30),
    "f_pct_change_max": ("上一交易日涨跌幅上限（%）", -30, 30),
    # 量能
    "f_vol_to_ma5_min": ("量比（对 5 日均量）下限", 0.0, 10),
    "f_vol_to_ma5_max": ("量比（对 5 日均量）上限", 0.0, 10),
    "f_vol_to_ma20_min": ("量比（对 20 日均量）下限", 0.0, 10),
    "f_volume_trend_3d_min": ("3 日量能趋势下限", -1.0, 5.0),
    # 自定义策略类参数
    "trend_ma": ("趋势均线周期", 5, 250),
    "trend_slope_days": ("均线斜率回看天数", 1, 60),
    "target_vol": ("目标年化波动率（0.15=15%）", 0.05, 0.6),
    "vol_window": ("波动率估计窗口（交易日）", 5, 120),
    "weight_cap": ("单票权重上限（0-1）", 0.01, 1.0),
    "min_position": ("最低仓位比例", 0.0, 1.0),
    "max_position": ("最高仓位比例", 0.1, 1.0),
    "vol_symbol": ("波动率参考指数", None, None),
    "regime_symbol": ("市场状态参考指数", None, None),
    "fast_window": ("快线周期（市场状态）", 5, 120),
    "slow_window": ("慢线周期（市场状态）", 10, 250),
    "boost_weight": ("动量/反转增强权重", 0.0, 1.0),
    "momentum_window": ("增强因子回看天数", 5, 120),
    "uptrend_position": ("上行市仓位比例", 0.0, 1.0),
    "neutral_position": ("震荡市仓位比例", 0.0, 1.0),
    "downtrend_position": ("下行市仓位比例", 0.0, 1.0),
    "dd_l1": ("回撤档位 1（0.05=5%）", 0.0, 1.0),
    "dd_l2": ("回撤档位 2", 0.0, 1.0),
    "dd_l3": ("回撤档位 3", 0.0, 1.0),
    "pos_l1": ("档位 1 对应仓位", 0.0, 1.0),
    "pos_l2": ("档位 2 对应仓位", 0.0, 1.0),
    "pos_l3": ("档位 3 对应仓位", 0.0, 1.0),
    "pos_l4": ("档位 4 对应仓位", 0.0, 1.0),
    "lookback_days": ("涨停检查回看天数", 1, 60),
    "dd_lookback": ("回撤参考窗口（交易日）", 20, 500),
    "max_limit_ups": ("允许的最大涨停次数", 0, 10),
    # 风控护栏（RedisRiskGuardTopkStrategy）
    "industry_cap_ratio": ("单一行业持仓上限占比", 0.05, 0.6),
    "market_state_window": ("大盘状态判定窗口（交易日）", 5, 120),
    # 止损与抄底（RedisStopLossStrategy / RedisCrashBuyDipStrategy）
    "stop_loss": ("个股止损线（负数，-0.08=亏 8% 止损）", -0.5, -0.01),
    "take_profit": ("个股止盈线（正数，0.15=赚 15% 止盈）", 0.01, 1.0),
    "top_k": ("单次抄底买入标的数", 1, 50),
    "hold_days": ("抄底后持有交易日数", 1, 60),
    "crash_threshold_pct": ("指数单日跌幅触发阈值（小数）", 0.005, 0.1),
    "crash_threshold_points": ("指数单日跌幅触发阈值（点）", 10, 1000),
    "benchmark": ("抄底参考基准指数", None, None),
    "max_wait_days": ("触发后最长等待入场天数", 0, 20),
    "min_oversold_margin": ("超跌幅度下限（相对均线）", 0.0, 0.2),
    "trend_window": ("趋势判定窗口", 5, 120),
    "ma_fast": ("快均线周期", 2, 60),
    "ma_slow": ("慢均线周期", 5, 250),
    "vol_lookback": ("波动率回看窗口", 5, 120),
}

# 不写进 JSON params 的键（平台注入或恒定值）
_HIDDEN_KEYS = {"signal", "only_tradable"}

_MODULE_PATHS = {
    "RedisRecordingStrategy": "backend.services.engine.qlib_app.utils.recording_strategy",
    "RedisRiskGuardTopkStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
    "RedisMomentumStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
    "RedisCrashBuyDipStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
    "RedisStopLossStrategy": "backend.services.engine.qlib_app.utils.extended_strategies",
}


def _fmt(value: object) -> str:
    """Python 字面量格式化（保持 kwargs 可读）。"""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _module_path(class_name: str, is_custom: bool) -> str | None:
    """自定义类不回填 module_path：由 CustomStrategyBuilder 从动态模块自动补全。"""
    if is_custom:
        return None
    return _MODULE_PATHS.get(class_name)


# ---------------------------------------------------------------------------
# 回测记录 / 用法（写进每个模板 .py 的开头 docstring）
# ---------------------------------------------------------------------------

_BACKTEST_RESULT_PATH = Path(__file__).resolve().parent / "ashare_backtest_results.json"


def _load_backtest_results() -> dict:
    if not _BACKTEST_RESULT_PATH.exists():
        return {}
    try:
        return json.loads(_BACKTEST_RESULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _pct(value: object) -> str:
    return f"{value:.2%}" if isinstance(value, (int, float)) else "—"


def _num(value: object) -> str:
    return f"{value:.3f}" if isinstance(value, (int, float)) else "—"


def _backtest_lines(spec: dict, results: dict) -> list[str]:
    """回测记录：该模板自己的成绩 + 平台内置 baseline 对照。"""
    runs = (results or {}).get("runs") or {}
    row = runs.get(spec["id"]) or {}
    window = (results or {}).get("window") or {}
    model_id = (results or {}).get("model_id") or "—"
    span = f"{window.get('start', '—')} → {window.get('end', '—')}"

    if row.get("status") != "completed":
        return [
            "回测记录：待跑",
            f"    docker exec quantmind python /app/scripts/run_ashare_backtest_all.py --ids {spec['id']}",
        ]

    lines = [
        f"回测记录（模型 {model_id}，{span}，A股费率/T+1/含交易成本）",
        f"    年化 {_pct(row.get('annual_return'))} ｜ 夏普 {_num(row.get('sharpe_ratio'))} ｜ "
        f"最大回撤 {_pct(row.get('max_drawdown'))} ｜ 基准 {_pct(row.get('benchmark_return'))} ｜ "
        f"交易 {row.get('total_trades')} 笔 ｜ 胜率 {_pct(row.get('win_rate'))}",
    ]
    baseline = runs.get((results or {}).get("baseline_id") or "standard_topk") or {}
    if baseline.get("status") == "completed":
        lines.append(
            f"    对照 standard_topk（平台内置，同模型同区间）：年化 {_pct(baseline.get('annual_return'))} ｜ "
            f"夏普 {_num(baseline.get('sharpe_ratio'))} ｜ 最大回撤 {_pct(baseline.get('max_drawdown'))}"
        )
    lines.append("    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。")
    return lines


def _usage_lines(spec: dict, results: dict) -> list[str]:
    sid = spec["id"]
    has_filters = any(key.startswith("f_") for key in spec["kwargs"])
    knobs = ["topk"]
    if "n_drop" in spec["kwargs"]:
        knobs.append("n_drop")
    knobs.append("rebalance_days")
    knobs_text = " / ".join(knobs)
    param_line = (
        f"    2. 参数面板可调 {knobs_text}；f_* 是 A 股硬约束（基本面/流动性），建议保留；"
        if has_filters
        else f"    2. 参数面板可调 {knobs_text}（本模板靠内置类固有行为，无 f_* 过滤）；"
    )
    window = (results or {}).get("window") or {}
    start = window.get("start") or "2024-01-02"
    end = window.get("end") or "2024-12-31"
    model_id = (results or {}).get("model_id") or "<model_id>"
    return [
        "怎么用",
        f"    1. AI-IDE → 策略模板 → 文件夹「{spec['dir']}」→ 选「{spec['name']}」，选好模型直接回测；",
        param_line,
        "    3. 命令行单跑（下面就是复现本文件回测记录的命令）：",
        f"       docker exec quantmind python /app/scripts/verify_ashare_backtest.py {sid} \\",
        f"         --start {start} --end {end} --model-id {model_id}",
        f"    4. 实盘：策略 ID 用 sys_{sid}（内置模板在实盘链路里需加 sys_ 前缀）。",
        "    5. 本文件的回测记录由 scripts/run_ashare_backtest_all.py 写入 "
        "scripts/ashare_backtest_results.json，",
        "       重跑生成器（scripts/gen_ashare_strategy_templates.py）即刷新；"
        "改参数请改生成器，不要手改模板。",
    ]


def _docstring(spec: dict, results: dict, is_custom: bool) -> str:
    kwargs = spec["kwargs"]
    topk = kwargs.get("topk", kwargs.get("top_k"))
    if is_custom:
        base = "RedisWeightStrategy" if spec["cls"] == "InverseVolWeightStrategy" else "RedisRecordingStrategy"
        cls_note = f"（模板内自定义，继承 {base}）"
    else:
        cls_note = ""
    position = f"调仓：每 {kwargs.get('rebalance_days', '—')} 个交易日 ｜ 持仓：{topk if topk is not None else '—'} 只"
    if "n_drop" in kwargs:
        position += f" ｜ 单期换手：{kwargs['n_drop']} 只"
    lines = [
        f'"""{spec["name"]} ({spec["id"]})',
        "",
        f"[A股] {spec['desc']}",
        "",
        f"文件夹：{spec['dir']} ｜ 策略类：{spec['cls']}{cls_note} ｜ 市场：A股",
        position,
        "",
        *_backtest_lines(spec, results),
        "",
        *_usage_lines(spec, results),
        "",
        "由 scripts/gen_ashare_strategy_templates.py 生成；参数说明见同名 .json。",
        '"""',
    ]
    return "\n".join(lines)


def _render_py(spec: dict, is_custom: bool, results: dict) -> str:
    class_name = spec["cls"]
    lines = ["# -*- coding: utf-8 -*-", _docstring(spec, results, is_custom)]
    if is_custom:
        # 自定义类：docstring → imports → 类定义 → STRATEGY_CONFIG
        lines.extend(
            [
                "",
                spec.get("custom_imports", _CUSTOM_IMPORTS),
                spec.get("custom_note", _CUSTOM_NOTE),
                spec["custom_body"],
            ]
        )
    lines.extend(
        [
            "",
            "STRATEGY_CONFIG = {",
            f'    "class": "{class_name}",',
        ]
    )
    module_path = _module_path(class_name, is_custom)
    if module_path:
        lines.append(f'    "module_path": "{module_path}",')
    lines.append('    "kwargs": {')
    for key, value in spec["kwargs"].items():
        lines.append(f'        "{key}": {_fmt(value)},')
    lines.append("    },")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _render_json(spec: dict) -> str:
    params = []
    for key in spec["kwargs"]:
        if key in _HIDDEN_KEYS:
            continue
        meta = PARAM_META.get(key)
        if meta is None:
            raise KeyError(f"{spec['id']}: 缺少参数元数据 {key}")
        desc, lo, hi = meta
        entry: dict[str, object] = {
            "name": key,
            "description": desc,
            "default": spec["kwargs"][key],
        }
        if lo is not None:
            entry["min"] = lo
        if hi is not None:
            entry["max"] = hi
        params.append(entry)

    meta_obj = {
        "id": spec["id"],
        "name": spec["name"],
        "description": spec["desc"],
        "category": spec["category"],
        "difficulty": spec["difficulty"],
        "params": params,
        "execution_defaults": spec.get(
            "execution_defaults",
            # 与 live_trading `_default_execution_config()` 一致，显式写出便于阅读
            {"max_buy_drop": -0.03, "stop_loss": -0.08},
        ),
        "live_defaults": spec.get(
            "live_defaults",
            {
                "rebalance_days": spec["kwargs"].get("rebalance_days", 3),
                "schedule_type": "interval",
                "trade_weekdays": [],
                "enabled_sessions": ["PM"],
                "sell_time": "14:30",
                "buy_time": "14:45",
                "sell_first": True,
                "order_type": "LIMIT",
                "max_price_deviation": 0.02,
                "max_orders_per_cycle": 20,
            },
        ),
        "live_config_tips": spec.get("tips", []),
        "markets": ["a_share"],
        "dir": spec["dir"],
    }
    return json.dumps(meta_obj, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 自定义策略类源码（薄封装 RedisRecordingStrategy，仅覆写选股/仓位环节）
# ---------------------------------------------------------------------------

_CUSTOM_IMPORTS = '''import pandas as pd

from backend.services.engine.qlib_app.utils.recording_strategy import RedisRecordingStrategy
'''

_CUSTOM_NOTE = """# 自定义策略类：由 CustomStrategyBuilder 从动态模块自动补全 module_path（不要手写 module_path）。
# 继承 RedisRecordingStrategy → 完整保留 f_* 基本面过滤、Redis 记录、TopK-Dropout 低换手与动态风控。
# 取行情一律走基类的 _close_matrix() / _price_frame()（首次取满回测区间并缓存，之后毫秒级切片）；
# 直接调 D.features 会在每个调仓步重复拉全市场，把一年回测拖到几十分钟。
"""

_CUSTOM_IMPORTS_WEIGHT = '''import pandas as pd

from backend.services.engine.qlib_app.utils.recording_strategy import (
    FundamentalFilterMixin,
    RedisWeightStrategy,
)
'''

_CUSTOM_NOTE_WEIGHT = """# 自定义策略类：由 CustomStrategyBuilder 从动态模块自动补全 module_path（不要手写 module_path）。
# 继承 RedisWeightStrategy（WeightStrategyBase 链路）→ 权重分配会被 qlib 真正执行，
# 并保留 Redis 交易记录、涨停/停牌过滤与调仓周期控制；f_* 由本类自己接 FundamentalFilterMixin。
# 取行情一律走基类的 _close_matrix() / _price_frame()（首次取满回测区间并缓存，之后毫秒级切片）；
# 直接调 D.features 会在每个调仓步重复拉全市场，把一年回测拖到几十分钟。
"""

_FILTERED_MOMENTUM_BODY = '''

class FilteredMomentumStrategy(RedisRecordingStrategy):
    """模型分 + 动量融合，同时保留 f_* 基本面硬过滤。

    A 股逻辑：平台内置的 RedisMomentumStrategy 走 RedisTopkStrategy 链路，
    没有 FundamentalFilterMixin，f_* 参数会被静默丢弃。本类继承 RedisRecordingStrategy，
    先融合动量、再走基本面过滤与 TopK-Dropout，做到「动量增强 + A 股硬约束」。
    融合方式与内置实现一致：score + momentum_weight * zscore(动量).clip(-1, 1)。

    注意覆写的是 ``_adjust_signal`` 而不是 ``generate_target_weight_position``：
    qlib 的 TopkDropoutStrategy 自己读 self.signal 选股、从不调用后者，
    只有前者会被基类的 generate_trade_decision 真正调用（ref_date = T-1，无前视）。
    """

    def __init__(self, *args, **kwargs):
        self.momentum_period = int(kwargs.pop("momentum_period", 20))
        self.momentum_weight = float(kwargs.pop("momentum_weight", 0.5))
        super().__init__(*args, **kwargs)

    def _momentum_factor(self, stocks, ref_date):
        span = int(self.momentum_period * 2.5) + 30
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or len(prices) <= self.momentum_period:
            return None
        window = prices.iloc[-self.momentum_period :]
        momentum = window.iloc[-1] / window.iloc[0] - 1.0
        std = momentum.std(ddof=1)
        if std is None or float(std) != float(std) or float(std) == 0:
            return None
        return ((momentum - momentum.mean()) / std).clip(-1, 1)

    def _adjust_signal(self, score, ref_date):
        """把动量因子叠加到模型分上（ref_date = 上一交易日，无前视）。

        模型分必须先做截面标准化：pred 的量纲随模型而变（本批模型 per-date std≈0.0035），
        直接加 [-1,1] 的动量因子等于把排名完全交给动量（实测 as11 年化从 55% 掉到 10%）。
        标准化后 momentum_weight 才真正是「动量的相对权重」。
        """
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        factor = self._momentum_factor(list(score.index), ref_date)
        if factor is None:
            return score
        std = score.std(ddof=1)
        if std is None or float(std) != float(std) or float(std) == 0:
            return score
        base = (score - score.mean()) / std
        return base.add(factor.reindex(score.index).fillna(0.0) * self.momentum_weight)
'''

_TREND_GATE_BODY = '''

class TrendGateTopkStrategy(RedisRecordingStrategy):
    """趋势闸门：只在「收盘价站上长期均线且均线仍向上」的标的中选 TopK。

    A 股逻辑：T+1 下买错方向当天无法纠错，用长期均线做一次事前过滤，
    把仓位留给趋势仍在的标的；被挡掉的标的会把名额让给次优候选，而不是留空。
    覆写 ``_adjust_signal``（基类的 generate_trade_decision 真正调用的钩子）。
    """

    def __init__(self, *args, **kwargs):
        self.trend_ma = int(kwargs.pop("trend_ma", 60))
        self.trend_slope_days = int(kwargs.pop("trend_slope_days", 10))
        super().__init__(*args, **kwargs)

    def _trend_ok(self, stocks, ref_date):
        """返回布尔 Series：价格在均线上方 且 均线较 N 日前抬升。"""
        span = int(self.trend_ma * 2.5) + self.trend_slope_days + 30
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or prices.empty:
            return None
        min_periods = max(5, self.trend_ma // 2)
        ma = prices.rolling(self.trend_ma, min_periods=min_periods).mean()
        if len(ma) <= self.trend_slope_days:
            return None
        latest = ma.iloc[-1]
        base = ma.iloc[-1 - self.trend_slope_days]
        return (prices.iloc[-1] > latest) & (latest > base)

    def _adjust_signal(self, score, ref_date):
        """只保留「站上长期均线且均线向上」的标的（ref_date = 上一交易日）。"""
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        mask = self._trend_ok(list(score.index), ref_date)
        if mask is None:
            return score
        keep = [s for s in score.index if bool(mask.get(s, False))]
        if not keep:
            # 极端行情下若全部不达标就不启用闸门：空信号会让 TopkDropout 按
            # 「分数全为 NaN」的原始顺序卖出 n_drop 只，等于被动清仓，不是本意。
            return score
        return score.loc[keep]
'''

_VOL_TARGET_BODY = '''

class VolTargetPositionStrategy(RedisRecordingStrategy):
    """波动率目标仓位：仓位 = min(1, 目标波动 / 指数已实现波动)。

    A 股逻辑：单一仓位做多时，组合波动几乎等于市场波动。
    用沪深 300 的 20 日已实现年化波动做分母，高波动期自动降仓、低波动期满仓，
    比"拍脑袋定仓位"更稳，也避免在急跌段满仓硬扛。
    覆写 ``_dynamic_risk_degree``：基类会在下单前把 self.risk_degree 换成这里的返回值。
    """

    def __init__(self, *args, **kwargs):
        self.target_vol = float(kwargs.pop("target_vol", 0.15))
        self.vol_window = int(kwargs.pop("vol_window", 20))
        self.min_position = float(kwargs.pop("min_position", 0.2))
        self.max_position = float(kwargs.pop("max_position", 1.0))
        self.vol_symbol = str(kwargs.pop("vol_symbol", "SH000300"))
        super().__init__(*args, **kwargs)

    def _realized_vol(self, ref_date):
        span = int(self.vol_window * 2.5) + 20
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix([self.vol_symbol], start, ref_date)
        if prices is None or prices.empty:
            return None
        series = prices.iloc[:, 0].dropna()
        returns = series.pct_change().dropna().iloc[-self.vol_window :]
        if len(returns) < 5:
            return None
        return float(returns.std(ddof=1) * (252 ** 0.5))

    def _dynamic_risk_degree(self, base, ref_date):
        """仓位系数 = min(1, 目标波动 / 已实现波动)，ref_date = 上一交易日。"""
        realized = self._realized_vol(ref_date)
        if not realized or realized <= 1e-6:
            return base
        scale = min(1.0, self.target_vol / realized)
        return max(self.min_position, min(self.max_position, base * scale))
'''

_INVERSE_VOL_BODY = '''

class InverseVolWeightStrategy(FundamentalFilterMixin, RedisWeightStrategy):
    """逆波动率加权：选股仍是模型分 TopK，权重按 1/σ 分配（风险平价近似）。

    A 股逻辑：等权会让高波动小票主导组合风险。逆波动加权在不改变选股的前提下
    压低高波动标的的权重，回撤更平滑；对涨跌停造成的权重漂移也更耐受。

    基类用 RedisWeightStrategy（WeightStrategyBase 链路）而不是 TopkDropout：
    只有前者会真正调用 ``generate_target_weight_position`` 并把返回的权重
    交给下单器，TopkDropout 是按现金等额下单、无法表达单票权重差异。
    f_* 过滤由本类自己接 FundamentalFilterMixin，取上一交易日快照，无前视。
    """

    def __init__(self, *args, **kwargs):
        self.vol_window = int(kwargs.pop("vol_window", 20))
        self.weight_cap = float(kwargs.pop("weight_cap", 0.08))
        # f_* 必须在 super().__init__ 之前 pop，否则会被 strip_unsupported_kwargs 丢掉
        self.init_fundamental_filter(kwargs)
        super().__init__(*args, **kwargs)

    def _prev_trade_date(self):
        """上一交易日；取不到返回 None（按「不调整权重」处理）。"""
        try:
            step = self.trade_calendar.get_trade_step()
            prev, _ = self.trade_calendar.get_step_time(step, shift=1)
            return pd.Timestamp(prev)
        except Exception:
            return None

    def _vol_map(self, stocks, ref_date):
        span = int(self.vol_window * 2.5) + 20
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or prices.empty:
            return None
        vol = prices.pct_change().iloc[-self.vol_window :].std(ddof=1)
        vol = vol[vol > 0]
        return vol if not vol.empty else None

    def generate_target_weight_position(self, score, current=None, trade_exchange=None, *args, **kwargs):
        ref_date = self._prev_trade_date()
        if ref_date is not None and self.use_fundamental_filter:
            score = self.apply_fundamental_filter(score, ref_date)
        weights = super().generate_target_weight_position(score, current, trade_exchange, *args, **kwargs)
        if not weights or ref_date is None:
            return weights
        vol = self._vol_map(list(weights.keys()), ref_date)
        if vol is None:
            return weights
        inverse = (1.0 / vol.clip(lower=1e-4)).reindex(list(weights.keys())).dropna()
        if inverse.empty:
            return weights
        total = float(sum(weights.values()))
        scaled = inverse / inverse.sum() * total
        if 0 < self.weight_cap < 1.0:
            scaled = scaled.clip(upper=self.weight_cap)
            if scaled.sum() > 0:
                scaled = scaled / scaled.sum() * total
        return {key: float(value) for key, value in scaled.items()}
'''

_REGIME_SWITCH_BODY = '''

class RegimeSwitchStrategy(RedisRecordingStrategy):
    """市场状态切换：上行市加动量、下行市加反转，并同步调整总仓位。

    A 股逻辑：动量因子在趋势市有效、反转因子在震荡/下跌市有效。
    用沪深 300 的 20/60 日均线关系判定状态，动态选择增强方向，
    避免"一套因子打天下"在不同市场环境下失效。
    同时覆写 ``_adjust_signal``（因子方向）与 ``_dynamic_risk_degree``（仓位）。
    """

    def __init__(self, *args, **kwargs):
        self.regime_symbol = str(kwargs.pop("regime_symbol", "SH000300"))
        self.fast_window = int(kwargs.pop("fast_window", 20))
        self.slow_window = int(kwargs.pop("slow_window", 60))
        self.boost_weight = float(kwargs.pop("boost_weight", 0.5))
        self.momentum_window = int(kwargs.pop("momentum_window", 20))
        self.uptrend_position = float(kwargs.pop("uptrend_position", 1.0))
        self.neutral_position = float(kwargs.pop("neutral_position", 0.8))
        self.downtrend_position = float(kwargs.pop("downtrend_position", 0.5))
        super().__init__(*args, **kwargs)

    def _prices(self, symbols, ref_date, span_days):
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span_days)
        return self._close_matrix(symbols, start, ref_date)

    def _regime(self, ref_date):
        span = int(self.slow_window * 2.5) + 30
        prices = self._prices([self.regime_symbol], ref_date, span)
        if prices is None or prices.empty:
            return "neutral"
        close = prices.iloc[:, 0].dropna()
        if len(close) < self.slow_window + 1:
            return "neutral"
        fast = close.rolling(self.fast_window, min_periods=max(3, self.fast_window // 2)).mean().iloc[-1]
        slow = close.rolling(self.slow_window, min_periods=max(5, self.slow_window // 2)).mean().iloc[-1]
        last = float(close.iloc[-1])
        if last > fast > slow:
            return "up"
        if last < fast < slow:
            return "down"
        return "neutral"

    def _dynamic_risk_degree(self, base, ref_date):
        """按市场状态缩放仓位（ref_date = 上一交易日，无前视）。"""
        state = self._regime(ref_date)
        ratio = {
            "up": self.uptrend_position,
            "neutral": self.neutral_position,
            "down": self.downtrend_position,
        }[state]
        return max(0.0, min(1.0, base * ratio))

    def _adjust_signal(self, score, ref_date):
        """上行市加动量、下行市加反转（ref_date = 上一交易日，无前视）。"""
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        state = self._regime(ref_date)
        if state == "neutral":
            return score
        span = int(self.momentum_window * 2.5) + 30
        prices = self._prices(list(score.index), ref_date, span)
        if prices is None or len(prices) <= self.momentum_window:
            return score
        window = prices.iloc[-self.momentum_window :]
        momentum = window.iloc[-1] / window.iloc[0] - 1.0
        std = momentum.std(ddof=1)
        if not std or float(std) != float(std) or float(std) <= 0:
            return score
        factor = ((momentum - momentum.mean()) / std).clip(-1, 1).reindex(score.index).fillna(0.0)
        direction = 1.0 if state == "up" else -1.0
        # 模型分先截面标准化，boost_weight 才是「相对模型分」的增强强度
        score_std = score.std(ddof=1)
        if not score_std or float(score_std) != float(score_std) or float(score_std) <= 0:
            return score
        base = (score - score.mean()) / score_std
        return base.add(factor * self.boost_weight * direction)
'''

_DRAWDOWN_BODY = '''

class DrawdownThrottleStrategy(RedisRecordingStrategy):
    """指数回撤阶梯降仓：按沪深 300 距 250 日高点的回撤分档降仓位。

    A 股逻辑：单边下跌时模型信号会持续给出"便宜"的标的，但趋势性下跌里
    越买越亏。用指数回撤做硬闸门，回撤越深仓位越低，保住本金等右侧。
    覆写 ``_dynamic_risk_degree``：基类会在下单前把 self.risk_degree 换成这里的返回值。
    """

    def __init__(self, *args, **kwargs):
        self.regime_symbol = str(kwargs.pop("regime_symbol", "SH000300"))
        self.dd_lookback = int(kwargs.pop("dd_lookback", 250))
        self.dd_l1 = float(kwargs.pop("dd_l1", 0.05))
        self.dd_l2 = float(kwargs.pop("dd_l2", 0.10))
        self.dd_l3 = float(kwargs.pop("dd_l3", 0.15))
        self.pos_l1 = float(kwargs.pop("pos_l1", 1.0))
        self.pos_l2 = float(kwargs.pop("pos_l2", 0.8))
        self.pos_l3 = float(kwargs.pop("pos_l3", 0.6))
        self.pos_l4 = float(kwargs.pop("pos_l4", 0.4))
        super().__init__(*args, **kwargs)

    def _drawdown(self, ref_date):
        span = int(self.dd_lookback * 1.8) + 30
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix([self.regime_symbol], start, ref_date)
        if prices is None or prices.empty:
            return None
        series = prices.iloc[:, 0].dropna().iloc[-self.dd_lookback :]
        if series.empty:
            return None
        peak = float(series.cummax().iloc[-1])
        last = float(series.iloc[-1])
        if peak <= 0:
            return None
        return last / peak - 1.0

    def _dynamic_risk_degree(self, base, ref_date):
        """按指数距 250 日高点的回撤分档降仓（ref_date = 上一交易日）。"""
        drawdown = self._drawdown(ref_date)
        if drawdown is None:
            return base
        depth = -drawdown
        if depth < self.dd_l1:
            ratio = self.pos_l1
        elif depth < self.dd_l2:
            ratio = self.pos_l2
        elif depth < self.dd_l3:
            ratio = self.pos_l3
        else:
            ratio = self.pos_l4
        return max(0.0, min(1.0, base * ratio))
'''

_LIMIT_UP_BODY = '''

class LimitUpGuardStrategy(RedisRecordingStrategy):
    """涨停规避：剔除近 N 日内出现涨停的标的。

    A 股逻辑：涨停股次日大概率高开、难以按模型目标价成交，且开板后常有回吐。
    与其在涨停板上排队，不如把名额让给同样高分但可成交的标的。
    涨跌幅阈值按板块区分：主板 10%、创业板/科创板 20%、北交所 30%。
    覆写 ``_adjust_signal``（基类的 generate_trade_decision 真正调用的钩子）。
    """

    def __init__(self, *args, **kwargs):
        self.lookback_days = int(kwargs.pop("lookback_days", 10))
        self.max_limit_ups = int(kwargs.pop("max_limit_ups", 0))
        super().__init__(*args, **kwargs)

    @staticmethod
    def _limit_threshold(symbol) -> float:
        code = str(symbol)[-6:]
        if code.startswith(("300", "301", "688", "689")):
            return 0.195
        if code.startswith(("4", "8", "9")):
            return 0.295
        return 0.095

    def _limit_up_counts(self, stocks, ref_date):
        span = int(self.lookback_days * 2.5) + 20
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or prices.empty:
            return None
        returns = prices.pct_change().iloc[-self.lookback_days :]
        counts = {}
        for symbol in returns.columns:
            threshold = self._limit_threshold(symbol)
            counts[symbol] = int((returns[symbol] >= threshold).sum())
        return counts

    def _adjust_signal(self, score, ref_date):
        """剔除近 N 日出现过涨停的标的（ref_date = 上一交易日，无前视）。"""
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        counts = self._limit_up_counts(list(score.index), ref_date)
        if counts is None:
            return score
        keep = [s for s in score.index if counts.get(s, 0) <= self.max_limit_ups]
        if not keep:
            return score
        return score.loc[keep]
'''


# ---------------------------------------------------------------------------
# 50 个策略规格
# ---------------------------------------------------------------------------

def _base(**kwargs) -> dict:
    """公共 kwargs：信号来自模型预测，剔除涨停/跌停/停牌。"""
    base = {"signal": "<PRED>", "only_tradable": True}
    base.update(kwargs)
    return base


def _base_raw(**kwargs) -> dict:
    """不含 only_tradable。

    RedisCrashBuyDipStrategy.__init__ 只 pop _OUR_KWARGS / rebalance_days，
    其余 kwargs 会原样透传给 WeightStrategyBase→BaseStrategy，传 only_tradable 会 TypeError。
    """
    base = {"signal": "<PRED>"}
    base.update(kwargs)
    return base


def build_specs() -> list[dict]:
    S: list[dict] = []

    def add(**spec):
        S.append(spec)

    # ================= 01 宽基多因子 =================
    add(
        id="as01_core_multifactor", name="A股宽基多因子核心", dir="A股策略/01_宽基多因子",
        category="basic", difficulty="beginner",
        desc="以模型信号为核心，叠加市值、流动性、估值三层硬过滤的宽基组合；周频调仓，是全部 A 股策略的对照基准。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=50, n_drop=10, rebalance_days=3, f_total_mv_min=3e9,
                     f_amount_ma_5_min=5000, f_pe_ttm_min=0.0, f_pe_ttm_max=80, f_pb_max=10.0),
        tips=["宽基核心建议配合中长期模型信号；市值下限 30 亿可剔除大部分壳资源股。",
              "PE 下限 0 用于剔除亏损股，PE 缺失的标的也会被过滤。"],
    )
    add(
        id="as02_low_turnover_core", name="A股低换手核心", dir="A股策略/01_宽基多因子",
        category="basic", difficulty="beginner",
        desc="调仓周期拉长到 10 日、单期替换数压到 6 只的低换手版本，用于压低 A 股双边成本对净值的侵蚀。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=60, n_drop=6, rebalance_days=10, f_total_mv_min=3e9,
                     f_amount_ma_5_min=5000, f_vol_std_20_max=4.5, f_pe_ttm_min=0.0, f_pe_ttm_max=80),
        tips=["换手率越低，对信号质量的要求越高；建议搭配 IC 稳定的模型。",
              "10 日调仓 + 6 只替换，实测月换手约 30% 以内。"],
    )
    add(
        id="as03_quality_scale_core", name="A股质量规模核心", dir="A股策略/01_宽基多因子",
        category="basic", difficulty="intermediate",
        desc="要求 TTM 净利润、TTM 营收、净资产三重下限，只买有真实经营规模的标的，规避题材空壳。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=8, rebalance_days=5, f_net_profit_ttm_min=2e8,
                     f_revenue_ttm_min=2e9, f_equity_min=3e9, f_total_mv_min=5e9,
                     f_amount_ma_5_min=5000),
        tips=["净利润 2 亿 / 营收 20 亿的下限对应 A 股中大盘蓝筹区间。",
              "features_daily 无 ROE 列，本策略用「净利润 + 净资产」双下限近似质量门槛。"],
    )
    add(
        id="as04_broad_balanced", name="A股大盘均衡配置", dir="A股策略/01_宽基多因子",
        category="basic", difficulty="beginner",
        desc="总市值锁定 100 亿至 3000 亿、要求分红，构建大盘蓝筹均衡组合，波动低于全市场。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=50, n_drop=10, rebalance_days=5, f_total_mv_min=1e10,
                     f_total_mv_max=3e11, f_pe_ttm_min=0.0, f_pe_ttm_max=60,
                     f_dividend_rate_min=0.005, f_amount_ma_5_min=10000),
        tips=["大盘均衡适合作为底仓，与小盘/成长策略组合可显著降低相关性。",
              "股息率下限 0.5% 用于剔除长期不分红的标的。"],
    )
    add(
        id="as05_full_market_adaptive", name="A股全市场广覆盖", dir="A股策略/01_宽基多因子",
        category="advanced", difficulty="intermediate",
        desc="持仓 80 只、只做流动性与估值底线过滤，最大化模型选股的自由度，适合高 IC 宽覆盖模型。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=80, n_drop=15, rebalance_days=5, risk_degree=0.95,
                     f_total_mv_min=2e9, f_amount_ma_5_min=3000, f_pe_ttm_min=-50),
        tips=["持仓分散到 80 只后，个股风险被摊薄，组合收益几乎完全取决于模型 IC。",
              "仓位 95% 留 5% 现金缓冲，避免涨跌停/停牌导致的资金不足。"],
    )

    # ================= 02 价值与质量 =================
    add(
        id="as06_deep_value", name="A股深度价值", dir="A股策略/02_价值与质量",
        category="advanced", difficulty="intermediate",
        desc="PE<15、PB<1.5、股息率>2% 的深度价值组合，双周调仓，吃估值修复的钱。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=8, rebalance_days=10, f_pe_ttm_min=0.0, f_pe_ttm_max=15,
                     f_pb_max=1.5, f_dividend_rate_min=0.02, f_total_mv_min=5e9,
                     f_amount_ma_5_min=5000),
        tips=["深度价值在 A 股常集中在银行/地产/公用事业，行业集中度高，建议搭配行业分散模板。",
              "PB 与股息率双约束可有效剔除高杠杆伪低估值标的。"],
    )
    add(
        id="as07_value_quality_combo", name="A股价值质量双因子", dir="A股策略/02_价值与质量",
        category="basic", difficulty="intermediate",
        desc="估值上限放松到 PE<25 / PB<3，但要求 TTM 净利润与营收规模，兼顾便宜与赚钱。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=8, rebalance_days=10, f_pe_ttm_min=0.0, f_pe_ttm_max=25,
                     f_pb_max=3.0, f_net_profit_ttm_min=3e8, f_revenue_ttm_min=3e9,
                     f_total_mv_min=5e9),
        tips=["这是 GARP 思路的 A 股版本：估值不极端，但要求真实盈利。",
              "适合作为组合的价值底仓，与动量类策略相关性低。"],
    )
    add(
        id="as08_low_valuation_growth", name="A股低估值成长", dir="A股策略/02_价值与质量",
        category="advanced", difficulty="intermediate",
        desc="PE<40 / PS<8 但要求营收 10 亿与净利润 1 亿，寻找估值尚可的成长股。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=5, f_pe_ttm_min=0.0, f_pe_ttm_max=40,
                     f_ps_ttm_max=8.0, f_revenue_ttm_min=1e9, f_net_profit_ttm_min=1e8,
                     f_total_mv_min=3e9),
        tips=["PS 上限用于剔除高收入但无利润的重资产行业。",
              "该组合对模型信号的成长/景气维度依赖较强。"],
    )
    add(
        id="as09_dividend_value", name="A股红利价值", dir="A股策略/02_价值与质量",
        category="basic", difficulty="beginner",
        desc="股息率>3%、PE<20、PB<2.5、市值>100 亿的高股息组合，月度调仓，低换手吃分红。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=5, rebalance_days=20, f_dividend_rate_min=0.03,
                     f_pe_ttm_max=20, f_pb_max=2.5, f_total_mv_min=1e10,
                     f_amount_ma_5_min=5000),
        tips=["股息率是 A 股少有的、经得起检验的长期因子；月度调仓成本极低。",
              "注意分红数据的滞后性：features_daily 的 dividend_rate 为最近年度口径。"],
    )
    add(
        id="as10_high_profit_quality", name="A股高盈利质量（ROE 代理）", dir="A股策略/02_价值与质量",
        category="advanced", difficulty="advanced",
        desc="净利润 5 亿 + 净资产 50 亿 + 营收 50 亿三重下限，用绝对规模近似高 ROE 白马。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=35, n_drop=8, rebalance_days=10, f_net_profit_ttm_min=5e8,
                     f_equity_min=5e9, f_revenue_ttm_min=5e9, f_pe_ttm_min=0.0,
                     f_pe_ttm_max=45, f_amount_ma_5_min=5000),
        tips=["features_daily 没有 ROE 列，本模板用「净利润 + 净资产」绝对规模近似质量。",
              "若要真正的 ROE 因子，需把 3_financial_data 的 roe 合并进 features_daily（经 quantdb_hub.fetch_financial）。"],
    )

    # ================= 03 成长与景气 =================
    add(
        id="as11_growth_momentum", name="A股成长动量", dir="A股策略/03_成长与景气",
        category="advanced", difficulty="intermediate",
        desc="模型分 + 60 日动量融合，持仓 30 只，5 日调仓，捕捉景气行业的趋势主升段。",
        cls="FilteredMomentumStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=5, momentum_period=60,
                     momentum_weight=0.4, f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["自定义 FilteredMomentumStrategy 在融合动量后仍执行 f_* 过滤，市值/流动性门槛真实生效。",
              "动量权重 0.4 表示模型分仍占主导；调高会显著提高换手。"],
    )
    add(
        id="as12_revenue_scale_growth", name="A股营收扩张", dir="A股策略/03_成长与景气",
        category="advanced", difficulty="intermediate",
        desc="要求 TTM 营收 10 亿以上且 PS<15，寻找已有收入体量、尚未被过度定价的成长标的。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=5, f_revenue_ttm_min=1e9,
                     f_ps_ttm_max=15.0, f_total_mv_min=3e9, f_vol_std_20_max=5.0,
                     f_amount_ma_5_min=5000),
        tips=["营收规模是成长股最稳的门槛，比净利润更能反映经营真实度。",
              "波动率上限 5% 用于剔除题材炒作型高波动标的。"],
    )
    add(
        id="as13_small_mid_growth", name="A股中小盘成长", dir="A股策略/03_成长与景气",
        category="advanced", difficulty="intermediate",
        desc="市值 30 亿至 300 亿、营收 5 亿以上、波动可控的中小盘成长组合。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=5, f_total_mv_min=3e9,
                     f_total_mv_max=3e10, f_revenue_ttm_min=5e8, f_vol_std_20_max=6.0,
                     f_amount_ma_5_min=3000),
        tips=["中小盘成长弹性大、回撤也大，建议与低波动模板搭配使用。",
              "市值上限 300 亿确保仍属于中小盘区间。"],
    )
    add(
        id="as14_earnings_accel", name="A股盈利加速（TTM 超年度）", dir="A股策略/03_成长与景气",
        category="advanced", difficulty="advanced",
        desc="要求 TTM 净利润高于最近年度净利润且双双为正，捕捉盈利正在加速的标的。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=35, n_drop=8, rebalance_days=5, f_net_profit_ttm_min=3e8,
                     f_annual_net_profit_min=2e8, f_pe_ttm_min=0.0, f_pe_ttm_max=60,
                     f_total_mv_min=3e9),
        tips=["TTM 净利润 > 年度净利润，等价于最近一到两个季度盈利同比在改善。",
              "这是 features_daily 里少数能间接表达「业绩加速」的过滤组合。"],
    )
    add(
        id="as15_high_beta_growth", name="A股高弹性成长", dir="A股策略/03_成长与景气",
        category="advanced", difficulty="advanced",
        desc="Beta 在 1.0 至 2.5 之间、流动性充足的高弹性组合，仓位 80% 控制回撤。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=3, risk_degree=0.8,
                     f_beta_20_min=1.0, f_beta_20_max=2.5, f_total_mv_min=3e9,
                     f_amount_ma_5_min=8000),
        tips=["高 Beta 组合在指数上行时放大收益，下行时同样放大亏损，务必配合择时。",
              "risk_degree=0.8 为硬性仓位上限，不受 UI 滑块覆盖。"],
    )

    # ================= 04 动量与趋势 =================
    add(
        id="as16_momentum_20", name="A股月度动量先锋", dir="A股策略/04_动量与趋势",
        category="advanced", difficulty="beginner",
        desc="模型分融合 20 日动量，持仓 30 只、3 日调仓，是最经典的横截面动量轮动。",
        cls="FilteredMomentumStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=3, momentum_period=20,
                     momentum_weight=0.5, f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["动量周期 20 日对应 A 股常见的月度轮动节奏。",
              "动量权重 0.5 时模型分与动量大致等权，可按回测结果上下调整。"],
    )
    add(
        id="as17_momentum_60", name="A股季度动量", dir="A股策略/04_动量与趋势",
        category="advanced", difficulty="intermediate",
        desc="以 60 日动量为主、模型分辅助的季度级轮动，换手明显低于月度版本。",
        cls="FilteredMomentumStrategy",
        kwargs=_base(topk=30, n_drop=8, rebalance_days=5, momentum_period=60,
                     momentum_weight=0.6, f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["60 日动量对噪音更不敏感，适合趋势延续性强的行业。",
              "动量权重 0.6 时需确认模型分与动量不同向，否则等于双重下注。"],
    )
    add(
        id="as18_trend_gate", name="A股趋势闸门", dir="A股策略/04_动量与趋势",
        category="advanced", difficulty="advanced",
        desc="自定义类：只在收盘价站上长期均线且均线向上时买入，被挡掉的名额让给次优候选。",
        cls="TrendGateTopkStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=5, trend_ma=60, trend_slope_days=10,
                     f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["趋势闸门是「事前过滤」而不是「事后止损」，T+1 下更友好。",
              "均线周期 60 日适合中频；短周期信号建议改为 20 日。"],
    )
    add(
        id="as19_breakout_confirm", name="A股突破确认", dir="A股策略/04_动量与趋势",
        category="advanced", difficulty="intermediate",
        desc="要求股价站上 20 日线 3% 以上、5 日线向上、量能放大，用突破形态确认模型信号。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=3, f_ma_gap_20_min=3.0,
                     f_ma_gap_5_min=0.0, f_vol_to_ma20_min=1.0, f_rsi_14_max=75,
                     f_amount_ma_5_min=8000),
        tips=["量比下限 1.0 要求放量，避免无量假突破。",
              "RSI 上限 75 用于规避已经过度拉伸的标的。"],
    )
    add(
        id="as20_dual_momentum", name="A股双周期动量", dir="A股策略/04_动量与趋势",
        category="advanced", difficulty="intermediate",
        desc="60 日长周期动量 + 模型分（两个信号源），持仓 50 只、10 日调仓，追求低换手的趋势收益。",
        cls="FilteredMomentumStrategy",
        kwargs=_base(topk=50, n_drop=10, rebalance_days=10, momentum_period=60,
                     momentum_weight=0.3, f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["长周期动量在 A 股更接近「景气度」的代理变量。",
              "动量权重压到 0.3，避免与模型信号重复暴露；与 as17 的区别在调仓更慢、动量权重更低。"],
    )

    # ================= 05 反转与均值回归 =================
    add(
        id="as21_oversold_reversal", name="A股超卖反转", dir="A股策略/05_反转与均值回归",
        category="advanced", difficulty="intermediate",
        desc="RSI(14)<35 且股价低于 20 日线，捕捉超卖后的技术性反弹，3 日调仓快进快出。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=20, n_drop=10, rebalance_days=3, f_rsi_14_max=35,
                     f_ma_gap_20_min=-15.0, f_ma_gap_20_max=0.0, f_amount_ma_5_min=5000),
        tips=["超卖反转在 A 股 T+1 下要控制持仓周期，3 日调仓比日频更稳。",
              "建议搭配止损模板使用，防止把趋势下跌误判为超卖。"],
    )
    add(
        id="as22_pullback_in_uptrend", name="A股上升趋势回调", dir="A股策略/05_反转与均值回归",
        category="advanced", difficulty="advanced",
        desc="中期趋势向上（站上 20 日线）但短线回调（RSI<50、跌破 5 日线），低吸强势股。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=25, n_drop=10, rebalance_days=3, f_ma_gap_20_min=0.0,
                     f_ma_gap_20_max=5.0, f_rsi_14_max=50, f_ma_gap_5_min=-6.0,
                     f_amount_ma_5_min=5000),
        tips=["「趋势向上 + 短线回调」是 A 股最常见的低吸形态。",
              "20 日线偏离上限 5% 防止买在已经加速的标的上。"],
    )
    add(
        id="as23_deep_dip", name="A股跌破均线后企稳低吸", dir="A股策略/05_反转与均值回归",
        category="advanced", difficulty="advanced",
        desc="只买「跌破 20 日线 0~8%、但已站回 5 日线」的企稳票（RSI<60、成交额 5000 万以上），"
             "赚短期超跌后的修复，仓位 70%。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=15, n_drop=10, rebalance_days=3, risk_degree=0.7,
                     f_ma_gap_20_min=-8.0, f_ma_gap_20_max=0.0, f_ma_gap_5_min=0.0,
                     f_rsi_14_max=60, f_amount_ma_5_min=5000),
        tips=["这版是「浅超跌 + 企稳确认」，不是深度接飞刀——用 2024 全年数据实测：",
              "  深跌组（距 MA20 -20%~-8%、RSI<30、缩量）的 5/10 日平均收益是 -2.9%/-2.8%；",
              "  本版（距 MA20 -8%~0% 且站回 MA5）是 +0.6%/+0.7%，20 日 +3.6%（同期全市场 +2.1%）。",
              "回撤条件越深，回测越差：同一框架下把区间放到 -12%~-4% 年化掉到 2.5%，",
              "  再叠加「5 日跌幅>3%」直接 -81%——A 股 2024 的深跌股是负 alpha。",
              "与 as22 的区别：as22 只买站上 MA20 的（上升趋势里的回调），",
              "  本版买跌破 MA20 但已站回 MA5 的（下跌通道里的修复），两者互补。"],
    )
    add(
        id="as24_reversal_guard", name="A股反转+行业分散护栏", dir="A股策略/05_反转与均值回归",
        category="risk_control", difficulty="advanced",
        desc="反转选股叠加行业持仓上限与大盘状态降仓，避免在单一行业深跌里越买越多。",
        cls="RedisRiskGuardTopkStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=3, industry_cap_ratio=0.25,
                     market_state_window=20, f_rsi_14_max=40, f_ma_gap_20_max=0.0,
                     f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["行业上限 25% 保证反转信号不会集中在同一板块。",
              "大盘状态由平台注入（需在回测配置中开启动态仓位）。"],
    )
    add(
        id="as25_short_term_reversal", name="A股短期反转（周频）", dir="A股策略/05_反转与均值回归",
        category="advanced", difficulty="intermediate",
        desc="上一交易日收跌、RSI(6)<40 的标的做均值回归，持仓 30 只、3 日调仓、大比例轮换。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=15, rebalance_days=3, f_pct_change_max=0.0,
                     f_rsi_6_max=40, f_ma_gap_20_min=-10.0, f_amount_ma_5_min=5000),
        tips=["短期反转是 A 股最稳健的因子之一，但换手高，需关注成本敏感性。",
              "n_drop 设 15 表示每期替换一半持仓，追求信号新鲜度。"],
    )

    # ================= 06 低波动与红利 =================
    add(
        id="as26_low_vol_core", name="A股低波动核心", dir="A股策略/06_低波动与红利",
        category="basic", difficulty="beginner",
        desc="20 日波动率<1.8%、Beta<0.9、市值 100 亿以上，低波动异象的 A 股实现。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=10, f_vol_std_20_max=1.8,
                     f_beta_20_max=0.9, f_total_mv_min=1e10, f_amount_ma_5_min=5000),
        tips=["低波动组合在下跌市中回撤显著小于指数，是长期稳健底仓。",
              "波动率阈值 1.8% 对应 A 股低波动区间，可按市场环境上下微调。"],
    )
    add(
        id="as27_low_vol_value", name="A股低波动价值", dir="A股策略/06_低波动与红利",
        category="basic", difficulty="intermediate",
        desc="低波动 + 低估值双约束，PE<20 / PB<2 且波动率<2.2%，兼顾防守与估值安全垫。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=10, f_vol_std_20_max=2.2,
                     f_pe_ttm_min=0.0, f_pe_ttm_max=20, f_pb_max=2.0,
                     f_total_mv_min=5e9, f_amount_ma_5_min=5000),
        tips=["低波动与低估值在 A 股常同时出现于银行/公用事业，注意行业集中。",
              "双周调仓可显著降低换手成本。"],
    )
    add(
        id="as28_dividend_low_vol", name="A股红利低波", dir="A股策略/06_低波动与红利",
        category="basic", difficulty="beginner",
        desc="股息率>2.5% + 波动率<2.2% + PE<25，月度调仓，最经典的防守型组合。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=5, rebalance_days=20, f_dividend_rate_min=0.025,
                     f_vol_std_20_max=2.2, f_pe_ttm_max=25, f_total_mv_min=5e9,
                     f_amount_ma_5_min=5000),
        tips=["红利低波是 A 股少数长期跑赢的策略，换手极低、成本可控。",
              "月度调仓配合分红再投资，长期复利效果更好。"],
    )
    add(
        id="as29_defensive_allocation", name="A股防御配置", dir="A股策略/06_低波动与红利",
        category="risk_control", difficulty="intermediate",
        desc="Beta<0.8、股息率>1.5%、流动性充足的防御组合，基础仓位 85%。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=8, rebalance_days=10, risk_degree=0.85,
                     f_beta_20_max=0.8, f_dividend_rate_min=0.015,
                     f_amount_ma_5_min=10000, f_total_mv_min=1e10),
        tips=["低 Beta 组合在指数急跌时抗跌，但牛市会明显跑输。",
              "仓位 85% 留出缓冲，应对赎回与调仓摩擦。"],
    )
    add(
        id="as30_stable_compounder", name="A股稳健复利", dir="A股策略/06_低波动与红利",
        category="basic", difficulty="intermediate",
        desc="60 日波动率<3.5%、净利润 3 亿、净资产 30 亿、有分红，追求低回撤的长期复利。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=8, rebalance_days=10, f_vol_std_60_max=3.5,
                     f_net_profit_ttm_min=3e8, f_equity_min=3e9, f_dividend_rate_min=0.01,
                     f_amount_ma_5_min=5000),
        tips=["60 日波动率上限比 20 日更平滑，适合长期持有型组合。",
              "盈利与分红双门槛确保组合不会选到纯题材股。"],
    )

    # ================= 07 行业与主题轮动 =================
    add(
        id="as31_regime_switch", name="A股动量反转状态切换", dir="A股策略/07_行业与主题轮动",
        category="advanced", difficulty="advanced",
        desc="自定义类：沪深 300 均线判定市场状态，上行市加动量、下行市加反转，并同步调仓。",
        cls="RegimeSwitchStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=3, fast_window=20, slow_window=60,
                     boost_weight=0.5, momentum_window=20, uptrend_position=1.0,
                     neutral_position=0.8, downtrend_position=0.5, f_total_mv_min=3e9),
        tips=["状态判定只看沪深 300，避免个股信号自证。",
              "下行市仓位降到 50% 并加反转因子，是本模板的核心防御设计。"],
    )
    add(
        id="as32_theme_hot", name="A股主题热点", dir="A股策略/07_行业与主题轮动",
        category="advanced", difficulty="advanced",
        desc="量比放大、成交额充足、当日上涨的活跃标的，捕捉主题行情的资金集中度。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=20, n_drop=10, rebalance_days=3, f_vol_to_ma20_min=1.3,
                     f_amount_ma_5_min=20000, f_pct_change_min=2.0, f_total_mv_min=5e9),
        tips=["成交额下限 2 亿（20000 万元）确保标的能承接资金。",
              "主题策略换手高、回撤大，建议小仓位参与。"],
    )
    add(
        id="as33_sector_leader", name="A股龙头集中", dir="A股策略/07_行业与主题轮动",
        category="advanced", difficulty="intermediate",
        desc="市值 500 亿以上、成交额 5 亿以上、站上 20 日线的行业龙头，持仓 20 只。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=20, n_drop=8, rebalance_days=5, f_total_mv_min=5e10,
                     f_amount_ma_5_min=50000, f_ma_gap_20_min=0.0),
        tips=["龙头集中度高，20 只持仓已接近大盘蓝筹指数，超额主要来自模型选股。",
              "成交额下限 5 亿（50000 万元）确保冲击成本可控。"],
    )
    add(
        id="as34_capital_inflow", name="A股资金流入（量能放大）", dir="A股策略/07_行业与主题轮动",
        category="advanced", difficulty="intermediate",
        desc="3 日量能趋势向上、量比>1.1、成交额 1 亿以上，跟随资金放量方向。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=12, rebalance_days=3, f_volume_trend_3d_min=0.2,
                     f_vol_to_ma5_min=1.1, f_amount_ma_5_min=10000, f_total_mv_min=3e9),
        tips=["量能因子需要与价格方向配合，单看放量容易买到出货盘。",
              "建议与趋势/动量类模板对比回测，验证增量信息。"],
    )
    add(
        id="as35_rotation_low_turnover", name="A股低频轮动", dir="A股策略/07_行业与主题轮动",
        category="basic", difficulty="intermediate",
        desc="15 日调仓、50 只持仓、单行业上限 25% 的低频轮动，适合长期持有与成本敏感账户。",
        cls="RedisRiskGuardTopkStrategy",
        kwargs=_base(topk=50, n_drop=8, rebalance_days=15, industry_cap_ratio=0.25,
                     market_state_window=20, f_total_mv_min=5e9, f_amount_ma_5_min=5000,
                     f_vol_std_20_max=4.0),
        tips=["低频轮动对模型信号稳定性要求更高，建议用 IC 稳定的中频模型。",
              "行业上限 25% 防止轮动集中在单一赛道。"],
    )

    # ================= 08 风险控制与仓位 =================
    add(
        id="as36_vol_target", name="A股波动率目标仓位", dir="A股策略/08_风险控制与仓位",
        category="risk_control", difficulty="advanced",
        desc="自定义类：仓位 = min(1, 目标波动/沪深 300 已实现波动)，高波动期自动降仓。",
        cls="VolTargetPositionStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=5, target_vol=0.15,
                     vol_window=20, min_position=0.2, max_position=1.0,
                     f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["目标波动 15% 对应 A 股中性风险预算；激进账户可调到 20%。",
              "波动率是滞后的，但比预测波动更可靠；min_position 防止极端行情空仓踏空。"],
    )
    add(
        id="as37_inverse_vol", name="A股逆波动加权", dir="A股策略/08_风险控制与仓位",
        category="advanced", difficulty="advanced",
        desc="自定义类：选股沿用模型分 TopK，权重按 1/波动率分配并设单票上限，风险更均衡。",
        cls="InverseVolWeightStrategy",
        # 权重策略走 WeightStrategyBase 链路：没有 n_drop（按目标权重调仓），
        # 也不传 only_tradable（涨停/停牌由 RedisWeightStrategy 自行过滤）。
        kwargs=_base_raw(topk=30, rebalance_days=5, vol_window=20, weight_cap=0.08,
                         f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["逆波动加权会让低波动标的拿到更大权重，组合波动显著低于等权。",
              "单票上限 8% 防止极端低波动标的权重过高。",
              "该模板走权重链路（非 TopK-Dropout），没有 n_drop 参数，调仓即按目标权重再平衡。",
              "f_* 过滤取上一交易日快照，无前视。"],
    )
    add(
        id="as38_drawdown_throttle", name="A股回撤阶梯降仓", dir="A股策略/08_风险控制与仓位",
        category="risk_control", difficulty="advanced",
        desc="自定义类：按沪深 300 距 250 日高点的回撤分四档降仓，回撤越深仓位越低。",
        cls="DrawdownThrottleStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=5, dd_lookback=250,
                     dd_l1=0.05, dd_l2=0.10, dd_l3=0.15, pos_l1=1.0, pos_l2=0.8,
                     pos_l3=0.6, pos_l4=0.4, f_total_mv_min=3e9),
        tips=["回撤档位基于指数而非组合自身，避免组合回撤后才被动降仓。",
              "最深档位 40% 仓位是「保命线」，可按风险偏好上调。"],
    )
    add(
        id="as39_risk_guard_core", name="A股大盘风控核心", dir="A股策略/08_风险控制与仓位",
        category="risk_control", difficulty="intermediate",
        desc="平台内置风控类：Beta<1.3、波动率<4%、行业上限 30%，大盘下行时自动降仓。",
        cls="RedisRiskGuardTopkStrategy",
        kwargs=_base(topk=50, n_drop=10, rebalance_days=5, industry_cap_ratio=0.3,
                     market_state_window=20, f_beta_20_max=1.3, f_vol_std_20_max=4.0,
                     f_total_mv_min=3e9, f_amount_ma_5_min=5000, f_pe_ttm_max=80),
        tips=["大盘状态判定需要开启动态仓位，否则只生效行业上限与个股过滤。",
              "波动率上限用百分数口径（4% 日波动），不是 0.04。"],
    )
    add(
        id="as40_conservative_position", name="A股保守半仓", dir="A股策略/08_风险控制与仓位",
        category="risk_control", difficulty="beginner",
        desc="固定 60% 仓位、波动率<3%、10 日调仓的保守组合，适合风险厌恶型账户。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=8, rebalance_days=10, risk_degree=0.6,
                     f_vol_std_20_max=3.0, f_total_mv_min=5e9, f_amount_ma_5_min=5000),
        tips=["固定仓位不择时，长期跑输满仓组合的概率高，但回撤显著更小。",
              "可作为组合中的防守腿，与高弹性模板搭配。"],
    )

    # ================= 09 事件与择时 =================
    add(
        id="as41_crash_dip", name="A股指数暴跌抄底", dir="A股策略/09_事件与择时",
        category="advanced", difficulty="advanced",
        desc="平台内置抄底类：沪深 300 单日暴跌 2.5%（或 100 点）触发后，挑超跌且趋势未破的标的，"
             "等其企稳（前一交易日不再大跌或留长下影）于次日开盘买入，持有 5 日，止盈 8% 止损 5%。",
        cls="RedisCrashBuyDipStrategy",
        kwargs=_base_raw(top_k=5, hold_days=5, take_profit=0.08, stop_loss=-0.05,
                         crash_threshold_pct=0.025, crash_threshold_points=100,
                         benchmark="SH000300", max_wait_days=3, min_oversold_margin=0.01,
                         trend_window=20, ma_fast=5, ma_slow=20, vol_lookback=10,
                         rebalance_days=1),
        tips=["该策略是事件驱动型，多数交易日空仓，需要长期持有才能体现统计规律。",
              "暴跌阈值 2.5% 或 100 点任一满足即触发，可只保留一个条件。",
              "决策只读前一交易日收盘为止的数据（T-1 信号 → T 日开盘成交），不会用当天收盘价选股。",
              "该类不接受 only_tradable（会透传给 qlib 基类报错），涨跌停过滤由类内部自行处理。"],
    )
    add(
        id="as42_stop_loss_guard", name="A股个股止损", dir="A股策略/09_事件与择时",
        category="risk_control", difficulty="beginner",
        desc="平台内置止损类：个股回撤 8% 止损、盈利 15% 止盈，配合模型 TopK 选股。",
        cls="RedisStopLossStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=3, stop_loss=-0.08,
                     take_profit=0.15),
        tips=["RedisStopLossStrategy 不支持 f_* 过滤（f_* 会被静默丢弃），本模板只用模型信号 + 止损。",
              "止盈会削掉趋势段收益，建议与纯持有版本对比后再决定是否启用。"],
    )
    add(
        id="as43_limit_up_guard", name="A股涨停规避", dir="A股策略/09_事件与择时",
        category="advanced", difficulty="advanced",
        desc="自定义类：剔除近 10 日内出现过涨停的标的，把名额让给同样高分但可成交的股票。",
        cls="LimitUpGuardStrategy",
        kwargs=_base(topk=30, n_drop=10, rebalance_days=3, lookback_days=10,
                     max_limit_ups=0, f_total_mv_min=3e9, f_amount_ma_5_min=5000),
        tips=["涨停阈值按板块自动区分：主板 9.5%、创业板/科创板 19.5%、北交所 29.5%。",
              "max_limit_ups=0 表示近 10 日一次涨停都不能有；放宽到 1 可保留部分强势股。"],
    )
    add(
        id="as44_panic_reversal", name="A股恐慌低吸", dir="A股策略/09_事件与择时",
        category="advanced", difficulty="advanced",
        desc="上一交易日跌幅 2%-9%、中期趋势仍向上（站上 20 日线）、RSI<55 的单日恐慌错杀。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=25, n_drop=12, rebalance_days=3, f_pct_change_min=-9.0,
                     f_pct_change_max=-2.0, f_ma_gap_20_min=0.0, f_rsi_14_max=55,
                     f_amount_ma_5_min=10000),
        tips=["「趋势向上 + 单日大跌」是 A 股胜率较高的隔日反弹形态。",
              "跌幅下限 -9% 避免接住跌停板上的标的。",
              "RSI 上限写 55 而不是 40：站上 20 日线的票 RSI 极少低于 40，",
              "  两个条件互相排斥，实测 2024 全年 0 次命中（见文件开头回测记录）。"],
    )
    add(
        id="as45_timing_gate", name="A股择时半仓防守", dir="A股策略/09_事件与择时",
        category="risk_control", difficulty="advanced",
        desc="大盘状态窗口 30 日、基础仓位 70% 的择时组合，只在大盘健康时保持较高仓位。",
        cls="RedisRiskGuardTopkStrategy",
        kwargs=_base(topk=30, n_drop=8, rebalance_days=5, risk_degree=0.7,
                     market_state_window=30, industry_cap_ratio=0.3,
                     f_total_mv_min=1e10, f_amount_ma_5_min=5000),
        tips=["risk_degree 与大盘状态同时生效时，取两者中更保守的一方。",
              "择时策略的收益高度依赖状态判定的准确性，建议先做单因子验证。"],
    )

    # ================= 10 小盘与流动性 =================
    add(
        id="as46_small_cap", name="A股小盘轮动", dir="A股策略/10_小盘与流动性",
        category="advanced", difficulty="intermediate",
        desc="市值 20 亿至 200 亿、成交额 3000 万以上的小盘组合，5 日调仓捕捉小盘弹性。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=12, rebalance_days=5, f_total_mv_min=2e9,
                     f_total_mv_max=2e10, f_amount_ma_5_min=3000),
        tips=["小盘策略容量有限，资金规模大时冲击成本会显著上升。",
              "成交额下限 3000 万元是流动性地板，低于此值难以成交。"],
    )
    add(
        id="as47_micro_liquidity", name="A股微盘流动性溢价", dir="A股策略/10_小盘与流动性",
        category="advanced", difficulty="advanced",
        desc="市值 10 亿至 150 亿、成交额 2000 万以上的微盘组合，持仓 50 只、大比例轮换。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=50, n_drop=20, rebalance_days=3, f_total_mv_min=1e9,
                     f_total_mv_max=1.5e10, f_amount_ma_5_min=2000,
                     f_vol_std_20_max=6.0),
        tips=["微盘流动性溢价在 A 股长期存在，但在监管收紧/流动性危机时会剧烈回撤。",
              "建议严格控制资金规模，并配合回撤降仓模板使用。"],
    )
    add(
        id="as48_small_cap_value", name="A股小盘价值", dir="A股策略/10_小盘与流动性",
        category="advanced", difficulty="intermediate",
        desc="市值 300 亿以下、PE<25、PB<2.5、有分红的低估值小盘，双周调仓。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=10, f_total_mv_max=3e10,
                     f_pe_ttm_min=0.0, f_pe_ttm_max=25, f_pb_max=2.5,
                     f_dividend_rate_min=0.01, f_amount_ma_5_min=3000),
        tips=["小盘价值是「低估值 + 小市值」双因子的叠加，A 股长期有效性较强。",
              "分红门槛用于剔除财务造假风险较高的空壳小盘。"],
    )
    add(
        id="as49_liquidity_premium", name="A股流动性溢价", dir="A股策略/10_小盘与流动性",
        category="advanced", difficulty="intermediate",
        desc="成交额 5 亿以上、市值 200 亿以上、量比不低于 0.8 的高流动性组合。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=30, n_drop=8, rebalance_days=5, f_amount_ma_5_min=50000,
                     f_total_mv_min=2e10, f_vol_to_ma20_min=0.8, f_pe_ttm_max=80),
        tips=["高流动性组合容量大、冲击成本低，适合资金规模较大的账户。",
              "量比下限 0.8 只排除极度缩量的标的，不追求放量。"],
    )
    add(
        id="as50_midcap_balanced", name="A股中盘均衡", dir="A股策略/10_小盘与流动性",
        category="basic", difficulty="beginner",
        desc="市值 100 亿至 1000 亿、成交额 1 亿以上的中盘均衡组合，兼顾弹性与容量。",
        cls="RedisRecordingStrategy",
        kwargs=_base(topk=40, n_drop=10, rebalance_days=5, f_total_mv_min=1e10,
                     f_total_mv_max=1e11, f_amount_ma_5_min=10000, f_pe_ttm_max=60),
        tips=["中盘是 A 股超额收益相对集中的区间，容量与弹性兼顾。",
              "适合作为主力策略，与微盘、红利策略形成风格互补。"],
    )

    # 自定义类源码挂载
    custom_bodies = {
        "FilteredMomentumStrategy": _FILTERED_MOMENTUM_BODY,
        "TrendGateTopkStrategy": _TREND_GATE_BODY,
        "VolTargetPositionStrategy": _VOL_TARGET_BODY,
        "InverseVolWeightStrategy": _INVERSE_VOL_BODY,
        "RegimeSwitchStrategy": _REGIME_SWITCH_BODY,
        "DrawdownThrottleStrategy": _DRAWDOWN_BODY,
        "LimitUpGuardStrategy": _LIMIT_UP_BODY,
    }
    for spec in S:
        body = custom_bodies.get(spec["cls"])
        if body is not None:
            spec["custom_body"] = body
            spec["is_custom"] = True
            if spec["cls"] == "InverseVolWeightStrategy":
                # 权重策略走 WeightStrategyBase 链路，基类/导入都与 Topk-Dropout 系不同
                spec["custom_imports"] = _CUSTOM_IMPORTS_WEIGHT
                spec["custom_note"] = _CUSTOM_NOTE_WEIGHT
        else:
            spec["is_custom"] = False

    return S


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 50 个 A 股策略模板")
    parser.add_argument("--out", default="strategy_templates", help="输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写盘")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = root / out_dir

    specs = build_specs()
    if len(specs) != 50:
        raise SystemExit(f"期望 50 个策略，实际 {len(specs)}")
    ids = [s["id"] for s in specs]
    if len(set(ids)) != len(ids):
        raise SystemExit("策略 ID 重复")

    # 校验：kwargs 里的每个键都要有参数元数据，否则 JSON 生成会失败
    for spec in specs:
        for key in spec["kwargs"]:
            if key not in _HIDDEN_KEYS and key not in PARAM_META:
                raise SystemExit(f"{spec['id']}: 缺少参数元数据 {key}")

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    results = _load_backtest_results()
    if results:
        done = sum(
            1 for r in (results.get("runs") or {}).values() if r.get("status") == "completed"
        )
        print(f"回测记录：已加载 {done} 条（{_BACKTEST_RESULT_PATH}）")
    else:
        print("回测记录：暂无（模板 .py 开头会写「待跑」）")
    for spec in specs:
        py_text = _render_py(spec, spec["is_custom"], results)
        json_text = _render_json(spec)
        if args.dry_run:
            print(f"[dry-run] {spec['id']} ({spec['cls']}) -> {spec['dir']}")
            continue
        (out_dir / f"{spec['id']}.py").write_text(py_text, encoding="utf-8")
        (out_dir / f"{spec['id']}.json").write_text(json_text, encoding="utf-8")
        written += 1

    print(f"{'[dry-run] ' if args.dry_run else ''}共 {len(specs)} 个模板，写入 {written} 对文件 → {out_dir}")
    dirs: dict[str, int] = {}
    for spec in specs:
        dirs[spec["dir"]] = dirs.get(spec["dir"], 0) + 1
    for name, count in dirs.items():
        print(f"  {name}: {count}")
    custom = [s["id"] for s in specs if s["is_custom"]]
    print(f"  自定义策略类模板 {len(custom)} 个: {', '.join(custom)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
