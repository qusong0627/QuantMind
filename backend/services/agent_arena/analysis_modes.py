# ── 移植注（2026-09-29，arena 原生迁移 A2）─────────────────────────────
# 逐字节副本：quant-Trader `prompts/analysis_modes.py`（上游已退役）。
# 改写点（其余含 MODES / MARKET_RULES 与源逐字节一致）：
#   ① 落点：`<repo>/configs/comp-config.json` → `<数据根>/arena/comp-config.json`
#      （A1 已按同语义落盘），且改为**延迟解析**（env 切换数据根在进程内生效）；
#   ② 写路径加固：写前建父目录 + 临时文件原子替换（上游直接 write_text，
#      中断会留半截 JSON）。
# 消费者：arena 前端 GET/PUT /comp-config；`rotated_modes` 等引擎侧函数暂留
# （A2 只读面用不到，留作 B 批次决策层迁移的现成口径，勿删）。
# ─────────────────────────────────────────────────────────────────────
"""比赛配置目录 + 每模型选择（Arena「比赛配置」↔ 分析引擎 的单一事实源）。

比赛配置：每个模型可多选分析配置；选中 N 个配置 → 每次分析按 N 个配置各做一轮。
- Arena 前端：GET/PUT /comp-config（本包路由层读写本模块）
- 分析引擎：原 scripts/live_hourly_analysis.py（随上游退役，未迁移）
配置文件：`<数据根>/arena/comp-config.json`（QM 落点，见 arena_config）
"""

import json
import os
from pathlib import Path
from typing import Dict, List

from backend.services.agent_arena import arena_config


def _config_file() -> Path:
    """比赛配置落点（延迟解析，见移植注①）。"""
    return arena_config.comp_config_file()

MODES = [
    {
        "id": "baseline",
        "name": "基线模式",
        "prompt": "按标准流程逐只简评：结合当前盈亏、今日涨跌与趋势给出可执行建议。"
                  "A股硬约束先核对：T+1（可卖量0=今日买入不可卖）、跌停不接、"
                  "单笔买入≤剩余额度20%、持仓总市值≤权益×1.5（超线系统自动强平）、"
                  "交易摩擦按佣金万2.5+印花税万5估算。冷静客观，不追涨停不接跌停。",
    },
    {
        "id": "monk",
        "name": "苦行模式",
        "prompt": "精简推理：直接给结论与操作建议，少说废话。严格风控：单笔买入≤剩余额度20%、"
                  "保留现金、避开 ST（is_st=1）与高换手妖股；A股 T+1 当日不追买隔夜变数大的票、"
                  "跌停不接；系统已有自动强平守护（1.5×权益）和拒单重放，不需要你发明熔断规则。",
    },
    {
        "id": "awareness",
        "name": "情境感知",
        "prompt": "知己知彼：清楚当前赛况（各 agent 总收益与当日盈亏排行榜，虚拟净值口径、"
                  "与实时估值有延迟）。知道自己是领先还是落后，据此调整进攻/防守节奏；"
                  "落后别为了排名追涨杀跌——A股 T+1，当日买错当天难纠。把与对手的对比结论写进分析。",
    },
    {
        "id": "leverage",
        "name": "极限杠杆",
        "prompt": "高风险高回报风格（可选激进）：敢于集中持仓，但 A股硬边界必须守住——"
                  "杠杆=持仓市值/权益≤1.5×（超线系统直接自动强平，不是商量）、单票≤剩余额度20%、"
                  "T+1 限制：满仓后当日无法纠错，至少保留一手可卖的纠错资金；"
                  "只在自己明确看好的强趋势票上加杠杆，跌停品种永远不碰。",
    },
]

DEFAULT = "baseline"  # 未选择时的兜底配置

# 各市场交易规则（注入系统提示词，让同一套模式目录在不同市场措辞正确）
MARKET_RULES: Dict[str, str] = {
    "cn": "注意 A股 T+1（当日买入次日方可卖出）、涨跌停板、单笔不超权益 20%、日亏 5% 熔断。",
    "hk": "注意港股 T+0（当日可买卖）、可做空（需融券额度）、无涨跌停板、最小交易单位 100 股、汇率风险。",
    "us": "注意美股 T+0、可做空、无涨跌停板、最小交易单位 1 股、盘前盘后流动性低。",
}


