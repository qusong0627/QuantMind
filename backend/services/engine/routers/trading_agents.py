"""TradingAgents REST API — analyze, progress, report, history."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from backend.shared.report_archive import RESULTS_DIR as _RESULTS_DIR
from backend.shared.report_archive import resolve_results_dir as _resolve_results_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/trading-agents", tags=["TradingAgents"])

# In-memory tracker registry (keyed by analysis_id)
_trackers: dict[str, object] = {}
_threads: dict[str, object] = {}


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., description="股票/标的代码，如 300750")
    trade_date: str = Field(default_factory=lambda: date.today().isoformat(), description="分析日期 YYYY-MM-DD")
    llm_provider: str = Field(default="minimax", description="LLM 供应商")
    deep_think_llm: str = Field(default="MiniMax-M2.7", description="深度思考模型")
    quick_think_llm: str = Field(default="MiniMax-M2.7-highspeed", description="快速思考模型")
    market: str = Field(default="CN", description="市场: CN(默认)/US/HK/CRYPTO/FUTURES")


class StopRequest(BaseModel):
    analysis_id: str


# 市场 → 数据供应商。quantmind_local 读本地 parquet，各市场网络回退源不同。
_MARKET_VENDOR_FALLBACK = {
    "CN": "a_stock",
    "HK": "hk_stock",
    "US": "us_stock",
    "CRYPTO": "crypto",
    "FUTURES": "futures",
}


def _build_config(req: AnalyzeRequest) -> dict:
    """Build TradingAgents config from request params."""
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        config = DEFAULT_CONFIG.copy()
    except ImportError:
        # Fallback config when TradingAgents is not installed
        config = {
            "llm_provider": "openai",
            "deep_think_llm": "gpt-5.4",
            "quick_think_llm": "gpt-5.4-mini",
            "backend_url": None,
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "data_cache_dir": "/app/db/trading_agents_cache",
            "results_dir": "/app/db/trading_agents_results",
        }

    config["llm_provider"] = req.llm_provider
    config["deep_think_llm"] = req.deep_think_llm
    config["quick_think_llm"] = req.quick_think_llm
    market_upper = str(req.market or "CN").upper()
    fallback_vendor = _MARKET_VENDOR_FALLBACK.get(market_upper, "a_stock")
    config["market"] = market_upper
    config["data_vendors"] = {
        # quantmind_local reads local QuantDB parquet; <fallback> is the network fallback
        # used whenever the local store has no data for the symbol/date.
        "core_stock_apis": f"quantmind_local,{fallback_vendor}",
        "technical_indicators": f"quantmind_local,{fallback_vendor}",
        "fundamental_data": f"quantmind_local,{fallback_vendor}",
        "news_data": fallback_vendor,
        "signal_data": fallback_vendor,
    }
    config["tool_vendors"] = {
        "get_industry_comparison": f"quantmind_local,{fallback_vendor}",
    }
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    config["output_language"] = "Chinese"
    return config


@router.post("/analyze")
async def start_analysis(req: AnalyzeRequest):
    """Start a new TradingAgents analysis."""
    from backend.services.engine.trading_agents.progress import ProgressTracker
    from backend.services.engine.trading_agents.runner import run_analysis_in_thread

    analysis_id = str(uuid.uuid4())[:8]

    config = _build_config(req)
    tracker = ProgressTracker()
    _trackers[analysis_id] = tracker

    thread = run_analysis_in_thread(
        ticker=req.ticker,
        trade_date=req.trade_date,
        config=config,
        tracker=tracker,
        analysis_id=analysis_id,
        market=config.get("market", "CN"),
    )
    _threads[analysis_id] = thread

    return {
        "code": 200,
        "data": {
            "analysis_id": analysis_id,
            "ticker": req.ticker,
            "trade_date": req.trade_date,
            "market": config.get("market", "CN"),
            "message": "分析已启动",
        },
    }


@router.get("/progress/{analysis_id}")
async def get_progress(analysis_id: str):
    """Get current progress of an analysis."""
    tracker = _trackers.get(analysis_id)
    if not tracker:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found")
    return {"code": 200, "data": tracker.to_dict()}


@router.get("/report/{analysis_id}")
async def get_report(analysis_id: str):
    """Get the full report of a completed analysis."""
    tracker = _trackers.get(analysis_id)
    if not tracker:
        # Try loading from disk
        return await _load_report_from_disk(analysis_id)

    if tracker.is_running:
        return {"code": 202, "data": {"message": "分析仍在进行中", "progress": tracker.to_dict()}}

    if tracker.error:
        return {"code": 500, "data": {"error": tracker.error}}

    return {
        "code": 200,
        "data": {
            "ticker": tracker.ticker,
            "trade_date": tracker.trade_date,
            "signal": tracker.signal,
            "final_state": tracker.final_state,
            "stage_reports": tracker.stage_reports,
            "stats": {
                "llm_calls": tracker.llm_calls,
                "tool_calls": tracker.tool_calls,
                "tokens_in": tracker.tokens_in,
                "tokens_out": tracker.tokens_out,
            },
            "elapsed": tracker.elapsed,
        },
    }


async def _load_report_from_disk(analysis_id: str) -> dict:
    """Try to load a report from database or disk storage."""
    # Try database first
    try:
        import os
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        db_url = os.getenv("DATABASE_URL", "").strip()
        if not db_url:
            host = os.getenv("DB_HOST", "localhost")
            port = os.getenv("DB_PORT", "5432")
            user = os.getenv("DB_USER", "quantmind")
            password = os.getenv("DB_PASSWORD", "quantmind2026")
            db_name = os.getenv("DB_NAME", "quantmind")
            db_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"
        elif "asyncpg" in db_url:
            db_url = db_url.replace("asyncpg", "psycopg2")

        engine = create_engine(db_url, pool_size=2, max_overflow=2)
        Session = sessionmaker(bind=engine)
        session = Session()

        row = session.execute(
            text("""
                SELECT ticker, trade_date, signal, llm_provider, deep_think_llm, quick_think_llm,
                       stage_reports, final_state, stats, elapsed_seconds, error, created_at
                FROM qm_trading_agents_history
                WHERE analysis_id = :aid
            """),
            {"aid": analysis_id},
        ).fetchone()

        session.close()
        engine.dispose()

        if row:
            stage_reports = {}
            final_state = {}
            stats = {}
            try:
                stage_reports = json.loads(row[6]) if row[6] else {}
            except Exception:
                pass
            try:
                final_state = json.loads(row[7]) if row[7] else {}
            except Exception:
                pass
            try:
                stats = json.loads(row[8]) if row[8] else {}
            except Exception:
                pass

            return {
                "code": 200,
                "data": {
                    "analysis_id": analysis_id,
                    "ticker": row[0],
                    "trade_date": str(row[1]) if row[1] else "",
                    "signal": row[2] or "",
                    "llm_provider": row[3] or "",
                    "deep_think_llm": row[4] or "",
                    "quick_think_llm": row[5] or "",
                    "stage_reports": stage_reports,
                    "final_state": final_state,
                    "stats": stats,
                    "elapsed": row[9] or 0,
                    "error": row[10],
                    "created_at": str(row[11]) if row[11] else "",
                },
            }
    except Exception as e:
        logger.warning("Failed to load report from database: %s", e)

    # Fallback: try disk
    for path in _RESULTS_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("analysis_id") == analysis_id:
                return {"code": 200, "data": data}
        except Exception:
            continue
    raise HTTPException(status_code=404, detail=f"Report {analysis_id} not found")


@router.get("/history")
async def list_history(
    limit: int = Query(20, ge=1, le=100),
):
    """List recent analysis history from database + in-memory."""
    history = []

    # From database
    try:
        import os
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        db_url = os.getenv("DATABASE_URL", "").strip()
        if not db_url:
            host = os.getenv("DB_HOST", "localhost")
            port = os.getenv("DB_PORT", "5432")
            user = os.getenv("DB_USER", "quantmind")
            password = os.getenv("DB_PASSWORD", "quantmind2026")
            db_name = os.getenv("DB_NAME", "quantmind")
            db_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"
        elif "asyncpg" in db_url:
            db_url = db_url.replace("asyncpg", "psycopg2")

        engine = create_engine(db_url, pool_size=2, max_overflow=2)
        Session = sessionmaker(bind=engine)
        session = Session()

        rows = session.execute(
            text("""
                SELECT analysis_id, ticker, trade_date, signal,
                       llm_provider, deep_think_llm, quick_think_llm,
                       stats, elapsed_seconds, error, created_at
                FROM qm_trading_agents_history
                ORDER BY created_at DESC
                LIMIT :lim
            """),
            {"lim": limit},
        ).fetchall()

        for r in rows:
            stats = {}
            try:
                stats = json.loads(r[7]) if r[7] else {}
            except Exception:
                pass
            history.append({
                "analysis_id": r[0],
                "ticker": r[1],
                "trade_date": str(r[2]) if r[2] else "",
                "signal": r[3] or "",
                "llm_provider": r[4] or "",
                "model": r[5] or r[6] or "",
                "stats": stats,
                "market": str(stats.get("market") or "CN").upper(),
                "elapsed": r[8] or 0,
                "error": r[9],
                "created_at": str(r[10]) if r[10] else "",
                "source": "database",
            })

        session.close()
        engine.dispose()
    except Exception as e:
        logger.warning("Failed to read history from database: %s", e)

    # Also include in-memory trackers not yet persisted
    for aid, tracker in _trackers.items():
        if tracker.is_complete and not any(h["analysis_id"] == aid for h in history):
            history.append({
                "analysis_id": aid,
                "ticker": tracker.ticker,
                "trade_date": tracker.trade_date,
                "signal": tracker.signal,
                "market": str(getattr(tracker, "market", "") or "CN").upper(),
                "elapsed": tracker.elapsed,
                "source": "memory",
            })

    return {"code": 200, "data": {"history": history[:limit], "total": len(history)}}


@router.post("/stop")
async def stop_analysis(req: StopRequest):
    """Stop a running analysis (best-effort — daemon threads can't be killed)."""
    tracker = _trackers.get(req.analysis_id)
    if not tracker:
        raise HTTPException(status_code=404, detail=f"Analysis {req.analysis_id} not found")
    if not tracker.is_running:
        return {"code": 200, "data": {"message": "分析已完成或未在运行"}}

    tracker.mark_error("用户手动停止")
    return {"code": 200, "data": {"message": "已发送停止信号"}}


@router.get("/config")
async def get_config():
    """Get available LLM providers and models."""
    try:
        from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
    except ImportError:
        # Fallback when TradingAgents is not installed
        MODEL_OPTIONS = {
            "minimax": {
                "quick": [("MiniMax-M2.7-highspeed", "MiniMax-M2.7-highspeed")],
                "deep": [("MiniMax-M2.7", "MiniMax-M2.7")],
            },
            "deepseek": {
                "quick": [("DeepSeek V3.2", "deepseek-chat")],
                "deep": [("DeepSeek V4 Pro", "deepseek-v4-pro")],
            },
            "openai": {
                "quick": [("GPT-5.4 Mini", "gpt-5.4-mini")],
                "deep": [("GPT-5.4", "gpt-5.4")],
            },
        }

    providers = []
    for provider_key, modes in MODEL_OPTIONS.items():
        providers.append({
            "key": provider_key,
            "quick_models": [{"label": label, "value": val} for label, val in modes.get("quick", [])],
            "deep_models": [{"label": label, "value": val} for label, val in modes.get("deep", [])],
        })

    return {"code": 200, "data": {"providers": providers}}


@router.get("/download/{analysis_id}")
async def download_report(analysis_id: str):
    """Download analysis report as JSON file."""
    report = await _load_report_from_disk(analysis_id)
    data = report.get("data", {})

    from fastapi.responses import Response

    content = json.dumps(data, ensure_ascii=False, indent=2)
    filename = f"trading_agents_{data.get('ticker', 'unknown')}_{data.get('trade_date', '')}_{analysis_id}.json"

    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================
# 报告文件管理（分析报告档案库：列表 / PDF 预览 / 删除 / 文件夹）
# ============================================================================

import re


def _safe_filename(name: str) -> bool:
    """拒绝路径穿越/特殊字符，只允许安全文件名。"""
    if not name or name in (".", ".."):
        return False
    return not any(ch in name for ch in ("/", "\\", "\x00"))


def _safe_folder_path(path: str) -> bool:
    """校验文件夹路径：允许「市场/股票名」两级路径，逐段校验，拒绝穿越。"""
    if not path or path in (".", ".."):
        return False
    if path.startswith("/") or path.endswith("/") or "\\" in path or "\x00" in path:
        return False
    return all(_safe_filename(part) for part in path.split("/"))


# _resolve_results_dir 现由 backend/shared/report_archive.py 提供（顶部已导入）：
# 解析顺序 env → 新目录 → 旧目录，且**不创建目录**。本模块与三个报告脚本共用同一实现。



def _parse_report_meta(filename: str) -> dict:
    """从文件名解析元数据：{ticker, name(股票名), date, signal}。

    新格式: {股票名}{代码}_{date}_投研分析报告.pdf
    例: 贵州茅台600519_2026-08-15_投研分析报告.pdf → ticker=600519, name=贵州茅台
    旧格式: {ticker}_{date}_投研分析报告.pdf
    例: 002594_2026-08-14_投研分析报告.pdf → ticker=002594, name=""
    """
    stem = filename.rsplit(".", 1)[0]
    parts = stem.split("_")
    meta = {
        "filename": filename,
        "ticker": "",
        "date": "",
        "time": "",
        "name": "",
        "signal": None,
    }
    if len(parts) >= 2:
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})$", parts[1])
        if date_match:
            # 新格式：{股票名}{代码}_{date}_...
            head = parts[0]
            ticker_match = re.search(r"(\d{4,6})$", head)
            if ticker_match:
                meta["ticker"] = ticker_match.group(1)
                meta["name"] = head[: ticker_match.start()]
            else:
                meta["ticker"] = head
            meta["date"] = parts[1]
        else:
            # 旧格式：{ticker}_{date}_...
            meta["ticker"] = parts[0]
            meta["date"] = parts[1]
    # 尝试从文件名解析评级（Buy/Overweight/Hold/Underweight/Sell）
    for kw in ("buy", "overweight", "hold", "underweight", "sell"):
        if f"_{kw}" in stem.lower():
            meta["signal"] = kw.capitalize()
            break
    return meta


