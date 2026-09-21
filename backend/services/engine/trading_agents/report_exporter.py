"""分析完成后自动导出 md + PDF 报告（默认行为，失败不阻断主流程）。

导出规格（与 trading-agents 技能包一致）：
- 文件命名：{股票名}{代码}_{trade_date}_投研分析报告.{md,pdf}
  （股票名查不到时回退 {ticker}_{trade_date}_投研分析报告.{md,pdf}）
- 存放目录：{结果目录}/{市场中文名}/{股票名}/（CN→A股市场、US→美股市场、
  HK→港股市场、CRYPTO→区块链市场、FUTURES→期货市场；股票名查不到时不建子文件夹，
  直接放市场目录下），结果目录默认 /app/db/trading_agents_results
- md 标题：`{股票名}({ticker}) 投研分析报告`（股票名从本地 QuantDB 数据查，
  查不到回退纯代码）
- 副标题行：交易日期 / 分析时间 / 耗时 / 最终评级
- 正文按 12 阶段分节（各分析师结论 → 多空辩论 → 风控 → 最终决策）
- PDF 由 backend/scripts/md_to_pdf_report.py 转换（TTF 内嵌中文字体，A4 排版）
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.services.engine.trading_agents.progress import (
    PIPELINE_STAGES,
    ProgressTracker,
)
from backend.shared.export_disclaimer import DISCLAIMER_SENTENCE, format_export_time

logger = logging.getLogger(__name__)

# 阶段 id → 中文名（报告分节标题）
_STAGE_NAMES = {s["id"]: s["name"] for s in PIPELINE_STAGES}

# 市场 → 中文市场名（报告子目录名）
_MARKET_NAMES = {
    "CN": "A股市场",
    "US": "美股市场",
    "HK": "港股市场",
    "CRYPTO": "区块链市场",
    "FUTURES": "期货市场",
}

_RESULTS_DIR = Path(
    os.getenv("TRADING_AGENTS_RESULTS_DIR", "").strip() or "/data/reports/trading_agents"
)


def _resolve_stock_name(ticker: str, market: str) -> str:
    """从本地数据查股票名称（CN: QuantDB instrument_detail；US/HK: 板块 parquet）。"""
    # 统一候选代码（纯代码 / 带后缀）
    suffixes = {
        "CN": [".SH", ".SZ", ".BJ"],
        "HK": [".HK"],
        "US": [""],
        "CRYPTO": [""],
        "FUTURES": [""],
    }.get(market.upper(), [""])

    for suffix in suffixes:
        code = ticker
        if suffix and not ticker.upper().endswith(suffix):
            code = f"{ticker}{suffix}"

        # 1) CN: QuantDB instrument_detail（Symbol / Name 列）
        if market.upper() == "CN":
            try:
                import pandas as pd

                detail_dir = (
                    Path(os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb").strip())
                    / "2_base_sector"
                    / "instrument_detail"
                )
                detail_path = detail_dir / "instrument_list.parquet"
                if not detail_path.exists():
                    detail_path = detail_dir / "instrument_detail.parquet"
                if detail_path.exists():
                    df = pd.read_parquet(detail_path, columns=["Symbol", "Name"])
                    hit = df[df["Symbol"] == code]
                    if not hit.empty:
                        name = str(hit.iloc[0]["Name"]).strip()
                        if name and name.lower() != "nan":
                            return name
            except Exception:
                pass

        # 2) US/HK: 板块 parquet（可能含 name 列）
        if market.upper() in ("US", "HK"):
            for base in ("/data/quantus", "/data/quanthk"):
                p = Path(base) / "2_base_sector" / "sector" / f"{code}.parquet"
                if p.exists():
                    try:
                        import pandas as pd

                        df = pd.read_parquet(p)
                        for col in ("name", "Name", "NAME", "symbol_name", "cn_name"):
                            if col in df.columns:
                                name = str(df.iloc[0][col]).strip()
                                if name and name.lower() != "nan":
                                    return name
                    except Exception:
                        pass
    return ""


def _build_report_md(
    ticker: str,
    trade_date: str,
    stock_name: str,
    signal: str,
    tracker: ProgressTracker,
    market: str,
) -> str:
    """组装带标题（股票名 + 日期 + 分析时间）的 Markdown 报告。"""
    # 一个时刻、两种格式：分析时间（到分，沿用既有展示）与免责段的生成时间
    # （到秒，与其它导出件同格式）。分两次 `datetime.now()` 会让同一份报告里
    # 出现两个差几秒的时间戳 —— 那种"看起来像两个事件"的假象没必要制造。
    now_dt = datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M")
    title = f"{stock_name}({ticker})" if stock_name else ticker

    lines: list[str] = []
    lines.append(f"# {title} 投研分析报告")
    lines.append("")
    lines.append(
        f"> **交易日期**: {trade_date}　|　**分析时间**: {now}　"
        f"|　**耗时**: {tracker.elapsed:.0f}s"
    )
    lines.append(f"> **最终评级**: **{signal or 'N/A'}**　|　**市场**: {market}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for stage in PIPELINE_STAGES:
        text = (tracker.stage_reports.get(stage["id"]) or "").strip()
        if not text:
            continue
        lines.append(f"## {stage['name']}")
        lines.append("")
        lines.append(text)
        lines.append("")

    # 免责段：措辞取自单源（`shared/export_disclaimer`），生成时间由同一模块给出。
    # 此前这里自带一份变体（「投研分析管线自动生成（{now}）」），与 PDF 转换脚本
    # 里的另一份（「仅供研究参考」）同时出现在同一份 PDF 上 —— 一页两句话不一样。
    lines.append("---")
    lines.append("")
    lines.append(f"> {DISCLAIMER_SENTENCE}")
    lines.append(f"> 生成时间：{format_export_time(now_dt)}")
    lines.append("")
    return "\n".join(lines)


def _convert_to_pdf(md_path: Path, pdf_path: Path) -> bool:
    """调用 backend/scripts/md_to_pdf_report.py 转 PDF（TTF 内嵌字体）。"""
    try:
        import backend.scripts.md_to_pdf_report as converter

        converter.main(str(md_path), str(pdf_path))
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception as exc:
        logger.warning("md→PDF 转换失败 %s: %s", md_path.name, exc)
        return False


def _sanitize_name(raw: str) -> str:
    """清洗股票名/文件名非法字符（Windows/路径分隔符等）。"""
    cleaned = raw.replace("/", "").replace("\\", "").replace(":", "").replace("*", "")
    cleaned = cleaned.replace("?", "").replace('"', "").replace("<", "").replace(">", "")
    return cleaned.replace("|", "").strip() or "未命名"


def export_report_files(
    ticker: str,
    trade_date: str,
    signal: str,
    tracker: ProgressTracker,
    market: str,
) -> dict[str, Any]:
    """分析完成后自动导出 md + PDF（默认必做）。任何一步失败都只告警，不抛异常。

    目录结构：{市场名}/{股票名}/ 下，文件 {股票名}{代码}_{trade_date}_投研分析报告.{md,pdf}
    """
    result: dict[str, Any] = {"md": None, "pdf": None, "dir": None, "error": None}
    try:
        market_dir = _MARKET_NAMES.get(market.upper(), market or "CN")
        stock_name = _resolve_stock_name(ticker, market)
        safe_stock_name = _sanitize_name(stock_name) if stock_name else ""

        out_dir = _RESULTS_DIR / market_dir
        if safe_stock_name:
            out_dir = out_dir / safe_stock_name
        out_dir.mkdir(parents=True, exist_ok=True)

        file_prefix = f"{safe_stock_name}{ticker}" if safe_stock_name else ticker
        md_path = out_dir / f"{file_prefix}_{trade_date}_投研分析报告.md"
        pdf_path = out_dir / f"{file_prefix}_{trade_date}_投研分析报告.pdf"

        md_text = _build_report_md(ticker, trade_date, stock_name, signal, tracker, market)
        md_path.write_text(md_text, encoding="utf-8")
        result["md"] = str(md_path)
        result["dir"] = str(out_dir)
        logger.info("投研报告 md 已导出: %s", md_path)

        if _convert_to_pdf(md_path, pdf_path):
            result["pdf"] = str(pdf_path)
            logger.info("投研报告 PDF 已导出: %s", pdf_path)
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("投研报告自动导出失败（%s %s）: %s", ticker, trade_date, exc)
    return result
