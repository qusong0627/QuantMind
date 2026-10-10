#!/usr/bin/env python3
"""T4-1（审计 M2）存量迁移：实时推理链路标的身份「digits 折叠 → 后缀身份」。

背景：旧实现把热集符号经 ``digits()`` 折叠后落账/落库——``000001.SH``（上证指数，
``REGIME_INDEXES`` 常驻热集）与 ``000001.SZ``（平安银行）在两个面上被并成同一裸
6 位键：① Redis 账本 ``qm:realtime:infer:ledger:*`` 的 ``symbols`` 列表（回放
``group_frames`` 把指数/股票归档帧并进同一指针列表）；② PG ``engine_signal_scores``
（``source='realtime'``）行（老库还有 ``000300`` 行）。生成侧已改为 ``identity()``
后缀身份（``realtime_core``，同日修复），本脚本修存量。

两个面的**实测语义**不同（2026-10-10 全量扫描 6 个账本日 / 1,817 条 + PG 10,720 行）：

- 热集到达服务前是 ``sorted(smembers)``（``tdx_hot_set_feed``），符号按**原始后缀
  字符串排序**——同一 digits 的候选里 ``.SH``（指数）恒在 ``.SZ``（股票）**前**。
  账本 ``symbols`` 保留全部位置且顺序 = 该排序；PG 冲突键按日合并、**末写胜出** =
  当日最后周期最后插入的位置（``.SZ`` 股票一边），指数行被并失。
- 因此：**账本按位置拆**（同 digits 的 k 个位置按候选升序对齐）；**PG 归口末位**
  （= 候选里字典序最大者，即股票孪生）。
- 000001 的指纹验证：1,573 条双现条目零反例（cut=快照水印，逐条命中对应市场的
  归档帧；无一条指向反序）；3 条单现条目经归档帧指纹判定为**指数**（股票当时未进
  热集）。个股/指数并失的那份无从恢复——修复上线后新周期起自然两行，验收以新账本
  ``p6_replay_verify`` diff=0 为准。

口径：
- 一般裸 6 位 → ``identity()``（6/9→SH、0/3/2→SZ、4/8→BJ；15/16/18、5xxxxx 场内
  基金 → SZ/SH）。
- 指数驻留位（``REGIME_INDEXES`` 的 digits，现为 000001/000300）：
  - 账本 k=2 → 位置[:k] 按 ``sorted([指数] + 孪生)`` 对齐（首=指数、尾=孪生股票）；
  - 账本 k=1 → 指数（常驻位；实测 3 例均指数）；
  - PG → 候选字典序最大者（末写胜出=孪生股票；000300 无孪生 → 指数）。
- 已是后缀身份（修复后写入）→ 跳过（幂等，二次执行为空操作）。
- PG 只动 ``source='realtime'`` 行；``source='batch'`` 裸 6 位是既有契约（混形由
  ``signal_scores.SYMBOL_COUNT_KEY`` 折叠），不碰。
- PG 冲突键 (tenant,user,trade_date,symbol,model_version,feature_version,run_id)
  目标行已存在 → 跳过该行并报告（不覆盖）。

用法（容器内）:
    python backend/scripts/migrate_realtime_symbol_identity.py            # dry-run 报表
    python backend/scripts/migrate_realtime_symbol_identity.py --apply    # 执行
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict

from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

from backend.services.engine.inference.realtime_core import (  # noqa: E402
    digits,
    identity,
)
from backend.shared.hot_set import REGIME_INDEXES  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_realtime_symbol_identity")

LEDGER_KEY_PREFIX = "qm:realtime:infer:ledger"

_RE_SUFFIX = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
_RE_BARE = re.compile(r"^\d{6}$")

#: 指数驻留位 digits → 指数后缀（REGIME_INDEXES 单源派生）
INDEX_DIGIT_SUFFIX: dict[str, str] = {digits(idx): idx for idx in REGIME_INDEXES}

#: 指数驻留位的**真实孪生证券**（同 digits 且真实挂牌；人工钉死——挂牌存在性不在
#: 本脚本可判定范围）。实证（2026-10）：仅 000001.SZ 平安银行；000300.SZ 不是
#: 挂牌证券（000300 只有沪深300指数）。新增驻留指数时需同步核孪生。
INDEX_STOCK_TWINS: dict[str, tuple[str, ...]] = {"000001": ("000001.SZ",)}


def index_candidates(d: str) -> list[str]:
    """digits → 该数字下真实存在的证券后缀，升序（= 热集排序下同 digits 的相对序）。"""
    return sorted({INDEX_DIGIT_SUFFIX[d], *INDEX_STOCK_TWINS.get(d, ())})


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


def _map_plain(sym: str) -> tuple[str, str]:
    """非指数位的裸键/其它形态判定。返回 (目标, 判定 ∈ identity/unrecognized/already)。"""
    s = str(sym or "").strip()
    if _RE_SUFFIX.fullmatch(s):
        return s, "already"
    if _RE_BARE.match(s):
        new = identity(s)
        if new != s and _RE_SUFFIX.fullmatch(new):
            return new, "identity"
    return s, "unrecognized"


def map_symbol_pg(sym: str) -> tuple[str, str]:
    """PG 归口：指数位取候选字典序最大者（末写胜出），其余走 identity。"""
    s = str(sym or "").strip()
    if _RE_BARE.match(s) and s in INDEX_DIGIT_SUFFIX:
        return index_candidates(s)[-1], "pg_lastwrite"
    return _map_plain(s)


# ── PG：engine_signal_scores（source='realtime'）─────────────────────


def migrate_pg(engine, apply: bool) -> int:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT symbol, count(*) AS n,
                           min(trade_date)::text AS d0, max(trade_date)::text AS d1
                    FROM engine_signal_scores
                    WHERE source = 'realtime' AND symbol ~ '^[0-9]{6}$'
                    GROUP BY symbol ORDER BY symbol
                    """
                )
            )
            .mappings()
            .all()
        )
    total = sum(int(r["n"]) for r in rows)
    print(
        f"[PG] engine_signal_scores source='realtime' 裸 6 位行：{total} 行 / {len(rows)} 个键"
    )
    skipped_keys: list[str] = []
    for r in rows:
        old = str(r["symbol"])
        new, kind = map_symbol_pg(old)
        tag = " ←指数位:末写胜出" if kind == "pg_lastwrite" else ""
        if kind == "unrecognized" or new == old:
            tag = " ⚠无法识别(保留)"
            skipped_keys.append(old)
        print(f"[PG]   {old} → {new}  {r['n']} 行  [{r['d0']} ~ {r['d1']}]{tag}")

    if not apply:
        return 0

    # 集合式更新：每句 400 个 (old→new) 对走 `UPDATE ... FROM (VALUES ...)`——单次
    # 扫描 + hash 连接（逐键单句 UPDATE 在 1400 万行表上是逐句全扫，实测 ~1.4s/键）。
    # 目标冲突键已存在 → 不覆盖（NOT EXISTS；user_id 可空用 IS NOT DISTINCT FROM，
    # 裸 = 在 NULL 上恒 NULL）。
    pairs: list[tuple[str, str]] = []
    skipped_rows = 0
    for r in rows:
        old = str(r["symbol"])
        new, kind = map_symbol_pg(old)
        if kind == "unrecognized" or new == old:
            skipped_rows += int(r["n"])
            continue
        pairs.append((old, new))
    updated_total = 0
    for start in range(0, len(pairs), 400):
        part = pairs[start : start + 400]
        # 不要写 `:o0::text`：SQLAlchemy 的 text() 绑定参数字面量正则带 `(?!:)`，
        # `:` 紧邻会吞掉识别 → PG 端语法错；VALUES 全字符串参数由比较列推型即可
        values_sql = ", ".join(f"(:o{i}, :n{i})" for i in range(len(part)))
        params: dict[str, str] = {}
        for i, (old, new) in enumerate(part):
            params[f"o{i}"] = old
            params[f"n{i}"] = new
        with engine.begin() as conn:
            ret = conn.execute(
                text(
                    f"""
                    UPDATE engine_signal_scores t SET symbol = m.n
                    FROM (VALUES {values_sql}) AS m(o, n)
                    WHERE t.source = 'realtime' AND t.symbol = m.o
                      AND NOT EXISTS (
                        SELECT 1 FROM engine_signal_scores o
                        WHERE o.tenant_id = t.tenant_id
                          AND o.user_id IS NOT DISTINCT FROM t.user_id
                          AND o.trade_date = t.trade_date
                          AND o.model_version = t.model_version
                          AND o.feature_version = t.feature_version
                          AND o.run_id = t.run_id
                          AND o.symbol = m.n
                      )
                    """
                ),
                params,
            )
        updated_total += int(ret.rowcount or 0)
    conflict_total = total - updated_total - skipped_rows

    # 写后回读复验：残余裸 6 位必须恰等于「跳过 + 冲突」之和
    with engine.connect() as conn:
        left = conn.execute(
            text(
                "SELECT count(*) FROM engine_signal_scores "
                "WHERE source = 'realtime' AND symbol ~ '^[0-9]{6}$'"
            )
        ).scalar()
    expected = total - updated_total
    assert int(left or 0) == expected, (
        f"回读不符: 残余 {left} ≠ 预期 {expected}（更新 {updated_total}、冲突 {conflict_total}）"
    )
    print(
        f"[PG] 已更新 {updated_total} 行；冲突跳过 {conflict_total} 行；"
        f"残余裸键 {left} 行（{'跳过键: ' + ','.join(skipped_keys) if skipped_keys else '无跳过'}）"
    )
    return 0


