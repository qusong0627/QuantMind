#!/usr/bin/env python3
"""akshare 港股日线 → QuantHK 增量同步脚本。

用 akshare stock_hk_daily（不复权全量历史）抓取港股日线，按 QuantDB 的
Hive 分区格式增量落盘到 quanthk 本地 parquet，作为付费数据的补充数据源
（多源冗余）。

落盘格式:
  {quanthk}/1_kline_data/daily_forward/dt=YYYYMMDD/data.parquet
  stock_code 5位（00700）→ 4位+.HK（0700.HK）
  存原始价（不复权），与现有付费数据口径一致。

增量逻辑:
  对每只股票，检查已落盘分区缺失的日期，仅抓取缺失部分。
  由于 stock_hk_daily 返回全量，直接全量拉取后按已有分区过滤。

用法:
  python backend/scripts/quanthk_akshare_kline.py --days 5
  python backend/scripts/quanthk_akshare_kline.py --symbol 00700
  python backend/scripts/quanthk_akshare_kline.py --concurrent 8
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
log = logging.getLogger("quanthk_akshare_kline")

QUANTHK_DATA_DIR = Path(
    os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk"))
)
REL_DIR = "1_kline_data/daily_forward"

OUT_COLS = [
    "symbol", "time", "open", "high", "low", "close",
    "volume", "amount", "release_id", "published_at",
]

DEFAULT_THREADS = 6

#: 单次 HTTP 请求的默认超时（秒）。akshare 的 ``stock_hk_daily`` 走
#: ``requests.get(url)`` → ``Session.request()``，而 akshare **不传 timeout**——
#: 一条死连接就能让 ``as_completed`` 永远等下。2026-11-06 实测：2875 只单只 0.3s、
#: 6 并发本该 2.4 分钟跑完，实际挂满 110 分钟、CPU 0.14%，直到任务软超时（115 分钟）
#: 被杀；当晚本可落盘的 11-05 数据一行没写。
DEFAULT_HTTP_TIMEOUT = float(os.getenv("QM_HK_HTTP_TIMEOUT_S") or 20)

#: 整轮抓取的兜底预算（秒）。单请求超时之外再加一道：防止「每个请求都恰好不超时、
#: 但整体慢到吃满夜窗」。到点后不再等剩余标的，已拿到的照常落盘。
DEFAULT_FETCH_BUDGET = float(os.getenv("QM_HK_FETCH_BUDGET_S") or 1800)


def _install_default_http_timeout(seconds: float) -> None:
    """给 ``requests`` 打默认超时——只补**调用方没传**的情形。

    补在 ``Session.request`` 上而不是 ``requests.get``：akshare 里两种写法都有，
    而 ``requests.get`` 内部本就是 ``Session().request()``，补 Session 覆盖面最大。
    显式传了 ``timeout`` 的调用方一律不动（别人的超时语义不该被我们改写）。

    幂等：模块级函数对象上打标记，重复调用只装一次。
    """
    import requests

    if getattr(requests.Session.request, "_qm_default_timeout", None):
        return
    original = requests.Session.request

    def request_with_timeout(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = seconds
        return original(self, method, url, **kwargs)

    request_with_timeout._qm_default_timeout = seconds  # type: ignore[attr-defined]
    # 留一条回退到原函数的引用：进程内被别的模块再打补丁时可还原，测试也据此
    # 在用例之间复位（否则一次安装会污染整个 pytest 进程的 requests 语义）。
    request_with_timeout._qm_original = original  # type: ignore[attr-defined]
    requests.Session.request = request_with_timeout  # type: ignore[method-assign]


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


def _to_qhk_symbol(code: str) -> str:
    """5位港股代码 → 4位+.HK。00700 → 0700.HK。"""
    code = code.strip().zfill(5)
    if code.startswith("0") and len(code) == 5:
        code = code[1:]
    return f"{code}.HK"


def _existing_partitions() -> set[str]:
    d = _quanthk_root() / REL_DIR
    if not d.is_dir():
        return set()
    return {p.name[3:] for p in d.glob("dt=*")}


def _stock_list() -> list[str]:
    """港股代码池。优先读 hk.csv，否则用常见代码。"""
    for csv_path in (
        Path(__file__).parent / "hk.csv",
        Path("/data/hk.csv"),
        Path("/app/backend/scripts/hk.csv"),
    ):
        if csv_path.is_file():
            try:
                df = pd.read_csv(csv_path, encoding="utf-8-sig")
                if "id" in df.columns:
                    return df["id"].astype(str).str.zfill(5).tolist()
            except Exception:
                pass
    # 兜底：常用蓝筹
    return ["00001", "00002", "00005", "00006", "00700", "00939", "00941", "00998", "01299", "02318"]


def _fetch_stock(symbol: str, min_date: str) -> pd.DataFrame | None:
    """抓取单只港股全量日线（akshare stock_hk_daily，不复权）。"""
    import akshare as ak

    try:
        df = ak.stock_hk_daily(symbol=symbol)
        if df is None or df.empty:
            return None
        df = df.rename(columns={"date": "time"})
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        # 只保留待补日期（min_date 之后）
        if min_date:
            df = df[df["time"].dt.strftime("%Y%m%d") >= min_date]
        if df.empty:
            return None
        df["symbol"] = _to_qhk_symbol(symbol)
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # 仙股/竞价数据上游噪声：high 可能低于 open/close（反之 low），
        # 写盘前按行收敛，保证 OHLC 自洽（max/min 自动跳过 NaN）
        df["high"] = df[["high", "open", "close"]].max(axis=1)
        df["low"] = df[["low", "open", "close"]].min(axis=1)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        df["release_id"] = "akshare"
        df["published_at"] = datetime.now().isoformat(timespec="seconds")
        return df[OUT_COLS].dropna(subset=["close"])
    except Exception as exc:  # noqa: BLE001
        log.debug("抓取 %s 失败: %s", symbol, exc)
        return None


def sync(
    *,
    symbols: list[str] | None = None,
    days: int = 5,
    concurrent: int = DEFAULT_THREADS,
    dry_run: bool = False,
) -> dict:
    """增量同步 akshare 港股日线。"""
    existing = _existing_partitions()
    # 待补日期范围：最近 days 个交易日
    end = date.today()
    start = end - timedelta(days=days * 1.7)  # 粗略覆盖工作日
    min_date = start.strftime("%Y%m%d")

    syms = symbols or _stock_list()

    # 增量早退：最新分区已含今天，说明每晚同步已把当日数据落盘，
    # 再全量拉 2807 只×9 天只会白耗 40-60 分钟并拖垮整条 CCASS/南向链路。
    # 分区分批写入在各股抓取完成后统一进行，被超时杀掉的任务不会留下半截分区，
    # 故「最新分区 == 今天」可安全视为已最新。
    newest = max(existing) if existing else ""
    if newest >= end.strftime("%Y%m%d"):
        log.info(
            "日线已最新（最新分区 %s，今天 %s），跳过全量拉取",
            newest, end.strftime("%Y%m%d"),
        )
        return {
            "status": "up_to_date", "newest_partition": newest,
            "stocks": len(syms), "existing_partitions": len(existing),
        }
    # 只补「最新分区之后」的日期：夜间正常增量只抓今天一天；落盘延迟/长假后
    # 自动变成多日回补（仍全量抓取后按此日期过滤，数据不重不漏）。
    if newest:
        nxt = (
            datetime.strptime(newest, "%Y%m%d") + timedelta(days=1)
        ).strftime("%Y%m%d")
        if nxt > min_date:
            min_date = nxt

    log.info("待同步标的: %d 只，最近 %d 天 (从 %s)", len(syms), days, min_date)

    if dry_run:
        return {"stocks": len(syms), "min_date": min_date, "existing_partitions": len(existing), "dry_run": True}

    frames: list[pd.DataFrame] = []
    ok = 0
    err = 0
    timed_out = 0
    _install_default_http_timeout(DEFAULT_HTTP_TIMEOUT)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        futures = {pool.submit(_fetch_stock, s, min_date): s for s in syms}
        try:
            # timeout 必须挂在 as_completed 上：只靠循环体里的 deadline 判断的话，
            # 「全部标的都卡住」时一个 future 都不会完成，那个判断永远轮不到，
            # 整轮照样挂到任务软超时（正是要修的那个形态）。
            for done, future in enumerate(
                as_completed(futures, timeout=DEFAULT_FETCH_BUDGET), start=1
            ):
                df = future.result()
                if df is not None and not df.empty:
                    frames.append(df)
                    ok += 1
                else:
                    err += 1
                # 进度日志：卡住时要能一眼看出「停在第几只」，否则整轮静默（2026-11-06）
                if done % 500 == 0:
                    log.info(
                        "日线进度 %d/%d（ok=%d err=%d 用时 %.0fs）",
                        done, len(syms), ok, err, time.monotonic() - started,
                    )
        except TimeoutError:
            timed_out = max(len(syms) - ok - err, 0)
            log.warning(
                "日线抓取预算 %.0fs 用尽：已完成 %d/%d，剩余 %d 只不再等待"
                "（已拿到的照常落盘；调 QM_HK_FETCH_BUDGET_S 可放宽）",
                DEFAULT_FETCH_BUDGET, ok + err, len(syms), timed_out,
            )

    if not frames:
        return {
            "stocks": len(syms), "ok": ok, "err": err, "rows": 0, "timed_out": timed_out
        }

    all_df = pd.concat(frames, ignore_index=True)
    target_dir = _quanthk_root() / REL_DIR

    # 按交易日分区写入（增量合并去重）
    grouped = {ts.strftime("%Y%m%d"): g for ts, g in all_df.groupby(all_df["time"].dt.date)}
    written = 0
    for date_str, chunk in sorted(grouped.items()):
        dt_dir = target_dir / f"dt={date_str}"
        dt_dir.mkdir(parents=True, exist_ok=True)
        out = dt_dir / "data.parquet"
        if out.exists():
            old = pd.read_parquet(out)
            combined = pd.concat([old, chunk], ignore_index=True)
            combined = combined.drop_duplicates(subset=["symbol", "time"], keep="last")
            combined.to_parquet(out, index=False)
        else:
            chunk.to_parquet(out, index=False)
        written += 1

    return {
        "stocks": len(syms),
        "ok": ok,
        "err": err,
        "timed_out": timed_out,
        "rows": int(len(all_df)),
        "partitions_written": written,
        "start_date": min(grouped),
        "end_date": max(grouped),
        "target_dir": str(target_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="akshare 港股日线 → QuantHK")
    parser.add_argument("--days", type=int, default=5, help="同步最近多少个交易日")
    parser.add_argument("--symbol", default=None, help="指定股票代码（5位），逗号分隔多个")
    parser.add_argument("--concurrent", type=int, default=DEFAULT_THREADS, help="并发数")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不抓取")
    args = parser.parse_args()

    try:
        syms = [s.strip().zfill(5) for s in args.symbol.split(",") if s.strip()] if args.symbol else None
        result = sync(symbols=syms, days=args.days, concurrent=args.concurrent, dry_run=args.dry_run)
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
