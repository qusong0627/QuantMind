#!/usr/bin/env python3
"""
QuantMind 日常数据同步脚本
==========================

整合所有数据同步步骤：
1. 增量拉取远程 PG 行情数据 (investment_data → baostock → akshare → eltdx)
2. 更新本地 Parquet 核心资产
3. 校准指标 (MA/换手率/收益率)
4. 增量更新 Qlib 二进制引擎数据
5. 计算 51 维模型特征（动量/波动率/流动性/资金流/风格因子）

用法:
    # 完整同步（所有步骤）
    python backend/scripts/run_daily_sync.py

    # 快速同步（跳过 investment_data 下载）
    python backend/scripts/run_daily_sync.py --quick

    # 仅同步特定步骤
    python backend/scripts/run_daily_sync.py --only pg-sync,calibrate

    # 查看状态
    python backend/scripts/run_daily_sync.py --status

    # 指定市场（A股/HK/US/Crypto）
    python backend/scripts/run_daily_sync.py --market A
    python backend/scripts/run_daily_sync.py --market HK

环境变量:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD  - 数据库连接
    REDIS_URL                                        - Redis 进度追踪
    QM_INVESTMENT_DATA_DIR                           - investment_data 目录
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daily_sync")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STEPS = [
    ("investment_data", "更新 investment_data (GitHub qlib_bin)"),
    ("pg_sync", "增量拉取 PG 行情数据"),
    ("qlib_bin", "更新 Qlib 二进制引擎数据"),
    ("calibrate", "校准指标 (MA/换手率/收益率)"),
    ("parquet", "更新 Parquet 核心资产 + 计算 51 维特征"),
]

# Redis progress tracking
SYNC_PROGRESS_KEY = "quantmind:daily_sync:progress"


def _get_redis():
    """Get Redis client for progress tracking."""
    try:
        import redis
        # Try multiple Redis URLs
        urls = [
            os.getenv("REDIS_URL", ""),
            "redis://quantmind-redis:6379/0",
            "redis://redis:6379/0",
            "redis://localhost:6379/0",
        ]
        for url in urls:
            if not url:
                continue
            try:
                client = redis.from_url(url, socket_timeout=2)
                client.ping()
                return client
            except Exception:
                continue
        return None
    except Exception:
        return None


def update_progress(step: str, status: str, detail: str = "", pct: int = 0):
    """Update sync progress in Redis."""
    rds = _get_redis()
    if not rds:
        return
    data = {
        "step": step,
        "status": status,  # running, done, error, skipped
        "detail": detail,
        "pct": pct,
        "started_at": datetime.now().isoformat(),
    }
    rds.set(SYNC_PROGRESS_KEY, json.dumps(data), ex=3600)


def get_progress() -> dict:
    """Get current sync progress."""
    rds = _get_redis()
    if not rds:
        return {"step": "unknown", "status": "no_redis"}
    raw = rds.get(SYNC_PROGRESS_KEY)
    return json.loads(raw) if raw else {"step": "idle", "status": "idle"}


def clear_progress():
    """Clear sync progress."""
    rds = _get_redis()
    if rds:
        rds.delete(SYNC_PROGRESS_KEY)


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def step_investment_data(skip: bool = False) -> dict:
    """Step 1: Update investment_data (GitHub qlib_bin download)."""
    if skip:
        log.info("[1/5] Skipping investment_data update")
        update_progress("investment_data", "skipped", "Quick mode")
        return {"status": "skipped"}

    log.info("[1/5] Updating investment_data...")
    update_progress("investment_data", "running", "Downloading qlib_bin from GitHub", 5)

    try:
        from backend.scripts.daily_data_sync import update_investment_data
        result = update_investment_data()
        log.info("[1/5] investment_data updated: %s", result.get("status", "ok"))
        update_progress("investment_data", "done", str(result), 20)
        return result
    except Exception as e:
        log.error("[1/5] investment_data failed: %s", e)
        update_progress("investment_data", "error", str(e))
        return {"status": "error", "error": str(e)}


def step_pg_sync(market: str = "A", symbols: Optional[list[str]] = None) -> dict:
    """Step 2: Incremental PG data sync from remote sources."""
    log.info("[2/5] Syncing PG data for market=%s...", market)
    update_progress("pg_sync", "running", f"Syncing {market} market data", 25)

    try:
        from backend.scripts.daily_data_sync import run_sync

        result = run_sync(
            market=market,
            symbols=symbols,
            incremental=True,
            update_qlib=False,  # We'll do this separately
            calibrate=False,    # We'll do this separately
        )
        log.info("[2/5] PG sync completed: %d stocks synced",
                 result.get("investment_data_synced", 0) +
                 result.get("baostock_synced", 0) +
                 result.get("akshare_synced", 0))
        update_progress("pg_sync", "done", f"Synced {result}", 45)
        return result
    except Exception as e:
        log.error("[2/5] PG sync failed: %s", e)
        update_progress("pg_sync", "error", str(e))
        return {"status": "error", "error": str(e)}


def step_qlib_bin(market: str = "A") -> dict:
    """Step 3: Update Qlib binary engine data.

    Note: Qlib bin is updated as part of run_sync (pg_sync step).
    This step verifies the Qlib data is consistent with PG.
    """
    log.info("[3/5] Verifying Qlib binary data...")
    update_progress("qlib_bin", "running", "Verifying Qlib bin consistency", 50)

    try:
        import pandas as pd
        from backend.scripts.daily_data_sync import _find_qlib_root, _load_calendar, _get_pg_latest_dates, _get_engine
        from datetime import datetime as dt

        qroot = _find_qlib_root()
        if not qroot:
            log.warning("[3/5] Qlib root not found")
            update_progress("qlib_bin", "done", "Qlib root not found (will be created on next full sync)", 60)
            return {"status": "skipped", "reason": "qlib_root_not_found"}

        cal = _load_calendar()
        if len(cal) == 0:
            log.warning("[3/5] Qlib calendar empty")
            update_progress("qlib_bin", "done", "Empty calendar", 60)
            return {"status": "warning", "reason": "empty_calendar"}

        qlib_end = pd.to_datetime(cal[-1]).date()
        engine = _get_engine()
        pg_dates = _get_pg_latest_dates(engine)
        pg_max = max(pg_dates.values()) if pg_dates else None

        lag_days = (pg_max - qlib_end).days if pg_max and pg_max > qlib_end else 0

        log.info("[3/5] Qlib calendar: %s to %s (%d days)", cal[0], cal[-1], len(cal))
        log.info("[3/5] PG latest: %s, Qlib lag: %d days", pg_max, lag_days)

        if lag_days > 5:
            log.warning("[3/5] Qlib bin is %d days behind PG - run full sync to update", lag_days)
            update_progress("qlib_bin", "done", f"Qlib {lag_days}d behind PG", 60)
            return {"status": "warning", "lag_days": lag_days}
        else:
            log.info("[3/5] Qlib bin is up to date (lag: %d days)", lag_days)
            update_progress("qlib_bin", "done", f"Qlib up to date (lag: {lag_days}d)", 60)
            return {"status": "ok", "lag_days": lag_days}

    except Exception as e:
        log.error("[3/5] Qlib bin verification failed: %s", e)
        update_progress("qlib_bin", "error", str(e))
        return {"status": "error", "error": str(e)}


def step_calibrate(market: str = "A", days: int = 90) -> dict:
    """Step 4: Calibrate technical indicators (MA, turnover, returns)."""
    log.info("[4/5] Calibrating indicators (last %d days)...", days)
    update_progress("calibrate", "running", f"Calibrating MA/turnover/returns ({days}d)", 65)

    try:
        from backend.scripts.daily_data_sync import _get_engine, _calibrate_indicators

        engine = _get_engine()
        _calibrate_indicators(engine, symbols=None, days=days)
        log.info("[4/5] Indicators calibrated")
        update_progress("calibrate", "done", "Indicators calibrated", 75)
        return {"status": "ok", "days": days}
    except Exception as e:
        log.error("[4/5] Calibration failed: %s", e)
        update_progress("calibrate", "error", str(e))
        return {"status": "error", "error": str(e)}


def step_parquet(market: str = "A", since: Optional[str] = None) -> dict:
    """Step 5: Update Parquet + compute 51-dim features."""
    log.info("[5/5] Updating Parquet + computing features...")
    update_progress("parquet", "running", "Computing 51-dim features (momentum/vol/liquidity/flow/style)", 80)

    try:
        if market == "A":
            result = {"status": "skipped", "reason": "A 股训练已直读 QuantDB；不再更新 feature_snapshots"}
        else:
            # For HK/US/Crypto, use update_market_features.py
            script = PROJECT_ROOT / "backend" / "scripts" / "update_market_features.py"
            if script.exists():
                result = subprocess.run(
                    [sys.executable, str(script), "--market", market],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    cwd=str(PROJECT_ROOT),
                )
                if result.returncode == 0:
                    result = {"status": "ok", "output": result.stdout[-500:]}
                else:
                    result = {"status": "error", "error": result.stderr[-500:]}
            else:
                result = {"status": "skipped", "reason": "Script not found"}

        log.info("[5/5] Parquet updated: %s", result.get("status", "unknown"))
        update_progress("parquet", "done", str(result), 95)
        return result
    except Exception as e:
        log.error("[5/5] Parquet update failed: %s", e)
        update_progress("parquet", "error", str(e))
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run_full_sync(
    market: str = "A",
    quick: bool = False,
    only_steps: Optional[list[str]] = None,
    symbols: Optional[list[str]] = None,
    calibrate_days: int = 90,
) -> dict:
    """Run full daily sync pipeline.

    Args:
        market: Market code (A, HK, US, Crypto)
        quick: Skip investment_data download
        only_steps: List of steps to run (None = all)
        symbols: Specific symbols to sync (None = all)
        calibrate_days: Days to calibrate indicators for

    Returns:
        Dict with results for each step
    """
    start_time = time.time()
    results = {
        "market": market,
        "started_at": datetime.now().isoformat(),
        "steps": {},
    }

    log.info("=" * 60)
    log.info("QuantMind Daily Sync - Starting")
    log.info("  Market: %s", market)
    log.info("  Quick mode: %s", quick)
    log.info("  Steps: %s", only_steps or "all")
    log.info("=" * 60)

    update_progress("init", "running", "Starting daily sync pipeline", 0)

    steps_to_run = only_steps or [s[0] for s in STEPS]

    # Step 1: investment_data
    if "investment_data" in steps_to_run:
        results["steps"]["investment_data"] = step_investment_data(skip=quick)
    else:
        results["steps"]["investment_data"] = {"status": "skipped"}

    # Step 2: PG sync
    if "pg_sync" in steps_to_run:
        results["steps"]["pg_sync"] = step_pg_sync(market=market, symbols=symbols)
    else:
        results["steps"]["pg_sync"] = {"status": "skipped"}

    # Step 3: Qlib bin
    if "qlib_bin" in steps_to_run:
        results["steps"]["qlib_bin"] = step_qlib_bin(market=market)
    else:
        results["steps"]["qlib_bin"] = {"status": "skipped"}

    # Step 4: Calibrate
    if "calibrate" in steps_to_run:
        results["steps"]["calibrate"] = step_calibrate(market=market, days=calibrate_days)
    else:
        results["steps"]["calibrate"] = {"status": "skipped"}

    # Step 5: Parquet + features
    if "parquet" in steps_to_run:
        results["steps"]["parquet"] = step_parquet(market=market)
    else:
        results["steps"]["parquet"] = {"status": "skipped"}

    elapsed = time.time() - start_time
    results["completed_at"] = datetime.now().isoformat()
    results["elapsed_seconds"] = round(elapsed, 1)

    # Summary
    success_count = sum(1 for s in results["steps"].values() if s.get("status") in ("ok", "done", "skipped", "up_to_date", "warning"))
    error_count = sum(1 for s in results["steps"].values() if s.get("status") == "error")

    log.info("=" * 60)
    log.info("Daily Sync Complete!")
    log.info("  Duration: %.1fs", elapsed)
    log.info("  Success: %d, Errors: %d", success_count, error_count)
    for step, res in results["steps"].items():
        status = res.get("status", "unknown")
        icon = "✓" if status in ("ok", "done", "up_to_date") else "○" if status == "skipped" else "⚠" if status == "warning" else "✗"
        log.info("  %s %s: %s", icon, step, status)
    log.info("=" * 60)

    update_progress("done", "done", f"Completed in {elapsed:.0f}s", 100)

    return results


def show_status():
    """Show current data sync status."""
    try:
        from backend.scripts.daily_data_sync import get_sync_status
        status = get_sync_status()
        print("\n📊 Data Sync Status")
        print("=" * 50)
        print(f"PG Latest Date:    {status.get('pg_latest_date', 'N/A')}")
        print(f"PG Earliest Date:  {status.get('pg_earliest_date', 'N/A')}")
        print(f"PG Symbol Count:   {status.get('pg_symbol_count', 'N/A')}")
        print(f"PG Total Rows:     {status.get('pg_total_rows', 'N/A'):,}")
        print("-" * 50)
        print(f"Calendar Start:    {status.get('calendar_start', 'N/A')}")
        print(f"Calendar End:      {status.get('calendar_end', 'N/A')}")
        print(f"Calendar Days:     {status.get('calendar_days', 'N/A')}")
        print(f"Qlib Stocks:       {status.get('qlib_stocks', 'N/A')}")
        print("=" * 50)

        # Check feature parquet
        parquet_path = PROJECT_ROOT / "db" / "feature_snapshots" / "model_features_2026.parquet"
        if parquet_path.exists():
            import os
            stat = parquet_path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
            size_mb = stat.st_size / (1024 * 1024)
            print(f"\n📁 Feature Parquet")
            print(f"Path:              {parquet_path}")
            print(f"Size:              {size_mb:.1f} MB")
            print(f"Last Modified:     {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
        print()
    except Exception as e:
        print(f"Error getting status: {e}")


# ---------------------------------------------------------------------------
# 系统事件记录
# ---------------------------------------------------------------------------

def _record_sync_event(market: str, ok: bool, reason: str = "", failed_steps: list | None = None) -> None:
    """把一次日常数据同步的成功/失败落成一条 system_events（data_sync）。失败不阻断主流程。"""
    try:
        from backend.shared.system_events import record_system_event

        failed_steps = failed_steps or []
        record_system_event(
            event_type="data_sync",
            level="info" if ok else "error",
            source="sync",
            title=f"日常数据同步{'完成' if ok else '失败'}（{market}）",
            message=reason or (f"市场 {market} 同步完成，失败步骤: {', '.join(failed_steps)}" if failed_steps else f"市场 {market} 同步完成"),
            meta={"market": market, "ok": ok, "failed_steps": failed_steps, "reason": reason},
        )
    except Exception as e:  # noqa: BLE001 - 事件记录非关键路径
        log.warning("记录数据同步事件失败: %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="QuantMind 日常数据同步",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                           # 完整同步
  %(prog)s --quick                   # 快速同步（跳过 investment_data）
  %(prog)s --only pg-sync,calibrate  # 仅执行特定步骤
  %(prog)s --market HK               # 同步港股
  %(prog)s --status                  # 查看状态

步骤说明:
  investment_data  更新 GitHub qlib_bin 数据
  pg_sync          增量拉取 PG 行情数据
  qlib_bin         更新 Qlib 二进制引擎数据
  calibrate        校准指标 (MA/换手率/收益率)
  parquet          更新 Parquet + 计算 51 维特征
        """,
    )
    parser.add_argument("--market", default="A", choices=["A", "CN", "HK", "US", "Crypto"],
                        help="市场 (default: A)")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式：跳过 investment_data 下载")
    parser.add_argument("--only", default="",
                        help="仅执行指定步骤（逗号分隔）")
    parser.add_argument("--symbols", default="",
                        help="指定股票（逗号分隔，仅 A 股）")
    parser.add_argument("--calibrate-days", type=int, default=90,
                        help="指标校准回溯天数 (default: 90)")
    parser.add_argument("--status", action="store_true",
                        help="显示当前数据状态")
    parser.add_argument("--progress", action="store_true",
                        help="显示同步进度")
    args = parser.parse_args()

    if args.status:
        show_status()
        return 0

    if args.progress:
        progress = get_progress()
        print(json.dumps(progress, indent=2, ensure_ascii=False))
        return 0

    only_steps = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None

    sync_market = args.market
    try:
        results = run_full_sync(
            market=sync_market,
            quick=args.quick,
            only_steps=only_steps,
            symbols=symbols,
            calibrate_days=args.calibrate_days,
        )
    except Exception as exc:  # noqa: BLE001 - 失败也要落一条事件
        _record_sync_event(sync_market, ok=False, reason=str(exc))
        raise

    # Exit with error code if any step failed
    error_count = sum(1 for s in results["steps"].values() if s.get("status") == "error")
    _record_sync_event(sync_market, ok=error_count == 0,
                       failed_steps=[k for k, v in results["steps"].items() if v.get("status") == "error"])
    return 1 if error_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
