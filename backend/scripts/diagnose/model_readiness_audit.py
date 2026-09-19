"""模型就绪体检：把注册表里所有模型的推理门禁逐条跑一遍并归类。

动机：注册表 `status=ready` 与「真的能推理」是两件事。2026-09-20 推理中心审计
发现两类「注册为 ready、实际永远跑不起来」的模型：

1. **目录缺失**：注册行指向已被清理的 `storage_path`。
2. **schema hash 漂移**：模型记录的整体因子库哈希与当前库不一致，被 precheck 硬闸门拒绝。

第 2 类的性质要单独判：hash 不一致时继续检查「该模型实际要用的列是否还在库里」。
列都在 ⇒ 那是一台被误锁的机器，问题在尺子（整库哈希相等）而不在数据。

**门禁口径不复制**：就绪检查直接调 `InferenceScriptRunner.query_readiness`，
与推理前检同一份分派表。复制一份判断逻辑的体检脚本，迟早会与真门禁分叉。

日期口径跑两遍，把两类原因分开：
- `结构`（trade_date=""）：不看数据时效，只问「这台机器本身还转得动吗」
- `含时效`（trade_date=今天）：叠加日期范围检查，即用户点下去的那一刻真实结果

用法（容器内，backend 已挂载）：
    docker exec -w /app quantmind python -m backend.scripts.diagnose.model_readiness_audit
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.shared.database_manager_v2 import get_session

_MODELS_USERS_ROOT_FALLBACK = Path("/app/models/users")


async def _load_models() -> list[dict]:
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT model_id, status, storage_path, metadata_json
                    FROM qm_user_models
                    WHERE status <> 'archived'
                    """
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _classify(model_dir: Path, trade_date: str) -> tuple[str, dict]:
    """返回 (分类, 就绪结果)。分类是给汇总看的，结果是给取证看的。

    `primary_data_dir` 传模型目录而非 precheck 的 `_get_model_data_dir()` 结果：
    该字段只用于 qlib provider URI，就绪检查一律自己从 metadata 解析数据根
    （`_resolve_market_factor_data_dir` / `resolve_feature_snapshot_dir`），
    所以这里不影响分类结论，也免去从 api 层反向 import。
    """
    runner = InferenceScriptRunner(
        primary_model_dir=str(model_dir),
        primary_data_dir=str(model_dir),
        primary_model_id=model_dir.name,
    )
    _label, readiness = runner.query_readiness(trade_date=trade_date)
    if readiness.get("ready"):
        return "ok", readiness

    detail = str(readiness.get("detail") or "未知原因")
    # 「最新可用 X 早于请求」这类失败必须带上是**哪个库**：不同库的滞后天数不同，
    # 只印源名（l1_factors）会把三个不同的库混成一堆，看不出该催哪个同步。
    if readiness.get("latest_available_date"):
        meta = runner._read_primary_metadata()
        try:
            from backend.services.engine.inference.script_runner import (
                _resolve_market_factor_data_dir,
            )

            lib = Path(_resolve_market_factor_data_dir(meta)).name
        except Exception:  # noqa: BLE001 - 定位失败不影响分类
            lib = "?"
        detail = f"{detail} [{lib}]"
    return detail, readiness


async def _resolve_trade_dates(rows: list[dict], today: str) -> dict[str, str]:
    """每个模型按其市场日历把「今天」回退到最近交易日，与 precheck 同口径。

    不这么做的话，周末/节假日跑体检会把**全部**模型报成「请求日期超出数据范围」——
    那不是门禁结论，那是没做日历回退的假象。
    """
    from backend.services.api.routers.model_training import (
        _get_model_calendar,
        _resolve_trade_date_for_owner,
    )

    requested = date.fromisoformat(today)
    cache: dict[str, str] = {}
    resolved: dict[str, str] = {}
    for row in rows:
        sp = str(row.get("storage_path") or "")
        model_dir = Path(sp)
        if not sp or not model_dir.is_dir():
            continue
        calendar = _get_model_calendar(model_dir)
        if calendar not in cache:
            td, _fell_back = await _resolve_trade_date_for_owner(
                tenant_id="default", user_id="00000001", requested_date=requested, market=calendar
            )
            cache[calendar] = td.isoformat()
        resolved[str(row["model_id"])] = cache[calendar]
    print(f"日历回退：今天 {today} → " + ", ".join(f"{k}={v}" for k, v in sorted(cache.items())))
    return resolved


def _scan(rows: list[dict], mode: str, today: str, effective: dict[str, str]) -> None:
    buckets: Counter[str] = Counter()
    for row in rows:
        sp = str(row.get("storage_path") or "")
        model_dir = Path(sp)
        if not sp or not model_dir.is_dir():
            buckets["模型目录不存在"] += 1
            continue
        try:
            # 含时效档用该模型自己日历回退出的交易日，与 precheck 完全同口径
            trade_date = today if mode == "含时效" else ""
            if mode == "含时效":
                trade_date = effective.get(str(row["model_id"]), "")
            detail, _readiness = _classify(model_dir, trade_date)
        except Exception as exc:  # noqa: BLE001 - 体检脚本把异常算成一类，不中断
            buckets[f"异常 {type(exc).__name__}"] += 1
            continue
        buckets["就绪" if detail == "ok" else detail] += 1

    print(f"\n── 分类 [{mode}] ─────────────────────────────────")
    for key, n in buckets.most_common():
        print(f"  {n:4d}  {key[:100]}")


def main() -> int:
    async def _run() -> tuple[list[dict], dict[str, str]]:
        # 单事件循环：分两次 asyncio.run 会让连接池里上一个循环的 Future 跨循环复用
        # （表现为 ``coroutine 'Connection._cancel' was never awaited`` 警告）
        rows = await _load_models()
        return rows, await _resolve_trade_dates(rows, today)

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    rows, effective = asyncio.run(_run())
    print(f"注册表在册模型（非 archived）: {len(rows)}    今天 {today}")

    for mode in ("结构", "含时效"):
        _scan(rows, mode, today, effective)

    print(
        "\n两个口径要看的是不同的病：\n"
        "  [结构]   不看数据时效 —— 剩下的失败都是资产/口径问题（目录被清、列被删改名）\n"
        "  [含时效] 叠加日期范围 —— 与用户点击的那一刻完全一致；此处多出来的失败\n"
        "           说明「模型没问题，但它依赖的因子库没跟上」，属于同步链路问题。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
