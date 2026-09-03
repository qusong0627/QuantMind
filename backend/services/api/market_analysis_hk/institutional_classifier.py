"""CCASS 参与者分类规则 — 机构持仓分析（内资/港资/外资·亚太/外资·欧美）。

纯规则模块，不依赖数据目录（人工覆盖表经 load_overrides(data_dir) 注入）。
规则表依据 2026-09-03 对 ccass_top50 全量 504 个参与者名册逐条核对的种子词，
桶检查顺序即消歧顺序（先查最特异的长词，后查通用词）：
  southbound/hkscc（ID+特例）→ apac（中國信託、國泰證券 等台湾/日韩/东南亚系）
  → us_eu（欧美系）→ cn_broker（中資系）→ hk 白名单 → 默认桶（C/B 前缀 → 港资）。

分类必有误差（名称关键词启发式），配套「参与者分类审计」接口 + 人工覆盖表
institutional_overrides.parquet（participant_id 精确匹配优先，其次名称精确匹配，
category="other" 表示强制排除）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# ---- 类别 ID 与展示名 ----
CATEGORY_SOUTHBOUND = "southbound"  # 内资·港股通（境内结算席，持股与港股通披露一致）
CATEGORY_CN_BROKER = "cn_broker"  # 内资·中資券商/银行
CATEGORY_HK = "hk"  # 港资（本地券商/银行，默认桶）
CATEGORY_US_EU = "us_eu"  # 外资·欧美
CATEGORY_APAC = "apac"  # 外资·亚太（除中港外的亚洲）
CATEGORY_OTHER = "other"  # 个人/零售/未识别
CATEGORY_HKSCC = "hkscc"  # 香港中央結算(代理人) — 托管池，防御规则，不参与分类加总

CATEGORY_LABELS = {
    CATEGORY_SOUTHBOUND: "内资·港股通",
    CATEGORY_CN_BROKER: "内资·中資券商",
    CATEGORY_HK: "港资",
    CATEGORY_US_EU: "外资·欧美",
    CATEGORY_APAC: "外资·亚太",
    CATEGORY_OTHER: "其他",
    CATEGORY_HKSCC: "托管池(HKSCC)",
}

# ---- 席位性质 ----
KIND_SETTLEMENT = "settlement"  # 结算/登记（南向 A 席）
KIND_BROKER = "broker"  # 券商（经纪/自营）
KIND_CUSTODIAN = "custodian"  # 托管银行/信托
KIND_OTHER = "other"

KIND_LABELS = {
    KIND_SETTLEMENT: "结算",
    KIND_BROKER: "券商",
    KIND_CUSTODIAN: "托管",
    KIND_OTHER: "其他",
}

# 展示顺序（稳定输出）
CATEGORY_ORDER = (
    CATEGORY_CN_BROKER,
    CATEGORY_SOUTHBOUND,
    CATEGORY_HK,
    CATEGORY_US_EU,
    CATEGORY_APAC,
    CATEGORY_OTHER,
)

# ---- 特例 ID ----
# 港股通境内结算席（中国证券登记结算）：持股与港股通披露一致（2026-09 逐股实测 ±2% 内）
# A00003/A00004 = 沪深通主席 A00002/A00006 = 少量补充账户
_SOUTHBOUND_IDS = frozenset({"A00002", "A00003", "A00004", "A00006"})
# 中国证券登记结算(香港)有限公司：非港股通通道持仓（H 股大宗存量等，2026-09 实测
# 209 只股票其中 120 只不在南向披露、且与披露口径逐股不重合）——仍属中资，归内资桶
_CN_SETTLE_IDS = frozenset({"A00005"})

# ---- 关键词表（繁体为主，HKEX 原文；桶查序即消歧序） ----

# 外资·亚太（中港之外）：台湾/日本/韩国/新加坡/泰国/马来西亚 系
_APAC_TOKENS = (
    "中國信託", "中信託", "國泰證券",  # 台湾（注意与國泰君安消歧，见下）
    "台新", "永豐金", "元大", "群益", "凱基", "富邦", "中國信託綜合",
    "大華繼顯", "未來資產", "韓國投資", "京華山一", "軟庫",
    "瑞穗", "MIZUHO", "MUFG", "三菱",
    "岡三", "東洋", "大和", "星展", "DBS", "華僑", "OCBC",
    "盤谷", "馬銀", "奕豐", "大眾金融", "大眾銀行", "輝立", "PHILLIP",
)

# 外资·欧美：美国/欧洲投行与银行
_US_EU_TOKENS = (
    "MORGAN STANLEY", "J.P. MORGAN", "JPMORGAN", "摩根大通", "GOLDMAN", "高盛",
    "UBS", "CITIBANK", "花旗", "STATE STREET", "道富", "BANK OF NEW YORK", "紐約梅隆",
    "NORTHERN TRUST", "BROWN BROTHERS", "BANK OF AMERICA", "MLFE",
    "CREDIT SUISSE", "瑞信", "DEUTSCHE", "德意志", "BNP", "法國巴黎",
    "SOCIETE GENERALE", "CREDIT AGRICOLE", "ABN AMRO", "COMMERZBANK",
    "STANDARD CHARTERED", "渣打", "巴克萊", "BARCLAYS", "富瑞", "JEFFERIES",
    "盈透", "INTERACTIVE BROKERS", "瑞士盈豐", "SOFI", "建達",
)

# 内资：中資券商/银行（通用「中國」兜底放最后；长词先于其消歧）
_CN_TOKENS = (
    "中國國際金融", "國泰君安", "中信建投", "中信里昂", "中信証券", "中信證券",
    "申萬宏源", "中銀國際", "中國銀河", "中泰國際", "中州國際", "中財",
    "交銀", "交通銀行", "建銀", "工銀", "農銀", "招銀", "民銀", "浦銀", "光銀",
    "信銀", "上銀", "南洋", "集友",
    "光大", "華泰", "廣發", "銀河", "海通", "招商", "方正", "東方證券(香港)",
    "平安", "復星", "越秀", "長江", "興證國際", "興業", "東吳", "東興", "東海",
    "財通", "浙商", "山證國際", "山高", "信達", "國元", "國信", "國投", "國都",
    "國聯", "國創", "國農", "華興", "華福", "華安", "華金", "山西證", "哈富",
    "富途", "老虎", "長橋", "華盛", "微牛", "雲鋒", "尊嘉", "佳兆業",
    "天風", "中原", "粵商", "大灣區", "混沌天成", "第一上海", "清科", "太平",
    "浦東發展", "中國",
)

# 港资白名单（默认桶之外的显式港资；主要防误配与文档化）
_HK_TOKENS = (
    "香港上海匯豐", "匯豐", "滙豐", "HSBC", "恒生", "創興", "東亞",
    "大新", "耀才", "英皇", "時富", "金利豐", "致富", "結好", "南華", "六福",
    "上海商業",
)

# 托管性质特征词（C 席默认托管；B 席命中说明兼营托管）
_BANK_TOKENS = ("銀行", "BANK", "信託", "TRUST")

# 结算/登记特征词
_SETTLE_TOKENS = ("證券登記結算", "証券登記結算", "證券登記結算(香港)", "CSDC")
# 个人/零售特征
_PERSONAL_TOKENS = ("珠寶金行", "金行", "錢庄", "找換")


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def _upper(name: str) -> str:
    return (name or "").upper()


def _kind_for(participant_id: str | None, name: str) -> str:
    """席位性质：结算席 / 托管行 / 券商 / 其他。

    名称含「信託」不一定是托管（如 中國信託綜合證券 是券商）——
    有「證券」字样时按券商计，除非同时出现银行类字样。
    """
    pid = participant_id or ""
    if pid.startswith("A"):
        return KIND_SETTLEMENT
    if pid.startswith("P"):
        return KIND_OTHER
    up = _upper(name)
    if pid.startswith("C") or any(tok in up for tok in _BANK_TOKENS):
        if "證券" in up or "证券" in up:
            return KIND_BROKER if not any(tok in up for tok in ("銀行", "BANK")) else KIND_CUSTODIAN
        return KIND_CUSTODIAN
    return KIND_BROKER


def _match_tokens(name_up: str, tokens: tuple[str, ...]) -> bool:
    return any(tok in name_up for tok in tokens)


def recognize(participant_id: str | None, participant_name: str) -> tuple[str, str] | None:
    """ID/名称特例识别（先于关键词表）。返回 (category, kind) 或 None。"""
    pid = participant_id or ""
    if pid in _CN_SETTLE_IDS:
        # 中国结算(香港)：中资结算机构，但持仓非港股通口径（见 _CN_SETTLE_IDS 注释）
        return (CATEGORY_CN_BROKER, KIND_SETTLEMENT)
    up = _upper(participant_name)
    if pid in _SOUTHBOUND_IDS or _match_tokens(up, _SETTLE_TOKENS):
        return (CATEGORY_SOUTHBOUND, KIND_SETTLEMENT)
    if _match_tokens(up, ("香港中央結算", "香港中央结算", "HKSCC")):
        # 代理人托管池：当前数据源不含该席（2026-09 实测），防御未来变更
        return (CATEGORY_HKSCC, KIND_OTHER)
    if pid.startswith("P"):
        # P = 个人参与者/零售（如 周大福珠寶金行）
        return (CATEGORY_OTHER, KIND_OTHER)
    if "*" in participant_name or _match_tokens(up, _PERSONAL_TOKENS):
        return (CATEGORY_OTHER, KIND_OTHER)
    return None


def classify(
    participant_id: str | None,
    participant_name: str,
    overrides: pd.DataFrame | None = None,
) -> tuple[str, str]:
    """参与者 → (category, kind)。

    优先级：人工覆盖表 > 特例识别 > 关键词表（apac → us_eu → cn → hk 白名单）
    > 默认桶（C/B 前缀 → 港资；其余 → 其他）。
    """
    name = (participant_name or "").strip()
    if overrides is not None and len(overrides):
        pid_str = "" if participant_id is None else str(participant_id)
        by_id = overrides[
            overrides["participant_id"].notna()
            & overrides["participant_id"].astype(str).eq(pid_str)
        ]
        if len(by_id):
            row = by_id.iloc[0]
            if isinstance(row["category"], str) and row["category"]:
                return (row["category"], str(row.get("kind") or _kind_for(participant_id, name)))
        by_name = overrides[
            overrides["participant_name"].notna()
            & overrides["participant_name"].astype(str).eq(name)
        ]
        if len(by_name):
            row = by_name.iloc[0]
            if isinstance(row["category"], str) and row["category"]:
                return (row["category"], str(row.get("kind") or _kind_for(participant_id, name)))

    special = recognize(participant_id, name)
    if special is not None:
        return special

    up = _upper(name)
    for category, tokens in (
        (CATEGORY_APAC, _APAC_TOKENS),
        (CATEGORY_US_EU, _US_EU_TOKENS),
        (CATEGORY_CN_BROKER, _CN_TOKENS),
        (CATEGORY_HK, _HK_TOKENS),
    ):
        if _match_tokens(up, tokens):
            return (category, _kind_for(participant_id, name))

    if (participant_id or "").startswith(("C", "B")):
        return (CATEGORY_HK, _kind_for(participant_id, name))
    return (CATEGORY_OTHER, _kind_for(participant_id, name))


# ---- 人工覆盖表 ----

_OVERRIDE_COLS = ("participant_id", "participant_name", "category", "kind", "reason", "updated_at")


def load_overrides(data_dir: Path | str) -> pd.DataFrame:
    """读人工覆盖表（{data_dir}/2_base_sector/institutional_overrides.parquet）。

    不存在或损坏时返回空表（分类器继续用关键词规则，不抛错）。
    """
    path = Path(data_dir) / "2_base_sector" / "institutional_overrides.parquet"
    if not path.exists():
        return pd.DataFrame(columns=list(_OVERRIDE_COLS))
    try:
        df = pd.read_parquet(path)
        for col in _OVERRIDE_COLS:
            if col not in df.columns:
                df[col] = pd.NA
        return df[_OVERRIDE_COLS]
    except Exception:  # noqa: BLE001 - 覆盖表损坏不阻断主链路
        return pd.DataFrame(columns=list(_OVERRIDE_COLS))
