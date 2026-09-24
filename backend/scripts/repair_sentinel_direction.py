#!/usr/bin/env python3
"""sentinel_alerts.direction 存量修复：把写错的告警方向按当前口径重算回填。

**为什么需要**：``alert_direction`` 曾把 ``anomaly:price_surge`` 一律判 ``up``——
而该 kind 是**双向**的（涨跌幅正负分开发），且默认只推 ``warn``（下行），
于是**每一条被推出去的大幅下行告警都记成了看涨**（实测 2026-09-24：600503.SH
−9.86% 存成 up）。已修的是写入路径（``sentinel_alert_contract.alert_direction``），
但**存量行不会自己变**——而 T+1 回填（每日 01:35）正是拿这一列算 ``hit`` 的，
不改就会把「跌前提醒对了」记成误报，整类统计反向。

用法（容器内）::

    python backend/scripts/repair_sentinel_direction.py            # DRY-RUN（默认）
    python backend/scripts/repair_sentinel_direction.py --apply    # 实际写库

口径（机构级，保守）
--------------------
* **检测用的是写入侧同一个函数**（``alert_direction(alert_type, detail->payload)``），
  不是另写一套规则——两套规则必然漂移，而漂移的检测等于没检测；
* 扫**全表**（不只 price_surge）：任何类型只要存量值与当前口径不符都会被列出并按类型
  汇总——这样下一次口径再改，这个脚本仍然是对的；
* **已判过 hit 的行不自动改**：``hit`` 是拿旧 direction 算出来的，单改方向会让两者
  自相矛盾。这类行**只列出、要求人工复核**（脚本不替人做资金/统计判断）；
* 幂等：改完再跑应为 0 行；
* ``--apply`` 后**回读复验**（重跑同一检测），仍有残留即非零退出。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SCAN_SQL = """
SELECT id, alert_type, symbol, trade_date, severity, direction,
       hit IS NOT NULL AS scored,
       detail -> 'payload' AS payload
FROM sentinel_alerts
ORDER BY id
"""


def _recompute(alert_type: str, payload_raw: object) -> str:
    """当前口径下的方向（与写入路径同一实现）。"""
    from backend.shared.sentinel_alert_contract import alert_direction

    if isinstance(payload_raw, str):
        try:
            payload_raw = json.loads(payload_raw)
        except ValueError:
            payload_raw = None
    payload = payload_raw if isinstance(payload_raw, dict) else {}
    return alert_direction(str(alert_type or ""), payload)


async def _scan(session) -> tuple[list[dict], list[dict]]:
    """→ (可自动修复的行, 需人工复核的行)。"""
    from sqlalchemy import text as sa_text

    rows = (await session.execute(sa_text(_SCAN_SQL))).fetchall()
    fixable: list[dict] = []
    manual: list[dict] = []
    for row_id, alert_type, symbol, trade_date, severity, stored, scored, payload in rows:
        want = _recompute(alert_type, payload)
        if want == stored:
            continue
        item = {
            "id": int(row_id),
            "alert_type": str(alert_type),
            "symbol": str(symbol),
            "trade_date": trade_date.isoformat() if trade_date else "",
            "severity": str(severity),
            "stored": str(stored),
            "want": want,
        }
        (manual if scored else fixable).append(item)
    return fixable, manual


def _report(fixable: list[dict], manual: list[dict]) -> None:
    by_type = Counter((r["alert_type"], r["stored"], r["want"]) for r in fixable)
    print(f"[repair] 方向不符当前口径：可自动修 {len(fixable)} 行，需人工 {len(manual)} 行")
    for (atype, stored, want), n in by_type.most_common():
        print(f"  可修  {atype}: {stored} -> {want}  ×{n}")
    for r in manual[:10]:
        print(f"  [需人工] #{r['id']} {r['trade_date']} {r['symbol']} {r['alert_type']}: "
              f"{r['stored']} -> {r['want']}（已判过 hit，方向与命中会自相矛盾）")
    if len(manual) > 10:
        print(f"  [需人工] …另有 {len(manual) - 10} 行")


async def run(*, apply: bool) -> int:
    from backend.shared.database_manager_v2 import close_database, get_session

    async with get_session(read_only=True) as session:
        fixable, manual = await _scan(session)
    _report(fixable, manual)

    if not apply:
        print("[repair] DRY-RUN（未写库）。确认清单后加 --apply 执行。")
        await close_database()
        return 0

    from sqlalchemy import text as sa_text

    async with get_session(read_only=False) as session:
        for item in fixable:
            await session.execute(
                sa_text(
                    "UPDATE sentinel_alerts SET direction = :d WHERE id = :i AND direction = :old"
                ),
                {"d": item["want"], "i": item["id"], "old": item["stored"]},
            )
        await session.commit()
    print(f"[repair] 已回填 {len(fixable)} 行方向")

    # 回读复验：同一检测再跑一遍，残留即失败（不拿「写了几行」当验收）。
    async with get_session(read_only=True) as session:
        remaining, remaining_manual = await _scan(session)
    if remaining:
        print(f"[repair] 复验失败：仍有 {len(remaining)} 行不符", file=sys.stderr)
        await close_database()
        return 1
    print(f"[repair] 复验通过：0 行不符（另 {len(remaining_manual)} 行需人工，与修前一致）")
    await close_database()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="sentinel_alerts.direction 存量修复")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 DRY-RUN）")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
