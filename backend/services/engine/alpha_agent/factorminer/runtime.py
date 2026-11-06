"""FM 因子的生产运行时：取数 → 字段帧 → 求值 → 窗口长序列。

T-FM-01 的取数/组帧管线（``scripts/alpha_agent/run_paper_factor_test.py``）
在此收敛为**唯一实现**：runner 反向调用本模块（SystemExit 包装保持不变），
因子池里 ``fm:`` 前缀的表达式经 ``routers/alpha_agent`` 的
``_run_expression_factor_subprocess`` 子进程调 ``evaluate_window``——两条
链路（实验与生产）共用同一套 vwap 口径 / $returns 价差法 / 暖机语义，
不可能两说。

生产取数口径（与挖掘侧 ``panel.warmup_start`` 同款）：

- ``fetch_start`` = 窗口起点沿交易日历回退 ``dsl.min_history(expr)`` 个
  bar（不够则日历首日起）——窗口内每根 bar 的滚动窗都是满窗；
- 每个标的「自取数首格起算 position < min_history」的格置 NaN，
  复现 qlib 表达式路径「暖机不足不出值」的观感（FM 求值器单序列滚动是
  ``min_periods=1``，不掩的话窗口头部/新上市标的会提前出值，两边口径
  不一致）；
- 输出长序列 index=(trade_date, symbol)，覆盖窗口内**全部**面板格
  （缺格 NaN），与 ``panel.load_price_panel`` 的网格约定一致。

当前面向 A 股（论文因子落库为 a_share；``panel._qlib_features`` 按 CN
region 初始化 qlib）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import numpy as np
import pandas as pd

from ..statistical import panel as panel_mod
from . import dsl
from .evaluator import evaluate

logger = logging.getLogger(__name__)

#: vwap 合理性覆盖率下限（低于即单位口径有问题——volume 股 / amount 万元）
VWAP_COVERAGE_FLOOR = 0.99

#: 日历回退余量：min_history×2 + 40 天足够覆盖节假日，回退不足由
#: ``warmup_start`` 在日历首日钳位兜底。
_CALENDAR_BACKOFF_FACTOR = 2
_CALENDAR_BACKOFF_DAYS = 40


class VwapConventionError(ValueError):
    """$vwap 合理性覆盖率过低（单位口径错）；runner 把它翻成 SystemExit。"""


def normalize_single(raw: pd.DataFrame, field: str) -> pd.Series:
    """单列 qlib 取数 → 标准长序列（index=(trade_date, symbol)）。

    与 ``panel.normalize_panel`` 同一套 index 约定（raw 的 index 是
    ``(instrument, datetime)``，reset 后前两列即 symbol/trade_date）。
    """
    frame = raw[[field]].rename(columns={field: "value"}).reset_index()
    keys = list(frame.columns[:2])
    frame = frame.rename(columns={keys[0]: "symbol", keys[1]: "trade_date"})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["symbol"] = frame["symbol"].astype(str)
    return frame.set_index(["trade_date", "symbol"])["value"].astype(float).sort_index()


def build_field_frames(
    price_full: pd.DataFrame, factor_long: pd.Series
) -> tuple[dict[str, pd.DataFrame], dict]:
    """长面板 → FM-DSL 的 8 个字段宽面 + vwap 口径判定记录。

    （自 runner 原样迁移；覆盖率守卫由 SystemExit 改为
    ``VwapConventionError``，CLI 出口由 runner 包装。）
    """

    def wide(long_series: pd.Series) -> pd.DataFrame:
        return long_series.unstack(level="symbol").sort_index(axis=0).sort_index(axis=1)

    close = wide(price_full["close"])
    volume = wide(price_full["volume"])
    amount = wide(price_full["amount"])
    factor = wide(factor_long).reindex(columns=close.columns)
    vm = (amount * 1e4 / volume.where(volume > 0)).replace([np.inf, -np.inf], np.nan)
    high, low = wide(price_full["high"]), wide(price_full["low"])

    def coverage(vwap: pd.DataFrame) -> float:
        valid = vwap.notna() & low.notna() & high.notna()
        inside = valid & (vwap >= low - 1e-6) & (vwap <= high + 1e-6)
        return float(inside.to_numpy().sum() / max(1, valid.to_numpy().sum()))

    cov_factor = coverage(vm * factor)
    cov_raw = coverage(vm)
    use_factor = cov_factor >= cov_raw
    vwap = vm * factor if use_factor else vm
    info = {
        "vwap_convention": "amount/volume×$factor" if use_factor else "amount/volume",
        "vwap_coverage": cov_factor if use_factor else cov_raw,
        "vwap_coverage_with_factor": cov_factor,
        "vwap_coverage_without_factor": cov_raw,
    }
    if info["vwap_coverage"] < VWAP_COVERAGE_FLOOR:
        raise VwapConventionError(
            f"$vwap 合理性覆盖率过低（×factor={cov_factor:.4f}，"
            f"不带 factor={cov_raw:.4f}，下限 {VWAP_COVERAGE_FLOOR}）——"
            "单位口径有问题（volume 股 / amount 万元），vwap 类因子会整体"
            "失真，先修数据口径再跑。"
        )
    # $returns 与仓库候选同式（candidates.py `_ret = close/close.shift(1) - 1`）：
    # 在长面板上逐标的算再展开成宽面——停牌缺口复牌首日=整段收益，且不产生
    # 合成 0 收益（早前先在宽面上 ffill().pct_change() 会把停牌格填 0，注入
    # 波动/偏度窗口）。价差法同时绕开 pandas 2/3 的 pct_change fill_method 漂移。
    close_long = price_full["close"]
    returns_long = close_long / close_long.groupby(level="symbol").shift(1) - 1.0
    frames = {
        "$open": wide(price_full["open"]),
        "$high": high,
        "$low": low,
        "$close": close,
        "$volume": volume,
        "$amt": amount,
        "$vwap": vwap,
        "$returns": wide(returns_long),
    }
    return frames, info


def evaluate_window(
    expression: str,
    *,
    instruments,
    start: str,
    end: str,
    qlib_dir: str | None = None,
    loader=None,
    calendar_fn=None,
) -> pd.Series:
    """求值 ``fm:`` 表达式 → 窗口内全网格长序列（index=(trade_date, symbol)）。

    ``instruments`` 接受标的列表（qlib 式小写代码）或 qlib 配置字典
    （``D.instruments`` 的返回形态，自动经 ``list_instruments`` 解析）。

    ``loader``/``calendar_fn`` 可注入（测试）：loader 签名与
    ``panel.load_price_panel`` 约定一致 ``(instruments, fields, start, end)``。
    """
    if expression.startswith(dsl.FM_CODE_PREFIX):
        expression = expression[len(dsl.FM_CODE_PREFIX) :]
    node = dsl.parse(expression)
    warmup = dsl.min_history(node)
    qlib_dir = qlib_dir or panel_mod.DEFAULT_QLIB_DIR
    if isinstance(instruments, Mapping):
        # qlib 配置字典（``D.instruments(market=…)`` 的返回形态，路由器
        # 原样透传）——直接迭代只会拿到 "market"/"filter_pipe" 两个键名，
        # 整池静默取空。真列表在 ``list_instruments(cfg, as_list=True)``，
        # 同 ``panel.qlib_instruments`` 已钉的两步坑。
        import qlib  # noqa: PLC0415
        import qlib.data as qd  # noqa: PLC0415
        from qlib.constant import REG_CN  # noqa: PLC0415

        qlib.init(provider_uri=qlib_dir, region=REG_CN)
        instruments = list(qd.D.list_instruments(instruments, as_list=True))
    symbols = [str(item) for item in (instruments or [])]
    if not symbols:
        raise ValueError("标的池为空——FM 表达式求值至少需要 1 只标的")
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if end_ts < start_ts:
        raise ValueError(f"窗口颠倒：start={start} end={end}")
    ext_start = (
        start_ts
        - pd.Timedelta(days=warmup * _CALENDAR_BACKOFF_FACTOR + _CALENDAR_BACKOFF_DAYS)
    ).strftime("%Y-%m-%d")
    if calendar_fn is None:
        calendar = panel_mod.qlib_calendar(ext_start, end, qlib_dir=qlib_dir)
    else:
        calendar = calendar_fn(ext_start, end)
    if len(calendar) == 0:
        raise ValueError(f"交易日历为空（{ext_start}~{end}）——检查 qlib 缓存")
    fetch_start = pd.Timestamp(panel_mod.warmup_start(start, calendar, warmup))
    fetch_start_text = fetch_start.strftime("%Y-%m-%d")
    fields = [*panel_mod.PRICE_FIELDS, "$factor"]
    if loader is None:
        raw = panel_mod._qlib_features(symbols, fields, fetch_start_text, end, qlib_dir)
    else:
        raw = loader(symbols, fields, fetch_start_text, end)
    price_full = panel_mod.normalize_panel(raw)
    if price_full.empty:
        # 取数整片为空时，后面 vwap 覆盖率会算成 0.0 并抛
        # VwapConventionError——把「没取到数」误诊成「单位口径有问题」。
        # 空取数的最常见成因是标的池形态错（配置字典当真列表、代码前缀/
        # 后缀式不符），给专门的错误信息。
        raise ValueError(
            f"取数为空：{len(symbols)} 只标的在 {fetch_start_text}~{end} "
            f"无任何行情——先查标的池（qlib 式小写代码，如 sh600036）与 "
            f"qlib_dir（{qlib_dir}），不是 vwap 单位问题。"
        )
    factor_long = normalize_single(raw, "$factor")
    frames, vwap_info = build_field_frames(price_full, factor_long)
    logger.info(
        "FM 求值 %s~%s（暖机 %d bar，fetch_start=%s）：vwap 口径=%s，覆盖率 %.4f",
        start,
        end,
        warmup,
        fetch_start_text,
        vwap_info["vwap_convention"],
        vwap_info["vwap_coverage"],
    )
    wide = evaluate(node, frames)
    wide = wide.loc[start_ts:end_ts]
    # pandas 3 的 stack 不再吃 dropna 参数（新实现全组合展开，缺格即 NaN；
    # pandas 2 的 dropna=True 丢格）——两种行为在 reindex 后等价，故不传参
    long = wide.stack()
    long.index = long.index.set_names(["trade_date", "symbol"])
    panel_index = price_full.index
    dates = panel_index.get_level_values(0)
    in_win = np.asarray(dates >= start_ts) & np.asarray(dates <= end_ts)
    base_index = panel_index[in_win]
    out = long.reindex(base_index)
    # 引擎同款头掩码：position 自该标的取数首格起算（与挖掘侧
    # `panel.groupby(level=1).cumcount()` 一致），暖机不足的格不给值。
    position = price_full.groupby(level=1).cumcount().to_numpy()[in_win]
    out = out.mask(position < warmup)
    out.name = "factor"
    return out