# ── Redis：qm:realtime:infer:ledger:*（symbols 按位置重标）───────────


def relabel_entry(syms: list[str], notes: Counter) -> list[str]:
    """一条账本条目的 symbols → 重标目标（cuts 位置对齐，只改标签不改序/值）。"""
    new = [str(s) for s in syms]
    index_pos: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(new):
        if _RE_BARE.match(s) and s in INDEX_DIGIT_SUFFIX:
            index_pos[s].append(i)
    for d, positions in index_pos.items():
        cands = index_candidates(d)
        if len(positions) == 1:
            # 常驻指数单现（实测 3 例=指数；若个股独热也会落到指数=安全侧：
            # 消费方按持仓匹配指数查不到 → 回退日频行）
            new[positions[0]] = INDEX_DIGIT_SUFFIX[d]
            notes[f"index_singleton:{d}"] += 1
        elif len(positions) == len(cands):
            # 按热集排序（原始后缀字符串升序）位置对齐：首=指数、尾=孪生股票
            for pos, target in zip(positions, cands, strict=True):
                new[pos] = target
            notes[f"index_split:{d}"] += 1
        else:
            notes[f"index_unexpected:{d}:k{len(positions)}"] += 1  # 保持原样 + 报告
    for i, s in enumerate(new):
        if _RE_BARE.match(s) and s in INDEX_DIGIT_SUFFIX:
            continue  # 上面已裁定（unexpected 的保持原样）
        mapped, kind = _map_plain(s)
        notes[kind] += 1
        new[i] = mapped
    return new


