"""因子研究模块 —— QuantDB 数据加载层（支持多市场）。

本层只负责把 QuantDB parquet 读成宽表面板（index=交易日, columns=symbol），
供 engine / financials 计算因子；不做任何因子语义。
所有路径经 ``feature_source.resolve_market_root`` 解析（CN 即原
``quantdb_paths`` 候选表，零行为变化；禁硬编码）。

市场分层（2026-10-16）：全部读取函数带 ``market``（缺省 ``"CN"``）。各市场
盘上口径差异在本层收口（上层不必感知）：
- ``time`` 列：CN 是 ``YYYYMMDD`` 串，非 CN 是 timestamp → 统一转 DatetimeIndex；
- ``amount`` 单位：CN 万元，非 CN 元（20261016 实测 amount/(close×volume)
  ≈1e-4 vs 1.0）→ ``amount_yi_factor()`` 唯一换算入口；
- ``instrument_detail``（ST/退市/申万）为 A 股专属；非 CN 的名称/行业来自
  ``security_master`` / ``sector/``（``load_security_master``/``load_sector_map``）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 数据目录（相对**市场根**）
DAILY_PARTS = ("1_kline_data", "daily_forward")
INDEX_PARTS = ("1_kline_data", "index_daily")
VALUATION_PARTS = ("5_technical_derived", "valuation")
INSTRUMENT_PARTS = ("2_base_sector", "instrument_detail")
ST_PIT_PARTS = ("2_base_sector", "st_pit")
SECURITY_MASTER_PARTS = ("2_base_sector", "security_master", "data.parquet")
SECTOR_PARTS = ("2_base_sector", "sector")
# 港股估值/行业：5_technical_derived/valuation 在 HK 为空目录，改用本地
# akshare_financial（总市值/已发行股本/PE/PB）与 akshare_profile（恒生行业）。
HK_FINANCIAL_PARTS = ("2_base_sector", "akshare_financial")
HK_PROFILE_PARTS = ("2_base_sector", "akshare_profile")
# 期货品种规则（乘数 / 品种名 → 板块）；连续合约 XX0.CN 由品种码映射。
FUTURES_TRADE_RULE_PARTS = ("2_base_sector", "trade_rule")
INCOME_PARTS = ("3_financial_data", "income")
BALANCE_PARTS = ("3_financial_data", "balance")
CASHFLOW_PARTS = ("3_financial_data", "cashflow")
PERSHARE_PARTS = ("3_financial_data", "pershare_index")
HOLDER_PARTS = ("3_financial_data", "holder_num")

# 基准指数（demo 口径用中证500；回归市场组合同样用它做基准）
BENCHMARK_SYMBOL = "000905.SH"

# stock_snapshot 估值列（市值/PE/PB）的 partition 供给注册表：**加市场 = 加条目**。
# 判据是数据质量实测（20261016）：US valuation 550 分区可用。HK 的 valuation
# 目录为空，改走 ``akshare_financial``（见 ``_load_hk_valuation``）；FUTURES 无
# 市值概念，供给由快照构建器按持仓名义规模另算（``load_valuation_snapshot`` 返回 None）。
VALUATION_SNAPSHOT_MARKETS = {"US"}


def _market_root(market: str) -> Path:
    from backend.shared.feature_source import resolve_market_root, validate_market

    return resolve_market_root(validate_market(market))


def market_path(dir_parts: tuple[str, ...], market: str = "CN") -> Path:
    """市场根下的目录（多市场分层后的唯一路径解析入口）。

    2026-10-22 修：四市场分层（6ce32c4b）删掉了本模块的
    ``resolve_quantdb_subdir`` 重导出，``build_factor_research.py`` 的
    ``frdata.resolve_quantdb_subdir(...)`` 直到下一次财务因子构建才炸
    （AttributeError，非导入期），属静默断链。凡需在数据层之外拼路径的脚本
    一律走本函数，不要各自 import 旧 CN 专用解析器。
    """
    return _market_root(market).joinpath(*dir_parts)


def amount_yi_factor(market: str) -> float:
    """成交额(列值) → 亿元 的换算系数：CN 万元→亿 = 1e-4；非 CN 元→亿 = 1e-8。

    单位是实测口径（见模块 docstring），不是猜的：两处都不许各写各的除数。
    """
    from backend.shared.feature_source import validate_market

    return 1e-4 if validate_market(market) == "CN" else 1e-8


def _to_trade_dates(idx: pd.Index) -> pd.DatetimeIndex:
    """分区 time 列 → 交易日索引（CN 为 YYYYMMDD 串/整数，非 CN 为 timestamp）。"""
    if pd.api.types.is_datetime64_any_dtype(idx):
        return pd.DatetimeIndex(idx).normalize()
    try:
        return pd.to_datetime(idx.astype(str), format="%Y%m%d")
    except (ValueError, TypeError):
        # 分区 time 格式不齐（如 object 里混着 Timestamp）时的通用兜底
        return pd.DatetimeIndex(pd.to_datetime(idx)).normalize()


def _partition_files(
    dir_parts: tuple[str, ...],
    start: str | None,
    end: str | None,
    market: str = "CN",
) -> list[Path]:
    """列出 dt=YYYYMMDD 分区目录（按 dt 过滤，升序）。"""
    root = _market_root(market).joinpath(*dir_parts)
    out = []
    for p in sorted(root.glob("dt=*")):
        if not p.is_dir():
            continue
        dt = p.name[3:]
        if start and dt < start:
            continue
        if end and dt > end:
            continue
        f = p / "data.parquet"
        if f.exists():
            out.append(f)
    return out


def _read_files(files: list[Path], columns: list[str] | None = None) -> pd.DataFrame:
    parts = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=columns)
        except Exception as e:  # 单分区损坏不拖垮全量（记日志跳过）
            logger.warning("读取失败跳过 %s: %s", f, e)
            continue
        parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    # 跨分区 ``time`` 列 dtype 实测不齐：HK daily_forward 在 20260508→20260511 之间
    # 由 date32[day]（→ datetime.date）改为 timestamp[ns]（→ Timestamp），拼起来是
    # object 列。不归一的话下游 pivot_table 的 groupby("last") 要对键排序比较，
    # Timestamp 与 datetime.date 比较直接 TypeError（2026-10-16 实机构建所踩）。
    # 只碰 object 列：CN/US/FUT 的分区恒 timestamp[ns]（datetime64，原样通过），
    # 整数/字符串 YYYYMMDD 的遗留列也不进此分支（仍由 ``_to_trade_dates`` 收口）。
    if "time" in out.columns and out["time"].dtype == object:
        out["time"] = pd.to_datetime(out["time"])
    return out


def load_daily_panel(
    start: str,
    end: str,
    fields: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount"),
    market: str = "CN",
    parts: tuple[str, ...] = DAILY_PARTS,
) -> dict[str, pd.DataFrame]:
    """读取 daily_forward → {字段: 宽表(交易日 × symbol)}。前复权价格 + 股/成交额。

    成交额单位随市场（CN 万元 / 非 CN 元）——用 ``amount_yi_factor(market)`` 换算。

    ``parts`` 可指向其他 K 线库（如 ``("1_kline_data", "daily_unadjusted")``）；
    需要**名义价格**与复权价格混算的调用方（alpha360 的 VWAP 基准）用它取原始价。
    """
    files = _partition_files(parts, start, end, market)
    if not files:
        raise FileNotFoundError(f"{'/'.join(parts)} 无分区: {start}~{end}")
    cols = ["time", "symbol", *fields]
    df = _read_files(files, columns=cols)
    df["symbol"] = df["symbol"].astype("category")
    out: dict[str, pd.DataFrame] = {}
    for fld in fields:
        out[fld] = df.pivot_table(
            index="time", columns="symbol", values=fld, aggfunc="last"
        )
    for fld, df_ in list(out.items()):
        df_.index = _to_trade_dates(df_.index)
        df_.sort_index(inplace=True)
        if df_.index.has_duplicates:  # 分区 time 格式不一致时 to_datetime 可能撞出重复
            logger.warning("daily_forward 索引存在重复日期，已去重（keep=last）")
            out[fld] = df_[~df_.index.duplicated(keep="last")]
    logger.info(
        "%s 载入 %d 交易日 × %d 只 (%s~%s)",
        parts[-1],
        len(out["close"]),
        out["close"].shape[1],
        start,
        end,
    )
    return out


def load_valuation_panel(
    start: str,
    end: str,
    fields: tuple[str, ...] = (
        "close",
        "total_capital",
        "circulating_capital",
        "total_mv",
        "float_mv",
        "net_profit_ttm",
        "revenue_ttm",
        "equity",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dividend_rate",
    ),
    market: str = "CN",
) -> dict[str, pd.DataFrame]:
    """读取 valuation → {字段: 宽表}。total_mv/float_mv 单位元。"""
    files = _partition_files(VALUATION_PARTS, start, end, market)
    if not files:
        raise FileNotFoundError(f"valuation 无分区: {start}~{end}")
    cols = ["time", "symbol", *fields]
    df = _read_files(files, columns=cols)
    df["symbol"] = df["symbol"].astype("category")
    out: dict[str, pd.DataFrame] = {}
    for fld in fields:
        out[fld] = df.pivot_table(
            index="time", columns="symbol", values=fld, aggfunc="last"
        )
    for fld, df_ in list(out.items()):
        df_.index = _to_trade_dates(df_.index)
        df_.sort_index(inplace=True)
        if df_.index.has_duplicates:  # 同上：防御分区 time 格式不一致
            logger.warning("valuation 索引存在重复日期，已去重（keep=last）")
            out[fld] = df_[~df_.index.duplicated(keep="last")]
    return out


def load_index_close(
    start: str, end: str, symbol: str = BENCHMARK_SYMBOL, market: str = "CN"
) -> pd.Series:
    """读取指数收盘价序列（默认中证500；非 CN 传本市场指数代码）。"""
    files = _partition_files(INDEX_PARTS, start, end, market)
    df = _read_files(files, columns=["time", "symbol", "close"])
    if df.empty:
        return pd.Series(dtype=float)
    df = df[df["symbol"] == symbol]
    s = df.set_index("time")["close"].sort_index()
    s.index = _to_trade_dates(s.index)
    return s.astype(float)


def load_instrument(market: str = "CN") -> pd.DataFrame:
    """读取证券主表快照（ST/退市/行业/名称）。**A 股专属**（instrument_detail）。"""
    from backend.shared.feature_source import validate_market

    mk = validate_market(market)
    if mk != "CN":
        raise FileNotFoundError(
            f"instrument_detail（ST/退市/申万口径）为 A 股专属，{mk} 市场不供给；"
            "非 CN 股票池请以 instr=None 走 build_universe 的全池近似"
        )
    root = _market_root(mk).joinpath(*INSTRUMENT_PARTS)
    f = root / "instrument_detail.parquet"
    df = pd.read_parquet(
        f,
        columns=["Symbol", "Name", "IsSTGP", "IsQuitGP", "rs_hycode_sim", "rs_hyname"],
    )
    df = df.rename(
        columns={
            "Symbol": "symbol",
            "Name": "name",
            "IsSTGP": "is_st",
            "IsQuitGP": "is_quit",
            "rs_hycode_sim": "industry_code",
            "rs_hyname": "industry",
        }
    )
    return df


def load_security_master(market: str = "CN") -> pd.DataFrame | None:
    """证券主表（``2_base_sector/security_master/data.parquet``：名称）。

    非 CN 的名称来源（HK/US 有；FUTURES 无此目录 → None，调用方回退 symbol）。
    CN 的对应物是 instrument_detail（本函数不服务 CN 名称面）。
    """
    p = _market_root(market).joinpath(*SECURITY_MASTER_PARTS)
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if "cn_name" not in df.columns:
        df["cn_name"] = None
    if "en_name" not in df.columns:
        df["en_name"] = None
    return df[["symbol", "cn_name", "en_name"]]


def _load_hk_profile_industry(market: str) -> pd.DataFrame | None:
    """港股恒生行业（``akshare_profile/所属行业``）→ symbol/industry。

    ``2_base_sector/sector`` 的 HK 文件实测 industry/sector 全为空串；profile
    是同一数据平台里唯一稳定的行业源（2026-11-15 实测 2783/2784 有值）。
    """
    d = _market_root(market).joinpath(*HK_PROFILE_PARTS)
    if not d.is_dir() or not any(d.glob("*.parquet")):
        return None
    import duckdb

    out = duckdb.sql(
        "SELECT symbol, NULLIF(TRIM(\"所属行业\"), '') AS industry "
        f"FROM read_parquet('{d}/*.parquet', union_by_name=true)"
    ).to_df()
    if out.empty or "symbol" not in out.columns:
        return None
    out = out.dropna(subset=["industry"]).drop_duplicates("symbol", keep="last")
    return out if not out.empty else None


def load_sector_map(market: str = "CN") -> pd.DataFrame | None:
    """行业映射（``2_base_sector/sector/<symbol>.parquet`` → symbol/industry/sector）。

    目录缺失或全空 → None（调用方把行业列置空，绝不编造）。逐文件 schema 有漂移
    （HK 实测 industry/sector 全为空串、US 两类都有），故用 DuckDB ``union_by_name``
    一次扫全目录，返回**实际存在的**列（调用方 industry 缺则回退 sector）。

    HK：sector 目录存在但行业全空时，回退 ``akshare_profile/所属行业``（恒生行业，
    2783/2784 覆盖）；不回退成空串，也不拿别的市场行业代填。
    """
    from backend.shared.feature_source import validate_market

    mk = validate_market(market)
    d = _market_root(mk).joinpath(*SECTOR_PARTS)
    out = None
    if d.is_dir() and any(d.glob("*.parquet")):
        import duckdb

        raw = duckdb.sql(
            f"SELECT * FROM read_parquet('{d}/*.parquet', union_by_name=true)"
        ).to_df()
        keep = [c for c in ("symbol", "industry", "sector") if c in raw.columns]
        if "symbol" in keep:
            out = raw[keep]
    if mk != "HK":
        return out
    profile = _load_hk_profile_industry(mk)
    if out is None:
        return profile
    if profile is None or out.empty:
        return out
    base = out.copy()
    if "industry" not in base.columns:
        base["industry"] = None
    base["industry"] = base["industry"].replace("", pd.NA)
    merged = base.merge(
        profile.rename(columns={"industry": "_profile_industry"}),
        on="symbol",
        how="outer",
    )
    # 恒生行业（中文、与市场分析页同源）优先于 sector/ 的 Yahoo 英文行业；
    # profile 缺该股时再保留 sector/ 值，最后才留空。
    merged["industry"] = merged["_profile_industry"].fillna(merged["industry"])
    return merged.drop(columns=["_profile_industry"])


def _load_hk_valuation(market: str) -> pd.DataFrame | None:
    """港股估值快照（``akshare_financial``）→ symbol/total_mv/shares/pe_ttm/pb。

    ``总市值(港元)`` 是抓取时点的快照；``已发行股本(股)`` 供快照构建器用最新
    收盘价重算市值（价格变动后仍能对齐）。脏行（市值 ≤0）置 NaN，不伪装成 0。
    """
    d = _market_root(market).joinpath(*HK_FINANCIAL_PARTS)
    if not d.is_dir() or not any(d.glob("*.parquet")):
        return None
    import duckdb

    out = duckdb.sql(
        "SELECT symbol, "
        "TRY_CAST(\"总市值(港元)\" AS DOUBLE) AS total_mv, "
        "TRY_CAST(\"已发行股本(股)\" AS DOUBLE) AS shares, "
        "TRY_CAST(\"市盈率\" AS DOUBLE) AS pe_ttm, "
        "TRY_CAST(\"市净率\" AS DOUBLE) AS pb "
        f"FROM read_parquet('{d}/*.parquet', union_by_name=true)"
    ).to_df()
    if out.empty or "symbol" not in out.columns:
        return None
    out = out.drop_duplicates("symbol", keep="last")
    out.loc[~(out["total_mv"] > 0), "total_mv"] = np.nan
    out.loc[~(out["shares"] > 0), "shares"] = np.nan
    return out


def load_valuation_snapshot(market: str = "CN") -> pd.DataFrame | None:
    """最新一期估值截面（symbol/total_mv/pe_ttm/pb）；无数据源 → None。

    - US：读 ``5_technical_derived/valuation`` 最新 dt 分区（注册表准入）；
    - HK：读 ``2_base_sector/akshare_financial``（5_technical_derived/valuation 为空）；
    - FUTURES：无市值概念（名义规模在快照构建器里按持仓×乘数另算）→ None。

    单位：total_mv 为**本币元**（HK=港元、US=美元），快照构建器统一 /1e8 转亿。
    """
    from backend.shared.feature_source import validate_market

    mk = validate_market(market)
    if mk == "HK":
        return _load_hk_valuation(mk)
    if mk not in VALUATION_SNAPSHOT_MARKETS:
        return None
    files = _partition_files(VALUATION_PARTS, None, None, mk)
    if not files:
        return None
    df = pd.read_parquet(files[-1])
    cols = [c for c in ("symbol", "total_mv", "pe_ttm", "pb") if c in df.columns]
    if "symbol" not in cols:
        return None
    return df[cols]


def futures_variety_code(symbol: str) -> str:
    """期货代码 → 品种码：``CU0.CN``→``CU``、``CU2610.CN``→``CU``、``CL.FUT``→``CL``。"""
    s = str(symbol or "").upper().strip()
    if s.endswith(".CN"):
        return re.sub(r"\d+\.CN$", "", s)
    if s.endswith(".FUT"):
        return s[:-4]
    return s


#: 外盘/现货品种 → 板块（国内品种由 trade_rule 的中文品种名规则判定）。
_FOREIGN_FUTURES_SECTOR: dict[str, str] = {
    "CL": "能化",
    "NG": "能化",
    "BO": "能化",
    "OIL": "能化",
    "EUA": "能化",
    "GC": "贵金属",
    "SI": "贵金属",
    "XAU": "贵金属",
    "XAG": "贵金属",
    "XPT": "贵金属",
    "XPD": "贵金属",
    "PL": "贵金属",
    "PA": "贵金属",
    "HG": "有色",
    "NI": "有色",
    "SN": "有色",
    "AL": "有色",
    "CU": "有色",
    "ZN": "有色",
    "PB": "有色",
    "PBD": "有色",
    "NID": "有色",
    "SND": "有色",
    "AHD": "有色",
    "C": "农产品",
    "S": "农产品",
    "W": "农产品",
    "CT": "农产品",
    "FCPO": "农产品",
    "LHC": "农产品",
    "RSS3": "农产品",
    "RS": "农产品",
    "BTC": "金融",
    "CAD": "金融",
    "FEF": "黑色",
    "ZSD": "其他",
    "TRB": "其他",
}

_FUTURES_SECTOR_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("金融", ("股指", "国债", "期权")),
    ("贵金属", ("黄金", "白银", "铂", "钯")),
    (
        "有色",
        (
            "铜",
            "铝",
            "锌",
            "铅",
            "镍",
            "锡",
            "不锈钢",
            "氧化铝",
            "铝合金",
            "工业硅",
            "碳酸锂",
            "多晶硅",
        ),
    ),
    ("黑色", ("螺纹", "热轧", "线材", "铁矿石", "焦炭", "焦煤", "锰硅", "硅铁", "动力煤")),
    (
        "能化",
        (
            "原油",
            "燃料油",
            "沥青",
            "橡胶",
            "纸浆",
            "聚乙烯",
            "聚氯乙烯",
            "聚丙烯",
            "苯乙烯",
            "乙二醇",
            "液化石油气",
            "甲醇",
            "PTA",
            "玻璃",
            "纯碱",
            "尿素",
            "短纤",
            "对二甲苯",
            "烧碱",
            "丙烯",
            "瓶片",
            "纯苯",
            "胶版印刷纸",
            "集运",
            "碳排放",
        ),
    ),
    (
        "农产品",
        (
            "豆",
            "玉米",
            "淀粉",
            "鸡蛋",
            "粳米",
            "生猪",
            "苹果",
            "红枣",
            "花生",
            "菜籽",
            "菜粕",
            "菜油",
            "白糖",
            "棉花",
            "棉纱",
            "强麦",
            "普麦",
            "稻",
            "原木",
            "纤维板",
            "胶合板",
            "棕榈油",
        ),
    ),
)


def _futures_sector(variety: str, name: str, exchange: str) -> str:
    """期货品种 → 板块（金融/贵金属/有色/黑色/能化/农产品/其他）。"""
    text = f"{name} {variety}"
    if exchange in ("中金所", "CFFEX"):
        return "金融"
    if variety.startswith(("AU", "AG", "PT", "MAU")):
        return "贵金属"
    for sector, keywords in _FUTURES_SECTOR_KEYWORDS:
        if any(k in text for k in keywords):
            return sector
    return _FOREIGN_FUTURES_SECTOR.get(variety, "其他")


def load_futures_rule_snapshot() -> pd.DataFrame | None:
    """最新期货交易规则（variety/multiplier/variety_name/exchange）；无分区 → None。"""
    files = _partition_files(FUTURES_TRADE_RULE_PARTS, None, None, "FUTURES")
    if not files:
        return None
    df = pd.read_parquet(files[-1])
    cols = [
        c
        for c in ("variety", "multiplier", "variety_name", "exchange")
        if c in df.columns
    ]
    if "variety" not in cols:
        return None
    out = df[cols].copy()
    out["variety"] = out["variety"].astype(str).str.upper().str.strip()
    out = out.drop_duplicates("variety", keep="last")
    return out


def futures_variety_sector_map() -> dict[str, str]:
    """品种码 → 板块（来自最新 trade_rule；外盘/现货用显式表兜底）。"""
    out = dict(_FOREIGN_FUTURES_SECTOR)
    rules = load_futures_rule_snapshot()
    if rules is None:
        return out
    for row in rules.itertuples(index=False):
        variety = str(getattr(row, "variety", "") or "").upper().strip()
        if not variety:
            continue
        # 期权品种（CU_O 等）不映射到板块；快照里只有期货连续合约。
        if variety.endswith("_O") or "_" in variety:
            continue
        out[variety] = _futures_sector(
            variety,
            str(getattr(row, "variety_name", "") or ""),
            str(getattr(row, "exchange", "") or ""),
        )
    return out


def futures_multiplier_map() -> dict[str, float]:
    """品种码 → 合约乘数（trade_rule 唯一来源；缺失品种不猜测）。"""
    rules = load_futures_rule_snapshot()
    if rules is None or "multiplier" not in rules.columns:
        return {}
    mult = pd.to_numeric(rules["multiplier"], errors="coerce")
    return {
        str(v): float(m)
        for v, m in zip(rules["variety"], mult, strict=False)
        if pd.notna(m) and float(m) > 0
    }


def month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """月度调仓采样日 = 每月最后交易日；末尾追加最新交易日（当期快照）。"""
    s = pd.Series(index, index=index)
    me = s.groupby([index.year, index.month]).last().tolist()
    dates = [pd.Timestamp(d) for d in me]
    if dates and dates[-1] != index[-1]:
        dates.append(index[-1])
    return dates


def load_st_pit(market: str = "CN") -> pd.DataFrame | None:
    """PIT ST 稀疏表（``symbol,time,is_st``）；表缺失/非 CN → None。

    数据面由 ``backend/scripts/build_st_pit.py`` 生成（baostock 逐日 ``isST``）。
    ``IsSTGP`` 快照回答的是「**现在**是不是 ST」，拿它当历史过滤条件就是前视
    （2026-10-22 审计：预测 top decile 的 ST 占比 8.5%，全市场 4.0%）。
    """
    from backend.shared.feature_source import validate_market

    mk = validate_market(market)
    if mk != "CN":
        return None
    f = _market_root(mk).joinpath(*ST_PIT_PARTS) / "st_pit.parquet"
    if not f.exists():
        return None
    return pd.read_parquet(f)[["symbol", "time", "is_st"]]


def st_mask_over(dates: pd.Index, symbols, st_pit: pd.DataFrame | None) -> pd.DataFrame:
    """(交易日 × symbol) 的 PIT ST 布尔掩码；稀疏表未命中 = 非 ST。

    这是全仓唯一实现（构建脚本与本模块共用），避免两处口径漂移。
    ``dates`` 可以是 datetime64 或 ``YYYYMMDD`` 字符串。
    """
    idx = pd.Index(dates)
    cols = list(symbols)
    mask = pd.DataFrame(False, index=idx, columns=cols, dtype=bool)
    if st_pit is None or st_pit.empty or mask.empty:
        return mask
    row_pos = {pd.Timestamp(d).strftime("%Y%m%d"): i for i, d in enumerate(idx)}
    col_pos = {s: i for i, s in enumerate(cols)}
    sparse = st_pit[st_pit["symbol"].isin(col_pos)]
    sparse = sparse[sparse["time"].astype(str).isin(row_pos)]
    if sparse.empty:
        return mask
    values = np.zeros(mask.shape, dtype=bool)  # pandas 3 CoW：to_numpy 只读
    values[
        sparse["time"].astype(str).map(row_pos).to_numpy(),
        sparse["symbol"].map(col_pos).to_numpy(),
    ] = True
    return pd.DataFrame(values, index=idx, columns=cols)


# 哨兵：区分「调用方没提 PIT」与「提了但表缺失」——后者必须炸，不能静默退回
# 有前视的静态口径。
_PIT_UNSET = object()


def build_universe(
    instr: pd.DataFrame | None,
    close: pd.DataFrame,
    min_hist: int = 120,
    *,
    st_pit=_PIT_UNSET,
) -> pd.DataFrame:
    """股票池布尔掩码（交易日 × symbol）。

    两种口径，**按显式参数切换**，不会悄悄混用：

    - ``st_pit`` 未传（遗留）：ST/退市用 ``instrument_detail`` **静态快照**
      （有前视，仅供旧口径对照/回归）；
    - ``st_pit`` 传 PIT 稀疏表：按**逐日** ST 状态过滤；``is_quit`` 静态列
      **不参与**——「现在已退市」套到全历史就是幸存者偏差，当日在市与否由
      close 是否有价格决定；
    - ``st_pit=None``（显式要 PIT 但表缺失）：抛 ``FileNotFoundError``，绝不
      静默退回有前视的口径。

    非 CN 传 ``instr=None``：ST/退市口径是 A 股专属，本市场无对应物 → 只做
    历史过滤（缺哪层过滤的事实由构建器写入快照 meta，绝不冒充）。
    通用规则：当日有价格、近 min_hist 日有效历史 ≥ min_hist*0.6（滤新股/长停）。
    """
    syms = close.columns
    if instr is not None and len(instr) == 0:
        # 反例锁死：把 instr 误传成空表必须全拒，不能当成「全部合格」
        return pd.DataFrame(False, index=close.index, columns=syms)
    if st_pit is _PIT_UNSET:
        if instr is None:
            base_ok = pd.Series(True, index=syms)
        else:
            meta = instr.set_index("symbol").reindex(syms)
            base_ok = (
                (
                    (meta["is_st"].astype(str) == "0")
                    & (meta["is_quit"].astype(str) == "0")
                )
                .reindex(syms)
                .fillna(False)
            )
    else:
        if st_pit is None:
            raise FileNotFoundError(
                "PIT ST 表缺失：先跑 backend/scripts/build_st_pit.py --apply 生成 "
                "2_base_sector/st_pit/st_pit.parquet"
            )
        base_ok = pd.Series(True, index=syms)
    valid = close.notna()
    hist = valid.rolling(min_hist, min_periods=min_hist).sum() >= int(min_hist * 0.6)
    mask = hist & base_ok.reindex(syms).fillna(False)
    if st_pit is not _PIT_UNSET:
        mask = mask & ~st_mask_over(close.index, syms, st_pit)
    return mask


def industry_map(instr: pd.DataFrame) -> pd.Series:
    """symbol → 行业名（申万细分）。"""
    return instr.set_index("symbol")["industry"].reindex(sorted(set(instr["symbol"])))
