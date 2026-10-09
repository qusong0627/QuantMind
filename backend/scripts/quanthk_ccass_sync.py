#!/usr/bin/env python3
"""CCASS top50 机构持股 → QuantHK 爬虫同步脚本。

复用 HKEX 爬虫（ccass-data4.py）的核心类（AsyncHKEXFetcher / HKEXTradingCalendar /
StockListManager），抓取港股 CCASS 机构持股，直接落盘 parquet 分区（不导出 CSV）。

增量逻辑（日期 + 股票双补）：
  1. 按日期：对最近 N 个交易日，若本地 ccass_top50/dt=YYYYMMDD/ 分区不存在则抓全市场
  2. 按股票：对已存在的分区，检查是否缺股票（如新股），缺则补抓该股该日

落盘格式:
  {quanthk}/2_base_sector/ccass_top50/dt=YYYYMMDD/data.parquet
  stock_code 保持 5 位（如 00700），不加 .HK 后缀。

用法:
  python backend/scripts/quanthk_ccass_sync.py --days 5
  python backend/scripts/quanthk_ccass_sync.py --date 2026-08-08          # 补指定日期
  python backend/scripts/quanthk_ccass_sync.py --symbol 00700 --days 30   # 补指定股票
  python backend/scripts/quanthk_ccass_sync.py --dry-run                   # 预览待补
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quanthk_ccass_sync")

QUANTHK_DATA_DIR = Path(
    os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk"))
)
REL_DIR = "2_base_sector/ccass_top50"

# 爬虫脚本路径（项目内，容器 bind mount 可访问；可用 QM_CCASS_CRAWLER 覆盖）
DEFAULT_CRAWLER = str(Path(__file__).parent / "ccass_crawler.py")
CRAWLER_PATH = os.getenv("QM_CCASS_CRAWLER", DEFAULT_CRAWLER)

# 输出列（与现有分区对齐）
OUT_COLS = [
    "stock_code",
    "stock_name",
    "participant_id",
    "participant_name",
    "holding_quantity",
    "holding_percentage",
    "query_date",
]

# 每批提交/落盘的股票数（断点续写粒度；批次太大则封禁时丢失多）
TASK_CHUNK = 200

_crawler_mod = None


def _load_crawler():
    """加载爬虫模块（复用其类，不触发交互 main）。"""
    global _crawler_mod
    if _crawler_mod is not None:
        return _crawler_mod
    path = Path(CRAWLER_PATH)
    if not path.is_file():
        raise FileNotFoundError(
            f"爬虫脚本不存在: {CRAWLER_PATH}（可用 QM_CCASS_CRAWLER 指定）"
        )
    spec = importlib.util.spec_from_file_location("ccass_crawler", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _crawler_mod = mod
    return mod


def _quanthk_root() -> Path:
    env_val = os.getenv("QM_QUANTHK_DATA_DIR", "").strip()
    if env_val:
        p = Path(env_val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if Path("/data/quanthk").is_dir():
        return Path("/data/quanthk")
    QUANTHK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return QUANTHK_DATA_DIR


def _target_dir() -> Path:
    return _quanthk_root() / REL_DIR


def _existing_partitions() -> set[str]:
    d = _target_dir()
    if not d.is_dir():
        return set()
    return {p.name[3:] for p in d.glob("dt=*")}


def _existing_stocks(partition: str) -> set[str]:
    """某分区已有的股票代码集合。"""
    f = _target_dir() / f"dt={partition}" / "data.parquet"
    if not f.exists():
        return set()
    df = pd.read_parquet(f, columns=["stock_code"])
    return set(df["stock_code"].astype(str))


def _trading_days(end: date, n_days: int, calendar_cls) -> list[date]:
    """取最近 n 个交易日（用爬虫的交易日历）。"""
    days = []
    d = end
    while len(days) < n_days:
        if calendar_cls.is_trading_day(d.strftime("%Y-%m-%d")):
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _normalise_fetch(
    df: pd.DataFrame, stock_code: str, stock_name: str, query_date: date
) -> pd.DataFrame | None:
    """爬虫输出 → 标准化列。取 top50（按持股数量降序）。"""
    if df is None or df.empty:
        return None
    # 爬虫列：参与者编号/参与者名称/持股数量/占已发行股份百分比
    rename = {
        "参与者编号": "participant_id",
        "参与者名称": "participant_name",
        "持股数量": "holding_quantity",
        "占已发行股份百分比": "holding_percentage",
    }
    df = df.rename(columns=rename)
    df["participant_id"] = df["participant_id"].astype(str).str.strip()
    df["participant_name"] = df["participant_name"].astype(str).str.strip()
    df["holding_quantity"] = (
        pd.to_numeric(df["holding_quantity"], errors="coerce").fillna(0).astype("int64")
    )
    # 百分比 "32.44%" → 0.3244
    df["holding_percentage"] = (
        df["holding_percentage"]
        .astype(str)
        .str.replace("%", "", regex=False)
        .str.strip()
    )
    df["holding_percentage"] = (
        pd.to_numeric(df["holding_percentage"], errors="coerce").fillna(0.0) / 100.0
    )
    # 按持股数量降序取 top50
    df = df.sort_values("holding_quantity", ascending=False).head(50).copy()
    # 代码统一为后缀格式（00700 → 0700.HK；创业板 8 开头保留 5 位+.HK）
    from backend.shared.stock_utils import StockCodeUtil

    df["stock_code"] = StockCodeUtil.to_hk_suffix(stock_code)
    df["stock_name"] = stock_name
    df["query_date"] = query_date
    return df[OUT_COLS]


async def _fetch_stock_day(
    fetcher, stock_code: str, stock_name: str, query_date: date, hkex_date: str
) -> pd.DataFrame | None:
    """抓取单股单日并标准化。"""
    try:
        raw = await fetcher.fetch_data(stock_code, hkex_date)
        return _normalise_fetch(raw, stock_code, stock_name, query_date)
    except Exception as exc:  # noqa: BLE001
        log.debug("抓取 %s@%s 失败: %s", stock_code, query_date, exc)
        return None


def _append_partition(partition_dir: Path, chunk: pd.DataFrame) -> None:
    """把新抓的批次合并进分区（去重按 stock_code+participant_id）。"""
    partition_dir.mkdir(parents=True, exist_ok=True)
    out_path = partition_dir / "data.parquet"
    if out_path.exists():
        old = pd.read_parquet(out_path)
        combined = pd.concat([old, chunk], ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["stock_code", "participant_id"], keep="last"
        )
        combined.to_parquet(out_path, index=False)
    else:
        chunk.to_parquet(out_path, index=False)


async def sync_partition(
    target_day: date,
    *,
    max_concurrent: int = 8,
    skip_existing_stocks: bool = True,
    dry_run: bool = False,
    limit: int = 0,
    symbol: str | None = None,
) -> dict:
    """同步单个交易日分区。

    逻辑：
      1. 若分区已存在且不按股票补，则跳过
      2. 获取股票列表，遍历抓取每只股票该日 top50
      3. 聚合落盘（已有分区合并，缺的股票补进去）

    Args:
        limit: 最多抓取股票数（0=全部，用于小规模验证）
        symbol: 仅抓指定股票（5位）
    """
    mod = _load_crawler()
    date_str = target_day.strftime("%Y%m%d")
    hkex_date = target_day.strftime("%Y/%m/%d")

    target = _target_dir()
    partition_dir = target / f"dt={date_str}"
    existing = _existing_partitions()

    # 股票列表（复用 StockListManager）
    stock_mgr = mod.StockListManager
    csv_path = str(Path(CRAWLER_PATH).parent / "hk.csv")
    stock_df, _ = stock_mgr.refresh_stock_list(csv_path)
    if stock_df.empty:
        return {"date": date_str, "status": "no_stock_list"}

    existing_stocks = _existing_stocks(date_str) if date_str in existing else set()

    # 残缺分区检测：分区存在但股票数远少于全市场（如中断后只剩 1 只），
    # 视为残缺需补抓，否则增量逻辑会永远跳过残缺分区。
    incomplete = len(existing_stocks) < max(50, int(len(stock_df) * 0.5))
    if date_str in existing and skip_existing_stocks and not incomplete and not symbol:
        return {"date": date_str, "status": "exists", "stocks": len(existing_stocks)}

    todo = [r for r in stock_df.to_dict("records") if r["id"] not in existing_stocks]
    if symbol:
        todo = [r for r in todo if r["id"] == symbol.zfill(5)]
    if limit > 0:
        todo = todo[:limit]

    if not todo:
        return {
            "date": date_str,
            "status": "up_to_date",
            "stocks": len(existing_stocks),
        }

    if dry_run:
        return {
            "date": date_str,
            "status": "dry_run",
            "todo": len(todo),
            "missing": todo[:10],
        }

    log.info(
        "[%s] 抓取 %d 只股票（已有 %d）", date_str, len(todo), len(existing_stocks)
    )

    rows = []
    success = 0
    banned = False
    async with mod.AsyncHKEXFetcher(max_concurrent=max_concurrent) as fetcher:
        # 分批提交任务：每批最多 TASK_CHUNK 只，抓完立即落盘（断点续写），
        # 且检测 HKEX 封禁立即中止（否则被封后继续空转烧完整个股票池）
        for start_idx in range(0, len(todo), TASK_CHUNK):
            chunk = todo[start_idx : start_idx + TASK_CHUNK]
            tasks = [
                _fetch_stock_day(fetcher, r["id"], r["name"], target_day, hkex_date)
                for r in chunk
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            chunk_rows = []
            for df in results:
                if isinstance(df, Exception):
                    continue
                if df is not None and not df.empty:
                    chunk_rows.append(df)
                    success += 1

            if chunk_rows:
                rows.extend(chunk_rows)
                _append_partition(
                    partition_dir, pd.concat(chunk_rows, ignore_index=True)
                )

            if getattr(fetcher, "banned", False):
                banned = True
                log.error(
                    "[%s] HKEX 封禁，中止抓取（已抓 %d 只，已落盘续写）",
                    date_str,
                    success,
                )
                break

    if not rows:
        return {
            "date": date_str,
            "status": "no_data",
            "tried": len(todo),
            "success": success,
            "banned": banned,
        }

    return {
        "date": date_str,
        "status": "synced" if not banned else "partial",
        "tried": len(todo),
        "success": success,
        "rows": sum(len(r) for r in rows),
        "banned": banned,
    }


def run(
    *,
    days: int = 5,
    symbol: str | None = None,
    target_date: str | None = None,
    max_concurrent: int = 8,
    dry_run: bool = False,
    limit: int = 0,
) -> dict:
    """增量同步最近 N 天（或指定日期/股票）。"""
    mod = _load_crawler()
    mod.HKEXTradingCalendar._build_trading_calendar()

    end = date.today()
    if target_date:
        end = datetime.strptime(target_date, "%Y-%m-%d").date()

    trading = _trading_days(end, days, mod.HKEXTradingCalendar)
    log.info(
        "待同步交易日: %d 个 (%s ~ %s)",
        len(trading),
        trading[0] if trading else "-",
        trading[-1] if trading else "-",
    )

    results = []
    for day in trading:
        r = asyncio.run(
            sync_partition(
                day,
                max_concurrent=max_concurrent,
                dry_run=dry_run,
                limit=limit,
                symbol=symbol,
            )
        )
        results.append(r)
        log.info("[%s] %s", day.strftime("%Y%m%d"), r["status"])

    synced = [r for r in results if r["status"] in ("synced", "partial")]
    skipped = [r for r in results if r["status"] in ("exists", "up_to_date")]

    return {
        "days_checked": len(trading),
        "synced_days": len(synced),
        "skipped_days": len(skipped),
        "details": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="CCASS 爬虫 → QuantHK 增量同步")
    parser.add_argument("--days", type=int, default=5, help="同步最近多少个交易日")
    parser.add_argument(
        "--date", default=None, help="指定同步日期 YYYY-MM-DD（默认今天往前 days 天）"
    )
    parser.add_argument("--symbol", default=None, help="指定股票代码（5位）")
    parser.add_argument("--concurrent", type=int, default=8, help="抓取并发数")
    parser.add_argument(
        "--limit", type=int, default=0, help="限制抓取股票数（0=全部，用于验证）"
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览待同步，不抓取")
    args = parser.parse_args()

    try:
        result = run(
            days=args.days,
            symbol=args.symbol,
            target_date=args.date,
            max_concurrent=args.concurrent,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
