#!/usr/bin/env python3
"""把已注册模型迁移到另一个市场（改 metadata.market + 移动存储目录 + 更新注册表）。

背景：训练注册的市场跟随训练时的 ``context.market`` —— 用自定义市场
（CUSTOM / ``/data/quantcustom``）数据集训练的模型会注册进「自定义市场」，
不会出现在 A 股（CN）模型管理里。本脚本做一次可复用的市场迁移：

    1) 存储目录按目标市场布局搬迁：CN → ``<root>/<tenant>/<user>/<model_id>``；
       非 CN → ``<root>/<tenant>/<user>/<market_lower>/<model_id>``；
    2) 模型目录内 ``metadata.json``：``market`` / ``context.market`` 改写，
       并留 ``market_migration`` 溯源字段（from/to/at）；
    3) 注册表 ``qm_user_models``：``metadata_json``、``storage_path`` 更新，
       ``display_name`` 的市场后缀（``_CUSTOM`` → ``_CN``）同步改写。

注意：模型直读的因子数据根由 ``metadata.quantdb_dir``（训练时 pin）决定，
迁移只改市场归属、不改数据口径 —— 例如 CUSTOM 合并数据集（273 因子）训练
的模型迁到 CN 后，推理仍从 ``/data/quantcustom`` 取数。``model_id`` 前缀保持
不变（它是对外主键，市场识别一律以 metadata.market 为准）。

用法（容器内执行）：
    docker exec -w /app quantmind python backend/scripts/migrate_model_market.py \
        --model-id mdl_cust_train_20260914130341_887a7a0d_c2e90650 --to-market CN
    # 预演（只打印计划，不动文件不写库）：
    ... --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate_model_market")

if Path("/app/models/users").is_dir():
    MODELS_USERS_ROOT = Path("/app/models/users")
else:
    MODELS_USERS_ROOT = PROJECT_ROOT / "models" / "users"

_MARKET_ALIASES = {"A": "CN", "A_SHARE": "CN", "SSE": "CN"}


def _canonical_market(value: str | None) -> str:
    raw = str(value or "").upper().strip()
    return _MARKET_ALIASES.get(raw, raw or "CN")


def _target_dir(root: Path, tenant: str, user: str, model_id: str, market: str) -> Path:
    base = root / tenant / user
    if market == "CN":
        return base / model_id
    return base / market.lower() / model_id


def _rewrite_display_name(name: str, old_market: str, new_market: str) -> str:
    """``xxx_CUSTOM`` → ``xxx_CN``；无旧市场后缀则原样返回。"""
    suffix = f"_{old_market}"
    if name.upper().endswith(suffix.upper()):
        return name[: -len(suffix)] + f"_{new_market}"
    return name


async def migrate(
    *,
    model_id: str,
    to_market: str,
    tenant_id: str,
    user_id: str,
    root: Path,
    dry_run: bool = False,
) -> dict:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    to_market = _canonical_market(to_market)
    async with get_session() as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT model_id, storage_path, metadata_json
                        FROM qm_user_models
                        WHERE tenant_id = :t AND user_id = :u AND model_id = :m
                        LIMIT 1
                        """
                    ),
                    {"t": tenant_id, "u": user_id, "m": model_id},
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise SystemExit(f"未找到模型 {model_id}（tenant={tenant_id} user={user_id}）")

    metadata = row["metadata_json"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    context = (
        metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
    )
    old_market = _canonical_market(
        metadata.get("market") or context.get("market") or "CN"
    )

    target = _target_dir(root, tenant_id, user_id, model_id, to_market)
    current = Path(str(row["storage_path"] or ""))

    plan = {
        "model_id": model_id,
        "old_market": old_market,
        "new_market": to_market,
        "current_dir": str(current),
        "target_dir": str(target),
        "move_required": current.resolve() != target.resolve()
        if str(current)
        else True,
        "metadata_rewrite_required": old_market != to_market,
    }
    log.info("迁移计划: %s", json.dumps(plan, ensure_ascii=False, indent=2))

    if (
        old_market == to_market
        and str(current)
        and current.resolve() == target.resolve()
    ):
        log.info("模型已在目标市场与目标目录，无需迁移")
        return {**plan, "changed": False}

    if dry_run:
        log.info("[DRY-RUN] 不移动文件、不写库")
        return {**plan, "changed": False, "dry_run": True}

    # 1) 搬迁目录（DB 路径失效时回退尝试目标目录，保证重复执行可自愈）
    src = current if str(current) and current.is_dir() else target
    if not src.is_dir():
        raise SystemExit(f"模型目录不存在: DB={current} / target={target}")
    if src.resolve() != target.resolve():
        if target.exists():
            raise SystemExit(f"目标目录已存在，拒绝覆盖: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        log.info("目录已搬迁: %s → %s", src, target)
        # 迁移前的父级市场目录若已空则清掉（custom/ 等）
        try:
            src.parent.rmdir()
        except OSError:
            pass
    else:
        log.info("目录已在目标位置: %s", target)

    # 2) 改写模型目录内 metadata.json（推理链路按它解析市场/日历/数据根）
    meta_file = target / "metadata.json"
    if not meta_file.is_file():
        raise SystemExit(f"模型目录缺少 metadata.json: {target}")
    disk_meta = json.loads(meta_file.read_text(encoding="utf-8"))
    disk_meta["market"] = to_market
    ctx = disk_meta.get("context") if isinstance(disk_meta.get("context"), dict) else {}
    ctx["market"] = to_market
    disk_meta["context"] = ctx
    disk_meta["market_migration"] = {
        "from": old_market,
        "to": to_market,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    display_name = _rewrite_display_name(
        str(metadata.get("display_name") or model_id), old_market, to_market
    )
    disk_meta["display_name"] = display_name
    meta_file.write_text(
        json.dumps(disk_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("metadata.json 已改写: market=%s display_name=%s", to_market, display_name)

    # 3) 更新注册表（metadata_json 以磁盘版为准，保留注册时其余字段）
    metadata.update(
        {
            "market": to_market,
            "display_name": display_name,
            "market_migration": disk_meta["market_migration"],
        }
    )
    metadata["context"] = {**context, "market": to_market}
    async with get_session() as session:
        await session.execute(
            text(
                """
                UPDATE qm_user_models
                SET storage_path = :sp,
                    metadata_json = CAST(:meta AS JSONB),
                    updated_at = NOW()
                WHERE tenant_id = :t AND user_id = :u AND model_id = :m
                """
            ),
            {
                "sp": str(target.resolve()),
                "meta": json.dumps(metadata, ensure_ascii=False, default=str),
                "t": tenant_id,
                "u": user_id,
                "m": model_id,
            },
        )
        await session.commit()
    log.info("注册表已更新: storage_path=%s market=%s", target, to_market)

    # 4) 迁移后就绪校验（与推理门禁同口径：pin 优先 + describe 列/哈希检查）
    from backend.services.engine.inference.script_runner import (
        _resolve_market_factor_data_dir,
    )
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    data_dir = Path(_resolve_market_factor_data_dir(disk_meta))
    source = str(disk_meta.get("factor_source") or "l1_factors")
    status = QuantDBFactorReader(data_dir).describe(source)
    mapped = list((disk_meta.get("factor_field_sources") or {}).values())
    missing = [c for c in mapped if c not in status.columns]
    hash_ok = (not disk_meta.get("factor_schema_hash")) or (
        disk_meta["factor_schema_hash"] == status.schema_hash
    )
    ready = status.ready and not missing and hash_ok
    verdict = {
        "data_dir": str(data_dir),
        "factor_source": source,
        "coverage": f"{status.min_date}~{status.max_date}",
        "schema_hash_ok": hash_ok,
        "missing_mapped_fields": missing[:10],
        "precheck": "PASS" if ready else "FAIL",
    }
    log.info("迁移后就绪校验: %s", json.dumps(verdict, ensure_ascii=False))
    return {
        **plan,
        "changed": True,
        "display_name": display_name,
        "verification": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="模型市场迁移（metadata.market + 目录 + 注册表）"
    )
    parser.add_argument("--model-id", required=True, help="模型 ID")
    parser.add_argument(
        "--to-market", required=True, help="目标市场（CN/HK/US/CRYPTO/FUTURES）"
    )
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--user-id", default="00000001")
    parser.add_argument("--models-root", default=str(MODELS_USERS_ROOT))
    parser.add_argument("--dry-run", action="store_true", help="只打印迁移计划")
    args = parser.parse_args()

    result = asyncio.run(
        migrate(
            model_id=args.model_id,
            to_market=args.to_market,
            tenant_id=args.tenant_id,
            user_id=args.user_id,
            root=Path(args.models_root),
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    verification = result.get("verification") or {}
    return 1 if verification and verification.get("precheck") == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
