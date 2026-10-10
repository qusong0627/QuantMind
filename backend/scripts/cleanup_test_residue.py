#!/usr/bin/env python3
"""T7-2（审计 H5/M15）测试残留清理：把集成测试借真通道写进生产面的行/消费组清出场。

背景（2026-10-10 全天链审计，逐条核验过——不是「疑似」是「指认」）：
- notifications 362 条假告警：夹具词汇（T* 虚构代码 / 9000* 虚构账户 /
  itest-*、mdl_it_train_* 夹具模型 / 决策轮「fake-model」「pro」锚）276 条，
  加与锚同秒成簇的决策轮兄弟 54 条、其它同簇兄弟 32 条。生产真行 4 条
  （6706/6774/6775 决策轮 + 6332 整天没跑）**硬保护**——6332 生于夹具爆发同秒
  但内容属实（10-08 轮次确实没跑，开闸是 10-09），保留。
- sentinel_alerts 376 条假行：anomaly_engine 夹具 363 条（T 代码 135、
  9000 账户 101、老年代 itest 模型 65、mdl_it_train 新年代 32、老年代 data_jump
  的合成指纹 close=20.0/prev_close=11.0 落网的 30）+ news_intel 的 [itest-*]
  行 13 条。真身保留：price_surge 3,844 + 真实模型 IC（cust/cn）22。
- 3 张表的 t-* 租户残余（值写死在 TENANT_RESIDUE，发现新值须人工核验再扩）：
  simulation_fund_snapshots 40 / qm_user_models 8 / user_audit_logs 5。
- Redis intel:events 上 145 个测试消费组（sentinel-itest-* 70 + regime-test-* 75，
  ~31k pending）+ intel:latency:test-* 86 键（自过期 TTL，顺手清）。

根治不靠本脚本：出口闸（T7-1，`notification_publisher` 拒 t-*/_t_* 租户）
+ 各测试 finally 自清理（消费组 xgroup_destroy）已落地；本脚本是一次性运维工具，
只清闸落地前的历史存量，可重复执行（幂等）。

用法（容器内）:
    python backend/scripts/cleanup_test_residue.py             # dry-run 报告（只读）
    python backend/scripts/cleanup_test_residue.py --apply     # JSON 备份后清理
    python backend/scripts/cleanup_test_residue.py --apply --skip-redis
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("cleanup_test_residue")

# ── 谓词常量（T7-2；改这些常量 = 改删除面，必须先 dry-run 再 --apply）─────────
RE_SYMBOL_VOCAB = r"T[0-9A-F]{5}"  # 夹具虚构代码 T + 5 位 hex
RE_UID_VOCAB = r"9000[0-9a-f]{4}"  # 夹具虚构账户 9000 + 4 位 hex
LIKE_ITEST = "%itest-%"
LIKE_MODEL_IT = "%mdl_it_train%"
LIKE_ROUND_FAKE = "%「fake-model」%"
LIKE_ROUND_PRO = "%「pro」%"
ROUND_PREFIX = "决策轮%"  # 参数化传入，避免 SQL 里裸 %
ROUND_STALL_PREFIX = "决策轮整天没跑%"  # 6332 真行——删除面显式排除
DATA_JUMP_FIXTURE_METRICS = {"close": 20.0, "prev_close": 11.0}  # 夹具 data_jump 指纹

# 生产真行硬保护：出现在删除候选里 = 谓词漂移，立刻中止（宁可不动，不可误删）
PROTECTED_NOTIFICATION_IDS = (6332, 6706, 6774, 6775)
PROTECTED_NOTES = {
    6332: "决策轮整天没跑：2026-10-08——内容属实（开闸 10-09），唯一记录",
    6706: "决策轮 11:05「glm-5.3-flash」模型未出决策——真实模型",
    6774: "决策轮 14:00「deepseek-v4-flash」有 3 条腿下单失败——真实模型",
    6775: "决策轮 14:00「glm-5.3-flash」有 3 条腿下单失败——真实模型",
}

# 漂移护栏：候选数超过上限说明谓词扫到了不该扫的，中止人工复核
CAP_NOTIFICATIONS = 500  # 预期 362
CAP_SENTINEL = 500  # 预期 376
CAP_TENANT_PER_TABLE = 100  # 预期 40/8/5

# t-* 租户残余（全库 tenant_id 扫描 2026-10-10 实测只有这三处）
TENANT_RESIDUE = (
    ("simulation_fund_snapshots", "t-pending-life-fill-e2cb5a"),
    ("qm_user_models", "t-rename-display"),
    ("user_audit_logs", "t-default"),
)

INTEL_STREAM = "intel:events"
RE_TEST_GROUP = re.compile(r"^(sentinel-itest-|regime-test-)")
LATENCY_TEST_GLOB = "intel:latency:test-*"

BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "backups")


# ── 连接（与 migrate_realtime_symbol_identity 同范式）───────────────────────
def _get_engine():
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        db_url = (
            f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', '')}"
            f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}"
            f"/{os.getenv('DB_NAME', 'quantmind')}"
        )
    # 容器 DATABASE_URL 是 postgresql+asyncpg://（服务运行用），本脚本同步执行
    return create_engine(db_url.replace("+asyncpg", ""))


def _get_redis():
    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )


# ── 守护 ─────────────────────────────────────────────────────────────────
def assert_protected_absent(candidate_ids) -> None:
    hit = sorted(set(candidate_ids) & set(PROTECTED_NOTIFICATION_IDS))
    if hit:
        detail = "; ".join(f"{i}={PROTECTED_NOTES.get(i, '')}" for i in hit)
        raise RuntimeError(f"删除候选含受保护真行，谓词漂移，中止：{detail}")


def assert_capped(count: int, cap: int, label: str) -> None:
    if count > cap:
        raise RuntimeError(f"[{label}] 候选 {count} 超上限 {cap}，谓词疑似漂移，中止")


def is_test_group(name: str) -> bool:
    return bool(RE_TEST_GROUP.match(str(name or "")))


# ── PG 候选（report 与 apply 共用，保证「报告的即删除的」）──────────────────
_NOTIFICATION_SQL = text(
    """
    WITH vocab AS (
        SELECT id, title, created_at FROM notifications
        WHERE title ~ :re_symbol OR title ~ :re_uid
           OR title LIKE :like_itest OR title LIKE :like_model
           OR (title LIKE :round_prefix
               AND (title LIKE :like_fake OR title LIKE :like_pro))
    )
    SELECT n.id, n.title, n.created_at,
           CASE
             WHEN n.title ~ :re_symbol THEN 'vocab:T代码'
             WHEN n.title ~ :re_uid THEN 'vocab:9000账户'
             WHEN n.title LIKE :like_itest THEN 'vocab:itest'
             WHEN n.title LIKE :like_model THEN 'vocab:夹具模型'
             WHEN n.title LIKE :like_fake OR n.title LIKE :like_pro THEN 'vocab:决策轮锚'
             WHEN n.title LIKE :round_prefix THEN '同簇兄弟≤2s'
             ELSE '同簇兄弟≤3s'
           END AS arm,
           (SELECT v.title FROM vocab v
             WHERE v.id <> n.id
               AND abs(EXTRACT(EPOCH FROM (v.created_at - n.created_at)))
                   <= CASE WHEN n.title LIKE :round_prefix THEN 2 ELSE 3 END
             ORDER BY abs(EXTRACT(EPOCH FROM (v.created_at - n.created_at)))
             LIMIT 1) AS anchor_title
    FROM notifications n
    WHERE n.title ~ :re_symbol OR n.title ~ :re_uid
       OR n.title LIKE :like_itest OR n.title LIKE :like_model
       OR (n.title LIKE :round_prefix
           AND (n.title LIKE :like_fake OR n.title LIKE :like_pro))
       OR (n.title LIKE :round_prefix AND n.title NOT LIKE :stall_prefix
           AND EXISTS (SELECT 1 FROM vocab v WHERE v.id <> n.id
                 AND abs(EXTRACT(EPOCH FROM (v.created_at - n.created_at))) <= 2))
       OR (n.title NOT LIKE :round_prefix
           AND EXISTS (SELECT 1 FROM vocab v WHERE v.id <> n.id
                 AND abs(EXTRACT(EPOCH FROM (v.created_at - n.created_at))) <= 3))
    ORDER BY n.id
    """
)

_SENTINEL_SQL = text(
    """
    SELECT s.alert_id::text AS aid, s.ts, s.source, s.symbol,
           left(s.title, 90) AS title,
           CASE
             WHEN s.symbol ~ :re_symbol THEN 'sym:T代码'
             WHEN s.symbol ~ :re_uid THEN 'sym:9000账户'
             WHEN s.symbol LIKE :like_itest THEN 'sym:itest模型'
             WHEN s.detail->'payload'->>'subject' ~ :re_symbol THEN 'subj:T代码'
             WHEN s.detail->'payload'->>'subject' ~ :re_uid THEN 'subj:9000账户'
             WHEN s.detail->'payload'->>'subject' LIKE :like_model THEN 'subj:夹具模型'
             WHEN s.detail->'payload'->>'kind' = 'data_jump' THEN 'datajump:合成指纹'
             ELSE 'title:itest新闻'
           END AS arm
    FROM sentinel_alerts s
    WHERE (s.source = 'anomaly_engine'
           AND (s.symbol ~ :re_symbol OR s.symbol ~ :re_uid
                OR s.symbol LIKE :like_itest
                OR s.detail->'payload'->>'subject' ~ :re_symbol
                OR s.detail->'payload'->>'subject' ~ :re_uid
                OR s.detail->'payload'->>'subject' LIKE :like_model
                OR (s.detail->'payload'->>'kind' = 'data_jump'
                    AND s.detail->'payload'->'metrics' @> CAST(:dj_fp AS jsonb))))
       OR s.detail->'payload'->>'subject' LIKE :like_itest
       OR s.title LIKE :like_itest
    ORDER BY s.id
    """
)


def _params() -> dict:
    return {
        "re_symbol": RE_SYMBOL_VOCAB,
        "re_uid": RE_UID_VOCAB,
        "like_itest": LIKE_ITEST,
        "like_model": LIKE_MODEL_IT,
        "like_fake": LIKE_ROUND_FAKE,
        "like_pro": LIKE_ROUND_PRO,
        "round_prefix": ROUND_PREFIX,
        "stall_prefix": ROUND_STALL_PREFIX,
        "dj_fp": json.dumps(DATA_JUMP_FIXTURE_METRICS),
    }


def fetch_notification_candidates(conn) -> list[dict]:
    rows = conn.execute(_NOTIFICATION_SQL, _params()).mappings().fetchall()
    return [dict(r) for r in rows]


def fetch_sentinel_candidates(conn) -> list[dict]:
    rows = conn.execute(_SENTINEL_SQL, _params()).mappings().fetchall()
    return [dict(r) for r in rows]


def fetch_tenant_rows(conn, table: str, tenant_value: str) -> list[dict]:
    rows = conn.execute(
        text(f"SELECT * FROM {table} WHERE tenant_id = :v"),  # noqa: S608 - 表名来自白名单常量
        {"v": tenant_value},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


# ── Redis ────────────────────────────────────────────────────────────────
def fetch_test_groups(client) -> list[dict]:
    groups = []
    for g in client.xinfo_groups(INTEL_STREAM):
        name = g.get("name")
        if is_test_group(name):
            groups.append(
                {
                    "name": name,
                    "consumers": g.get("consumers", 0),
                    "pending": g.get("pending", 0),
                }
            )
    return sorted(groups, key=lambda x: x["name"])


def fetch_latency_test_keys(client) -> list[str]:
    return sorted(client.scan_iter(match=LATENCY_TEST_GLOB, count=500))


# ── 备份 ─────────────────────────────────────────────────────────────────
def write_backup(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)


# ── 主流程 ───────────────────────────────────────────────────────────────
def _arm_counts(rows: list[dict]) -> dict:
    counts: dict = {}
    for r in rows:
        counts[r["arm"]] = counts.get(r["arm"], 0) + 1
    return counts


def _print_samples(rows: list[dict], title: str, limit: int = 8) -> None:
    logger.info("── %s（共 %d）──", title, len(rows))
    for r in rows[:limit]:
        key = r.get("id") or r.get("aid")
        extra = r.get("symbol") or r.get("anchor_title") or ""
        logger.info("  [%s] %s | %s | %s", key, r.get("title"), r["arm"], str(extra)[:60])
    if len(rows) > limit:
        logger.info("  … 其余 %d 条见 JSON 备份/报告", len(rows) - limit)


def main() -> int:
    parser = argparse.ArgumentParser(description="测试残留清理（T7-2/H5/M15）")
    parser.add_argument("--apply", action="store_true", help="执行删除（默认只读报告）")
    parser.add_argument("--skip-redis", action="store_true", help="跳过 Redis 消费组/键清理")
    args = parser.parse_args()

    engine = _get_engine()
    with engine.connect() as conn:
        notif = fetch_notification_candidates(conn)
        sent = fetch_sentinel_candidates(conn)
        tenant_rows = {
            (t, v): fetch_tenant_rows(conn, t, v) for t, v in TENANT_RESIDUE
        }

    # 守护先行：受保护真行不许进候选；候选数不许破上限
    assert_protected_absent([r["id"] for r in notif])
    assert_capped(len(notif), CAP_NOTIFICATIONS, "notifications")
    assert_capped(len(sent), CAP_SENTINEL, "sentinel_alerts")
    for (t, v), rows in tenant_rows.items():
        assert_capped(len(rows), CAP_TENANT_PER_TABLE, f"{t}:{v}")

    groups: list[dict] = []
    latency_keys: list[str] = []
    client = None
    if not args.skip_redis:
        client = _get_redis()
        groups = fetch_test_groups(client)
        latency_keys = fetch_latency_test_keys(client)

    mode = "APPLY" if args.apply else "REPORT(只读)"
    logger.info("模式=%s 库=%s", mode, engine.url.render_as_string(hide_password=True))
    _print_samples(notif, "notifications 夹具候选", 10)
    logger.info("  分类：%s", _arm_counts(notif))
    _print_samples(sent, "sentinel_alerts 夹具候选", 10)
    logger.info("  分类：%s", _arm_counts(sent))
    for (t, v), rows in tenant_rows.items():
        logger.info("租户残余 %s tenant_id=%s：%d 行", t, v, len(rows))
    logger.info(
        "Redis：测试消费组 %d 个（pending 合计 %d）；latency 测试键 %d",
        len(groups),
        sum(g["pending"] for g in groups),
        len(latency_keys),
    )
    logger.info(
        "受保护真行应保留：%s（进入候选会被守护拦下）",
        list(PROTECTED_NOTIFICATION_IDS),
    )

    if not args.apply:
        logger.info("dry-run 结束（未写任何面）。确认无误后加 --apply 执行。")
        return 0

    # ── 备份（先备份，备份失败绝不删）────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join(BACKUP_DIR, f"cleanup_test_residue_{ts}.json")
    write_backup(
        backup_path,
        {
            "generated_at": ts,
            "ticket": "T7-2（审计 H5/M15）",
            "predicates": {
                "notifications": "vocab(T/9000/itest/mdl_it_train/fake-model/pro) + 同簇≤2s/≤3s 兄弟",
                "sentinel_alerts": "anomaly_engine 夹具四族 + data_jump 指纹 + itest 标题/主体",
                "protected_notification_ids": list(PROTECTED_NOTIFICATION_IDS),
            },
            "notifications": notif,
            "sentinel_alerts": sent,
            "tenant_rows": {f"{t}|{v}": rows for (t, v), rows in tenant_rows.items()},
            "redis_groups": groups,
            "redis_latency_keys": latency_keys,
        },
    )
    logger.info("已备份 %d 行到 %s", len(notif) + len(sent), backup_path)

    # ── 删除 ─────────────────────────────────────────────────────────────
    with engine.begin() as conn:
        if notif:
            n = conn.execute(
                text("DELETE FROM notifications WHERE id = ANY(:ids)"),
                {"ids": [r["id"] for r in notif]},
            ).rowcount
            logger.info("notifications 删除 %d 行（候选 %d）", n, len(notif))
        if sent:
            n = conn.execute(
                text("DELETE FROM sentinel_alerts WHERE alert_id::text = ANY(:ids)"),
                {"ids": [r["aid"] for r in sent]},
            ).rowcount
            logger.info("sentinel_alerts 删除 %d 行（候选 %d）", n, len(sent))
        for (t, v), rows in tenant_rows.items():
            if rows:
                n = conn.execute(
                    text(f"DELETE FROM {t} WHERE tenant_id = :v"),  # noqa: S608
                    {"v": v},
                ).rowcount
                logger.info("%s 删除 %d 行（tenant_id=%s）", t, n, v)

    if client is not None:
        for g in groups:
            client.xgroup_destroy(INTEL_STREAM, g["name"])
        logger.info("销毁测试消费组 %d 个", len(groups))
        if latency_keys:
            for i in range(0, len(latency_keys), 500):
                client.delete(*latency_keys[i : i + 500])
            logger.info("删除 latency 测试键 %d 个", len(latency_keys))
        client.close()

    # ── 复核：候选必须归零 ───────────────────────────────────────────────
    with engine.connect() as conn:
        left_notif = len(fetch_notification_candidates(conn))
        left_sent = len(fetch_sentinel_candidates(conn))
        left_tenant = {
            f"{t}|{v}": len(fetch_tenant_rows(conn, t, v)) for t, v in TENANT_RESIDUE
        }
    left_groups = 0
    if not args.skip_redis:
        check = _get_redis()
        left_groups = len(fetch_test_groups(check))
        left_latency = len(fetch_latency_test_keys(check))
        check.close()
    else:
        left_latency = 0
    logger.info(
        "复核：notifications=%d sentinel_alerts=%d tenant=%s groups=%d latency=%d",
        left_notif, left_sent, left_tenant, left_groups, left_latency,
    )
    if left_notif or left_sent or any(left_tenant.values()) or left_groups or left_latency:
        logger.error("复核未归零——可能有并发写入或部分失败，请看备份与库面")
        return 2
    logger.info("清理完成，全部候选归零。留档：%s", backup_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
