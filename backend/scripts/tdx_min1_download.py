"""通达信桥 1 分钟线批量下载（2026-10-30，样本外回放用）。

桥封装 ``get_market_data`` 只有 ``count``（无 start/end），单次 ``count=16000``
即可取回客户端本地全量分钟线（实测 600036.SH：15,840 根 = 2026-07-22~10-29）。
逐只落 ``data/tdx_min1/{SYMBOL}.parquet``，已存在即跳过（``--force`` 覆盖）。

宇宙口径与 ``sim_scan_multiday.py`` 一致：L1 ``amt_ma_20`` 前 N 只，锚定
``--anchor``（默认 20260825，严格早于样本外窗口）。

节流：默认并发 4、间隔 0.25s（≈150 次/分），低于桥 600/分 与采集侧 180/分
预算；遇 ``rate_limited`` 退避重试。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.services.live_trading.services.tdx_ingest.bridge_client import (  # noqa: E402
    BridgeError,
    TdxBridgeClient,
)

L1_DIR = ROOT / "data" / "quantdb" / "6_ml_datasets" / "l1_factors"
OUT_DIR = ROOT / "data" / "tdx_min1"

_COLS = (
    ("Date", "date"),
    ("Time", "time"),
    ("Open", "open"),
    ("High", "high"),
    ("Low", "low"),
    ("Close", "close"),
    ("Volume", "volume"),
    ("Amount", "amount"),
    ("ForwardFactor", "forward_factor"),
)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def load_universe(anchor: str, n: int) -> list[str]:
    path = L1_DIR / f"dt={anchor}" / "data.parquet"
    t = pq.read_table(path, columns=["symbol", "amt_ma_20"]).to_pydict()
    rows = [
        (str(s).upper(), float(a or 0.0))
        for s, a in zip(t["symbol"], t["amt_ma_20"], strict=False)
    ]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def parse_bars(result: dict, symbol: str) -> pa.Table | None:
    value = result.get("Value") or {}
    payload = value.get(symbol) or (next(iter(value.values())) if value else None)
    if not isinstance(payload, dict):
        return None
    cols = {}
    for src, dst in _COLS:
        raw = payload.get(src)
        if not isinstance(raw, list) or not raw:
            return None
        cols[dst] = raw
    n = len(cols["date"])
    if any(len(v) != n for v in cols.values()):
        return None
    return pa.table(
        {
            "date": pa.array([int(x) for x in cols["date"]], pa.int32()),
            "time": pa.array([int(x) for x in cols["time"]], pa.int32()),
            "open": pa.array([_f(x) for x in cols["open"]], pa.float64()),
            "high": pa.array([_f(x) for x in cols["high"]], pa.float64()),
            "low": pa.array([_f(x) for x in cols["low"]], pa.float64()),
            "close": pa.array([_f(x) for x in cols["close"]], pa.float64()),
            "volume": pa.array([_f(x) for x in cols["volume"]], pa.float64()),
            "amount": pa.array([_f(x) for x in cols["amount"]], pa.float64()),
            "forward_factor": pa.array(
                [_f(x) for x in cols["forward_factor"]], pa.float64()
            ),
        }
    )


async def fetch_one(
    client: TdxBridgeClient, symbol: str, count: int, sem: asyncio.Semaphore
) -> tuple[str, str]:
    async with sem:
        for attempt in range(3):
            try:
                result = await client.call(
                    "get_market_data",
                    {
                        "stock_list": [symbol],
                        "period": "1m",
                        "count": count,
                        "dividend_type": "none",
                    },
                )
                table = parse_bars(result, symbol)
                if table is None:
                    return symbol, "empty"
                path = OUT_DIR / f"{symbol}.parquet"
                tmp = path.with_suffix(".tmp")
                pq.write_table(table, tmp)
                tmp.replace(path)
                return symbol, f"ok {table.num_rows}"
            except BridgeError as exc:
                if exc.kind == "rate_limited" and attempt < 2:
                    await asyncio.sleep(3.0 * (attempt + 1))
                    continue
                return symbol, f"err {exc.kind}: {str(exc)[:120]}"
            except Exception as exc:  # noqa: BLE001
                return symbol, f"err {type(exc).__name__}: {str(exc)[:120]}"
    return symbol, "err retries"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=int, default=500)
    ap.add_argument("--anchor", default="20260825")
    ap.add_argument("--count", type=int, default=16000)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    universe = load_universe(args.anchor, args.universe)
    todo = [
        s for s in universe if args.force or not (OUT_DIR / f"{s}.parquet").exists()
    ]
    print(f"宇宙 {len(universe)} 只，待下载 {len(todo)} 只 → {OUT_DIR}")

    client = TdxBridgeClient(timeout_s=120)
    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.time()
    done = ok = 0
    failed: list[tuple[str, str]] = []
    tasks = []
    for sym in todo:
        tasks.append(asyncio.create_task(fetch_one(client, sym, args.count, sem)))
        await asyncio.sleep(0.35)
    for fut in asyncio.as_completed(tasks):
        sym, status = await fut
        done += 1
        if status.startswith("ok"):
            ok += 1
        else:
            failed.append((sym, status))
        if done % 25 == 0 or done == len(tasks):
            print(f"  {done}/{len(tasks)} ok={ok} 用时 {time.time() - t0:.0f}s")
    await client.aclose()

    print(f"完成：ok={ok} 失败={len(failed)} 用时 {time.time() - t0:.0f}s")
    for sym, status in failed[:20]:
        print("  FAIL", sym, status)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
