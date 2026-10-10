#!/usr/bin/env python3
"""T7-2 第二段：sentinel_alerts 存量 1-2 位裸字母目标按 T4-2 拒收口径对齐。

背景（2026-10-10 实测，逐条核验）：
- T4-2 在事件层（`news_intel.normalize_targets`）拒收 1-2 位裸字母后，事件面已
  冻结：sentinel_alerts 最后一条字母目标行 10-10 13:14 < 闸门上线 13:22，之后
  0 新增（本轮复核再证）。存量 1,804 行仍在（1511 全字母 + 293 混合）。
- 混合行（真目标 + 字母噪声）：按修复后管线产物对齐——剔除字母目标（保序），
  symbol 若为字母则修为首个存活目标 → 与 normalize 后重算结果逐字段一致。
- 全字母行（1,511 行，其中 440 已推送）：修复后管线不会产生（无实体→不推）→
  删除；全行 JSON 备份留档。样例含真实美股新闻（波音 737MAX / 福特 Q3 /
  美光）与噪声（Telegraph 体育/时政误配 BA、AI+宠物经济误配 IP）——按已记录
  取舍（假阴性只丢一条新闻，假目标不留在告警面）统一出清。

**富化表层（news_article_enrichment）不清理（口径更正，见整改方案 T7-2 记录）**：
1-2 位字母是 QuantUS 真实标的（stock_daily_latest_us 实测含 A/BA/C/F/GE/GM/GS/…），
美股终端 feed 按 `tickers @> ARRAY[symbol]` 消费（stock_terminal_us/feed/news.py）
——清理会破坏真实链路。富化表前 15 字母 token 覆盖 21,283/23,653 行（GIN `&&`
实测），绝大多为真新闻匹配（高盛/美光/福特/摩根士丹利…）。执行面闸门在事件层
已生效，召回面保留；已知残余 = 假阳性链接（spam 行的 IP/PG 等），仅影响研究面
展示，已记录。

根治不靠本脚本：事件层拒收（T4-2，已上线、已复核冻结）是唯一口径出处；本脚本
只清闸门前的历史存量，可重复执行（幂等）。

用法（容器内）:
    python backend/scripts/cleanup_letter_targets.py             # dry-run 报告（只读）
    python backend/scripts/cleanup_letter_targets.py --apply     # JSON 备份后执行
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
logger = logging.getLogger("cleanup_letter_targets")

# ── 谓词常量（改这些常量 = 改删除/改写面，必须先 dry-run 再 --apply）──────────
RE_LETTER_TARGET = r"^[A-Za-z]{1,2}$"  # T4-2 事件层拒收口径（唯一出处见 news_intel）
RE_LETTER_COMPILED = re.compile(RE_LETTER_TARGET)

# 漂移护栏：候选数超过上限说明谓词扫到了不该扫的，中止人工复核
CAP_LETTER_ROWS = 2500  # 预期 1,804

BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "backups")

# 候选面：targets 含字母 或 symbol 为字母（后者是纵深防御，预期 ⊆ 前者）
_CANDIDATE_SQL = text(
    """
    SELECT id, symbol, targets, title, ts, pushed
    FROM sentinel_alerts
    WHERE EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(targets) t WHERE t ~ :letter_re
    ) OR symbol ~ :letter_re
    ORDER BY id
    """
)


def is_letter_target(token: str) -> bool:
    """T4-2 拒收口径：1-2 位纯 ASCII 字母（GS/BA/IP/IT…；AAPL/NVDA 放行）。"""
    return bool(RE_LETTER_COMPILED.match(str(token or "")))


def classify_letter_row(symbol: str, targets: list[str]) -> dict | None:
    """分类单行。None=无字母不动；{'action':'delete'}；{'action':'scrub', …}。

    scrub 语义 = 修复后管线对该行的重算产物：剔字母目标（保序去重由原数组保证，
    normalize_targets 保序），symbol 为字母时取首个存活目标。
    """
    letters_in_targets = [t for t in targets if is_letter_target(t)]
    symbol_is_letter = is_letter_target(symbol)
    if not letters_in_targets and not symbol_is_letter:
        return None
    survivors = [t for t in targets if not is_letter_target(t)]
    if not survivors:
        # 目标面全字母（含「空目标 + 字母 symbol」的退化形态）：修复后不会产生
        return {"action": "delete"}
    return {
        "action": "scrub",
        "new_symbol": survivors[0] if symbol_is_letter else symbol,
        "new_targets": survivors,
    }


def assert_capped(count: int, cap: int, label: str) -> None:
    if count > cap:
        raise RuntimeError(f"{label} 候选 {count} 超上限 {cap}——谓词疑似漂移，中止")


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


def fetch_candidates(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(_CANDIDATE_SQL, {"letter_re": RE_LETTER_TARGET})
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def plan(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """→ (delete_rows, scrub_plans)；scrub_plans 含 preimage 全文供备份与执行。"""
    delete_rows, scrub_plans = [], []
    for r in rows:
        targets = list(r["targets"] or [])
        verdict = classify_letter_row(r["symbol"], targets)
        if verdict is None:
            continue
        if verdict["action"] == "delete":
            delete_rows.append(r)
        else:
            scrub_plans.append(
                {
                    "id": r["id"],
                    "old_symbol": r["symbol"],
                    "old_targets": targets,
                    "new_symbol": verdict["new_symbol"],
                    "new_targets": verdict["new_targets"],
                    "title": r["title"],
                }
            )
    return delete_rows, scrub_plans


def _write_backup(delete_rows: list[dict], scrub_plans: list[dict]) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(BACKUP_DIR, f"cleanup_letter_targets_{ts}.json")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "delete": [
            {
                **{
                    k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in r.items()
                },
                "targets": list(r["targets"] or []),
            }
            for r in delete_rows
        ],
        "scrub": scrub_plans,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)
    return path


def _apply(engine, delete_rows: list[dict], scrub_plans: list[dict]) -> None:
    path = _write_backup(delete_rows, scrub_plans)
    logger.info(
        "已备份 %d 行（删除 %d + 改写 %d）到 %s",
        len(delete_rows) + len(scrub_plans),
        len(delete_rows),
        len(scrub_plans),
        path,
    )
    with engine.begin() as conn:
        if delete_rows:
            conn.execute(
                text("DELETE FROM sentinel_alerts WHERE id = ANY(:ids)"),
                {"ids": [r["id"] for r in delete_rows]},
            )
        for plan_row in scrub_plans:
            conn.execute(
                text(
                    "UPDATE sentinel_alerts SET targets = CAST(:t AS JSONB), symbol = :s "
                    "WHERE id = :i"
                ),
                {
                    "t": json.dumps(plan_row["new_targets"]),
                    "s": plan_row["new_symbol"],
                    "i": plan_row["id"],
                },
            )
    logger.info("删除 %d 行；改写 %d 行", len(delete_rows), len(scrub_plans))


def _verify(engine) -> int:
    with engine.connect() as conn:
        remaining = conn.execute(
            text(
                "SELECT count(*) FROM sentinel_alerts WHERE EXISTS ("
                "SELECT 1 FROM jsonb_array_elements_text(targets) t WHERE t ~ :letter_re"
                ") OR symbol ~ :letter_re"
            ),
            {"letter_re": RE_LETTER_TARGET},
        ).scalar()
    if int(remaining or 0) != 0:
        logger.error("复核失败：仍有 %s 行含字母目标/符号", remaining)
        return 2
    logger.info("复核：字母目标/符号归零")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="备份后执行（默认 dry-run）"
    )
    args = parser.parse_args()

    engine = _get_engine()
    rows = fetch_candidates(engine)
    delete_rows, scrub_plans = plan(rows)
    assert_capped(len(delete_rows), CAP_LETTER_ROWS, "sentinel_alerts(delete)")
    assert_capped(len(scrub_plans), CAP_LETTER_ROWS, "sentinel_alerts(scrub)")

    pushed_deleted = sum(1 for r in delete_rows if r["pushed"])
    logger.info(
        "候选 %d：删除（全字母目标）%d（其中已推送 %d）；改写（剔除字母保序）%d",
        len(delete_rows) + len(scrub_plans),
        len(delete_rows),
        pushed_deleted,
        len(scrub_plans),
    )
    for r in delete_rows[:5]:
        logger.info(
            "  删  #%s %s %s「%s」",
            r["id"],
            r["symbol"],
            r["targets"],
            str(r["title"])[:44],
        )
    for p in scrub_plans[:5]:
        logger.info(
            "  改  #%s %s %s → %s %s",
            p["id"],
            p["old_symbol"],
            p["old_targets"],
            p["new_symbol"],
            p["new_targets"],
        )

    if not args.apply:
        logger.info("dry-run 完成（未动数据）；确认后加 --apply")
        return 0

    _apply(engine, delete_rows, scrub_plans)
    return _verify(engine)


if __name__ == "__main__":
    sys.exit(main())