@router.get("/files/list")
async def list_report_files():
    """列出报告目录下所有文件，按「市场文件夹 → 股票名文件夹 → 文件」分组（含元数据）。"""
    root = _resolve_results_dir()
    if not root.exists():
        return {"code": 200, "data": {"root": str(root), "folders": [], "files": []}}

    folders: list[dict] = []
    files: list[dict] = []

    def _file_meta(f: Path) -> dict:
        meta = _parse_report_meta(f.name)
        meta.update({
            "size": f.stat().st_size,
            "modified": f.stat().st_mtime,
        })
        return meta

    # 根目录文件（未分类，历史遗留）
    for f in sorted(
        (p for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".pdf", ".md")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        files.append(_file_meta(f))

    # 子文件夹：市场文件夹（可含股票名子文件夹）
    for market_dir in sorted(root.iterdir()):
        if not market_dir.is_dir():
            continue
        # 股票名子文件夹（{市场名}/{股票名}/{文件}）
        nested_folders: list[dict] = []
        direct_files: list[dict] = []
        for entry in sorted(market_dir.iterdir()):
            if entry.is_dir():
                sub_files = [
                    _file_meta(f)
                    for f in sorted(entry.iterdir())
                    if f.is_file() and f.suffix.lower() in (".pdf", ".md")
                ]
                nested_folders.append({"name": entry.name, "files": sub_files})
            elif entry.is_file() and entry.suffix.lower() in (".pdf", ".md"):
                direct_files.append(_file_meta(entry))
        # 兼容旧结构：{市场名} 直接放文件（无股票名子文件夹）
        if direct_files:
            direct_files.sort(key=lambda m: m["modified"], reverse=True)
        if nested_folders:
            nested_folders.sort(key=lambda f: f["name"])
        folders.append({
            "name": market_dir.name,
            "files": direct_files,
            "subfolders": nested_folders,
        })

    return {"code": 200, "data": {"root": str(root), "folders": folders, "files": files}}


@router.post("/files/upload")
async def upload_report_file(
    file: UploadFile = File(...),
    folder: str = Form(""),
):
    """前端上传 PDF 报告到报告目录（可选指定「市场/股票名」目标文件夹）。

    仅接受 .pdf；基名为路径穿越时拒绝；同名冲突追加时间戳后缀避免覆盖。
    """
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="非法文件名")

    root = _resolve_results_dir()
    target_dir = root
    if folder:
        if not _safe_folder_path(folder):
            raise HTTPException(status_code=400, detail="非法文件夹路径")
        target_dir = root / Path(folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    dest = target_dir / filename
    if dest.exists():
        dest = target_dir / f"{dest.stem}_{int(time.time())}{dest.suffix}"

    # 分块写入，上限 50MB；文件头必须是 PDF magic 字节，防扩展名伪装
    max_bytes = 50 * 1024 * 1024
    written = 0
    try:
        with dest.open("wb") as fh:
            head = await file.read(5)
            if head.lower() != b"%pdf-":
                raise HTTPException(status_code=400, detail="文件内容不是 PDF")
            fh.write(head)
            written += len(head)
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="文件过大（上限 50MB）")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    return {
        "code": 200,
        "data": {"filename": dest.name, "folder": folder or "", "size": written},
    }


