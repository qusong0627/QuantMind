"""因子报告支持的数据集注册表（构建脚本与 API 服务共用，避免两处口径漂移）。

市场分层（2026-10-16）：注册表按市场分组（``DATASETS_BY_MARKET``）。CN 保持
既有 14 个数据集的完整语义；HK/US/FUTURES 各接本市场 ``l1_factors``（HK 另接
ccass/south 两个 donor 库）。访问器一律带 ``market``（缺省 ``"CN"``，零行为
变化），目录 = ``resolve_market_root(market) / <dir_parts>``。
**未接入的市场不得静默回退 CN**：``_registry`` 对未接入市场抛 ``ValueError``
（路由层转 422 + reason）；未知数据集只回落到**本市场**的缺省数据集。

每个数据集声明：
  dir_parts    —— 相对**本市场数据根**的目录（经 ``resolve_market_root`` 解析：
                  CN 走 quantdb_paths 候选表、其余市场走各市场 env/默认路径；
                  禁硬编码绝对路径）
  label_mode   —— labels_table：独立标签表（含 fwd_ret_* 列）
                  close_fwd   ：用本表 close 在 T+k 分区的收盘价算 (close_{T+k}/close_T - 1)
                                与训练侧 features_daily.return_Nd 同口径
                  close_fwd_donor：同上，但本表**无 OHLCV**，行情列由 donor 库提供
                                （donor_parts；rd_mined 走这条）
  label_parts  —— labels_table 模式的标签目录
  donor_parts  —— close_fwd_donor 模式的行情列来源目录（其余模式不声明）
  meta_cols    —— 非因子列（标识 + OHLCV + 血缘元数据）；构建时再按 dtype 兜一层
  library_rule —— 因子库归属规则：alpha_prefix（按 a101/a158/gtja 前缀）|
                  fixed:L1 / fixed:L2 | l2_membership（L1+L2 表按是否也在 L2 表中判定）
  build_start  —— 一键重建的默认起点（YYYYMMDD）。构建器 --years 默认 5 年，
                  会把这个日期之后的早段静默砍掉；有 build_start 的数据集必须显式带
"""

from __future__ import annotations

from pathlib import Path

from backend.shared.futures_factors.registry import META_COLS as _FUTURES_META_COLS

_ID_COLS = ("symbol", "date", "time", "dt")
_OHLCV_COLS = ("open", "high", "low", "close", "volume", "amount")

# 前视收益（未来收益标签），**不是因子**，各模式构建时必须排除——
# 进了因子轴就会以 |IC|≈1 霸榜、经 kept 清单漏进训练面板（P0 泄露）。
# features_daily 实测两代命名：≤20260918 分区叫 return_Nd，20260930 起改
# future_return_Nd；两代全列，防旧分区漏网。
_FUTURE_RETURN_COLS = (
    "future_return_1d",
    "future_return_3d",
    "future_return_5d",
    "future_return_10d",
    "future_return_20d",
    "future_return_60d",
)
_LEGACY_RETURN_COLS = (
    "return_1d",
    "return_3d",
    "return_5d",
    "return_10d",
    "return_20d",
    "return_60d",
)

# HK/US/FUTURES 的 l1_factors 由各市场自己的构建链产出，表首混入 10 个
# **非因子原始列**（复权/换手/估值/市值——行情与估值输入，不是截面因子）；
# CN l1_factors 不含这些列（20261015 footer 实测：CN 119 列，HK 190 / US 176 /
# FUT 190，非 CN 表头 = symbol + 6 OHLCV + 这 10 列 + ~174 因子）。不剔除会让
# 「市值本身」进因子轴——市值对收益的解释力会把真因子全压出榜单。
#
# 公开名（无 ``_`` 前缀）：因子研究（factor_research.discovery）扫描非 CN 库时
# 也须剔除同一组列——两处各写一份名单迟早漂移，此处是唯一出处。
RAW_MARKET_COLS = (
    "adj_factor",
    "turnover_rate",
    "pe_ttm",
    "pb",
    "roe",
    "bp",
    "ep_ttm",
    "float_mv",
    "total_mv",
    "ln_mv_total",
)