def load_selection(market: str = "cn") -> Dict[str, List[str]]:
    """读取 {模型名: [配置id, ...]}（按市场分区）。

    文件结构：{"selection": {"cn": {模型: [...]}, "hk": {...}, ...}}。
    兼容旧格式 {"selection": {模型: [...]}}（无 market 分区）→ 视作 cn。
    """
    mk = (market or "cn").lower()
    try:
        data = json.loads(_config_file().read_text(encoding="utf-8"))
        sel = data.get("selection", {}) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    # 新格式：按市场分区
    if mk in sel and isinstance(sel[mk], dict):
        return {k: v for k, v in sel[mk].items() if isinstance(v, list)}
    # 旧格式兼容：扁平 {模型: [...]} 只在 cn 下回退
    if mk == "cn" and all(isinstance(v, list) for v in sel.values()):
        return dict(sel)
    return {}


def save_selection(market: str, selection: Dict[str, List[str]]) -> None:
    """原子写 selection（按市场分区，保留其他市场配置）。"""
    mk = (market or "cn").lower()
    target = _config_file()
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        if not isinstance(data, dict):
            data = {}
        sel = data.setdefault("selection", {})
        if not isinstance(sel, dict) or any(isinstance(v, list) for v in sel.values() if not isinstance(v, dict)):
            # 旧扁平格式 → 迁移到 cn 分区
            old = {k: v for k, v in sel.items() if isinstance(v, list)} if isinstance(sel, dict) else {}
            sel = {"cn": old} if old else {}
        sel[mk] = selection
        data["selection"] = sel
    except (OSError, json.JSONDecodeError):
        data = {"selection": {mk: selection}}
    # 移植注②：建父目录 + 原子替换
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)


def selected_modes(model: str, market: str = "cn") -> List[dict]:
    """模型选中的配置对象列表（按目录顺序）；未选择/全无效 → [基线模式]。"""
    ids = load_selection(market).get(model) or []
    by_id = {m["id"]: m for m in MODES}
    picked = [by_id[i] for i in ids if i in by_id]
    return picked or [by_id[DEFAULT]]


def rotated_modes(model: str, market: str = "cn") -> List[dict]:
    """模式轮转：一天一种模式，跨天轮换（多选时启用）。

    设计意图（2026-09-03 用户确认）：多模式全跑 = 每小时 N 份完整分析，
    token 消耗 N 倍且当天地来回换风格；改为按自然日取模选出一个模式——
    盘中各时段分析口径一致（无风格横跳），跨天自然轮换保证多视角覆盖。
    单选或 ANALYZE_MODE_ROTATE=0 时行为不变。
    """
    import datetime
    import os

    if os.getenv("ANALYZE_MODE_ROTATE", "1") != "1":
        return selected_modes(model, market)
    sel = selected_modes(model, market)
    if len(sel) <= 1:
        return sel
    day = datetime.date.today().toordinal()  # 连续自然日序号
    return [sel[day % len(sel)]]


def mode_label(model: str, market: str = "cn") -> str:
    """选中配置的中文名（'基线模式/苦行模式' 拼接），用于日志与落盘标注。"""
    return "/".join(m["name"] for m in selected_modes(model, market))


def market_rules(market: str = "cn") -> str:
    """该市场的交易规则提示词段。"""
    return MARKET_RULES.get((market or "cn").lower(), MARKET_RULES["cn"])


def comp_config_section(model: str, market: str = "cn") -> str:
    """把模型选中的配置翻译成系统提示词追加段（回放路径用：一轮运行内分 N 轮分析）。

    未选择/全无效 → 基线模式单轮，与历史行为等价。
    注入选中市场的交易规则（A股 T+1 / 港股 T+0 可做空 等），让同一套模式在不同市场措辞正确。
    """
    modes = selected_modes(model, market)
    lines = [
        "",
        "【本次分析要求 · 比赛配置】",
        f"你需要按 {len(modes)} 个配置分别完成独立分析（每个配置单独一轮：观察→推理→操作建议），",
        "轮次之间用分隔线标明配置名称，全部完成后再输出最终指令。",
        "",
        f"【市场规则】{market_rules(market)}",
        "",
    ]
    for m in modes:
        lines.append(f"配置《{m['name']}》：{m['prompt']}")
    return "\n".join(lines)
