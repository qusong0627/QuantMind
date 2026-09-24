#!/usr/bin/env python3
"""修复「多算法训练」子模型的算法私有元数据 + 归一 model_type 大小写。

背景：`ModelRegistryService.split_multi_model_entries` 在 2026-09-13 之前只把父模型的
元数据原样复制给每个子模型 → 13 个算法全写着父模型的 `model_class_name='GRU'`、
`framework='pytorch'`、`is_sequence_model=true`、3D `input_spec` 与 GRU 形状的
`model_params`。当日修复只对**后续新训练**生效，存量产物需本脚本补齐。

危害（实测）：
  - 推理端 `ModelLoader._get_metadata` 读的是模型目录下的 `metadata.json`（不是 DB），
    按 `model_class_name` 重建架构 → 树模型/sklearn 被当成 GRU 加载；
  - `linear`/`random_forest` 的 `framework` 被写成 `pytorch` → 走错加载分支；
  - `is_sequence_model=true` 让 6 个扁平模型被当成序列模型准备 3D 输入。

修复口径与拆分代码完全同源：调用 `backend/shared/model_algorithm_meta.build_algorithm_metadata`
重建算法私有字段（framework / model_class_name / model_params / is_sequence_model /
input_spec / feat_norm / dl_params / model_arch / algorithm_meta_source），
并剔除该算法的父模型遗留字段。**只动元数据，不碰 pred / metrics / 产物文件。**

同时归一 `model_type` 大小写：训练端 standalone 路径写驼峰 `NativeTFT`，拆分路径写小写
`nativetft`，而 `ALGO_CLASS_NAMES` / `ALGO_FRAMEWORKS` / `SEQUENCE_TYPES` 的键全为小写 →
按算法聚合时同一算法会裂成两组。规范值是小写。

幂等：重复执行第二次为空操作（重建结果与首次一致）。检测同时覆盖 **DB 与磁盘**
两侧（磁盘那份以 DB 的算法身份为准来校验），写入后回读复验，三者缺一不可 ——
只查 DB 会漏掉「DB 已修、磁盘没写对」的残留（首版教训）。写入前自动备份到
`<models_root>/../.repair_backup_<ts>.json`。

用法（容器内）:
    python backend/scripts/repair_multi_model_metadata.py              # dry-run：只报表
    python backend/scripts/repair_multi_model_metadata.py --apply      # 执行（可重跑）
    python backend/scripts/repair_multi_model_metadata.py --apply --model-id mdl_xxx
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

from backend.shared.model_algorithm_meta import (  # noqa: E402
    ALGO_CLASS_NAMES,
    ALGO_FRAMEWORKS,
    ALGO_SCOPED_FIELDS,
    DL_TYPES,
    SEQUENCE_TYPES,
    build_algorithm_metadata,
    read_xgboost_best_iteration,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("repair_algo_meta")

# 宿主机 ./models/users → 容器内 /app/models/users
DEFAULT_MODELS_ROOT = Path(os.getenv("QM_MODELS_ROOT", "/app/models/users"))
# 训练任务工作区（父 metadata.json 的权威来源）
DEFAULT_JOBS_ROOT = Path(os.getenv("QM_TRAINING_JOBS_ROOT", "/data/training_jobs"))

# 需要重建算法私有字段的字段集由 build_algorithm_metadata 给出；
# best_iteration 虽在剔除列表内，但 xgboost 子模型需按权重文件重算后写回，
# 不能简单丢弃（拆分代码亦如此）。
_XGB_BEST_ITERATION = "best_iteration"


def _get_engine():
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', '')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    return create_engine(db_url, pool_pre_ping=True, future=True)


def _canonical_case(model_type: str) -> str:
    """规范算法名：全小写（与 ALGO_CLASS_NAMES / ALGO_FRAMEWORKS 的键一致）。"""
    return str(model_type or "").strip().lower()


def detect_case_issue(meta: dict) -> str | None:
    """model_type 不是规范小写时返回规范值，否则 None。"""
    raw = str(meta.get("model_type") or "").strip()
    if raw and raw != raw.lower():
        return raw.lower()
    return None


def detect_algo_issues(meta: dict, model_type: str | None = None) -> list[str]:
    """检测算法私有字段与权威映射不一致之处（空列表=无需重建）。

    只覆盖「父子元数据串味」会破坏的那几个字段；`model_type` 大小写单独处理，
    避免对健康的 standalone 模型做无谓的全量重建。

    ``model_type`` 显式传入时以它为准（而非 `meta` 自身的）——用于**以 DB 身份
    校验磁盘副本**：磁盘那份的 `model_type` 可能正是被污染的值（实测全是 'gru'），
    拿它自查会「GRU 校验 GRU」恒过。
    """
    mtype = _canonical_case(model_type or meta.get("model_type"))
    issues: list[str] = []

    expected_fw = ALGO_FRAMEWORKS.get(mtype)
    if expected_fw and meta.get("framework") != expected_fw:
        issues.append(f"framework={meta.get('framework')!r} 应为 {expected_fw!r}")

    expected_cls = ALGO_CLASS_NAMES.get(mtype)
    stored_cls = meta.get("model_class_name")
    if stored_cls:
        if mtype not in DL_TYPES or expected_cls is None:
            issues.append(f"model_class_name={stored_cls!r} 应删除（{mtype} 非 Qlib DL 算法）")
        elif stored_cls != expected_cls:
            issues.append(f"model_class_name={stored_cls!r} 应为 {expected_cls!r}")

    seq = meta.get("is_sequence_model")
    if seq is not None and bool(seq) != (mtype in SEQUENCE_TYPES):
        issues.append(f"is_sequence_model={bool(seq)} 应为 {mtype in SEQUENCE_TYPES}")

    return issues


def load_parent_meta(job_name: str, child_meta: dict, jobs_root: Path) -> tuple[dict, str]:
    """取重建算法字段所依据的父 metadata。

    优先训练任务工作区的原始父 metadata.json（未被子模型改写）；
    找不到时退回子模型自身的 metadata —— 其中 `dl_params` / `feat_norm` /
    `feature_columns` / `input_spec` 均为父份原样拷贝，数值正确，
    而算法私有字段本就会被重建覆盖，故不影响结果。
    """
    if job_name:
        parent_path = jobs_root / job_name / "metadata.json"
        if parent_path.is_file():
            try:
                return json.loads(parent_path.read_text(encoding="utf-8")), str(parent_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("父 metadata 解析失败 %s: %s", parent_path, exc)
    return child_meta, "(子模型自身 metadata)"


def repair_meta(
    meta: dict,
    parent_meta: dict,
    model_dir: Path,
    *,
    model_type: str,
    rebuild: bool,
) -> dict:
    """修复单份 metadata（纯函数，不写盘）。

    ``model_type`` 由调用方传入，**不从 `meta` 自身读取**：损坏副本里的
    `model_type` 恰恰是父模型的值（实测 13 个子模型的磁盘 metadata.json 全部写着
    `model_type='gru'`），拿它当算法身份会「用 GRU 重建 GRU」而空转。权威身份是
    DB 记录的 `model_type`（拆分时已按算法正确写入）。

    ``rebuild=False`` 时只做大小写归一——standalone 模型的算法字段本就由训练端
    按算法写出，不应被无谓重建（会平白加上 `algorithm_meta_source`）。
    """
    mtype = _canonical_case(model_type)
    new_meta = dict(meta)
    if mtype:
        new_meta["model_type"] = mtype
    for key in ("model_types", "primary_model_type"):
        value = meta.get(key)
        if isinstance(value, list):
            new_meta[key] = [_canonical_case(v) for v in value]
        elif isinstance(value, str) and value:
            new_meta[key] = _canonical_case(value)

    if not rebuild or not mtype:
        return new_meta

    fields, drops = build_algorithm_metadata(mtype, parent_meta)
    for key in drops:
        if key != _XGB_BEST_ITERATION:
            new_meta.pop(key, None)
    new_meta.update(fields)

    # xgboost 的早停轮数需按权重文件重算（与拆分代码同口径）
    if mtype == "xgboost":
        model_file = str(meta.get("model_file") or "")
        if model_file:
            new_meta[_XGB_BEST_ITERATION] = read_xgboost_best_iteration(
                model_dir / model_file
            )
    return new_meta


def merge_disk_meta(new_meta: dict, disk_meta: dict, *, algo_repair: bool) -> dict:
    """以修复后的 DB 版为基合并磁盘副本，只保留**磁盘独有**的键。

    保留规则按字段语义分两类：
      - 非算法字段（is_ensemble / pool_* / factor_selection …）DB 从不落库，一律保留；
      - 算法字段在「本行是算法身份修复」（``algo_repair``）时**只认 new_meta**：
        新值有就用新值，没有就说明该算法本不该有这个字段（非 DL 算法的
        model_class_name / is_sequence_model / model_params …），绝不能从磁盘搬回。

    首版把「new_meta 里没有」等同于「磁盘独有」，刚删掉的脏字段被原样搬回，
    mlp 因此仍带 GRU 身份、加载走进 Qlib DL 分支报 Unknown model class。
    参照也不能用「修复后的 DB」：幂等重跑时 DB 已无那些键，又会误判。
    """
    disk_only = {
        k: v
        for k, v in disk_meta.items()
        if k not in new_meta and not (algo_repair and k in ALGO_SCOPED_FIELDS)
    }
    return {**disk_only, **new_meta}


def _diff(before: dict, after: dict) -> dict[str, tuple]:
    """仅列出发生变化的键（含新增/删除），供 dry-run 报表使用。"""
    out: dict[str, tuple] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old != new:
            out[key] = (old, new)
    return out


def fetch_models(engine, model_id: str) -> list[dict]:
    sql = """
        SELECT tenant_id, user_id, model_id, storage_path, metadata_json
        FROM qm_user_models
    """
    params: dict[str, str] = {}
    if model_id:
        sql += " WHERE model_id = :model_id"
        params["model_id"] = model_id
    sql += " ORDER BY created_at"
    with engine.begin() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="修复多算法子模型的算法私有元数据与 model_type 大小写"
    )
    parser.add_argument("--apply", action="store_true", help="执行写入（默认 dry-run）")
    parser.add_argument("--model-id", default="", help="只处理指定模型 ID")
    parser.add_argument("--models-root", default=str(DEFAULT_MODELS_ROOT))
    parser.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    args = parser.parse_args()

    models_root = Path(args.models_root)
    jobs_root = Path(args.jobs_root)
    engine = _get_engine()

    rows = fetch_models(engine, args.model_id)
    logger.info("扫描 %d 条模型记录", len(rows))

    plans: list[dict] = []
    missing_disk: list[str] = []
    for row in rows:
        meta = row.get("metadata_json") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        algo_type = _canonical_case(meta.get("model_type"))

        model_dir = Path(str(row.get("storage_path") or ""))
        disk_meta_path = model_dir / "metadata.json"
        disk_meta = None
        if disk_meta_path.is_file():
            disk_meta = json.loads(disk_meta_path.read_text(encoding="utf-8"))

        # DB 与磁盘两边都要查，且磁盘以 DB 的算法身份为准来校验（见 detect_algo_issues
        # 的 ``model_type`` 参数）。只查 DB 会让「DB 已修、磁盘写歪」的残留被漏报——
        # 首版就是这个坑：重跑报「需修复 0 条」，而磁盘元数据仍是父模型的 GRU 身份。
        case_issue = detect_case_issue(meta) or (
            detect_case_issue(disk_meta) if disk_meta else None
        )
        algo_issues = detect_algo_issues(meta)
        disk_issues = (
            detect_algo_issues(disk_meta, model_type=algo_type) if disk_meta else []
        )
        if not case_issue and not algo_issues and not disk_issues:
            continue
        if disk_meta is None:
            missing_disk.append(str(row["model_id"]))

        # 算法字段重建只在确实串味时做；纯大小写问题走最小改动
        rebuild = bool(algo_issues)
        job_name = str(meta.get("job_name") or "")
        parent_meta, source = load_parent_meta(job_name, meta, jobs_root)
        new_meta = repair_meta(
            meta, parent_meta, model_dir, model_type=algo_type, rebuild=rebuild
        )
        # 磁盘副本比 DB 更旧（实测身份字段全是父值：model_type='gru'、
        # model_file='model_gru.pth'、saved_models=父份全表），逐字段修不可靠；
        # 以修好的 DB 版为基，只保留**磁盘独有**的键（口径见 merge_disk_meta）。
        algo_repair = rebuild or bool(disk_issues)
        new_disk_meta = (
            None if disk_meta is None
            else merge_disk_meta(new_meta, disk_meta, algo_repair=algo_repair)
        )
        plans.append(
            {
                "row": row,
                "model_dir": model_dir,
                "disk_meta": disk_meta,
                "new_meta": new_meta,
                "new_disk_meta": new_disk_meta,
                "parent_source": source,
                "disk_issues": disk_issues,
                "remaining": detect_algo_issues(new_meta),
            }
        )

    print(f"\n需修复 {len(plans)} 条（其中磁盘 metadata 缺失 {len(missing_disk)} 条，仅修 DB）")
    for mid in missing_disk:
        print(f"  ! 缺磁盘 metadata: {mid}")

    # 噪音字段：diff 里必然出现但对本次修复无意义
    _NOISE = {"shap", "pred", "pred_source", "eval_report", "metrics", "updated_at"}
    for plan in plans:
        row = plan["row"]
        print(f"\n=== {row['model_id']}  [{plan['parent_source']}]")
        disk_diff = (
            {}
            if plan["disk_meta"] is None
            else _diff(plan["disk_meta"], plan["new_disk_meta"])
        )
        db_diff = _diff(plan["row"]["metadata_json"], plan["new_meta"])
        for key in sorted(set(db_diff) | set(disk_diff)):
            if key in _NOISE:
                continue
            where = "磁盘+DB" if key in db_diff and key in disk_diff else (
                "DB" if key in db_diff else "磁盘"
            )
            before = (db_diff.get(key) or disk_diff.get(key))[0]
            after = (db_diff.get(key) or disk_diff.get(key))[1]
            print(f"    [{where}] {key}: {_short(before)}  ->  {_short(after)}")
        if plan["disk_issues"]:
            print(f"    [磁盘] 命中: {plan['disk_issues']}")
        if plan["remaining"]:
            print(f"    !! 重建后仍有问题: {plan['remaining']}")

    if not plans:
        print("\n无需修复。")
        return 0
    if not args.apply:
        print("\n（dry-run，未写入。加 --apply 执行）")
        return 0

    backup = _backup(plans, models_root)
    print(f"\n已备份到 {backup}")

    written = 0
    for plan in plans:
        row = plan["row"]
        try:
            if plan["new_disk_meta"] is not None:
                plan["model_dir"].joinpath("metadata.json").write_text(
                    json.dumps(plan["new_disk_meta"], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE qm_user_models
                        SET metadata_json = CAST(:meta AS JSONB), updated_at = now()
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND model_id = :model_id
                        """
                    ),
                    {
                        "meta": json.dumps(plan["new_meta"], ensure_ascii=False),
                        "tenant_id": row["tenant_id"],
                        "user_id": row["user_id"],
                        "model_id": row["model_id"],
                    },
                )
            written += 1
            logger.info("已修复 %s", row["model_id"])
        except Exception as exc:  # noqa: BLE001
            logger.error("修复失败 %s: %s", row["model_id"], exc)

    print(f"\n完成：写入 {written}/{len(plans)} 条（磁盘 metadata.json + DB metadata_json）")

    # 写入后立即复验（回读落盘文件，而不是信任内存里的计划）：
    # 首版漏了这步，「磁盘写回旧值」也会显示成功。
    residual: list[tuple[str, str, list[str]]] = []
    for plan in plans:
        row = plan["row"]
        mtype = _canonical_case(plan["new_meta"].get("model_type"))
        disk_left = detect_algo_issues(
            json.loads(plan["model_dir"].joinpath("metadata.json").read_text(encoding="utf-8")),
            model_type=mtype,
        )
        if disk_left:
            residual.append((str(row["model_id"]), "磁盘", disk_left))
        db_left = detect_algo_issues(plan["new_meta"], model_type=mtype)
        if db_left:
            residual.append((str(row["model_id"]), "DB", db_left))
    for mid, where, left in residual:
        logger.error("复验未通过 %s [%s]: %s", mid, where, left)
    if residual:
        print(f"\n!! 复验未通过 {len(residual)} 处（详见上方 ERROR）")
        return 1
    print(f"复验通过：{len(plans)} 条磁盘与 DB 均已一致")
    return 0


def _short(value, limit: int = 70) -> str:
    """紧凑展示字段值，避免 dry-run 报表被特征数组淹没。"""
    text_value = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text_value if len(text_value) <= limit else text_value[:limit] + "…"


def _backup(plans: list[dict], models_root: Path) -> Path:
    """写入前备份受影响记录的**修复前**元数据（磁盘 + DB 两份）。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = models_root.parent / f".repair_backup_{ts}.json"
    payload = {
        str(p["row"]["model_id"]): {
            "db_metadata_json": p["row"]["metadata_json"],
            "disk_metadata_json": p["disk_meta"],
            "storage_path": str(p["model_dir"]),
        }
        for p in plans
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
