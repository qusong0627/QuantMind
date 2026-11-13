"""CCASS top50（HKEX 中央结算系统持股纪录）口径与修复的唯一实现。

数据口径
--------
HKEX ``searchsdw_c.aspx`` 按股票返回 CCASS 参与者的日终持股；本项目只保留
**持股量最大的前 50 名参与者**（``quanthk_ccass_sync._normalise_fetch``）。
每行是某个 CCASS 参与方（券商/托管行/结算机构）在某只股票上的代客持股，
不是机构自营持仓。

历史坑（2026-11-13 审计实锤）
------------------------------
1. ``holding_percentage`` 在 2024-11-22 ~ 2025-12-12 的分区里**同日混用两种单位**：
   ≥1% 的行存小数（0.3633），<1% 的行存百分点（0.94）。直接用会把 top50
   合计放大到 ~8.3（应为 ~0.83）。
2. HKEX 对少数股票（0884/1801/2269/4332-4338 等）返回的表格**没有百分比列**
   （4 列布局）。旧解析 fallback 到持股量列：带逗号的数解析失败写成 0，
   少于 1000 股写成 数量/100（990 股 → 9.9%）。
3. 百分比是 HKEX 两位小数四舍五入，小持仓的隐含股本会抖动；不要用它反推
   精确股本，只在同一股票日内做相对比较。

修复策略
--------
用量价无关的**数量列**做锚：同一股票日内取持股量最大的 3 行（它们通常是
≥1% 的大持仓、单位稳定），用 ``qty / pct`` 的稳健中位数估计当日总股本；
再逐行判断该行原始百分比是小数还是百分点。整日全部判为小数但合计 >1.05
时视为纯百分点日，整体 /100。无法判定的行标 NaN（绝不填 0）——数量列可用，
百分比缺失就让它缺失。

本模块同时提供参与方分类与规范化分组（ChinaClear 多席位、HSBC 多实体等），
供因子/流水数据集共用，避免各生成器各写一套。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

#: 单股票日 top50 合计的合理上限（超过视为单位/分母异常，百分比特征置空）
PCT_SUM_MAX = 1.05

#: 有效百分比上限（HKEX 为占已发行股份，单一行不应 >100%）
PCT_ROW_MAX = 1.0001

#: 判定「该行原始值是百分点」的倍数容差
PCT_SCALE_TOL = 0.25

#: 托管行关键词（繁体为主，HKEX 页面原文）
CUSTODIAN_TOKENS: tuple[str, ...] = (
    "匯豐", "HSBC", "CITIBANK", "花旗", "JPMORGAN", "摩根大通", "CHASE",
    "STANDARD CHARTERED", "渣打", "STATE STREET", "道富", "DBS", "星展",
    "BANK OF CHINA", "中國銀行", "恒生銀行", "BNP", "法國巴黎", "NORTHERN TRUST",
    "BANK OF NEW YORK", "紐約梅隆", "BROWN BROTHERS", "COMMERZBANK",
    "UBS AG", "德意志銀行", "DEUTSCHE", "MIZUHO", "瑞穗", "MUFG", "三菱",
    "SUMITOMO", "三井住友", "CREDIT SUISSE", "瑞信",
)

#: 中国证券登记结算（沪/深股通南向）多席位
SOUTH_IDS: frozenset[str] = frozenset({"A00002", "A00003", "A00004", "A00006"})

#: 同一集团多实体/改名的规范分组（id -> (group_id, group_name)）
_GROUP_OVERRIDES: dict[str, tuple[str, str]] = {
    "C00019": ("HSBC", "匯豐集團"),
    "B01089": ("HSBC", "匯豐集團"),
    "C00010": ("CITI", "花旗集團"),
    "C00039": ("SCB", "渣打集團"),
    "C00033": ("BOCHK", "中國銀行(香港)"),
    "B01130": ("BOCI", "中銀國際"),
    "C00018": ("HASE_BANK", "恒生銀行"),
    "B01284": ("HASE_SEC", "恒生證券"),
    "A00002": ("SOUTH", "中國證券登記結算"),
    "A00003": ("SOUTH", "中國證券登記結算"),
    "A00004": ("SOUTH", "中國證券登記結算"),
    "A00006": ("SOUTH", "中國證券登記結算"),
    "A00005": ("CSDC_HK", "中國證券登記結算(香港)"),
}

#: 只保留这些列做修复（其它列原样返回）
_PCT = "holding_percentage"
_QTY = "holding_quantity"
_CODE = "stock_code"


def classify_participant(participant_id: object, participant_name: object) -> str:
    """参与方类型：``csc``=中国结算（南向）、``cust``=托管行、``brok``=券商。"""
    pid = str(participant_id or "").strip()
    name = str(participant_name or "")
    upper = name.upper()
    if pid in SOUTH_IDS or "証券登記" in name or "证券登记" in name:
        return "csc"
    for token in CUSTODIAN_TOKENS:
        if token.upper() in upper:
            return "cust"
    return "brok"


def canonical_group(participant_id: object, participant_name: object) -> tuple[str, str]:
    """参与方规范分组 ``(group_id, group_name)``。

    未登记实体按 id 分组（id 稳定，名称会改名）；同一集团多实体合并到
    固定 group，避免 HSBC/ChinaClear 这类多席位被拆散。
    """
    pid = str(participant_id or "").strip()
    name = str(participant_name or "").strip()
    if pid in _GROUP_OVERRIDES:
        return _GROUP_OVERRIDES[pid]
    if pid in SOUTH_IDS or "証券登記" in name or "证券登记" in name:
        return ("SOUTH", "中國證券登記結算")
    return (pid, name or pid)


@dataclass(frozen=True)
class RepairStats:
    rows: int = 0
    fraction_rows: int = 0
    percent_point_rows: int = 0
    reference_rows: int = 0
    missing_rows: int = 0
    bad_stock_days: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "rows": self.rows,
            "fraction_rows": self.fraction_rows,
            "percent_point_rows": self.percent_point_rows,
            "reference_rows": self.reference_rows,
            "missing_rows": self.missing_rows,
            "bad_stock_days": self.bad_stock_days,
        }


def _as_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("float64")


def repair_percentages(
    df: pd.DataFrame,
    *,
    reference_issued: dict[str, float] | None = None,
    code_col: str = _CODE,
    qty_col: str = _QTY,
    pct_col: str = _PCT,
    return_stats: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, RepairStats]:
    """修复 ``holding_percentage`` 的历史单位/缺失问题（返回新 DataFrame）。

    - 在**同一股票日内**用前 3 大持仓估计总股本，逐行判定小数/百分点；
    - 整日全判小数但合计 > ``PCT_SUM_MAX`` ⇒ 视为纯百分点日整体 /100；
    - 无法判定的行置 NaN；若提供 ``reference_issued``（外部总股本锚）则用它兜底；
    - ``holding_quantity`` 不改动（审计确认数量列干净）。

    幂等：对已修复的数据再次运行不改变结果（判定仍为小数）。
    """
    out = df.copy()
    qty = _as_float(out[qty_col])
    raw = _as_float(out[pct_col])

    valid = raw.notna() & (raw > 0) & (qty > 0)
    work = pd.DataFrame(
        {
            code_col: out[code_col].astype("string"),
            "_qty": qty,
            "_raw": raw,
        }
    )
    work = work.loc[valid].copy()
    work["_implied"] = work["_qty"] / work["_raw"]

    # 锚：同一股票当日**百分比 ≤100%** 的行里持股量最大的 3 行。
    # 先排除 pct>1 的行：它们不可能是小数制百分比（百分点或旧解析的数量/100），
    # 若拿它们做锚会把整只股票带偏。全部行都 >1 时退化为最大行（纯百分点日
    # 由后面的 sum>1.05 分支整体 /100 兜底）。
    cand = work[work["_raw"] <= 1.0]
    if cand.empty:
        cand = work
    anchor = (
        cand.sort_values([code_col, "_qty"], ascending=[True, False])
        .groupby(code_col, sort=False)
        .head(3)
        .groupby(code_col, sort=False)["_implied"]
        .median()
        .rename("_anchor")
    )
    work = work.join(anchor, on=code_col)
    work["_exp"] = work["_qty"] / work["_anchor"]
    work["_ratio"] = work["_raw"] / work["_exp"]

    is_frac = (work["_ratio"] - 1.0).abs() <= PCT_SCALE_TOL
    is_pct = ((work["_ratio"] - 100.0).abs() <= 100.0 * PCT_SCALE_TOL) & ~is_frac
    work["_is_frac"] = is_frac
    work["_is_pct"] = is_pct
    work["_repaired"] = np.where(
        is_frac, work["_raw"], np.where(is_pct, work["_raw"] / 100.0, np.nan)
    )

    # 纯百分点日：所有行都判为「小数」，但合计远超 100% ⇒ 整体除以 100
    sums = work.groupby(code_col, sort=False)["_repaired"].sum(min_count=1)
    has_pct = work.groupby(code_col, sort=False)["_is_pct"].any()
    pure_pp = sums[(sums > PCT_SUM_MAX) & (~has_pct)].index
    if len(pure_pp):
        mask = work[code_col].isin(pure_pp)
        work.loc[mask, "_repaired"] = work.loc[mask, "_repaired"] / 100.0
        work.loc[mask, "_is_frac"] = False
        work.loc[mask, "_is_pct"] = True

    repaired = pd.Series(np.nan, index=out.index, dtype="float64")
    repaired.loc[work.index] = work["_repaired"]

    # 外部总股本锚兜底（只用于仍无法判定的行）。
    # 关键护栏：参考股本必须 ≥ 当日最大持仓（单一参与方不可能持有超过总股本），
    # 否则说明参考表已过期/来源错误（实测 8071.HK 曾把 1.3B 股按 ~90M 股折算，
    # 造出 13 倍的持仓占比）。不满足就保持 NaN，宁可缺也不造假。
    if reference_issued:
        code_series = out[code_col].astype("string")
        ref = code_series.map(lambda c: reference_issued.get(str(c), np.nan))
        max_qty = qty.groupby(code_series, sort=False).transform("max")
        ref_ok = ref.notna() & (ref * PCT_ROW_MAX >= max_qty)
        fallback = (qty / ref).astype("float64")
        need = repaired.isna() & qty.gt(0) & fallback.notna() & ref_ok
        repaired = repaired.where(~need, fallback)
    else:
        need = pd.Series(False, index=out.index)

    repaired = repaired.where((repaired >= 0) & (repaired <= PCT_ROW_MAX))
    out[pct_col] = repaired

    # 最终护栏：同一股票日修复后合计仍 >105% ⇒ 百分比整体不可信（典型：股本
    # 口径变化的细价股、4 列布局历史段）。全部置 NaN，数量列不受影响；绝不
    # 把 >100% 的占比交给研究层。
    bad_days = 0
    if len(out):
        code_series = out[code_col].astype("string")
        sums = out.groupby(code_series, sort=False)[pct_col].sum(min_count=1)
        bad_stocks = sums[sums > PCT_SUM_MAX].index
        bad_days = int(len(bad_stocks))
        if bad_days:
            out.loc[code_series.isin(bad_stocks), pct_col] = np.nan

    if not return_stats:
        return out

    # 诊断（不写回数据）
    frac_rows = int(work["_is_frac"].sum())
    pct_rows = int(work["_is_pct"].sum())
    ref_rows = int(need.sum())
    missing = int(out[pct_col].isna().sum())
    stats = RepairStats(
        rows=len(out),
        fraction_rows=frac_rows,
        percent_point_rows=pct_rows,
        reference_rows=ref_rows,
        missing_rows=missing,
        bad_stock_days=bad_days,
    )
    return out, stats


def load_reference_issued(path: str | Path | None = None) -> dict[str, float]:
    """读取百分比修复的总股本锚（由 ``quanthk_ccass_repair.py`` 生成）。"""
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        df = pd.read_parquet(p)
    except Exception:  # noqa: BLE001 - 参考文件损坏时退化为无锚
        log.warning("CCASS 总股本参考文件读取失败: %s", p, exc_info=True)
        return {}
    if not {"symbol", "issued_shares"}.issubset(df.columns):
        return {}
    return {
        str(s): float(v)
        for s, v in zip(df["symbol"], df["issued_shares"], strict=False)
        if pd.notna(v) and float(v) > 0
    }


def repair_partition(
    path: str | Path,
    *,
    reference_issued: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, RepairStats]:
    """读一个 CCASS 分区并修复百分比（不写回）。"""
    df = pd.read_parquet(path)
    repaired, stats = repair_percentages(
        df, reference_issued=reference_issued, return_stats=True
    )
    return repaired, stats


def qc_summary(df: pd.DataFrame, code_col: str = _CODE, pct_col: str = _PCT) -> dict:
    """分区级质检摘要（写 ``qc.json`` / 报表用）。"""
    qty = _as_float(df[_QTY])
    pct = _as_float(df[pct_col])
    sums = df.groupby(code_col, sort=False)[pct_col].sum(min_count=1)
    return {
        "rows": int(len(df)),
        "stocks": int(df[code_col].nunique()),
        "participants": int(df["participant_id"].nunique())
        if "participant_id" in df.columns
        else None,
        "qty_min": float(qty.min()) if len(qty) else None,
        "qty_zero_or_negative": int((qty <= 0).sum()),
        "pct_missing_rows": int(pct.isna().sum()),
        "pct_zero_rows": int((pct == 0).sum()),
        "pct_sum_median": float(sums.median()) if len(sums.dropna()) else None,
        "pct_sum_p99": float(sums.quantile(0.99)) if len(sums.dropna()) else None,
        "pct_sum_gt_105": int((sums > PCT_SUM_MAX).sum()),
    }
