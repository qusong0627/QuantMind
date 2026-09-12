"""美股个股终端 —— 财务三表的中文标签映射与数值出口工具。

yfinance 原始列名是英文长名（`Total Revenue` / `Net Income` / `Stockholders Equity`…），
这里维护一份**只映射实际存在列**的中文标签子集 —— 找不到的列就不展示，
不做 0 值兜底（0 会被前端当成真实财务数据）。

标签二元组 = (parquet 列名, 中文标签)。`/detail` 的 `financials` 面板按
`{key: 原始列名, label: 中文, values: 与 periods 等长的原始数值}` 下发，
金额的美元展示格式（万亿/亿/万）由前端统一处理，后端不再格式化金额字符串。

数值出口一律经 `num`（`safe_float` + NaN→None），避免 NaN 串进 JSON ——
前端 `null.toFixed` 白屏事故的根因就是 NaN/undefined 漏出。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from backend.services.api.market_analysis_shared.display import safe_float

# ---- 中文标签映射（只映射实际存在的列；列缺失即整条不展示，不做猜测） ----

INCOME_LABELS: tuple[tuple[str, str], ...] = (
    ("Total Revenue", "营业收入"),
    ("Cost Of Revenue", "营业成本"),
    ("Gross Profit", "毛利"),
    ("Operating Income", "营业利润"),
    ("Pretax Income", "税前利润"),
    ("Net Income", "净利润"),
    ("Research And Development", "研发费用"),
    ("Selling General And Administration", "销售及管理费"),
    ("EBITDA", "EBITDA"),
    ("Diluted EPS", "摊薄EPS"),
    ("Basic EPS", "基本EPS"),
)
BALANCE_LABELS: tuple[tuple[str, str], ...] = (
    ("Total Assets", "总资产"),
    ("Current Assets", "流动资产"),
    ("Cash And Cash Equivalents", "现金及等价物"),
    ("Accounts Receivable", "应收账款"),
    ("Inventory", "存货"),
    ("Total Liabilities Net Minority Interest", "总负债"),
    ("Current Liabilities", "流动负债"),
    ("Total Debt", "总债务"),
    ("Stockholders Equity", "股东权益"),
    ("Retained Earnings", "留存收益"),
)
CASHFLOW_LABELS: tuple[tuple[str, str], ...] = (
    ("Operating Cash Flow", "经营现金流"),
    ("Investing Cash Flow", "投资现金流"),
    ("Financing Cash Flow", "筹资现金流"),
    ("Free Cash Flow", "自由现金流"),
    ("Capital Expenditure", "资本开支"),
    ("Depreciation And Amortization", "折旧与摊销"),
    ("Stock Based Compensation", "股权激励"),
    ("Repurchase Of Capital Stock", "股份回购"),
    ("Cash Dividends Paid", "现金分红"),
)


def num(value: Any, ndigits: int = 2) -> float | None:
    """数值出口统一走 safe_float，NaN/None/空一律 None。"""
    if value is None or (isinstance(value, float) and value != value):
        return None
    if pd.isna(value):
        return None
    return round(safe_float(value), ndigits)
