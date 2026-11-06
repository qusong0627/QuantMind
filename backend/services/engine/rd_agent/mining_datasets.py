"""挖掘数据集注册表（单源，叶子模块：只依赖标准库）。

背景：H5 时代挖掘数据面（``rd_loop_wrapper._generate_h5_from_parquet``，已退役）
把 QuantDB 的预计算列并入 daily_pv.h5 供 LLM 因子表达式直接引用（``$<输出列名>``）。
2026-10 之前这份白名单写死在 ``rd_loop_wrapper``（three 组：features_daily /
l1_factors / l2_factors），任务无法选择；现在数据集选择要能**真正控制喂给
LLM 的特征**（移植 P1），因此把映射抽到本模块做单源：

- 提示面（``market_adapters/a_share.py`` 的 ``_l2_referenceable_columns`` 等）
- 数据面（因子代码经 ``quantdb_data.daily(数据集, [列])`` 按需读取）
- 校验面（``alpha_agent.mining_config`` 的请求校验与 UI 目录）

三方共用同一份 key/映射，避免"提示里能引用、读不到"或反向的漂移。

``l2_factors`` 的映射是 ``None``：它的列清单现场读盘（历史教训：parquet
物理列序随日漂移，依赖静态名单会与上游发布漂移），由本模块
``l2_factor_columns`` 单源提供。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

#: features_daily 数据集富化列：{源列名: 输出列名}（经
#: ``quantdb_data.daily('features_daily', [...])`` 读取，如 ``turn_20``）。
FEATURES_DAILY: dict[str, str] = {
    "rsi_14": "rsi_14",
    "macd_hist": "macd_hist",
    "vol_atr_14": "atr_14",
    "beta_20": "beta_20",
    "pe_ttm": "pe_ttm",
    "pb": "pb",
    "ps_ttm": "ps_ttm",
    "dividend_rate": "div_yield",
    "total_mv": "total_mv",
    "float_mv": "float_mv",
    "net_profit_ttm": "np_ttm",
}

#: l1_factors 数据集富化列（量价衍生/资金流/筹码/风格）。
L1_FACTORS: dict[str, str] = {
    "turn_5": "turn_5",
    "turn_20": "turn_20",
    "turn_z_20": "turn_z_20",
    "amt_net_flow_5": "netflow_5",
    "amt_net_flow_20": "netflow_20",
    "mfi_14": "mfi_14",
    "obv_slope_20": "obv_slope",
    "vol_parkinson_20": "parkinson_20",
    "tech_bb_width": "bb_width",
    "tech_bb_pos": "bb_pos",
    "tech_adx_14": "adx_14",
    "tech_max_drawdown_20": "maxdd_20",
    "fun_bp": "bp",
    "fun_ep": "ep",
    "fun_roe": "roe",
    "fun_peg": "peg",
    "fun_np_growth": "np_growth",
    "chip_profit_ratio_20": "chip_profit_20",
    "style_idio_vol_20": "idio_vol_20",
    "ind_strength_20": "ind_strength",
    "concept_hot_score": "concept_hot",
}

#: L2 微观结构因子数据集 key（列清单动态，见模块 docstring）。
L2_DATASET = "l2_factors"

#: 数据集 key → 富化映射；``None`` = 动态列集（现场读盘）。
ENRICH_BY_DATASET: dict[str, dict[str, str] | None] = {
    "features_daily": FEATURES_DAILY,
    "l1_factors": L1_FACTORS,
    L2_DATASET: None,
    # Composite and adjacent QuantDB sources are fetched on demand through qd.daily();
    # they are deliberately not materialized into the shared price panel.
    "l1_l2_factors": None,
    "technical_indicators": {
        "ma5": "ma5",
        "ma10": "ma10",
        "ma20": "ma20",
        "ma60": "ma60",
        "ma_gap_20": "ma_gap_20",
        "rsi_6": "rsi_6",
        "rsi_14": "rsi_14",
        "kdj_k": "kdj_k",
        "kdj_d": "kdj_d",
    },
    "valuation": {
        "pe_ttm": "pe_ttm",
        "pe_static": "pe_static",
        "pb": "pb",
        "total_mv": "total_mv",
        "float_mv": "float_mv",
        "net_profit_ttm": "np_ttm",
    },
    "market_sentiment": {
        "buy_pressure": "buy_pressure",
        "sell_pressure": "sell_pressure",
        "momentum_1d": "sent_mom_1d",
        "liquidity_score": "liquidity_score",
        "intraday_vol": "intraday_vol",
        "amount_per_trade": "amount_per_trade",
    },
    "margin_trading": None,
    "balance": None,
    "income": None,
    "cashflow": None,
    "capital": None,
    "per_share": None,
    "pershare_index": None,
    "dividend_factors": None,
    "holder_num": None,
    "min1_kline": None,
    "min5_kline": None,
    "index_daily": None,
    "etf_kline": None,
}

#: 可选的 QuantDB 挖掘数据源（顺序即 UI 展示顺序）。日频宽表由 quantdb_data.daily
#: 按需读取；财务表由 financial 按公告日对齐；分钟线由 minute 限定标的读取。
SUPPORTED_DATASETS: tuple[str, ...] = (
    "features_daily",
    "l1_factors",
    L2_DATASET,
    "l1_l2_factors",
    "technical_indicators",
    "valuation",
    "market_sentiment",
    "margin_trading",
    "balance",
    "income",
    "cashflow",
    "capital",
    "per_share",
    "pershare_index",
    "dividend_factors",
    "holder_num",
    "min1_kline",
    "min5_kline",
    "index_daily",
    "etf_kline",
)


def panel_cache_key(datasets) -> str:
    """Only legacy panel enrichment affects the cache; SDK tables stay external."""
    selected = resolve_datasets(datasets)
    return (
        "-".join(key for key in ("features_daily", "l1_factors") if key in selected)
        or "base-v1"
    )


#: key -> QuantDB 相对路径；供快照、数据目录和取数 SDK 共用。
DATASET_REL_PATHS: dict[str, str] = {
    "features_daily": "6_ml_datasets/features_daily",
    "l1_factors": "6_ml_datasets/l1_factors",
    "l2_factors": "6_ml_datasets/l2_factors",
    "l1_l2_factors": "6_ml_datasets/l1_l2_factors",
    "technical_indicators": "5_technical_derived/technical_indicators",
    "valuation": "5_technical_derived/valuation",
    "market_sentiment": "5_technical_derived/market_sentiment",
    "margin_trading": "2_base_sector/margin_trading",
    "balance": "3_financial_data/balance",
    "income": "3_financial_data/income",
    "cashflow": "3_financial_data/cashflow",
    "capital": "3_financial_data/capital",
    "per_share": "3_financial_data/per_share",
    "pershare_index": "3_financial_data/pershare_index",
    "dividend_factors": "3_financial_data/dividend_factors",
    "holder_num": "3_financial_data/holder_num",
    "min1_kline": "1_kline_data/min1_kline",
    "min5_kline": "1_kline_data/min5_kline",
    "index_daily": "1_kline_data/index_daily",
    "etf_kline": "4_bond_etf/etf_kline",
}

#: 中文标签（前端目录展示；未知 key 回落原始 key 的约定在调用方）。
DATASET_LABELS: dict[str, str] = {
    "features_daily": "日线因子宽表",
    "l1_factors": "L1 因子库",
    L2_DATASET: "L2 微观结构因子",
    "l1_l2_factors": "L1 + L2 综合因子库",
    "technical_indicators": "技术指标",
    "valuation": "估值指标",
    "market_sentiment": "市场情绪",
    "margin_trading": "融资融券",
    "balance": "财务 · 资产负债表",
    "income": "财务 · 利润表",
    "cashflow": "财务 · 现金流量表",
    "capital": "财务 · 股本结构",
    "per_share": "财务 · 每股指标",
    "pershare_index": "财务 · 每股与财务比率",
    "dividend_factors": "财务 · 分红指标",
    "holder_num": "财务 · 股东户数",
    "min1_kline": "分钟线 · 1 分钟",
    "min5_kline": "分钟线 · 5 分钟",
    "index_daily": "指数日线",
    "etf_kline": "ETF 行情",
}

#: 数据集短码（历史 h5 缓存文件名用，已退役；保留供日志/溯源可读）。
DATASET_SHORT_NAMES: dict[str, str] = {
    "features_daily": "fd",
    "l1_factors": "l1",
    L2_DATASET: "l2",
}


def resolve_datasets(selected: Sequence[str] | None) -> tuple[str, ...]:
    """把请求里的数据集选择归一为登记过的 key 元组（**登记序**、去重）。

    空 / None = **全选**（与迁移前行为一致——默认喂全套特征）。顺序取登记序
    而非提交序：规范化顺序保证「同集合、不同勾选顺序」得同一份溯源记录。未登记
    key 抛 ``ValueError``（消息列出可选项，UI 直接透出）。
    """
    if not selected:
        return SUPPORTED_DATASETS
    cleaned = {str(raw or "").strip() for raw in selected}
    cleaned.discard("")
    unknown = sorted(k for k in cleaned if k not in SUPPORTED_DATASETS)
    if unknown:
        raise ValueError(
            f"未登记的挖掘数据集：{', '.join(unknown)}；"
            f"可选：{', '.join(SUPPORTED_DATASETS)}"
        )
    return tuple(k for k in SUPPORTED_DATASETS if k in cleaned) or SUPPORTED_DATASETS


def datasets_from_env(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """读任务环境 ``QM_MINING_DATASETS``（逗号分隔）→ 登记序数据集元组。

    选择集决定提示面列清单与任务溯源（``task_env``），静默容忍非法 key 会让
    「选了 A 却拿到 B」变成哑故障，故非法 key 抛 ``ValueError``。未设 = 全选。
    """
    raw = (os.environ if env is None else env).get("QM_MINING_DATASETS", "")
    return resolve_datasets([p for p in str(raw or "").split(",") if p.strip()])


#: L2 布局里的非因子列（键列 + 与基础面板同名的原始 OHLCV 锚列）：混入会让
#: 引用静默指向错数据，列清单必须剔除。
_L2_SKIP_COLUMNS: frozenset[str] = frozenset(
    {
        # 键列
        "symbol",
        "date",
        "datetime",
        "instrument",
        "dt",
        "trade_date",
        # OHLCV 锚列：与基础面板列同名（且口径为原始未复权价）
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }
)


def l2_factor_columns(quantdb_dir: str) -> list[str]:
    """读 l2_factors 最新分区 schema，返回可引用的真因子列（字母序）。

    H5 退役前与挖掘数据面共用单源；现为提示面（``a_share``）与按需读取
    （``quantdb_data.daily('l2_factors', [...])``）的列清单单源。按字母序而非
    文件列序——parquet 物理列序随日漂移（历史教训），依赖文件序会在某天静默换序。
    目录缺失/分区不可读返回 []（调用方降级为不接 L2，不拦挖掘）。
    """
    import pyarrow.parquet as pq

    if not quantdb_dir:
        return []
    from pathlib import Path as _Path

    base = _Path(quantdb_dir) / "6_ml_datasets" / L2_DATASET
    if not base.is_dir():
        return []
    try:
        parts = sorted(
            (base / name for name in os.listdir(base) if name.startswith("dt=")),
            key=lambda d: d.name,
        )
    except OSError:
        return []
    for part in reversed(parts):  # 最新分区优先；最新分区损坏则退到早先分区
        files = sorted(part.glob("*.parquet"))
        if not files:
            continue
        try:
            names = pq.read_schema(files[0]).names
        except Exception:
            continue
        return sorted(c for c in names if c not in _L2_SKIP_COLUMNS)
    return []
