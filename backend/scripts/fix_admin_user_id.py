"""纠正 admin 的 user_id 为 10000001（幂等，可重复执行）。

用法（服务器上）：
    docker exec quantmind python3 /app/backend/scripts/fix_admin_user_id.py --dry-run
    docker exec quantmind python3 /app/backend/scripts/fix_admin_user_id.py

纠正后 admin 需重新登录一次（旧 JWT 的 sub='admin' / '00000001' 会由鉴权中间件映射）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from backend.shared.admin_identity import ADMIN_USER_ID


def _rename_redis_keys(dry_run: bool) -> list[str]:
    """把管理员历史后缀键改到 :10000001（目标已存在则跳过）。"""
    renamed: list[str] = []
    try:
        import redis
    except ImportError:
        print("redis 包不可用，跳过 Redis 键迁移")
        return renamed
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    prefixes = (
        "simulation:account:",
        "simulation:settings:",
        "trade:active_strategy:",
    )
    old_suffixes = ("admin", "00000001", "0", "1")
    for db in range(6):
        try:
            client = redis.Redis(
                host=host, port=port, db=db, password=password,
                socket_connect_timeout=5, decode_responses=True,
            )
            for prefix in prefixes:
                for key in client.scan_iter(match=f"{prefix}*", count=1000):
                    for old in old_suffixes:
                        if key.endswith(":" + old):
                            new_key = key[: -len(old)] + ADMIN_USER_ID
                            renamed.append(f"db{db}:{key} -> {new_key}")
                            if not dry_run and new_key != key and not client.exists(new_key):
                                client.rename(key, new_key)
                            break
            client.close()
        except Exception as exc:
            print(f"Redis db{db} 跳过: {exc}")
    return renamed


async def main() -> int:
    ap = argparse.ArgumentParser(description=f"纠正 admin user_id 为 {ADMIN_USER_ID}")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    ap.add_argument("--skip-redis", action="store_true", help="跳过 Redis 键迁移")
    args = ap.parse_args()

    from backend.shared.admin_identity import fix_admin_user_id

    report = await fix_admin_user_id(dry_run=args.dry_run)
    total = sum(report["updated"].values())
    print(f"DB: {len(report['updated'])} 张表，共 {total} 行 → {ADMIN_USER_ID}")
    for table, n in sorted(report["updated"].items()):
        print(f"  - {table}: {n}")
    print(f"users 主行已纠正: {report['users_fixed']}")

    if not args.skip_redis:
        renamed = _rename_redis_keys(args.dry_run)
        print(f"Redis: {len(renamed)} 个键")
        for r in renamed:
            print(f"  - {r}")

    if args.dry_run:
        print("[dry-run] 未写入任何数据")
    else:
        print("完成，admin 请重新登录一次")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
