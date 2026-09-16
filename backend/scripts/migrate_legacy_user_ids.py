"""存量用户 user_id 规范化迁移：非 8 位数字 ID → 8 位规范 ID（幂等）。

覆盖 admin 纠正（admin / 00000001 → 10000001）与其他历史坏 ID（随机分配，
与 auth_service._generate_user_id 同算法并做唯一性校验）。

迁移面：全部字符型 user_id 列（单事务，FK 先卸后建）+ Redis 用户键
（后缀 :<old> → :<new>）+ 股票池用户目录（u<old> → u<new>）。

用法（服务器上）：
    docker exec quantmind python3 /app/backend/scripts/migrate_legacy_user_ids.py --dry-run
    docker exec quantmind python3 /app/backend/scripts/migrate_legacy_user_ids.py

迁移后被改名的用户需重新登录一次（旧 JWT 的 sub 已失效）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _rename_redis_keys(plan: dict[str, str], dry_run: bool) -> list[str]:
    renamed: list[str] = []
    try:
        import redis
    except ImportError:
        print("redis 包不可用，跳过 Redis 键迁移")
        return renamed
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    for db in range(6):
        try:
            client = redis.Redis(
                host=host, port=port, db=db, password=password,
                socket_connect_timeout=5, decode_responses=True,
            )
            for old, new in plan.items():
                for key in client.scan_iter(match=f"*:{old}", count=1000):
                    new_key = key[: -len(old)] + new
                    renamed.append(f"db{db}:{key} -> {new_key}")
                    if not dry_run:
                        client.rename(key, new_key)
            client.close()
        except Exception as exc:
            print(f"Redis db{db} 跳过: {exc}")
    return renamed


def _rename_pool_dirs(plan: dict[str, str], dry_run: bool) -> list[str]:
    renamed: list[str] = []
    base = os.getenv("QM_STOCK_POOL_TXT_DIR", "/data/stock_pool")
    for old, new in plan.items():
        src = os.path.join(base, f"u{old}")
        dst = os.path.join(base, f"u{new}")
        if os.path.isdir(src) and not os.path.exists(dst):
            renamed.append(f"{src} -> {dst}")
            if not dry_run:
                try:
                    os.rename(src, dst)
                except Exception as exc:
                    print(f"池目录改名失败 {src}: {exc}")
    return renamed


async def main() -> int:
    ap = argparse.ArgumentParser(description="存量用户 user_id 规范化迁移")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    ap.add_argument("--skip-redis", action="store_true", help="跳过 Redis 键迁移")
    ap.add_argument("--skip-fs", action="store_true", help="跳过股票池目录改名")
    args = ap.parse_args()

    from backend.shared.admin_identity import migrate_user_ids, plan_legacy_migration

    try:
        plan = await plan_legacy_migration()
    except ValueError as exc:
        print(f"规划失败: {exc}", file=sys.stderr)
        return 1
    if not plan:
        print("无不规范 user_id，无需迁移")
        return 0
    print("迁移计划:")
    for old, new in plan.items():
        print(f"  - {old} -> {new}")

    report = await migrate_user_ids(plan, dry_run=args.dry_run)
    total = sum(report["updated"].values())
    print(f"DB: {len(report['updated'])} 张表，共 {total} 行")
    for table, n in sorted(report["updated"].items()):
        print(f"  - {table}: {n}")

    if not args.skip_redis:
        for r in _rename_redis_keys(plan, args.dry_run):
            print(f"  Redis: {r}")
    if not args.skip_fs:
        for r in _rename_pool_dirs(plan, args.dry_run):
            print(f"  池目录: {r}")

    if args.dry_run:
        print("[dry-run] 未写入任何数据")
    else:
        print("完成，被改名用户请重新登录一次")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
