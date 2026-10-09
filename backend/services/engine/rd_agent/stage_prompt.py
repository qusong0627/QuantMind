"""阶段化课程与评估口径提示块（提示词增量层）——纯函数，读时计算轮次。

设计来源：开源证券研究所《重新设计后的 prompt》阶段表（图14）：
S1 种子因子（1-3 轮）→ S2 因子族探索（4-8）→ S3 聚焦精炼（9-12）→
S4 正交组合（13-16）→ S5 非线性探索（17+）。

注入通道（``rd_loop_wrapper`` 对 ``tpl.load_content`` 的 patch）分两类，
**不要混用**：

- **静态块**（语言/方向/评估口径/池记忆）追加到 ``qlib_factor_background``——
  该键在 ``QlibFactorScenario`` 构造时被读进 ``_background`` 并冻结，
  之后每次假设生成与写码 prompt 都携带，内容在整个任务期内不变，正合适；
- **阶段块**（本模块 ``render_stage_block``）必须追加到
  ``factor_hypothesis_specification``——它在 ``QlibFactorHypothesisGen.prepare_context``
  里每轮实时加载；若挂到背景键上，会永远停在「第 1 轮」。

轮次判定优先级：
1. ``prompt_round_from_tag``：RD-Agent 当前日志 tag（``RDAgentLog._tag_ctx``
   ContextVar，形如 ``Loop_{li}.{step}``）→ 精确到当前 loop，无「目录还没
   建出来」的 ±1 竞态；
2. ``count_loops``：数任务日志目录下 ``Loop_<int>`` 直接子目录（兜底）；
3. 下限 1——注入是增益层，任何异常输入都不许向外抛。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: 阶段块渲染标题（``rd_loop_wrapper`` 的 patched_load 用它做防重复注入守卫）
STAGE_BLOCK_MARKER = "阶段化挖掘课程"

_LOOP_TAG_RE = re.compile(r"Loop_(\d+)")
_LOOP_DIR_RE = re.compile(r"^Loop_(\d+)$")


@dataclass(frozen=True)
class Stage:
    """单个阶段：轮次区间 + 目标 + 执行纪律（纪律逐条进提示词）。"""

    key: str
    name: str
    start: int
    end: int | None  # None = 无上限（末阶段）
    goal: str
    disciplines: tuple[str, ...]

    @property
    def range_label(self) -> str:
        if self.end is None:
            return f"第 {self.start} 轮起"
        return f"第 {self.start}-{self.end} 轮"


STAGES: tuple[Stage, ...] = (
    Stage(
        key="S1",
        name="种子因子",
        start=1,
        end=3,
        goal=(
            "快速建立覆盖不同基础维度的简单种子因子（趋势/反转、波动、"
            "流动性/换手、量价背离），每个公式简短、经济逻辑一句话讲得清"
        ),
        disciplines=(
            "每个假设只讲一类机制，不要在一轮里混合多个方向；",
            "优先复用数据源已有的字段与基础算子，不引入复杂组合；",
            "窗口参数取经济意义明确的整数（如 5/10/20/60 日），不做精细调参。",
        ),
    ),
    Stage(
        key="S2",
        name="因子族探索",
        start=4,
        end=8,
        goal=(
            "基于种子结果系统拓宽假设空间：在既定方向下探索不同因子族"
            "（时序 vs 截面、价 vs 量、条件化 vs 无条件、微观结构联动等），"
            "每族给出代表因子"
        ),
        disciplines=(
            "每轮显式说明本轮探索的因子族、以及它与已有因子的差异；",
            "失败的族要写清失败原因，不在相邻轮重复同一种失败；",
            "用 ICIR 与换手联合视角判断，而不是只看单轮 IC。",
        ),
    ),
    Stage(
        key="S3",
        name="聚焦精炼",
        start=9,
        end=12,
        goal=(
            "依据前两阶段反馈收敛：锁定表现最好的一到两个因子族做受控精炼"
            "（窗口、平滑、去极值、中性化、条件触发），追求稳定 ICIR 的抬升"
        ),
        disciplines=(
            "一次只动一个设计维度（控制变量），并说明改动针对哪个指标；",
            "不再另起全新方向；确需切换时先写明放弃依据；",
            "关注子区间稳定性，拒绝只在个别年份有效的拟合结果。",
        ),
    ),
    Stage(
        key="S4",
        name="正交组合",
        start=13,
        end=16,
        goal=(
            "把已验证因子走向正交组合：在与池内已有因子、本任务已有因子"
            "低相关的前提下构造互补组合，同时控制换手与成本敏感度"
        ),
        disciplines=(
            "新因子先自查相关性：目标 |ρ|<0.7；与池内因子 |ρ|≥0.9 会被查重硬拒；",
            "组合须说明每个组件的边际贡献来源，不靠堆叠提高拟合；",
            "评估口径看扣费后收益与换手：更高换手必须换来显著更强的预测力。",
        ),
    ),
    Stage(
        key="S5",
        name="非线性探索",
        start=17,
        end=None,
        goal=(
            "在已验证结构上探索非线性与状态依赖（分位/秩变换、交互项、"
            "按波动率/趋势/流动性状态条件化），挖掘残余 alpha"
        ),
        disciplines=(
            "每个非线性设计都要有可陈述的经济或行为学理由，拒绝纯黑箱拟合；",
            "严格看样本外一致性与扰动保真度（PFS），参数敏感性高即弃；",
            "复杂度有上限：公式保持可读、可复现。",
        ),
    ),
)


def stage_for_round(round_no: int | None) -> Stage:
    """轮次 → 阶段；0/负数/坏输入一律钳到 S1（不抛）。"""
    try:
        rnd = max(1, int(round_no))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        rnd = 1
    for stage in STAGES:
        if stage.end is None or rnd <= stage.end:
            return stage
    return STAGES[-1]


def prompt_round_from_tag(tag: str) -> int | None:
    """从 RD-Agent 当前日志 tag（如 ``Loop_3.direct_exp_gen``）解析 1-based 轮次。"""
    match = _LOOP_TAG_RE.search(str(tag or ""))
    return int(match.group(1)) + 1 if match else None


def count_loops(log_dir: str | Path | None) -> int:
    """任务日志目录下 ``Loop_<int>`` 直接子目录数（缺失/不可读 → 0）。"""
    if not log_dir:
        return 0
    try:
        return sum(
            1
            for p in Path(log_dir).iterdir()
            if p.is_dir() and _LOOP_DIR_RE.match(p.name)
        )
    except OSError:
        return 0


def current_round(tag: str = "", log_dir: str | Path | None = None) -> int:
    """当前轮次（1-based）：tag 精确优先，目录数兜底，下限 1。"""
    by_tag = prompt_round_from_tag(tag)
    if by_tag is not None:
        return by_tag
    return max(1, count_loops(log_dir))


def rdagent_tag() -> str:
    """RD-Agent 当前日志 tag（ContextVar 读取；导入/读取失败一律空串）。"""
    try:
        from rdagent.log import rdagent_logger

        return str(getattr(rdagent_logger, "_tag", "") or "")
    except Exception:  # noqa: BLE001 — 环境缺 rdagent 时不拦调用方
        return ""


def render_stage_block(*, total_loops: int | None, current_loop: int | None) -> str:
    """渲染阶段块：课程总览 + 当前进度 + 当前阶段纪律（+ 末轮收敛提示）。

    ``total_loops=None``（轮数未知）时不宣称「最后一轮」——只有已知总轮数
    且已到末轮才补收敛提示。total 小于 current 时按 current 钳平。
    """
    try:
        cur = max(1, int(current_loop))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        cur = 1
    total_known = total_loops is not None
    try:
        total = max(1, int(total_loops)) if total_known else cur  # type: ignore[arg-type]
    except (TypeError, ValueError):
        total_known, total = False, cur
    total = max(total, cur)
    stage = stage_for_round(cur)

    lines = [
        "",
        "",
        f"====== {STAGE_BLOCK_MARKER} / Staged Mining Curriculum ======",
        f"本任务共 {total} 轮，当前为第 {cur}/{total} 轮 → 处于 {stage.key}·{stage.name}（{stage.range_label}）。",
        "课程总览（按轮推进，不要跳阶段；轮数不足以走完全程时，把当前阶段做扎实优先）：",
    ]
    lines.extend(f"- {s.key} {s.name}（{s.range_label}）：{s.goal}。" for s in STAGES)
    lines.append(f"当前阶段纪律（{stage.key}·{stage.name}）：")
    lines.extend(f"{i}) {d}" for i, d in enumerate(stage.disciplines, start=1))
    if total_known and cur >= total:
        lines.append(
            "本轮是任务的最后一轮：优先收敛到最完整、可运行、可回测的假设，不再开启新的探索。"
        )
    return "\n".join(lines) + "\n"


def render_eval_criteria_block() -> str:
    """静态评估口径块——口径与 ``mining_plugins`` 评估器/门禁同源，不得臆造。

    事实出处：多头 = 截面 rank 前 30%（``evaluators/turnover_cost``）、
    双边成本 0.2%（``factor_research.analysis.COST_RATE`` 单一出处，
    ``r_net = r − 换手×0.002``）、|ρ|≥0.9 值级查重硬拒（materializer 既有语义）。
    """
    return (
        "\n\n====== 评估口径 / Evaluation Criteria ======\n"
        "你的因子将由统一回测框架以「截面多头组合」口径评估"
        "（多头 = 每日截面 rank 前 30%，按双边 0.2% 从收益中扣除交易成本）。"
        "请围绕以下口径设计，不要只盯单点 IC：\n"
        "- rankIC：因子值与未来收益的截面秩相关；单日高 IC 可能只是噪声，追求跨时间稳定。\n"
        "- ICIR：IC 均值 / IC 标准差，衡量预测力的稳定性——本框架的核心择优依据；"
        "低换手的高 ICIR 因子优于高 IC 的抖动因子。\n"
        "- PFS（扰动保真度）：截面加噪后排序保持率，检验因子是否依赖脆弱结构取巧。\n"
        "- RRE：时序排序稳定性（分组表现的单调与持续程度）。\n"
        "- 换手与成本：按双边 0.2%（一轮买卖合计）扣费；"
        "高换手因子必须换来显著更强的预测力才能覆盖成本。\n"
        "- 相关性：与池内已有因子 |ρ|≥0.9 会被值级查重直接拒绝入库；"
        "相关性偏高即使入库也会显著降分——正交性是硬指标。\n"
        "设计纪律：经济逻辑清晰、参数少、多子区间稳定；禁止为拟合单一样本区间堆叠参数。\n"
    )
