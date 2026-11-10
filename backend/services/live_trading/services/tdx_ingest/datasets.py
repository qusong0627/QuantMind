"""通达信桥采集数据集注册表——声明式单一事实源（先例 ``quantdb_datasets.py``）。

一张表回答全部问题：采什么（key/name）、怎么采（method/批量）、采到哪
（table/pk/jsonb 列）、什么时候采（slots 档位表）、什么状态（blocked 家族 /
挂起）、单位口径（units，字段字典出处 = ``~/.claude/skills/tdx-tq-local/SKILL.md``）。

档位表（交易日，北京时间；探针实测耗时后的终表，见工程文档 §3.3）
------------------------------------------------------------------
===========  ==========================================================
15:31        pricevol 全 A 收盘快照（1 次调用）
15:45        universe 股票清单（分类 market 全刷）
16:45        more_info 全 A 88 字段（5581 次单股调用 ≈31min）
17:20        go 一致预期（分批）
17:30        gp 个股专业序列（分批；等数据包，blocked 期间秒退）
23:50        补采：gp / sc / bk / zdt（zdt 挂起）
06:30        早间补采：gp / sc / bk（目标日=上一交易日）
===========  ==========================================================

状态位（三态）
--------------
* 正常：跑；
* ``blocked_family=True``（GP/BK/SC/GO/FN 家族）：数据包未下载前桥恒回
  ``json has no table_list`` —— 档位照挂，遇错置 blocked（不重试不刷告警），
  数据包就位后**零改动自动转正常**；
* ``enabled=False``（zdt）：结构性全零（缺短线宝权限，已用东财涨停池交叉证伪），
  挂起不入库（全零入库=污染）；手动 ``--force`` 可单跑（盘中复测用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: ``get_stock_list`` 采集的分类 market 取值（官方文档 + 探针实测子集）。
#: 5=所有A股（核心，pricevol/more_info 的宇宙）；其余为常用分类清单。
MARKET_KEYS: tuple[str, ...] = (
    "5",  # 所有 A 股（5581）
    "22",  # 含可转债正股（313）
    "23",  # 沪深300
    "24",  # 中证500
    "25",  # 中证1000
    "26",  # 国证2000
    "27",  # 中证2000
    "28",  # 中证A500
    "30",  # REITs
    "31",  # ETF
    "32",  # 可转债（331）
    "33",  # LOF
    "34",  # 所有可交易基金
    "35",  # 所有沪深基金
    "36",  # T+0 基金
    "50",  # 沪深A股
    "51",  # 创业板
    "52",  # 科创板
    "53",  # 北交所
    "56",  # 沪深股通
    "57",  # 融资融券
    "91",  # ETF 追踪指数（445）
)

#: 全部 A 股清单的 market key（宇宙取数的唯一出处）。
ALL_A_MARKET_KEY = "5"

# —— 字段清单（发送口径：与官方字段表同名；GP/SC 补零两版探针都埋了，
#    数据包就位后重跑探针 --all 即可一次判明，改这里一处即可）——

GP_FIELDS: tuple[str, ...] = tuple(f"GP{i:02d}" for i in range(1, 47))  # GP01–GP46
BK_FIELDS: tuple[str, ...] = tuple(f"BK{i}" for i in range(5, 20))  # BK5–BK19
SC_FIELDS: tuple[str, ...] = tuple(f"SC{i:02d}" for i in range(1, 43))  # SC01–SC42
GO_FIELDS: tuple[str, ...] = tuple(f"GO{i}" for i in range(1, 48))  # GO1–GO47（不补零）

# —— 单位字典（v1/v2 双值列以逗号分隔；出处 = SKILL.md 字段表）——

_GP_UNITS: dict[str, str] = {
    "GP01": "户",
    "GP02": "万元,万元",
    "GP03": "万元,股",
    "GP04": "元,万元",
    "GP05": "元,股",
    "GP06": "股",
    "GP07": "万元",
    "GP08": "个,万元",
    "GP09": "个,万元",
    "GP10": "次,家",
    "GP11": "万元,万元",
    "GP12": "股,股",
    "GP13": "万元,股",
    "GP14": "万元,次",
    "GP15": "状态,万元",
    "GP16": "万元",
    "GP17": "万元,万元",
    "GP18": "万元,万元",
    "GP19": "万股,万股",
    "GP20": "%",
    "GP21": "%",
    "GP22": ",",  # 封成比,封流比（无量纲）
    "GP23": "万股,万股",
    "GP24": "时间,万元",
    "GP25": "手,手",
    "GP26": "万元,万元",
    "GP27": "名,名",
    "GP28": "元,万股",
    "GP29": "状态,状态",
    "GP30": "万元,股",
    "GP31": "股,股",
    "GP32": "股,元",
    "GP33": "万元,次",
    "GP34": "时间,万元",
    "GP35": "股,股",
    "GP36": "万元",
    "GP37": "天",
    "GP38": "次,次",
    "GP39": "%,%",
    "GP40": "%,时间",
    "GP41": "日期",
    "GP42": "万元,万元",
    "GP43": "元,万股",
    "GP44": "分",
    "GP45": "系数",
    "GP46": "万股,%",
}

_BK_UNITS: dict[str, str] = {
    "BK5": "倍,倍",
    "BK6": "倍,倍",
    "BK7": "倍,倍",
    "BK8": "倍,倍",
    "BK9": "家,家",
    "BK10": "亿元,亿元",
    "BK11": "亿元,亿元",
    "BK12": "家,家",
    "BK13": "家,家",
    "BK14": ",个",  # 市场高度,2板及以上个数
    "BK15": "万元,万元",
    "BK16": "亿元,亿元",
    "BK17": "万元,万股",
    "BK18": "%,%",
    "BK19": "亿元,亿元",
}

_SC_UNITS: dict[str, str] = {
    "SC01": "万元,万元",
    "SC02": "亿元,亿元",
    "SC03": "个,个",
    "SC04": "个,个",
    "SC05": "手",
    "SC06": "手",
    "SC07": "手",
    "SC08": "亿份,亿份",
    "SC09": "万户",
    "SC10": "万元,万元",
    "SC11": "万元,万元",
    "SC12": "亿元,亿元",
    "SC13": "亿元",
    "SC14": "亿元",
    "SC15": "亿元,亿元",
    "SC16": "亿元,亿元",
    "SC17": "亿元,亿元",
    "SC18": "亿元,亿元",
    "SC19": "亿元,亿元",
    "SC20": "亿元,亿元",
    "SC21": "%,%",
    "SC22": "%,%",
    "SC23": "个,个",
    "SC24": "个,个",
    "SC25": "万元,万股",
    "SC26": "%",
    "SC27": "亿元",
    "SC28": "个,个",
    "SC29": "个,个",
    "SC30": ",个",
    "SC31": "家,家",
    "SC32": "个,个",
    "SC33": "亿元,亿元",
    "SC34": "万手,万手",
    "SC35": "家,%",
    "SC36": "个,个",
    "SC37": "亿元,亿元",
    "SC38": "亿元,亿元",
    "SC39": "家,家",
    "SC40": "亿元,万笔",
    "SC41": "手",
    "SC42": "亿元,亿元",
}

# GO 大部分为纯数值字段；日期/文本型字段按字符串落 value_text（units 标"日期"）。
_GO_UNITS: dict[str, str] = {
    "GO1": "元",
    "GO2": "万股",
    "GO3": "元",
    "GO4": "年",
    "GO5": "元",
    "GO6": "元",
    "GO7": "元",
    "GO8": "万元",
    "GO9": "万元",
    "GO10": "万元",
    "GO11": "万元",
    "GO12": "万元",
    "GO13": "万元",
    "GO14": "万元",
    "GO15": "万元",
    "GO16": "万元",
    "GO17": "元",
    "GO18": "元",
    "GO19": "元",
    "GO20": "%",
    "GO21": "%",
    "GO22": "%",
    "GO23": "倍",
    "GO24": "倍",
    "GO25": "倍",
    "GO26": "日期",
    "GO27": "万股",
    "GO28": "日期",
    "GO29": "家",
    "GO30": "万股",
    "GO31": "家",
    "GO32": "万股",
    "GO33": "万股",
    "GO34": "万股",
    "GO35": "日期",
    "GO36": "万元",
    "GO37": "万元",
    "GO38": "%",
    "GO39": "%",
    "GO40": "日期",
    "GO41": "万元",
    "GO42": "万元",
    "GO43": "万元",
    "GO44": "万元",
    "GO45": "万元",
    "GO46": "%",
    "GO47": "%",
}


@dataclass(frozen=True)
class DatasetSpec:
    """单个采集数据集的声明（全部字段冻结；改档期/批量只动本表）。"""

    key: str
    name: str
    method: str  # 桥方法名
    table: str  # PG 表
    slots: tuple[str, ...]  # 档位 HH:MM（北京时间；见模块 docstring）
    batch_size: int  # 桥批量（≤500 纪律）
    est_calls: int  # 每轮预估调用数（面板/预算展示）
    pk_cols: tuple[str, ...]  # upsert 冲突键
    jsonb_cols: tuple[str, ...] = ()  # 需要 json.dumps 的列
    blocked_family: bool = False  # 数据包家族（table_list 失败置 blocked）
    # 空集不是合法结果的集（市场级序列）：桥返回空 → 记 blocked 而不是 ok。
    # 2026-11-10 实测 SC01–SC42 对 3 周区间全返空，但台账一直写 ok（静默绿）。
    require_nonempty: bool = False
    enabled: bool = True  # False=挂起（不入库）
    suspended_reason: str = ""
    fields: tuple[str, ...] = ()  # field_list（空=不带）
    units: dict[str, str] = field(default_factory=dict)
    desc: str = ""


DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        key="pricevol",
        name="全市场收盘快照",
        method="get_pricevol",
        table="tdx_pricevol_daily",
        slots=("15:31",),
        batch_size=6000,
        est_calls=1,
        pk_cols=("stock_code", "trade_date"),
        desc="全 A 一次调用（实测 3.1s）→ 昨收/现价/成交量(手)，zaf 派生",
    ),
    DatasetSpec(
        key="universe",
        name="股票清单快照",
        method="get_stock_list",
        table="tdx_stock_list_snapshot",
        slots=("15:45",),
        batch_size=1,
        est_calls=len(MARKET_KEYS),
        pk_cols=("trade_date", "market_key", "stock_code"),
        desc="分类 market 全刷（含名称）；pricevol/more_info 的宇宙来源",
    ),
    DatasetSpec(
        key="more_info",
        name="收盘指标全字段",
        method="get_more_info",
        table="tdx_more_info_daily",
        slots=("16:45",),
        batch_size=1,  # 桥单参分支丢弃 field_list，恒全字段 88 项
        est_calls=5581,
        pk_cols=("stock_code", "trade_date"),
        jsonb_cols=("raw",),
        desc="逐股单参调用（≈31min）；热列 + raw 88 字段全量",
    ),
    DatasetSpec(
        key="go",
        name="一致预期/一次性数据",
        method="get_gp_one_data",
        table="tdx_gp_one_data",
        slots=("17:20",),
        batch_size=50,
        est_calls=112,
        pk_cols=("stock_code", "trade_date", "field_code"),
        blocked_family=True,
        fields=GO_FIELDS,
        units=_GO_UNITS,
        desc="GO1–GO47（目标价/一致预期/解禁/机构/业绩预告）；等数据包",
    ),
    DatasetSpec(
        key="gp",
        name="个股专业序列",
        method="get_gpjy_value",
        table="tdx_gpjy_long",
        slots=("17:30", "23:50", "06:30"),
        batch_size=500,
        est_calls=12,
        pk_cols=("stock_code", "trade_date", "field_code"),
        blocked_family=True,
        fields=GP_FIELDS,
        units=_GP_UNITS,
        desc="GP01–GP46（龙虎榜/两融/陆股通/质押/涨跌停…）；range 调用返多日，全收",
    ),
    DatasetSpec(
        key="sc",
        name="市场级序列",
        method="get_scjy_value",
        table="tdx_scjy_long",
        slots=("23:50", "06:30"),
        batch_size=1,
        est_calls=1,
        pk_cols=("trade_date", "field_code"),
        blocked_family=True,
        require_nonempty=True,
        fields=SC_FIELDS,
        units=_SC_UNITS,
        desc="SC01–SC42（两融/北向/涨停家数/质押…）；无股票维度",
    ),
    DatasetSpec(
        key="bk",
        name="板块专业序列",
        method="get_bkjy_value",
        table="tdx_bkjy_long",
        slots=("23:50", "06:30"),
        batch_size=500,
        est_calls=2,
        pk_cols=("bk_code", "trade_date", "field_code"),
        blocked_family=True,
        fields=BK_FIELDS,
        units=_BK_UNITS,
        desc="BK5–BK19（板块估值/涨跌数/涨停数/两融…）；板块码取自 get_sector_list",
    ),
    DatasetSpec(
        key="zdt",
        name="涨跌停状态",
        method="get_zdt_data",
        table="tdx_zdt_daily",
        slots=("23:50",),
        batch_size=500,
        est_calls=12,
        pk_cols=("stock_code", "trade_date"),
        jsonb_cols=("raw",),
        enabled=False,
        suspended_reason="get_zdt_data 结构性全零（缺短线宝权限；10-16 盘中复测）",
        desc="15 字段状态体（封单/连板天数/首末次涨跌停时间）；解锁前不入库",
    ),
)

DATASETS_BY_KEY: dict[str, DatasetSpec] = {d.key: d for d in DATASETS}

#: 06:30 档的目标交易日 = 上一交易日（早间补采上一场，数据夜间发布）。
SLOT_TARGET_OFFSET: dict[str, int] = {"06:30": -1}


def get_dataset_spec(key: str) -> DatasetSpec:
    try:
        return DATASETS_BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"未知数据集: {key}（可用: {sorted(DATASETS_BY_KEY)}）"
        ) from None


def all_slots() -> tuple[str, ...]:
    """注册表里出现过的全部档位（去重保序）——加数据集自动进档位循环。"""
    seen: list[str] = []
    for spec in DATASETS:
        for slot in spec.slots:
            if slot not in seen:
                seen.append(slot)
    return tuple(seen)


def datasets_for_slot(slot: str) -> tuple[DatasetSpec, ...]:
    return tuple(d for d in DATASETS if slot in d.slots)


__all__ = [
    "ALL_A_MARKET_KEY",
    "BK_FIELDS",
    "DATASETS",
    "DATASETS_BY_KEY",
    "DatasetSpec",
    "GO_FIELDS",
    "GP_FIELDS",
    "MARKET_KEYS",
    "SC_FIELDS",
    "SLOT_TARGET_OFFSET",
    "all_slots",
    "datasets_for_slot",
    "get_dataset_spec",
]