def migrate_redis(client, apply: bool) -> int:
    keys = sorted(client.keys(f"{LEDGER_KEY_PREFIX}:*"))
    print(f"[Redis] 账本键 {len(keys)} 个")
    grand_changed = 0
    for key in keys:
        entries = client.lrange(key, 0, -1)
        changed: list[tuple[int, str]] = []
        notes: Counter = Counter()
        for i, raw in enumerate(entries):
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                notes["bad_json"] += 1
                continue
            syms = [str(s) for s in (entry.get("symbols") or [])]
            new_syms = relabel_entry(syms, notes)
            if new_syms != syms:
                # cuts 与 symbols 位置对齐；只重标标签，不改任何值/序
                entry["symbols"] = new_syms
                changed.append(
                    (
                        i,
                        json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
                    )
                )
        detail = " ".join(
            f"{k}={v}" for k, v in sorted(notes.items()) if k != "already"
        )
        print(
            f"[Redis]   {key}: {len(entries)} 条, 需重标 {len(changed)} 条 ({detail or '无裸键'})"
        )
        if apply:
            for i, payload in changed:
                client.lset(key, i, payload)
            # 写后回读复验：该键不应再有可映射的裸 6 位
            for i, _ in changed:
                again = json.loads(client.lindex(key, i) or "{}")
                bad = [
                    str(s)
                    for s in (again.get("symbols") or [])
                    if _RE_BARE.match(str(s))
                    and (
                        str(s) in INDEX_DIGIT_SUFFIX
                        or _map_plain(str(s))[1] != "unrecognized"
                    )
                ]
                assert not bad, f"回读失败 {key}[{i}] 仍有可映射裸键: {bad[:5]}"
        grand_changed += len(changed)
    print(
        f"[Redis] 共需重标 {grand_changed} 条"
        + ("（已写入）" if apply else "（dry-run）")
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行写入（默认 dry-run）")
    args = parser.parse_args()

    for d in INDEX_DIGIT_SUFFIX:
        print(f"指数驻留位 {d} 候选（升序）: {index_candidates(d)}")
    engine = _get_engine()
    migrate_pg(engine, args.apply)
    client = _get_redis()
    try:
        migrate_redis(client, args.apply)
    finally:
        client.close()
    if not args.apply:
        print("dry-run 完成；加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
