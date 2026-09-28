"""QuantDB 数据集目录规格（云端 COS 对象与本地落盘的统一描述）。

从 quantdb_console 抽出为共享模块，供管理台路由与本地扫描脚本共用，
避免脚本反向 import 路由模块引入 FastAPI 依赖。

layout 决定落盘形态与扫描/预览读法：
  partition — dt=YYYYMMDD/data.parquet 按交易日分区（含 quarter=YYYYQN 季度分区）
  symbol    — 每标的一个 {SYMBOL}.parquet
  single    — 整个数据集一个 parquet（文件名不定，如 instrument_list.parquet）

云端 object key 与本地落盘相对路径一致（posix 分隔），
例如 ``1_kline_data/daily_forward/dt=20260804/data.parquet``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Layout = Literal["partition", "symbol", "single"]


@dataclass(frozen=True)
class DatasetSpec:
    dataset: str
    name: str
    category_id: str
    group: str
    rel_dir: str
    layout: Layout
    note: str = ""


GROUPS: list[dict[str, str]] = [
    {"id": "kline", "name": "K线行情", "category_id": "1"},
    {"id": "base_sector", "name": "基础板块", "category_id": "2"},
    {"id": "financial", "name": "财务数据", "category_id": "3"},
    {"id": "bond_etf", "name": "债券/ETF", "category_id": "4"},
    {"id": "technical", "name": "技术衍生", "category_id": "5"},
    {"id": "ml", "name": "ML数据集", "category_id": "6"},
]

DATASETS: tuple[DatasetSpec, ...] = (
    # 1 K线行情
    DatasetSpec("daily_forward", "日线前复权", "1", "kline", "1_kline_data/daily_forward", "partition", "训练/回测主用，随除权每日全量重写"),
    DatasetSpec("daily_backward", "日线后复权", "1", "kline", "1_kline_data/daily_backward", "partition", "当前价×复权因子，除权后整段回溯"),
    DatasetSpec("daily_unadjusted", "日线不复权", "1", "kline", "1_kline_data/daily_unadjusted", "partition", "原始价，撮合/涨跌停判定用"),
    DatasetSpec("index_daily", "指数日线", "1", "kline", "1_kline_data/index_daily", "partition", "主要指数日K走势"),
    DatasetSpec("min5_kline", "5分钟线", "1", "kline", "1_kline_data/min5_kline", "symbol", "5 分钟级日内行情"),
    DatasetSpec("min1_kline", "1分钟线", "1", "kline", "1_kline_data/min1_kline", "symbol", "体积大，按需同步"),
    DatasetSpec("tick_data", "Tick逐笔", "1", "kline", "1_kline_data/tick_data", "partition", "逐笔成交，流量消耗极高"),
    # 2 基础板块
    DatasetSpec("instrument_detail", "个股详情", "2", "base_sector", "2_base_sector/instrument_detail", "single", "152 列基本面快照"),
    DatasetSpec("sector_concept", "板块概念", "2", "base_sector", "2_base_sector/sector_concept", "single", "行业/概念板块成分"),
    DatasetSpec("index_weights", "指数权重", "2", "base_sector", "2_base_sector/index_weights", "symbol", "沪深300/中证500/1000 等"),
    DatasetSpec("trading_calendar", "交易日历", "2", "base_sector", "2_base_sector/trading_calendar", "single", "A股交易日历"),
    DatasetSpec("margin_trading", "融资融券", "2", "base_sector", "2_base_sector/margin_trading", "partition", "两融余额明细"),
    # 3 财务数据
    DatasetSpec("balance", "资产负债表", "3", "financial", "3_financial_data/balance", "symbol", "资产/负债/权益"),
    DatasetSpec("income", "利润表", "3", "financial", "3_financial_data/income", "symbol", "营收/净利/每股收益"),
    DatasetSpec("cashflow", "现金流量表", "3", "financial", "3_financial_data/cashflow", "symbol", "经营/投资/筹资现金流"),
    DatasetSpec("capital", "股本结构", "3", "financial", "3_financial_data/capital", "symbol", "总股本/流通股本变动"),
    DatasetSpec("pershare_index", "每股财务指标", "3", "financial", "3_financial_data/pershare_index", "symbol", "每股收益/净资产等"),
    DatasetSpec("per_share", "每股指标(TTM)", "3", "financial", "3_financial_data/per_share", "symbol", "TTM 每股收益/营收/现金流"),
    DatasetSpec("dividend_factors", "分红因子", "3", "financial", "3_financial_data/dividend_factors", "symbol", "历次分红送转因子"),
    DatasetSpec("holder_num", "股东户数", "3", "financial", "3_financial_data/holder_num", "symbol", "股东户数变化"),
    # 4 债券/ETF
    DatasetSpec("etf_pcf", "ETF申赎清单", "4", "bond_etf", "4_bond_etf/etf_pcf", "symbol", "ETF 申购赎回清单"),
    DatasetSpec("convertible_bond", "可转债", "4", "bond_etf", "4_bond_etf/convertible_bond", "symbol", "可转债行情与条款"),
    # 5 技术衍生
    DatasetSpec("valuation", "估值", "5", "technical", "5_technical_derived/valuation", "partition", "PE/PB/市值"),
    DatasetSpec("technical_indicators", "技术指标", "5", "technical", "5_technical_derived/technical_indicators", "partition", "MA/MACD/RSI 等技术指标"),
    DatasetSpec("market_sentiment", "市场情绪", "5", "technical", "5_technical_derived/market_sentiment", "partition", "市场情绪指标"),
    # 6 ML数据集
    DatasetSpec("features_daily", "日频特征", "6", "ml", "6_ml_datasets/features_daily", "partition", "技术指标 + 估值合并，PG 填充主源"),
    DatasetSpec("l1_factors", "L1 因子", "6", "ml", "6_ml_datasets/l1_factors", "partition", "因子挖掘核心"),
    DatasetSpec("l2_factors", "L2 因子", "6", "ml", "6_ml_datasets/l2_factors", "partition", "高频微观因子"),
    DatasetSpec("l1_l2_factors", "L1+L2 合并", "6", "ml", "6_ml_datasets/l1_l2_factors", "partition", "L1 与 L2 因子合并集"),
)

_BY_NAME: dict[str, DatasetSpec] = {ds.dataset: ds for ds in DATASETS}


def get_dataset_spec(dataset: str) -> DatasetSpec:
    spec = _BY_NAME.get(dataset)
    if spec is None:
        raise KeyError(f"未知数据集: {dataset}")
    return spec