def _iter_report_files(root: Path):
    """递归产出报告目录下所有 .md/.pdf 文件（根 + 任意层级子文件夹）。"""
    if not root.is_dir():
        return
    for entry in root.rglob("*"):
        if entry.is_file() and entry.suffix.lower() in (".pdf", ".md"):
            yield entry


@router.get("/files/pdf/{filename}")
async def get_report_pdf(filename: str):
    """返回 PDF 文件（供前端 iframe 内联预览，不触发下载）。

    支持任意层级：根目录、市场文件夹、股票名子文件夹。同名文件取修改时间最新的。
    """
    if not _safe_filename(filename):
        raise HTTPException(status_code=400, detail="非法文件名")
    root = _resolve_results_dir()
    candidates = [p for p in _iter_report_files(root) if p.name == filename]
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        p = candidates[0]
        if p.suffix.lower() == ".pdf":
            from fastapi.responses import FileResponse

            # 不传 filename 参数：保持 Content-Disposition 为空 → 浏览器 iframe 内联预览
            return FileResponse(
                p,
                media_type="application/pdf",
            )
    raise HTTPException(status_code=404, detail=f"PDF 不存在: {filename}")


class FileDeleteRequest(BaseModel):
    files: list[str] = Field(default_factory=list, description="待删除文件名列表")


