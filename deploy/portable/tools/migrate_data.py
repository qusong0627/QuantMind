"""便携包数据迁移：从既有部署(源库)迁移策略 + 用户模型到目标库。

用途（本机一次性迁移）:
    python migrate_data.py --src 源库DSN --dst 目标库DSN --models-root 目标模型根目录

- strategies: 按 (user_id 用户名映射, name) upsert 用户全部策略（含自建与模板修订）
- qm_user_models: 按 (tenant_id, user_id, model_id) upsert，storage_path 前缀 /app/models 改写为 --models-root
仅迁移，不删除目标库任何数据。
"""
import argparse
import sys

import psycopg2

_REDIRECT_PREFIXES = ("/app/models", "/data/models")


def _connect(dsn: str):
    return psycopg2.connect(dsn)


def _username_map(src, dst, tenant_id: str) -> dict[int, int]:
    """源库 user_id(int) → 目标库 user_id(int)，按 (tenant, username) 匹配。"""
    mapping: dict[int, int] = {}
    with src.cursor() as cs, dst.cursor() as cd:
        cs.execute("SELECT id, username FROM users")
        for sid, username in cs.fetchall():
            cd.execute(
                "SELECT id FROM users WHERE username = %s", (username,)
            )
            row = cd.fetchone()
            if row:
                mapping[int(sid)] = int(row[0])
                print(f"  user map: {sid}({username}) -> {row[0]}")
    return mapping


def _migrate_strategies(src, dst, user_map: dict[int, int]) -> int:
    with src.cursor() as cs, dst.cursor() as cd:
        cs.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='strategies' ORDER BY ordinal_position"
        )
        src_cols = [r[0] for r in cs.fetchall()]
        cd.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='strategies' ORDER BY ordinal_position"
        )
        dst_cols = {r[0] for r in cd.fetchall()}
        cols = [c for c in src_cols if c in dst_cols and c not in ("id",)]

        cs.execute("SELECT * FROM strategies")
        inserted = updated = skipped = 0
        # 预读目标库已有 (user_id, name) → id（无唯一约束，先查后更/插）
        existing: dict[tuple[int, str], int] = {}
        for dst_uid in set(user_map.values()):
            cd.execute("SELECT id, name FROM strategies WHERE user_id = %s", (dst_uid,))
            for rid, rname in cd.fetchall():
                existing[(dst_uid, str(rname))] = int(rid)
        for row in cs.fetchall():
            rec = dict(zip(src_cols, row))
            uid = user_map.get(int(rec["user_id"]))
            if uid is None:
                skipped += 1
                continue
            rec["user_id"] = uid
            key = (uid, str(rec["name"]))
            if key in existing:
                set_cols = [c for c in cols if c not in ("name",)]
                cd.execute(
                    f"UPDATE strategies SET {', '.join(f'{c}=%s' for c in set_cols)} "
                    f"WHERE id = %s",
                    [rec[c] for c in set_cols] + [existing[key]],
                )
                updated += 1
            else:
                cd.execute(
                    f"INSERT INTO strategies ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                    [rec[c] for c in cols],
                )
                inserted += 1
        dst.commit()
        print(f"  strategies: {inserted} 新增, {updated} 更新, {skipped} 跳过")
        return inserted


def _migrate_user_models(src, dst, user_map: dict[int, int], models_root: str) -> int:
    with src.cursor() as cs, dst.cursor() as cd:
        cs.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='qm_user_models' ORDER BY ordinal_position"
        )
        src_cols = [r[0] for r in cs.fetchall()]
        cd.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='qm_user_models' ORDER BY ordinal_position"
        )
        dst_cols = {r[0] for r in cd.fetchall()}
        cols = [c for c in src_cols if c in dst_cols and c not in ("created_at", "updated_at")]

        cs.execute("SELECT * FROM qm_user_models")
        count = 0
        for row in cs.fetchall():
            rec = dict(zip(src_cols, row))
            uid = user_map.get(int(rec["user_id"]))
            if uid is None:
                continue
            rec["user_id"] = uid
            sp = rec.get("storage_path")
            if sp:
                for pfx in _REDIRECT_PREFIXES:
                    if str(sp).startswith(pfx):
                        rec["storage_path"] = models_root + str(sp)[len(pfx):]
                        break
            values = [rec[c] for c in cols]
            placeholders = ", ".join(["%s"] * len(cols))
            assignments = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("model_id",))
            cd.execute(
                f"INSERT INTO qm_user_models ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (tenant_id, user_id, model_id) DO UPDATE SET {assignments}",
                values,
            )
            count += 1
        dst.commit()
        print(f"  qm_user_models upserted: {count}")
        return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="源库 DSN（已有部署）")
    ap.add_argument("--dst", required=True, help="目标库 DSN（便携包）")
    ap.add_argument("--models-root", required=True, help="目标模型根目录绝对路径（替换 /app/models）")
    args = ap.parse_args()

    src = _connect(args.src)
    dst = _connect(args.dst)
    try:
        print("用户映射:")
        umap = _username_map(src, dst, "default")
        if not umap:
            print("警告: 未匹配到任何用户，退出")
            sys.exit(1)
        print("迁移策略:")
        _migrate_strategies(src, dst, umap)
        print("迁移模型注册:")
        _migrate_user_models(src, dst, umap, args.models_root.rstrip("/"))
    finally:
        src.close()
        dst.close()
    print("迁移完成")


if __name__ == "__main__":
    main()
