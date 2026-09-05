"""Built-in semantic dictionary for QuantDB A-share training factors.

Definitions are derived from ``300_factors_lightgbm_design_v2.md``.  The
dictionary is deliberately code-owned: QuantDB remains raw/read-only while
the administrator can override every generated label in a catalog draft.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


DICTIONARY_VERSION = "quantdb-300-v2"
DICTIONARY_SOURCE = "300_factors_lightgbm_design_v2.md"


@dataclass(frozen=True)
class FactorDefinition:
    display_name: str
    explanation: str
    category_id: str
    category_name: str
    sort_order: int
    confidence: str = "documented"

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


_GROUPS = (
    ("mom_", "momentum", "动量", 100),
    ("vol_turnover_", "volume_turnover", "成交量与换手率", 220),
    ("vol_price_", "volume_turnover", "成交量与换手率", 220),
    ("vol_large_trade_", "volume_turnover", "成交量与换手率", 220),
    ("vol_tick_density", "volume_turnover", "成交量与换手率", 220),
    ("vol_gini", "volume_turnover", "成交量与换手率", 220),
    ("vol_kurtosis", "volume_turnover", "成交量与换手率", 220),
    ("vol_skew", "volume_turnover", "成交量与换手率", 220),
    ("vol_persistence", "volume_turnover", "成交量与换手率", 220),
    ("vol_up_down_ratio", "volume_turnover", "成交量与换手率", 220),
    ("vol_weighted_price", "volume_turnover", "成交量与换手率", 220),
    ("vol_", "volatility", "波动与风险", 200),
    ("amt_", "money_flow", "成交额与资金", 300),
    ("turn_", "turnover", "换手与流动性", 400),
    ("liq_", "turnover", "换手与流动性", 400),
    ("mfi_", "money_flow", "成交额与资金", 300),
    ("obv_", "money_flow", "成交额与资金", 300),
    ("tech_", "technical", "技术指标", 500),
    ("fun_", "fundamental", "基本面与估值", 600),
    ("style_", "style", "截面风格", 700),
    ("ind_", "industry", "行业轮动", 800),
    ("chip_", "chip", "筹码分布", 900),
    ("concept_", "concept", "概念板块", 1000),
    ("flow_cancel_", "order_flow", "撤单与委托流", 1200),
    ("flow_order_", "order_flow", "撤单与委托流", 1200),
    ("flow_", "money_flow_l2", "逐笔资金流", 1100),
    ("micro_vpin_", "toxicity", "信息不对称与毒性", 1300),
    ("micro_adverse_", "toxicity", "信息不对称与毒性", 1300),
    ("micro_toxicity_", "toxicity", "信息不对称与毒性", 1300),
    ("micro_informed_", "toxicity", "信息不对称与毒性", 1300),
    ("micro_order_imbalance_tox", "toxicity", "信息不对称与毒性", 1300),
    ("micro_", "microstructure", "价差与微观结构", 1400),
    # ── 港股持仓结构与南向资金 ──
    ("ca_", "holding_structure", "持仓结构", 1500),
    ("sb_", "holding_structure", "持仓结构", 1500),
    # Alpha 库（2026-08-29 接入）: 三套经典价量因子
    ("a101_", "alpha101", "经典Alpha101", 1600),
    ("gtja_", "gtja191", "GTJA191价量", 1700),
    ("a158_", "alpha158", "Alpha158多窗", 1800),
)

# High-value exact descriptions. The token renderer below covers documented
# variants and keeps newly published fields readable until an admin refines it.
_EXACT = {
    "amt_close_pos": ("成交额区间位置", "当前成交额在近期区间中的相对位置"),
    "amt_log": ("成交额对数", "当日成交额的对数，衡量资金活跃度"),
    "amt_z_20": ("20日成交额 Z 分数", "当前成交额相对近20日均值的标准化偏离"),
    "mfi_14": ("14日资金流量指标", "基于典型价格与成交量的资金流强弱指标"),
    "obv_slope_20": ("20日 OBV 斜率", "能量潮指标近20日趋势斜率"),
    "fun_float_mv": ("流通市值对数", "ln(不复权收盘价×流通股本)。衡量规模，使用时需 exp() 还原"),
    "fun_total_mv": ("总市值对数", "ln(不复权收盘价×总股本)。使用时需 exp() 还原为元"),
    "fun_mv": ("市值对数", "市值取对数口径（旧版 L1 命名，同 fun_float_mv）"),
    "fun_pe": ("市盈率", "总市值/净利润TTM，倍数"),
    "fun_pb": ("市净率", "总市值/净资产，倍数"),
    "fun_bp": ("账面市值比(B/P)", "1/市净率。价值因子，越高越偏价值"),
    "fun_ep": ("盈利收益率(E/P)", "净利润TTM/总市值。市盈率的倒数"),
    "fun_roe": ("净资产收益率", "净利润TTM/净资产×100。百分数值（11.76=11.76%）"),
    "fun_peg": ("PEG", "市盈率/净利润增长率。小于1常被视为成长性未被充分定价"),
    "fun_np_growth": ("净利润增长率", "净利润同比增长速度"),
    "fun_mv_rank": ("流通市值排名", "流通市值的当日全市场百分位（0~1），规模因子代表"),
    "fun_value_zscore": ("估值复合Z分", "(E/P与B/P各自截面Z分数的均值)。综合估值，越高越便宜"),
    "fun_turnover_1": ("1日换手率", "当日换手率（旧版 L1 命名）"),
    "fun_turnover_5": ("5日平均换手率", "近5日换手率均值（旧版 L1 命名）"),
    "fun_turnover_20": ("20日平均换手率", "近20日换手率均值（旧版 L1 命名）"),
    "turn_z_20": ("20日换手Z分数", "(当日换手率-20日均值)/20日标准差，捕捉换手率异常放大"),
    "tech_bb_width": ("20日布林带宽度", "布林带上下轨距离，衡量波动区间"),
    "tech_bb_pos": ("20日布林带位置", "收盘价在布林带中的相对位置（0=下轨，1=上轨）"),
    "tech_cci_20": ("20日CCI", "顺势指标。大于100超买，小于-100超卖"),
    "tech_adx_14": ("14日ADX", "平均趋向指数，衡量趋势强度（大于25为强趋势）"),
    "style_residual_ret_20": ("20日残差收益", "剔除市场Beta后的特质收益累计，残差动量因子"),
    "ind_strength_20": ("20日行业相对强度", "个股20日收益-所属行业20日均值收益，行业内的超额强度"),
    "chip_concentration_20": ("20日筹码集中度", "筹码成本分布90%区间宽度/现价。数值越小筹码越集中"),
    "chip_cost_90_width": ("90%筹码成本宽度", "筹码成本分布P95-P5的区间宽度（元）"),
    "liq_volume": ("成交量", "当日成交量"),
    "liq_amount": ("成交额", "当日成交额"),
    "liq_turnover_os": ("流通换手率", "当日成交量/流通股本"),
    "liq_amihud_20": ("20日Amihud非流动性", "近20日|收益|/成交额均值的对数口径，衡量价格冲击"),
    "liq_obv_20": ("20日OBV能量潮", "基于成交量累计的能量潮指标20日口径"),
    "liq_mfi_14": ("14日资金流量指标", "基于典型价格与成交量的资金流强弱指标"),
    "tech_close_to_high_20": ("20日收盘接近高点程度", "收盘价相对20日最高价的位置"),
    "tech_max_drawdown_20": ("20日最大回撤", "近20日价格路径的最大回撤幅度"),
    # ── 港股持仓结构（CCASS 日频因子）──
    "ca_n_pis": ("CCASS 参与人数量", "CCASS Top50 参与人数量（少=筹码集中，负向）"),
    "ca_hhi_disc": ("CCASS 披露集中度 HHI", "Top50 披露份额赫芬达尔指数，越高筹码越集中"),
    "ca_rank_overlap_20d": ("CCASS 20日名次重叠度", "参与人名单与 20 个交易日前的 Jaccard 重叠率"),
    "ca_broker_pct": ("券商席位占比", "券商类参与人披露份额合计"),
    "ca_disclosed_sum": ("CCASS 披露份额合计", "Top50 参与人披露比例合计（衡量披露质量）"),
    "ca_cust_share_disc": ("托管行披露份额", "托管行类参与人占披露份额比例"),
    "ca_south_pct": ("南向席位占比", "港股通席位（A00003/A00004）披露份额合计"),
    # ── 港股南向资金结构 ──
    "sb_quantity": ("南向持股数量", "港股通持股数量（正向）"),
    "sb_pct": ("南向持股占比", "港股通持股占已发行股本比例"),
    "sb_pct_d1": ("南向占比 1 日变化", "南向持股占比较上一披露日变化"),
    "sb_pct_d5": ("南向占比 5 日变化", "南向持股占比 5 个披露日变化"),
    "sb_pct_d20": ("南向占比 20 日变化", "南向持股占比 20 个披露日变化"),
    "sb_pct_z20": ("南向占比 20 日 Z 分数", "南向持股占比相对 20 日窗口的滚动标准化偏离"),
    "sb_consec_up": ("南向连增日数", "南向持股占比连续上升的披露日数"),
    "vol_amp_1": ("单日振幅", "当日最高价与最低价的相对振幅"),
    "vol_amp_20": ("20日平均振幅", "近20日单日振幅的统计水平"),
    "vol_gini": ("成交量基尼系数", "分钟成交量分布的不均匀程度"),
    "vol_kurtosis": ("成交量峰度", "分钟成交量分布的尖峰程度"),
    "vol_skew": ("成交量偏度", "分钟成交量分布的偏斜程度"),
    "vol_tick_density": ("成交密度", "单位时间内的成交笔数"),
    "vol_weighted_price": ("VWAP 偏离", "收盘价相对成交量加权平均价的偏离"),
    "micro_aesp": ("AESP 有效价差", "调整后的有效价差指标"),
    "micro_aqsp": ("AQSP 报价价差", "调整后的报价价差指标"),
}

_WORDS = {
    "ret": "收益率", "ma": "移动均线", "ema": "指数均线", "gap": "偏离", "macd": "MACD",
    "dif": "DIF", "dea": "DEA", "hist": "柱值", "rsi": "RSI", "kdj": "KDJ",
    "std": "标准差", "atr": "平均真实波幅", "parkinson": "Parkinson 波动率",
    "gk": "Garman-Klass 波动率", "rv": "已实现波动率", "rrv": "已实现范围波动率",
    "rskew": "已实现偏度", "rkurt": "已实现峰度", "jump": "跳跃", "ratio": "比率",
    "net": "净流入", "amount": "成交额", "volume": "成交量", "turnover": "换手率",
    "depth": "订单簿深度", "spread": "价差", "imbalance": "不平衡度", "liquidity": "流动性",
    "vpin": "VPIN", "trade": "成交", "order": "委托", "cancel": "撤单",
    "profit": "获利", "concentration": "集中度", "momentum": "动量", "rotation": "轮动",
    "crowding": "拥挤度", "beta": "Beta", "idio": "特质", "residual": "残差",
    "mom": "动量", "amt": "成交额", "turn": "换手", "close": "收盘", "open": "开盘",
    "high": "高位", "low": "低位", "pos": "位置", "score": "评分", "hot": "热点",
    "days": "天数", "netflow": "资金净流入", "flow": "资金流", "buy": "买入", "sell": "卖出",
    "large": "大单", "small": "小单", "super": "特大单", "medium": "中单",
    "time": "时间加权", "equal": "等权", "weighted": "加权", "mean": "均值", "median": "中位数",
    "skew": "偏度", "kurtosis": "峰度", "persistence": "持续性", "price": "价格",
    "bid": "买方", "ask": "卖方", "call": "集合", "auction": "竞价", "impact": "冲击",
    "cost": "成本", "recovery": "恢复", "effective": "有效", "extreme": "极端", "mid": "中间价",
    "slope": "斜率", "volatility": "波动率", "information": "信息", "share": "份额", "count": "次数",
    "flag": "标记", "size": "规模", "max": "最大", "illiquidity": "非流动性", "amihud": "Amihud",
    "kyle": "Kyle", "holden": "Holden", "roll": "Roll", "twa": "时间加权", "adverse": "逆向",
    "selection": "选择", "informed": "知情", "toxicity": "毒性", "asymmetry": "非对称性", "pin": "PIN",
    "fun": "基本面", "pe": "市盈率", "pb": "市净率", "bp": "账面市值比", "ep": "盈利收益率",
    "roe": "净资产收益率", "peg": "PEG", "mv": "市值", "np": "净利润", "growth": "增长率",
    "rank": "排名", "value": "估值", "zscore": "Z分数", "total": "总", "float": "流通",
    "z": "Z分数", "bb": "布林带", "cci": "CCI", "adx": "ADX", "strength": "强度",
    "width": "宽度", "os": "流通盘", "vwap": "VWAP",
}


def _group(column: str) -> tuple[str, str, int]:
    for prefix, category_id, category_name, order in _GROUPS:
        if column.startswith(prefix):
            return category_id, category_name, order
    return "other", "其他因子", 9999


def _render_name(column: str) -> str:
    if column in _EXACT:
        return _EXACT[column][0]
    patterns = (
        (r"mom_ret_(\d+)d", "{0}日收益率"),
        (r"mom_ma_gap_(\d+)", "收盘价偏离{0}日均线"),
        (r"mom_rsi_(\d+)", "{0}日 RSI 相对强弱指标"),
        (r"vol_std_(\d+)", "{0}日收益率标准差"),
        (r"vol_atr_(\d+)", "{0}日平均真实波幅"),
        (r"vol_parkinson_(\d+)", "{0}日 Parkinson 波动率"),
        (r"amt_ma_(\d+)", "{0}日成交额均线"),
        (r"amt_ratio_(\d+)_(\d+)", "成交额 {0} 日/{1} 日均线比"),
        (r"amt_net_flow_(\d+)", "{0}日成交额净流入"),
        (r"turn_(\d+)", "{0}日换手率"),
        (r"turn_ratio_(\d+)_(\d+)", "换手率 {0} 日/{1} 日均线比"),
        (r"liq_volume_ma_(\d+)", "{0}日成交量均线"),
        (r"liq_amount_ma_(\d+)", "{0}日成交额均线"),
        (r"liq_volume_ratio_(\d+)", "{0}日量比"),
        (r"liq_amount_ratio_(\d+)", "{0}日额比"),
        (r"fun_turnover_(\d+)", "{0}日平均换手率"),
        (r"chip_profit_ratio_(\d+)", "{0}日筹码获利比例"),
        (r"ind_ret_(\d+)", "{0}日行业收益排名"),
        (r"style_beta_(\d+)", "{0}日市场 Beta"),
        (r"style_idio_vol_(\d+)", "{0}日特质波动率"),
        (r"micro_vpin_(\d+)", "VPIN（{0} 等量分桶）"),
        (r"micro_depth_ratio_(\d+)", "{0}档买卖盘深度比"),
        (r"micro_depth_imbalance_(\d+)", "{0}档订单簿深度不平衡"),
        (r"micro_liquidity_amihud_(\d+)", "{0}日 Amihud 非流动性"),
        (r"micro_zone_vol_ratio_T(\d+)", "T{0} 时段成交量占比"),
        (r"vol_realized_(\d+)min", "{0}分钟已实现波动率"),
        # Alpha 库命名（2026-08-29 接入）
        (r"a101_(\d{3})", "Alpha101 #\1（Kakushadze 101 Formulaic Alphas）"),
        (r"gtja_(\d{3})", "GTJA191 #\1（国泰君安短周期价量因子）"),
        (r"a158_KMID", "K线实体比"),
        (r"a158_KLEN", "K线振幅比"),
        (r"a158_KMID2", "实体占振幅比"),
        (r"a158_KUP", "上影线比"),
        (r"a158_KUP2", "上影线占振幅比"),
        (r"a158_KLOW", "下影线比"),
        (r"a158_KLOW2", "下影线占振幅比"),
        (r"a158_KSFT", "K线重心偏移"),
        (r"a158_KSFT2", "K线重心偏移归一化"),
        (r"a158_OPEN0", "开盘相对收盘"),
        (r"a158_HIGH0", "最高相对收盘"),
        (r"a158_LOW0", "最低相对收盘"),
        (r"a158_VWAP0", "VWAP相对收盘"),
        (r"a158_(ROC|MA|BETA|RSQR|RESI|STD|MAX|MIN|QTLU|QTLD|RANK|RSV|IMAX|IMIN|IMXD|CORR|CORD|CNTP|CNTN|CNTD|SUMP|SUMN|SUMD|VMA|VSTD|WVMA|VSUMP|VSUMN|VSUMD)(\d+)",
         "{0} 因子（{1} 日窗口）"),
    )
    for pattern, template in patterns:
        match = re.fullmatch(pattern, column)
        if match:
            return template.format(*match.groups())
    parts = column.split("_")
    # 分类已经由 UI 分组展示，名称中不再重复“微观结构”前缀；卡片可直接
    # 显示“有效价差”“订单簿深度”等可辨识的短名称。
    if parts and parts[0] == "micro":
        parts = parts[1:]
    rendered = [_WORDS.get(part.lower(), part.upper() if part.isupper() else part) for part in parts]
    return " ".join(rendered)


def definition_for(column: str) -> dict[str, str | int]:
    """Return a non-destructive default definition for a raw QuantDB column."""
    category_id, category_name, order = _group(column)
    if column in _EXACT:
        name, explanation = _EXACT[column]
    else:
        name = _render_name(column)
        explanation = f"{category_name}因子：{name}。具体计算口径请参考官方帮助文档。"
    return FactorDefinition(
        display_name=name,
        explanation=explanation,
        category_id=category_id,
        category_name=category_name,
        sort_order=order,
        confidence="documented" if category_id != "other" else "needs_review",
    ).to_dict()
