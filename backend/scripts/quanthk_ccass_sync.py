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
import json
import logging
import os
import shutil
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


def _hk_trading_days() -> set[str]:
    """港股实际交易日集合（优先 index_daily，其次 daily_forward 分区）。

    分区由行情链维护，比 yfinance 日历可靠——实测 yfinance 把 2026-01-01、
    春节、复活节、端午等港股假期当成交易日，导致 CCASS 在假期抓到「上一
    交易日快照」并以假期日期落盘（8 个重复分区）。目录不可用时返回空集，
    调用方退化到工作日判断。
    """
    root = _quanthk_root()
    for rel in ("1_kline_data/index_daily", "1_kline_data/daily_forward"):
        d = root / rel
        if not d.is_dir():
            continue
        out = {
            p.name[3:]
            for p in d.glob("dt=*")
            if (p / "data.parquet").is_file()
        }
        if out:
            return out
    return set()


def _skip_reason(day: date, calendar: set[str]) -> str | None:
    """非交易日跳过原因；``None`` = 允许抓取。

    日历只覆盖到「已落盘的最后一个交易日」；对更新的日期（通常是今天，
    行情链还没写分区）不做日历否决，交给 HKEX + 重复指纹兜底。
    """
    if day.weekday() >= 5:
        return "weekend"
    if not calendar:
        return None
    ds = day.strftime("%Y%m%d")
    if ds in calendar:
        return None
    if ds > max(calendar):
        return None
    if ds < min(calendar):
        # 日历还没覆盖到该日期（历史回填早于行情分区起点）：不否决
        return None
    return "non_trading_day"


def _recent_trading_days(end: date, n_days: int, calendar: set[str]) -> list[date]:
    """最近 n 个候选交易日。

    日历只覆盖行情分区已有的日期：
    - 日历范围内：只取真实交易日（假期不抓）；
    - 日历范围外（今天/未来或更早的回填）：按工作日尝试，交给 HKEX +
      重复指纹判定。否则「今天还没写进 kline」会把当天 CCASS 漏掉。
    """
    days: list[date] = []
    d = end
    lo = min(calendar) if calendar else None
    hi = max(calendar) if calendar else None
    for _ in range(366 * 3):
        if len(days) >= n_days:
            break
        ds = d.strftime("%Y%m%d")
        if calendar and lo <= ds <= hi:
            if ds in calendar:
                days.append(d)
        elif d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _previous_partition_day(day: date, calendar: set[str]) -> str | None:
    """上一个有分区/在日历内的交易日（YYYYMMDD）。"""
    ds = day.strftime("%Y%m%d")
    candidates = {d for d in calendar if d < ds}
    candidates |= {d for d in _existing_partitions() if d < ds}
    return max(candidates) if candidates else None


def _is_duplicate_partition(
    date_str: str, partition_dir: Path, calendar: set[str]
) -> bool:
    """当日分区是否与上一交易日逐行相同（假期/上游未发布时 HKEX 会回旧数据）。"""
    cur = partition_dir / "data.parquet"
    if not cur.is_file():
        return False
    try:
        day = datetime.strptime(date_str, "%Y%m%d").date()
    except ValueError:
        return False
    prev_day = _previous_partition_day(day, calendar)
    if not prev_day:
        return False
    prev = _target_dir() / f"dt={prev_day}" / "data.parquet"
    if not prev.is_file():
        return False
    try:
        a = pd.read_parquet(
            prev, columns=["stock_code", "participant_id", "holding_quantity"]
        )
        b = pd.read_parquet(
            cur, columns=["stock_code", "participant_id", "holding_quantity"]
        )
    except Exception:  # noqa: BLE001 - 读取失败时不做重复判定
        return False
    if a.empty or b.empty:
        return False
    m = a.merge(
        b, on=["stock_code", "participant_id"], suffixes=("_a", "_b")
    )
    if len(m) < 0.99 * min(len(a), len(b)):
        return False
    same = (m["holding_quantity_a"] == m["holding_quantity_b"]).mean()
    return bool(same >= 0.999)


def _quarantine_root() -> Path:
    d = _quanthk_root() / "2_base_sector" / "ccass_top50_quarantine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _quarantine_partition(partition_dir: Path, reason: str) -> str:
    """把问题分区移到隔离目录并记账（不删除，可回查）。"""
    qroot = _quarantine_root()
    target = qroot / partition_dir.name
    if target.exists():
        target = qroot / f"{partition_dir.name}__{datetime.now():%Y%m%d%H%M%S}"
    shutil.move(str(partition_dir), str(target))
    manifest = qroot / "manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "dt": partition_dir.name[3:],
                    "reason": reason,
                    "moved_to": str(target),
                    "moved_at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return str(target)


