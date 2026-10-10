"""行情读写分裂探针（T4-4 审计 H14 验收）：来源标注与键位分布一眼清。

用法（容器内）::

    python backend/scripts/probe_quote_source.py                    # 只读清点
    python backend/scripts/probe_quote_source.py --clean-local-fake # 清本机伪时序键

只读为主，三段输出：
1) 解析结果：resolve_remote_quote_redis() 实际端点（不含口令）+ 是否回落内置公共服；
2) 远端行情 Redis（写侧家）：market:snapshot / market:series 键数；series 最新点
   抽样按 source 分布（席位标注应见 tdx_bridge / qmt_big 等）；persist_stats；
3) 本机 db3（REDIS_DB_MARKET）：legacy market:series / market:snapshot 清点，
   每键最新点 source 分布——修复前 quote_pusher 回写的伪实时点 = source:quantdb。

``--clean-local-fake``：删除本机 db3 中**最新点 source=quantdb** 的 market:series
键（只删全链已无读侧的 legacy 伪时序键；真实席位键不受影响）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import redis  # noqa: E402

from backend.shared.remote_quote_config import (  # noqa: E402
    remote_quote_disabled,
    resolve_remote_quote_redis,
    using_builtin_free_feed,
)

_SAMPLE_CAP = 300


def _scan_keys(client, pattern: str, cap: int = 5000) -> list[str]:
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = client.scan(cursor, match=pattern, count=500)
        keys.extend(batch)
        if cursor == 0 or len(keys) >= cap:
            break
    return keys[:cap]


def _latest_sources(client, keys: list[str], cap: int = _SAMPLE_CAP) -> tuple[Counter, str | None]:
    """取每键最新一个 series 点的 source 分布；（分布, 抽样内最新 datetime）。"""
    counter: Counter = Counter()
    newest_dt: str | None = None
    sample = keys[:cap]
    for i in range(0, len(sample), 100):
        pipe = client.pipeline(transaction=False)
        for key in sample[i : i + 100]:
            pipe.zrange(key, -1, -1)
        for raw in pipe.execute():
            if not raw:
                counter["<空>"] += 1
                continue
            try:
                point = json.loads(raw[0])
            except Exception:
                counter["<坏行>"] += 1
                continue
            counter[str(point.get("source") or "<无source>")] += 1
            dt = point.get("datetime")
            if dt and (newest_dt is None or dt > newest_dt):
                newest_dt = dt
    return counter, newest_dt


def _local_client() -> redis.Redis:
    password = (
        os.getenv("MARKET_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD", "")
    ).strip() or None
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "quantmind-redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=password,
        db=int(os.getenv("REDIS_DB_MARKET", "3")),
        decode_responses=True,
        socket_timeout=3,
    )


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean-local-fake",
        action="store_true",
        help="删除本机 db3 中最新点 source=quantdb 的 market:series 伪时序键",
    )
    args = parser.parse_args()

    _section("1. 解析结果")
    if remote_quote_disabled():
        print("REMOTE_QUOTE_DISABLED=true → 远端停用，读侧回落部署内 Redis（日线兜底场）")
    resolved = resolve_remote_quote_redis()
    if resolved is None:
        print("resolve_remote_quote_redis() → None")
    else:
        host, port, password, db = resolved
        print(
            f"远端行情 Redis: {host}:{port} db={db} "
            f"password={'已设置' if password else '无'} "
            f"builtin_free_feed={using_builtin_free_feed()}"
        )

    _section("2. 远端行情 Redis（写侧家）")
    if resolved is not None:
        host, port, password, db = resolved
        remote = redis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            decode_responses=True,
            socket_timeout=5,
        )
        try:
            snaps = _scan_keys(remote, "market:snapshot:*")
            series = _scan_keys(remote, "market:series:*")
            print(f"market:snapshot:* 键数: {len(snaps)}")
            print(f"market:series:*   键数: {len(series)}")
            dist, newest_dt = _latest_sources(remote, series)
            print(f"series 最新点 source 抽样分布（至多 {_SAMPLE_CAP} 键）: {dict(dist)}")
            print(f"series 最新点时间（抽样内最新）: {newest_dt}")
            print(f"persist_stats: {remote.get('market:stream:persist_stats')}")
        except Exception as e:  # noqa: BLE001 探针不因读败中断
            print(f"远端读取失败: {e}")

    _section("3. 本机 db3（legacy / 兜底场）")
    local = _local_client()
    try:
        l_series = _scan_keys(local, "market:series:*")
        l_snaps = _scan_keys(local, "market:snapshot:*")
        print(f"market:series:*   键数: {len(l_series)}")
        print(f"market:snapshot:* 键数: {len(l_snaps)}")
        dist, newest_dt = _latest_sources(local, l_series)
        print(f"series 最新点 source 分布: {dict(dist)}")
        print(f"series 最新点时间: {newest_dt}")

        if args.clean_local_fake and l_series:
            fake_keys = []
            for i in range(0, len(l_series), 100):
                batch = l_series[i : i + 100]
                pipe = local.pipeline(transaction=False)
                for key in batch:
                    pipe.zrange(key, -1, -1)
                for key, raw in zip(batch, pipe.execute(), strict=True):
                    try:
                        point = json.loads(raw[0]) if raw else {}
                    except Exception:
                        point = {}
                    if point.get("source") == "quantdb":
                        fake_keys.append(key)
            if fake_keys:
                local.delete(*fake_keys)
                print(f"已删除 {len(fake_keys)} 个 source=quantdb 的伪时序键（本机 db3）")
            else:
                print("无 source=quantdb 伪时序键可删")
    except Exception as e:  # noqa: BLE001
        print(f"本机读取失败: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
