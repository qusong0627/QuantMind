"""重建 QMT 全市场订阅标的集合 ``qm:universe:cn``。

伴生桥的 quote tap 以 ``tap.universe_key`` 指向的 Redis 集合作为订阅域；
本脚本从 PG ``stock_daily_latest``（最近交易日）取全市场代码，统一转成
**后缀式**（``600036.SH`` / ``920000.BJ``，QMT 口径）后原子替换该集合。

设计：
- 先写临时键再 ``RENAME``：桥侧 ``SMEMBERS`` 永远读到完整集合，不会读到半成品；
- 集合 TTL 7 天：数据管线每天重建一次，停跑一周后自然过期（不至于订阅空集合）；
- 只写缓存键，不改任何交易状态；``--dry-run`` 只看不写。

用法（容器内）：
    python backend/scripts/rebuild_quote_universe.py --dry-run
    python backend/scripts/rebuild_quote_universe.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time

DEFAULT_KEY = "qm:universe:cn"
META_KEY_SUFFIX = ":meta"
TTL_SECONDS = 7 * 24 * 3600


def normalize_symbols(symbols: list[str]) -> list[str]:
    """前缀式/裸码 → 后缀式（QMT 口径），去重并保持稳定顺序。"""
    from backend.shared.stock_utils import StockCodeUtil

    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        code = str(raw or "").strip().upper()
        if not code:
            continue
        suffix = (code if "." in code else StockCodeUtil.to_suffix(code)).upper()
        if not suffix or "." not in suffix:
            continue
        # 只收 A 股三市（SH/SZ/BJ）；港股/美股有各自的 universe 键
        if suffix.rsplit(".", 1)[-1] not in {"SH", "SZ", "BJ"}:
            continue
        if suffix in seen:
            continue
        seen.add(suffix)
        out.append(suffix)
    return out


def _redis_client():
    import redis

    return redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("QM_UNIVERSE_REDIS_DB", "5")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )


def _connect_pg():
    """psycopg(3) 优先，容器内实际只有 psycopg2 时自动回落。"""
    dsn = (
        f"host={os.getenv('DB_HOST', 'db')} port={os.getenv('DB_PORT', '5432')} "
        f"dbname={os.getenv('DB_NAME', 'quantmind')} user={os.getenv('DB_USER', 'quantmind')} "
        f"password={os.getenv('DB_PASSWORD', '')}"
    )
    try:
        import psycopg

        return psycopg.connect(dsn)
    except ImportError:
        import psycopg2

        return psycopg2.connect(dsn)


def _fetch_symbols() -> tuple[list[str], str]:
    """从 PG 取最近交易日的全市场代码。"""
    with _connect_pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM stock_daily_latest")
        row = cur.fetchone()
        trade_date = str(row[0]) if row and row[0] else ""
        if not trade_date:
            return [], ""
        cur.execute(
            "SELECT DISTINCT symbol FROM stock_daily_latest WHERE trade_date = %s",
            (trade_date,),
        )
        return [r[0] for r in cur.fetchall()], trade_date


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    raw, trade_date = _fetch_symbols()
    if not raw:
        print("PG 无可用标的（stock_daily_latest 为空？）", file=sys.stderr)
        return 2
    symbols = normalize_symbols(raw)
    print(f"trade_date={trade_date} raw={len(raw)} normalized={len(symbols)}")
    if args.dry_run:
        print("dry-run: 样例", symbols[:5], "...", symbols[-3:])
        return 0

    client = _redis_client()
    tmp_key = f"{args.key}:tmp:{int(time.time())}"
    pipe = client.pipeline(transaction=False)
    for start in range(0, len(symbols), 2000):
        pipe.sadd(tmp_key, *symbols[start : start + 2000])
    pipe.expire(tmp_key, TTL_SECONDS)
    pipe.execute()
    client.rename(tmp_key, args.key)
    client.expire(args.key, TTL_SECONDS)
    client.hset(
        f"{args.key}{META_KEY_SUFFIX}",
        mapping={
            "trade_date": trade_date,
            "count": str(len(symbols)),
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    client.expire(f"{args.key}{META_KEY_SUFFIX}", TTL_SECONDS)
    print(f"已写入 {args.key}: {client.scard(args.key)} 只（TTL {TTL_SECONDS}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