def _write_qc(partition_dir: Path, status: str) -> dict:
    """写分区级质检侧车 ``qc.json``（可观测性；数据读取方只认 data.parquet）。"""
    from backend.shared.ccass_top50 import qc_summary

    df = pd.read_parquet(partition_dir / "data.parquet")
    qc = qc_summary(df)
    qc.update(
        {
            "dt": partition_dir.name[3:],
            "status": status,
            "generated_at": datetime.now().isoformat(),
        }
    )
    tmp = partition_dir / ".qc.json.tmp"
    tmp.write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(partition_dir / "qc.json")
    return qc


def _trading_days(end: date, n_days: int, calendar_cls=None) -> list[date]:
    """最近 n 个交易日（保留旧签名；优先行情分区日历）。"""
    return _recent_trading_days(end, n_days, _hk_trading_days())


def _normalise_fetch(
    df: pd.DataFrame, stock_code: str, stock_name: str, query_date: date
) -> pd.DataFrame | None:
    """爬虫输出 → 标准化列。取 top50（按持股数量降序）。

    百分比口径（2026-11-13 修复）：
    - HKEX 对部分股票不返回百分比列（4 列布局），爬虫给 None；这里必须落
      NaN，**绝不能 fillna(0)**（0 会被下游当成「持仓占比 0」）；
    - 旧实现对缺失列 fallback 到持股量，带逗号的数解析失败变 0、小于 1000 股
      变 数量/100；这里对 >100% 的值一律置 NaN；
    - 跨期单位修复由 ``backend.shared.ccass_top50.repair_percentages`` 统一做。
    """
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
    df["participant_name"] = df["participant_name"].fillna("").astype(str).str.strip()
    qty = pd.to_numeric(df["holding_quantity"], errors="coerce")
    df = df.loc[qty.notna()].copy()
    df["holding_quantity"] = qty.loc[qty.notna()].astype("int64")
    df = df.loc[df["holding_quantity"] > 0]
    # 百分比 "36.33%" → 0.3633；None/""/-- → NaN；>100% → NaN
    pct_text = (
        df.get("holding_percentage", pd.Series(index=df.index, dtype="object"))
        .astype("object")
        .map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
        .str.replace("%", "", regex=False)
        .str.strip()
    )
    pct = pd.to_numeric(pct_text, errors="coerce") / 100.0
    # 0% 与缺失同义（HKEX 两位小数会把小持仓四舍五入成 0.00%），统一 NaN。
    df["holding_percentage"] = pct.where(pct.gt(0) & pct.le(1.0001))
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

    calendar = _hk_trading_days()
    skip = _skip_reason(target_day, calendar)
    if skip:
        log.info("[%s] 跳过（%s）", date_str, skip)
        return {"date": date_str, "status": skip}

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
    # 阈值 90%（2026-10-15 由 50% 上调）：50% 时 51%~99% 覆盖率的中断分区
    # 会被永久判"完整"（如 1985/2780≈71% 卡了两天），低于 90% 即视为残缺补抓。
    incomplete = len(existing_stocks) < max(50, int(len(stock_df) * 0.9))
    if date_str in existing and skip_existing_stocks and not incomplete and not symbol:
        return {"date": date_str, "status": "exists", "stocks": len(existing_stocks)}
    if incomplete and date_str in existing:
        log.warning(
            "[%s] 分区残缺：已有 %d 只 / 全市场约 %d 只，将补抓缺失标的",
            date_str,
            len(existing_stocks),
            len(stock_df),
        )

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

    # 重复快照守卫：HKEX 对非交易日（或尚未发布的最新交易日）会返回上一
    # 交易日数据。2026 年实测 8 个港股假期分区与前一日逐行相同。整市场
    # 2,700 只股票不可能在真实交易日完全不变，99.9% 行同值即判重复。
    if _is_duplicate_partition(date_str, partition_dir, calendar):
        prev_day = _previous_partition_day(target_day, calendar)
        qpath = _quarantine_partition(partition_dir, "duplicate_prev_day")
        log.warning(
            "[%s] 与上一交易日 %s 逐行相同，判为重复快照并隔离: %s",
            date_str,
            prev_day,
            qpath,
        )
        return {
            "date": date_str,
            "status": "duplicate_prev_day",
            "duplicate_of": prev_day,
            "quarantined_to": qpath,
            "tried": len(todo),
            "success": success,
        }

    qc = _write_qc(partition_dir, "synced" if not banned else "partial")
    return {
        "date": date_str,
        "status": "synced" if not banned else "partial",
        "tried": len(todo),
        "success": success,
        "rows": sum(len(r) for r in rows),
        "banned": banned,
        "qc": qc,
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
    """增量同步最近 N 天（或指定日期/股票）。

    交易日取自行情分区（不是 yfinance 日历）；节假日直接跳过，不创建分区。
    """
    calendar = _hk_trading_days()
    if not calendar:
        log.warning("行情分区日历不可用（index_daily/daily_forward 均无分区），退化为工作日判断")

    end = date.today()
    if target_date:
        end = datetime.strptime(target_date, "%Y-%m-%d").date()
        trading = [end]
    else:
        trading = _recent_trading_days(end, days, calendar)
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