class FileMoveRequest(BaseModel):
    files: list[str] = Field(default_factory=list, description="待移动文件名列表")
    target_folder: str = Field(..., description="目标文件夹（支持「市场/股票名」两级路径）")


@router.post("/files/move")
async def move_report_files(req: FileMoveRequest):
    """批量移动报告文件到目标文件夹（target_folder 支持两级路径）。"""
    if not _safe_folder_path(req.target_folder):
        raise HTTPException(status_code=400, detail="非法文件夹路径")
    root = _resolve_results_dir()
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / Path(req.target_folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    errors: list[str] = []
    # 收集所有源文件（任意层级，同名取最新）
    source_map: dict[str, Path] = {}
    for p in _iter_report_files(root):
        if p.name not in source_map or p.stat().st_mtime > source_map[p.name].stat().st_mtime:
            source_map[p.name] = p
    for name in req.files:
        if not _safe_filename(name):
            errors.append(f"非法文件名: {name}")
            continue
        src = source_map.get(name)
        if src is None:
            errors.append(f"文件不存在: {name}")
            continue
        try:
            dest = target_dir / name
            # 已在目标文件夹则跳过
            if src.resolve() == dest.resolve():
                continue
            src.replace(dest)
            moved.append(name)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"code": 200, "data": {"moved": moved, "errors": errors}}


