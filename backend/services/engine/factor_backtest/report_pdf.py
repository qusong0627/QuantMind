"""单报告 PDF 导出：报告标量块 → Markdown → PDF，落「因子研究」档案目录。

数据面纪律：

- 数字**只来自** :func:`report.build_report_block` 的产出（本模块不重算任何
  统计量，也不触碰 metrics 口径函数）；本模块只负责排版与落盘。
- 渲染复用 ``backend.scripts.md_to_pdf_report``（封面元信息取其「报告日期/
  数据截至」blockquote 行，页脚统一免责声明）；落点复用
  ``shared.report_archive.ensure_report_dir("因子研究")``——导出件同时出现在
  既有「报告档案」文件面（trading-agents files 面按文件名递归服务 PDF）。
- 文件名确定性（因子名 + 因子 ID8 + 市场 + 运行 ID8）：同一 run 重导出即覆盖。
  先渲染到**目标同目录**的临时文件再 ``os.replace``（``/tmp`` 与 ``/data``
  常不同卷，跨卷 rename 会炸），档案文件面永远不会探到半个 PDF。
- PDF 面向用户：不写内部工单号；暂缺分析块照实列出缺口，不造数。

降级（``block["available"] == False``）不是异常路径而是业务终态：调用方
（router）据此映射 409 并把 ``reason`` 原文带给用户；本模块的导出函数对
降级块直接 ``ValueError``，绝不产出「无数字的 PDF」。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services.engine.factor_backtest.profiles import get_market_profile
from backend.shared.report_archive import ensure_report_dir

logger = logging.getLogger(__name__)

#: 「报告档案」下的子目录名（复用既有报告文件面 UI，不另起目录）。
REPORT_DIR_NAME = "因子研究"

#: 试次数来源标签（report.py 的 ``n_trials_source`` → 用户可读文案）。
_N_TRIALS_SOURCE_LABELS = {
    "batch_completed_units": "批内完成单元数",
    "param": "显式指定参数",
    "default_single": "单跑默认值 1",
}

#: 暂缺块中文名（report.py ``_UNAVAILABLE_BLOCKS`` 的键 → 文案）。
_UNAVAILABLE_LABELS = {
    "capacity": "容量测算",
    "holding_period": "多持有期对比",
    "ic_half_life": "IC 半衰期衰减",
    "style_attribution": "风格归因",
}

#: 核心指标表行：(headline 键, 显示名, 口径说明, 格式)。顺序即展示顺序。
_HEADLINE_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ("returns", "年化收益", "日均多空收益 μ × 252（简单年化，非 CAGR）", "pct"),
    ("ir", "信息比率 IR", "μ / σ × √252", "num"),
    ("turnover", "日均换手", "日度双边换手均值（买、卖各计一次）", "pct"),
    ("fitness", "Fitness", "IR × √(|Returns| / max(Turnover, 0.125))", "num"),
    ("margin", "Margin（每单位换手收益）", "Returns / Turnover", "num3"),
    ("cum_return", "累计收益", "Π(1+日收益) − 1（区间累计，非年化）", "pct"),
    ("ann_vol", "年化波动", "日收益 σ × √252", "pct"),
    ("mu_daily", "日均收益", "多空组合日收益均值", "pct3"),
    ("sigma_daily", "日波动", "日收益标准差（样本）", "pct3"),
    ("n_days", "样本天数", "参与统计的有效交易日数", "int"),
)

#: 台账评估指标表行：(run 顶层键, 显示名, 格式)。
_LEDGER_ROWS: tuple[tuple[str, str, str], ...] = (
    ("ic_value", "IC 均值", "num4"),
    ("rank_ic", "Rank IC 均值", "num4"),
    ("icir", "ICIR", "num3"),
    ("rank_icir", "Rank ICIR", "num3"),
    ("sharpe_ratio", "Sharpe（评估期）", "num3"),
    ("annual_return", "年化收益（评估期）", "pct"),
    ("max_drawdown", "最大回撤", "pct"),
)


def _fmt(value: Any, kind: str) -> str:
    """数值 → 展示文本；None/NaN 一律 ``—``（绝不显示成 0）。"""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v != v:  # NaN
        return "—"
    if kind == "int":
        return str(int(v))
    if kind == "pct":
        return f"{v * 100:.2f}%"
    if kind == "pct3":
        return f"{v * 100:.3f}%"
    if kind == "num":
        return f"{v:.2f}"
    if kind == "num3":
        return f"{v:.3f}"
    if kind == "num4":
        return f"{v:.4f}"
    return f"{v:g}"


def _cell(text: Any) -> str:
    """表格单元格文本：竖线会撕列，统一换全角。"""
    return str(text if text is not None else "—").replace("|", "／")


def _market_label(run: dict[str, Any]) -> str:
    """市场中文标签 + 样本内外标注（因子×市场适配的第一眼信息）。"""
    market = str(run.get("market") or "")
    try:
        profile = get_market_profile(market)
    except Exception:  # noqa: BLE001 — 未知市场退回原始键，不挡导出
        return market or "—"
    suffix = "（样本内，因子挖掘原始市场）" if profile.in_sample else "（样本外）"
    return f"{profile.label}{suffix}"


def _safe_component(text: Any, *, max_len: int = 40) -> str:
    """文件名单段清洗：路径分隔/保留字符/空白 → ``_``；空 → ``unnamed``。"""
    s = re.sub(r'[\\/:*?"<>|\s\x00]+', "_", str(text or "")).strip("._")
    return s[:max_len] or "unnamed"


def report_pdf_filename(run: dict[str, Any]) -> str:
    """确定性文件名：``因子回测_{因子名}_{因子ID8}_{市场}_{运行ID8}.pdf``。

    同 run 重复导出 → 同名覆盖（幂等）；不同 run 之间靠 run_id8 区分。
    """
    name = _safe_component(run.get("factor_name") or run.get("factor_id"))
    fid8 = _safe_component(str(run.get("factor_id") or "")[:8], max_len=8)
    market = _safe_component(run.get("market") or "na", max_len=16)
    rid8 = _safe_component(str(run.get("run_id") or "")[:8], max_len=8)
    return f"因子回测_{name}_{fid8}_{market}_{rid8}.pdf"


def build_report_markdown(
    run: dict[str, Any],
    series: dict[str, Any] | None,
    block: dict[str, Any],
    *,
    market_label: str | None = None,
    generated_at: str | None = None,
) -> str:
    """装配报告 Markdown（纯函数、无 IO；测试直接断言文本）。

    仅接受可用块（``block["available"]`` 为真）；封面块引用行同时含
    「报告日期」「数据截至」——md_to_pdf_report 的封面提取靠它们。
    """
    if not block or not block.get("available"):
        raise ValueError("降级报告块不可装配 Markdown（调用方应先过滤 available）")

    headline = block.get("headline") or {}
    sig = block.get("significance") or {}
    cost = block.get("cost_grid") or {}
    excess = block.get("excess") or {}
    meta = block.get("meta") or {}
    s_meta = (series or {}).get("meta") or {}
    dates = (series or {}).get("dates") or []

    factor_name = _cell(run.get("factor_name") or run.get("factor_id") or "—")
    today = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_end = str(dates[-1]) if dates else "—"
    cost_bps = meta.get("cost_bps")

    lines: list[str] = []
    lines.append(f"# 因子回测报告 · {factor_name}")
    lines.append("")
    lines.append(f"> **报告日期**：{today}　｜　**数据截至**：{data_end}")
    lines.append(f"> 因子：{factor_name}（ID：{_cell(run.get('factor_id') or '—')}）")
    lines.append(
        f"> 运行 ID：{_cell(run.get('run_id') or '—')}　｜　批次："
        f"{_cell(run.get('batch_id') or '单跑')}"
    )
    lines.append(
        f"> 市场：{_cell(market_label or _market_label(run))}　｜　股票池："
        f"{_cell(run.get('universe') or '默认')}　｜　回测区间："
        f"{_cell(run.get('date_range') or '—')}"
    )
    lines.append(
        f"> 成本假设：{_cell(cost_bps if cost_bps is not None else '—')} bps"
        f"（日度双边）　｜　数据源：{_cell(run.get('data_source') or '—')}"
    )
    lines.append("")

    # ── 一、核心指标 ────────────────────────────────────────────────
    lines.append("## 一、核心指标（多空组合）")
    lines.append("")
    lines.append("| 指标 | 数值 | 口径 |")
    lines.append("| --- | --- | --- |")
    for key, label, definition, kind in _HEADLINE_ROWS:
        lines.append(f"| {label} | {_fmt(headline.get(key), kind)} | {definition} |")
    lines.append("")

    # ── 二、台账评估指标 ────────────────────────────────────────────
    lines.append("## 二、台账评估指标（IC 体系）")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    for key, label, kind in _LEDGER_ROWS:
        lines.append(f"| {label} | {_fmt(run.get(key), kind)} |")
    lines.append("")

    # ── 三、统计显著性 ──────────────────────────────────────────────
    boot = sig.get("bootstrap") or {}
    crowd = sig.get("crowding") or {}
    source_label = _N_TRIALS_SOURCE_LABELS.get(
        str(sig.get("n_trials_source") or ""), str(sig.get("n_trials_source") or "—")
    )
    boot_text = "—"
    if boot:
        boot_text = (
            f"[{_fmt(boot.get('lo'), 'num4')}, {_fmt(boot.get('hi'), 'num4')}]"
            f"（点估计 {_fmt(boot.get('point'), 'num4')}）"
        )
    crowd_text = "—"
    if crowd:
        crowd_text = (
            f"{_fmt(crowd.get('score'), 'num')}"
            f"（换手分位 {_fmt(crowd.get('turnover_pct'), 'pct')}；"
            f"IC 一阶自相关 {_fmt(crowd.get('ic_autocorr_lag1'), 'num3')}）"
        )
    lines.append("## 三、统计显著性")
    lines.append("")
    lines.append("| 检验 | 结果 | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| 普通 t 统计量 | {_fmt(sig.get('plain_t'), 'num')} |"
        " IC 均值 t 检验（未做自相关稳健，仅对照） |"
    )
    lines.append(
        f"| Newey-West t 统计量 | {_fmt(sig.get('nw_t'), 'num')} |"
        " 自相关稳健；**显著性判定以此行为准** |"
    )
    lines.append(
        f"| p 值（双侧） | {_fmt(sig.get('p_value'), 'num4')} | 由 NW t 的正态近似 |"
    )
    lines.append(
        f"| BY q 值（族校正） | {_fmt(sig.get('q_value_bhy'), 'num4')} |"
        f" {_cell(sig.get('family_note') or '—')}；族大小 N={_fmt(sig.get('family_n'), 'int')} |"
    )
    lines.append(
        f"| DSR（去膨胀 Sharpe） | {_fmt(sig.get('dsr'), 'num4')} |"
        f" 试次数 N={_fmt(sig.get('n_trials'), 'int')}（{_cell(source_label)}）；"
        f"{_cell(sig.get('dsr_note') or '—')} |"
    )
    lines.append(
        f"| Bootstrap 95% 置信区间 | {boot_text} | percentile 法重抽，n_boot={_fmt(boot.get('n_boot'), 'int')} |"
    )
    lines.append(f"| 拥挤度代理 | {crowd_text} | {_cell(crowd.get('note') or '—')} |")
    lines.append("")

    # ── 四、成本敏感性 ──────────────────────────────────────────────
    default_bps = cost.get("default_bps")
    lines.append("## 四、成本敏感性")
    lines.append("")
    lines.append("净收益 = 毛收益 − 日双边换手 × bps / 10000，逐日扣减：")
    lines.append("")
    lines.append("| 成本档（bps，双边） | 净年化收益 | 净 IR | 净 Fitness |")
    lines.append("| --- | --- | --- | --- |")
    for row in cost.get("rows") or []:
        bps = row.get("bps")
        mark = "（默认）" if default_bps is not None and bps == default_bps else ""
        lines.append(
            f"| {_fmt(bps, 'num')}{mark} | {_fmt(row.get('net_return'), 'pct')} |"
            f" {_fmt(row.get('net_ir'), 'num')} | {_fmt(row.get('net_fitness'), 'num')} |"
        )
    lines.append("")
    break_even = cost.get("break_even_bps")
    if break_even is not None:
        if cost.get("break_even_note"):
            lines.append(_cell(cost["break_even_note"]))
        else:
            lines.append(
                f"盈亏平衡成本约 {_fmt(break_even, 'num')} bps——成本高于该档时"
                "该方向净收益转负（= 净收益均值 / 日均换手均值）。"
            )
    lines.append("")

    # ── 五、超额收益口径 ────────────────────────────────────────────
    lines.append("## 五、超额收益口径")
    lines.append("")
    lines.append(
        f"{_cell(excess.get('label') or '—')}：{_cell(excess.get('note') or '—')}"
    )
    lines.append("")

    # ── 六、暂缺分析块 ──────────────────────────────────────────────
    lines.append("## 六、暂缺分析块（当前数据面无法计算）")
    lines.append("")
    for item in block.get("unavailable") or []:
        label = _UNAVAILABLE_LABELS.get(str(item.get("block")), str(item.get("block")))
        lines.append(f"- **{label}**：{_cell(item.get('reason') or '—')}")
    lines.append("")

    # ── 七、口径说明 ────────────────────────────────────────────────
    lines.append("## 七、口径说明")
    lines.append("")
    lines.append(
        "- 多空组合日收益由已落盘净值曲线无损反推（nav = Π(1 + r)），"
        "缺失日按落盘侧约定处理。"
    )
    lines.append("- 换手为**日度双边**（买、卖各计一次）；成本按上式从日收益逐日扣减。")
    lines.append(
        f"- 组合构建参数（落盘侧 meta）：分桶数 {_fmt(s_meta.get('n_buckets'), 'int')}、"
        f"头部/尾部比例 top_pct={_fmt(s_meta.get('top_pct'), 'num')}"
        "（多头 = 因子得分头部组等权，空头 = 尾部组等权，多空 = 多头 − 空头）。"
    )
    lines.append("- 本报告由系统自动生成，仅供学习研究参考，不构成投资建议。")
    return "\n".join(lines) + "\n"


def export_report_pdf(
    run: dict[str, Any],
    series: dict[str, Any] | None,
    block: dict[str, Any],
    *,
    market_label: str | None = None,
    generated_at: str | None = None,
) -> tuple[Path, str]:
    """装配并渲染 PDF，原子落「因子研究」档案目录。

    Returns:
        ``(落盘绝对路径, 文件名)``。同一 run 重复导出覆盖同名文件（幂等）。

    Raises:
        ValueError: 报告不可导出（降级块 / 序列缺失）——调用方映射 409。
        Exception: 渲染引擎异常原样上抛（调用方记日志并映射 500）。
    """
    if not block or not block.get("available"):
        reason = (
            (block or {}).get("reason") or (block or {}).get("status") or "报告不可用"
        )
        raise ValueError(f"报告不可导出：{reason}")

    filename = report_pdf_filename(run)
    md_text = build_report_markdown(
        run, series, block, market_label=market_label, generated_at=generated_at
    )

    out_dir = ensure_report_dir(REPORT_DIR_NAME)
    final_path = out_dir / filename

    # 临时 PDF 必须放目标同目录：os.replace 跨文件系统（/tmp ↔ /data）会 EXDEV；
    # 同目录替换还能保证档案文件面按 mtime 永远只见到完整文件。
    fd, tmp_name = tempfile.mkstemp(prefix=".pdf_tmp_", suffix=".pdf", dir=out_dir)
    os.close(fd)
    try:
        from backend.scripts.md_to_pdf_report import main as md_to_pdf

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", encoding="utf-8", delete=False
        ) as md_fp:
            md_fp.write(md_text)
            md_path = md_fp.name
        try:
            md_to_pdf(md_path, tmp_name)
        finally:
            try:
                os.unlink(md_path)
            except OSError:
                pass
        os.replace(tmp_name, final_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        logger.exception("因子报告 PDF 导出失败 run_id=%s", run.get("run_id"))
        raise
    logger.info("因子报告 PDF 已导出: %s", final_path)
    return final_path, filename