_CN_DATASETS: dict[str, dict] = {
    "alpha_library": {
        "label": "Alpha 库（Alpha101 / GTJA191 / Alpha158）",
        "dir_parts": ("6_ml_datasets", "alpha_library"),
        "label_mode": "labels_table",
        "label_parts": ("6_ml_datasets", "alpha_library_labels"),
        "meta_cols": _ID_COLS,
        "library_rule": "alpha_prefix",
        "universe": "A股全市场 · Alpha 库（Alpha101/GTJA191/Alpha158）",
    },
    # 因子清单库：只含**日频可复现**的因子。
    # ⚠️ 分钟族、财务 PIT 族，以及需要 point-in-time 指数成分股的，都不在内
    # （各自的缺口原因不同，补齐所需的数据依赖也不同）。
    # 具体的纳入/剔除清单见 <数据集>/MANIFEST.json。
    # 标签复用 alpha_library_labels：同一份 load_kline 网格、且标签只依赖 close。
    "factor_defs": {
        "label": "因子清单库（日频可复现部分）",
        "dir_parts": ("6_ml_datasets", "factor_defs"),
        "label_mode": "labels_table",
        "label_parts": ("6_ml_datasets", "alpha_library_labels"),
        "meta_cols": _ID_COLS,
        # 暂无子库切分：源清单的「分类」字段 46% 是「其他/未分类」，硬切反而误导；
        # 单库直出排行表，要按家族筛再看是否需要规则化。
        "library_rule": "fixed:factor_defs",
        "universe": "A股全市场 · 因子清单库（日频可复现因子）",
    },
    # L1 快照评估期自 20170103 起（2016 年为因子回看暖机段，不纳入评估）。
    # **必须显式 build_start**：盘上 L1 数据自 20160104 起，而构建器默认
    # --years 5 会把 2017–2021 静默砍掉——一键重建后页面的「评估期」会从
    # 2017 变成 2021，且全链无报错（2026-10-21 发现）。
    "l1_factors": {
        "label": "L1 因子（量价/换手/波动等基础因子）",
        "dir_parts": ("6_ml_datasets", "l1_factors"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:L1",
        "universe": "A股全市场 · L1 因子（量价/换手/波动等基础因子）",
        "build_start": "20170103",
    },
    "l2_factors": {
        "label": "L2 因子（逐笔微观结构，覆盖期短于 L1）",
        "dir_parts": ("6_ml_datasets", "l2_factors"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:L2",
        "universe": "A股全市场 · L2 因子（逐笔微观结构）",
    },
    "l1_l2_factors": {
        "label": "L1 + L2 合并因子",
        "dir_parts": ("6_ml_datasets", "l1_l2_factors"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "l2_membership",
        "universe": "A股全市场 · L1+L2 合并因子",
    },
    "tdxgs": {
        "label": "TDXGS 通达信技术指标（88）",
        "dir_parts": ("6_ml_datasets", "tdxgs"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:tdxgs",
        "universe": "A股全市场 · 通达信/同花顺技术指标（MyTT 口径）",
    },
    "jq110": {
        "label": "JQ110 聚宽因子（109）",
        "dir_parts": ("6_ml_datasets", "jq110"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:jq110",
        "universe": "A股全市场 · 聚宽策略因子（动量/情绪/技术/风险/风格）",
    },
    "alpha360": {
        "label": "Alpha360 原始量价回溯（360）",
        "dir_parts": ("6_ml_datasets", "alpha360"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:alpha360",
        "universe": "A股全市场 · 60 日原始量价序列（DL 用）",
    },
    # GAP_MINED_MARK: 空档挖掘因子库（2026-09 从 QuantDB 未开采矿脉挖出，69 因子）
    "gap_mined": {
        "label": "空档挖掘因子（股东机构/财务PIT/板块概念/两融/L2二阶/新闻情绪，69）",
        "dir_parts": ("6_ml_datasets", "gap_mined"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "gap_family",
        "universe": "A股全市场 · 空档挖掘因子（2020-01 起，L2二阶自 2022-01）",
    },
    # RD_MINED_MARK: RD-Agent 挖掘因子库（2026-10-09 接入）。
    # 数据链：RD-Agent 演化 → PG rd_agent_factors → 物化到 quantcustom 6_ml_datasets/rd_mined
    # → 毕业桥（backend/scripts/promote_rd_mined.py --register）镜像到本 CN 根。
    # 本表**没有 OHLCV**（列 = symbol/date + rd_* 因子），行情列由 l1_factors 充当
    # donor（label_mode=close_fwd_donor）——l1 与 rd_mined 同为后缀式 symbol、逐日
    # 全市场快照，20260930 实测代码重叠 94%，适合做收益基准。
    # 因子数**不写进 label**：挖掘每天产出，硬编码计数第二天就假。
    "rd_mined": {
        "label": "RD-Agent 挖掘因子",
        "dir_parts": ("6_ml_datasets", "rd_mined"),
        "label_mode": "close_fwd_donor",
        "donor_parts": ("6_ml_datasets", "l1_factors"),
        "meta_cols": _ID_COLS,
        "library_rule": "fixed:rd_mined",
        "universe": "A股全市场 · RD-Agent 自动挖掘因子（行情列取自 L1 因子库）",
        "build_start": "20200102",
    },
    # FACTOR1300_MARK: 因子清单1300（2026-10-15 物化）。
    # 源 = 私人收藏《1300 个因子.xlsx》清洗后的 1125 条公式，物化到 6_ml_datasets/factor1300
    # （列 = f13_XXXX/Alpha*，symbol/time/dt，无 OHLCV）。与 rd_mined 同构：
    # label_mode=close_fwd_donor，行情列由 l1_factors 充当 donor（同为 20160104 起、
    # 逐日后缀式全市场快照，20261013 实测代码重叠 93.5%）。
    "factor1300": {
        "label": "因子清单1300（收藏公式物化库）",
        "dir_parts": ("6_ml_datasets", "factor1300"),
        "label_mode": "close_fwd_donor",
        "donor_parts": ("6_ml_datasets", "l1_factors"),
        "meta_cols": _ID_COLS,
        "library_rule": "fixed:factor1300",
        "universe": "A股全市场 · 因子清单1300（行情列取自 L1 因子库）",
        "build_start": "20160104",
    },
    # CAND_FACTORS_MARK: 候选因子池（2026-10-15 纳入筛选/体检）。
    # 与 factor_defs **同名 238 列**（后者为其超集来源：快照 1336 − 238 = 私人库 1098，
    # 实测集合双向相等）。私人库扫描按字母序 cand_factors 先于 factor_defs 先到先得，
    # 筛选侧 REPORT_DATASETS 同序去重保持一致。close 与 l1_factors 同日同股比值恒 1.0。
    "cand_factors": {
        "label": "候选因子池（238）",
        "dir_parts": ("6_ml_datasets", "cand_factors"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS + _OHLCV_COLS,
        "library_rule": "fixed:cand_factors",
        "universe": "A股全市场 · 候选因子池（与因子清单库同名同源）",
        "build_start": "20160104",
    },
    # DAILY_FEATURES_MARK: 每日特征（技术+估值）（2026-10-15 纳入筛选/体检）。
    # ⚠️ 列代际三朝（实测）：≤20260630 分区 50 列（无 is_st/free_float_shares，旧名
    # return_Nd + Symbol_val/close_val）；20260918 分区 78 列；20260930 起标签改名
    # future_return_Nd。因子列取窗口内**最新分区**的 schema → 41 个；
    # 构建 worker 对旧分区缺列走 reindex 补 NaN（全退化口径）。close 与 l1 实测恒等。
    "features_daily": {
        "label": "每日特征（技术+估值）",
        "dir_parts": ("6_ml_datasets", "features_daily"),
        "label_mode": "close_fwd",
        "meta_cols": _ID_COLS
        + ("close", "close_val")
        + _LEGACY_RETURN_COLS
        + _FUTURE_RETURN_COLS,
        "library_rule": "fixed:features_daily",
        "universe": "A股全市场 · 每日特征（技术+估值，41 因子；前视收益列已排除）",
        "build_start": "20160104",
    },
    # L1_BATCH1_MARK: l1_batch1_factors（2026-10-15 纳入筛选/体检）。
    # 8 个批量因子、2584 分区稳定 11 列、无 OHLCV → donor 取 l1_factors；
    # 后缀式 symbol 与 l1 同日 5211/5211 全交集（20261009 实测）。
    "l1_batch1_factors": {
        "label": "L1 批量因子（batch1，8）",
        "dir_parts": ("6_ml_datasets", "l1_batch1_factors"),
        "label_mode": "close_fwd_donor",
        "donor_parts": ("6_ml_datasets", "l1_factors"),
        "meta_cols": _ID_COLS,
        "library_rule": "fixed:l1_batch1_factors",
        "universe": "A股全市场 · l1_batch1 批量因子（行情列取自 L1 因子库）",
        "build_start": "20160219",
    },
}

DEFAULT_DATASET = "alpha_library"

# HK/US/FUTURES 各接本市场 l1_factors（唯一带 OHLCV 的通用因子库）；
# HK 另有 ccass/south 两个持股因子库（无 OHLCV，donor 走 HK l1_factors——
# 两库 symbol 同为 0001.HK 四位后缀式，20261015 实测逐日全交集，可 join）。
# 标签一律走 close_fwd(_donor)：日期只从分区目录名取，各市场 l1 表无日期列
# 也无碍（HK/US/FUT l1 实测只有 symbol + 数值列）。
DATASETS_BY_MARKET: dict[str, dict[str, dict]] = {
    "CN": _CN_DATASETS,
    "HK": {
        "l1_factors": {
            "label": "L1 因子（量价/换手/波动等基础因子）",
            "dir_parts": ("6_ml_datasets", "l1_factors"),
            "label_mode": "close_fwd",
            "meta_cols": _ID_COLS + _OHLCV_COLS + RAW_MARKET_COLS,
            "library_rule": "fixed:L1",
            "universe": "港股全池（无 ST/退市过滤） · L1 因子（量价/换手/波动等基础因子）",
        },
        "ccass_factors": {
            "label": "CCASS 中央结算持股因子（22）",
            "dir_parts": ("6_ml_datasets", "ccass_factors"),
            "label_mode": "close_fwd_donor",
            "donor_parts": ("6_ml_datasets", "l1_factors"),
            # 两个诊断列不是因子：是否可算 pct / 是否撞分母异常
            "meta_cols": _ID_COLS + ("ca_pct_usable", "ca_pct_bad_sum"),
            "library_rule": "fixed:ccass",
            "universe": "港股 CCASS 覆盖池 · 中央结算持股因子（行情列取自 L1 因子库）",
            "build_start": "20241125",
        },
        "south_factors": {
            "label": "港股通南向持股因子（7）",
            "dir_parts": ("6_ml_datasets", "south_factors"),
            "label_mode": "close_fwd_donor",
            "donor_parts": ("6_ml_datasets", "l1_factors"),
            "meta_cols": _ID_COLS,
            "library_rule": "fixed:south",
            "universe": "港股通标的池 · 南向持股因子（行情列取自 L1 因子库）",
            "build_start": "20241127",
        },
    },
    "US": {
        "l1_factors": {
            "label": "L1 因子（量价/换手/波动等基础因子）",
            "dir_parts": ("6_ml_datasets", "l1_factors"),
            "label_mode": "close_fwd",
            "meta_cols": _ID_COLS + _OHLCV_COLS + RAW_MARKET_COLS,
            "library_rule": "fixed:L1",
            "universe": "美股全池（无 ST/退市过滤） · L1 因子（量价/换手/波动等基础因子）",
        },
    },
    "FUTURES": {
        "l1_factors": {
            "label": "L1 因子（量价/换手/波动等基础因子）",
            "dir_parts": ("6_ml_datasets", "l1_factors"),
            "label_mode": "close_fwd",
            "meta_cols": _ID_COLS + _OHLCV_COLS + RAW_MARKET_COLS,
            "library_rule": "fixed:L1",
            "universe": "期货主连+外盘品种池 · L1 因子（量价/换手/波动等基础因子）",
        },
        "futures_factors": {
            "label": "机构级期货因子库（期限结构/持仓/基差/库存/横截面）",
            "dir_parts": ("6_ml_datasets", "futures_factors"),
            "label_mode": "close_fwd",
            # meta_* 是可得性标记（0/1），不是 alpha，进因子轴会把 IC 榜单刷成
            # 「谁有库存数据」而不是「谁预测得准」。
            "meta_cols": _ID_COLS + _OHLCV_COLS + _FUTURES_META_COLS,
            "library_rule": "fixed:futures_institutional",
            "universe": "期货主连品种池 · 机构级因子（期限结构/持仓/基差/库存/横截面）",
        },
    },
}

DEFAULT_DATASET_BY_MARKET: dict[str, str] = {
    "CN": DEFAULT_DATASET,
    "HK": "l1_factors",
    "US": "l1_factors",
    "FUTURES": "l1_factors",
}

#: 兼容别名：CN 注册表（存量调用点最多，语义与分层前逐字相同）
DATASETS = DATASETS_BY_MARKET["CN"]


def market_key(market: str | None) -> str:
    """规范市场键（大小写/空白归一；空值按 CN 缺省）。

    service/router 的缓存键与响应也都用它——HK/US/FUTURES 共用 ``l1_factors``
    数据集名，任何跨市场缓存/存储键必须以此前缀区分，否则串数据。
    """
    return str(market or "CN").upper().strip()


def supports_market(market: str | None) -> bool:
    """该市场是否已接入因子报告（路由层据此给 422 + reason，而非静默落 CN）。"""
    return market_key(market) in DATASETS_BY_MARKET


def datasets_for_market(market: str = "CN") -> dict[str, dict]:
    """该市场的注册表（未接入市场返回空 dict，不做跨市场回退）。"""
    return DATASETS_BY_MARKET.get(market_key(market), {})


def _registry(market: str | None) -> dict[str, dict]:
    key = market_key(market)
    reg = DATASETS_BY_MARKET.get(key)
    if reg is None:
        raise ValueError(
            f"因子报告未接入市场: {market!r}（已接入: {', '.join(DATASETS_BY_MARKET)}）"
        )
    return reg


def _cfg(dataset: str, market: str | None = "CN") -> tuple[str, dict]:
    """``(规范市场键, 配置)``。未知数据集回落**本市场**缺省，不跨市场回退。"""
    key = market_key(market)
    reg = _registry(key)
    default = DEFAULT_DATASET_BY_MARKET.get(key) or next(iter(reg))
    return key, (reg.get(dataset) or reg[default])


def _market_root(key: str) -> Path:
    # 懒导入：本模块的存量调用点（eval 脚本等）只依赖标准库 + quantdb_paths，
    # feature_source 会拉进 pandas，保持 CN 路径零新增导入面的既有事实。
    from backend.shared.feature_source import resolve_market_root

    return resolve_market_root(key)


def dataset_names(market: str = "CN") -> list[str]:
    return list(_registry(market))


def dataset_dir(dataset: str, market: str = "CN") -> Path:
    key, cfg = _cfg(dataset, market)
    return _market_root(key) / Path(*cfg["dir_parts"])


def label_dir(dataset: str, market: str = "CN") -> Path | None:
    key, cfg = _cfg(dataset, market)
    parts = cfg.get("label_parts")
    return _market_root(key) / Path(*parts) if parts else None


def report_dir(dataset: str, market: str = "CN") -> Path:
    return dataset_dir(dataset, market) / "report"


def donor_dir(dataset: str, market: str = "CN") -> Path | None:
    """close_fwd_donor 模式的行情列来源目录；未声明 donor_parts 的数据集返回 None
    （构建器据此拒绝启动 donor 分支，而不是悄悄回退到不存在的本表 close）。"""
    key, cfg = _cfg(dataset, market)
    parts = cfg.get("donor_parts")
    return _market_root(key) / Path(*parts) if parts else None


def disk_state(dataset: str, market: str = "CN") -> dict:
    """盘上分区现状（只读 parquet footer）：``{n_factors, first_dt, last_dt}``。

    因子列口径与构建器一致：meta_cols 之外、dtype 以数值前缀开头的列。
    最新分区损坏/缺文件时 n_factors 记 None（页面不显示坏数），日期照报。
    """
    root = dataset_dir(dataset, market)
    parts = sorted(p for p in root.glob("dt=*") if p.is_dir())
    if not parts:
        return {"n_factors": None, "first_dt": None, "last_dt": None}
    first_dt = parts[0].name.split("=", 1)[1]
    last_dt = parts[-1].name.split("=", 1)[1]
    _, cfg = _cfg(dataset, market)
    meta = set(cfg["meta_cols"])
    try:
        import pyarrow.parquet as pq

        sch = pq.ParquetFile(f"{parts[-1]}/data.parquet").schema_arrow
    except Exception:  # noqa: BLE001 — 读不了列集不影响「数据到哪天」
        return {"n_factors": None, "first_dt": first_dt, "last_dt": last_dt}
    numeric_prefix = ("float", "double", "int", "decimal")
    cols = [
        c for c in sch.names
        if c not in meta and str(sch.field(c).type).startswith(numeric_prefix)
    ]
    return {"n_factors": len(cols), "first_dt": first_dt, "last_dt": last_dt}


def l2_columns(market: str = "CN") -> set[str]:
    """l2_factors 的列集合（用于 L1+L2 表的归属判定；仅 CN 注册表用该规则）。"""
    root = dataset_dir("l2_factors", market)
    parts = sorted(p for p in root.glob("dt=*") if p.is_dir())
    if not parts:
        return set()
    try:
        import pyarrow.parquet as pq

        return set(pq.ParquetFile(f"{parts[-1]}/data.parquet").schema_arrow.names)
    except Exception:  # noqa: BLE001
        return set()


def library_of(
    dataset: str, column: str, l2_cols: set[str] | None = None, market: str = "CN"
) -> str:
    _, cfg = _cfg(dataset, market)
    rule = str(cfg["library_rule"])
    if rule.startswith("fixed:"):
        return rule.split(":", 1)[1]
    if rule == "alpha_prefix":
        if column.startswith("a158"):
            return "alpha158"
        if column.startswith("a101"):
            return "alpha101"
        return "gtja191"
    if rule == "l2_membership":
        cols = l2_cols if l2_cols is not None else l2_columns(market)
        return "L2" if column in cols else "L1"
    if rule == "gap_family":
        # GAP_MINED_MARK: 按因子名前缀判子库（因子名自带家族语义）
        if column.startswith("HD_"):
            return "股东机构筹码"
        if column.startswith("FIN_"):
            return "财务基本面PIT"
        if column.startswith("MG2_"):
            return "融资融券"
        if column.startswith("N2_"):
            return "北向持股"
        if column.startswith(("IND_", "CONCEPT_")):
            return "板块概念"
        if column.startswith("NEWS_"):
            return "新闻情绪"
        return "L2二阶微观结构"
    return "unknown"