@router.post("/files/delete")
async def delete_report_files(req: FileDeleteRequest):
    """批量删除报告文件（支持子文件夹）。"""
    root = _resolve_results_dir()
    deleted: list[str] = []
    errors: list[str] = []
    for name in req.files:
        if not _safe_filename(name):
            errors.append(f"非法文件名: {name}")
            continue
        found = False
        for p in _iter_report_files(root):
            if p.name == name:
                try:
                    p.unlink()
                    deleted.append(name)
                    found = True
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                break
        if not found:
            errors.append(f"文件不存在: {name}")
    return {"code": 200, "data": {"deleted": deleted, "errors": errors}}


class FolderDeleteRequest(BaseModel):
    folder: str = Field(..., description="文件夹（支持「市场/股票名」两级路径）")


@router.post("/files/delete-folder")
async def delete_report_folder(req: FolderDeleteRequest):
    """删除报告文件夹（含其中所有文件与子文件夹）。"""
    if not _safe_folder_path(req.folder):
        raise HTTPException(status_code=400, detail="非法文件夹路径")
    root = _resolve_results_dir()
    target = root / Path(req.folder)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"文件夹不存在: {req.folder}")
    import shutil

    shutil.rmtree(target, ignore_errors=True)
    return {"code": 200, "data": {"deleted": req.folder}}


class FolderCreateRequest(BaseModel):
    folder: str = Field(..., description="新建文件夹（支持「市场/股票名」两级路径）")


@router.post("/files/create-folder")
async def create_report_folder(req: FolderCreateRequest):
    """新建报告文件夹（支持两级路径，如「A股市场/贵州茅台」）。"""
    if not _safe_folder_path(req.folder):
        raise HTTPException(status_code=400, detail="非法文件夹路径")
    root = _resolve_results_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / Path(req.folder)
    target.mkdir(parents=True, exist_ok=True)
    return {"code": 200, "data": {"created": req.folder}}
