#!/usr/bin/env python
"""一次性迁移：把「反向模型」的预测产物取负，使平台口径「正分=看涨」在 pred 产物里成立。

背景：训练把方向记在 `metadata.json` 的 `metrics.score_direction`，而推理模板读的是顶层
`meta["score_direction"]`（训练从不写顶层）→ 模板里的 `scores = -scores` 从未执行。
模板已修（改读 metrics），但 **`pred.parquet` / `pred.pkl` 里存的是训练期的原始预测**，
个股预测直读、多模型分数曲线、共识矩阵都直接读它 —— 这几个路径必须靠本脚本补正。

幂等：每修一个产物就在 metadata.json 里写 `pred_direction_applied = "reversed"`，
重复执行会跳过（否则会来回翻转）。CN/HK 的模型实测全为 normal，本脚本对它们是空操作。

用法：
    python backend/scripts/fix_pred_score_direction.py --dry-run     # 只报告
    python backend/scripts/fix_pred_score_direction.py               # 实际执行
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("fix_pred_direction")

MARKER = "pred_direction_applied"

# 宿主 models 目录（容器内 /app/models，宿主 ./models）
DEFAULT_MODELS_ROOT = Path(os.getenv("QM_MODELS_ROOT", "/app/models/users"))
# 训练任务工作区（拆分时从这里取 pred_{type}.parquet）
DEFAULT_JOBS_ROOT = Path(os.getenv("QM_TRAINING_JOBS_ROOT", "/data/training_jobs"))


def _direction_of(meta: dict) -> str:
    """方向以 metrics.score_direction 为准（顶层为历史写法，一并兼容）。"""
    return str(meta.get("score_direction") or (meta.get("metrics") or {}).get("score_direction") or "")


def _negate_parquet(path: Path) -> int:
    """pred 列取负并写回；返回影响行数。"""
    df = pd.read_parquet(path)
    if "pred" not in df.columns:
        return 0
    df["pred"] = -df["pred"]
    df.to_parquet(path, engine="pyarrow", compression="zstd", index=False)
    return len(df)


def _negate_pkl(path: Path) -> int:
    """qlib 回测用的 pred.pkl：score 列取负并写回；返回影响行数。"""
    df = pd.read_pickle(path)
    col = "score" if "score" in df.columns else ("pred" if "pred" in df.columns else None)
    if col is None:
        return 0
    df[col] = -df[col]
    df.to_pickle(path)
    return len(df)


def fix_model_dir(model_dir: Path, dry_run: bool, jobs_root: Path) -> dict:
    """修正单个模型目录；返回结果描述。"""
    meta_path = model_dir / "metadata.json"
    if not meta_path.exists():
        return {"dir": str(model_dir), "action": "skip", "reason": "无 metadata.json"}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if _direction_of(meta) != "reversed":
        return {"dir": str(model_dir), "action": "skip", "reason": "方向非 reversed"}
    if meta.get(MARKER) == "reversed":
        return {"dir": str(model_dir), "action": "skip", "reason": "已迁移（幂等标记）"}

    touched: list[str] = []
    rows = 0
    for name, fn in (("pred.parquet", _negate_parquet), ("pred.pkl", _negate_pkl)):
        p = model_dir / name
        if not p.exists():
            continue
        if dry_run:
            touched.append(f"{name}(dry)")
        else:
            rows += fn(p)
            touched.append(name)

    # 训练任务工作区里的 pred_{type}.parquet：拆分是从那里取的，不改的话将来重跑拆分
    # 又会把未取负的值带回模型目录。
    # 注意：任务元数据是**多个基模型共享**的，所以这里的标记必须按算法分别记，
    # 否则修完一个算法就会把其余算法误标成已迁移。
    run_id = str(meta.get("run_id") or "")
    model_type = str(meta.get("model_type") or "")
    job_dir = jobs_root / run_id
    job_pred = job_dir / f"pred_{model_type}.parquet"
    if run_id and model_type and job_pred.exists():
        job_meta_path = job_dir / "metadata.json"
        job_marks: dict = {}
        if job_meta_path.exists():
            try:
                raw = json.loads(job_meta_path.read_text(encoding="utf-8")).get(MARKER)
                job_marks = raw if isinstance(raw, dict) else {}
            except Exception:  # noqa: BLE001 — 标记读取失败不阻断主流程
                job_marks = {}
        if job_marks.get(model_type) != "reversed":
            if dry_run:
                touched.append(f"{job_pred.name}(dry)")
            else:
                rows += _negate_parquet(job_pred)
                touched.append(str(job_pred))
                job_marks[model_type] = "reversed"
                if job_meta_path.exists():
                    try:
                        jm = json.loads(job_meta_path.read_text(encoding="utf-8"))
                        jm[MARKER] = job_marks
                        job_meta_path.write_text(json.dumps(jm, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("任务工作区标记写入失败（不影响模型目录已修正）: %s", exc)

    if not dry_run:
        meta[MARKER] = "reversed"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"dir": str(model_dir), "action": "fix", "files": touched, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不写入")
    ap.add_argument("--models-root", type=Path, default=DEFAULT_MODELS_ROOT)
    ap.add_argument("--jobs-root", type=Path, default=DEFAULT_JOBS_ROOT)
    args = ap.parse_args()

    if not args.models_root.exists():
        logger.error("models 根目录不存在: %s", args.models_root)
        return 1

    results = [
        fix_model_dir(d, args.dry_run, args.jobs_root)
        for d in sorted(args.models_root.glob("*/*/*/mdl_*"))
        if d.is_dir()
    ]

    fixed = [r for r in results if r["action"] == "fix"]
    for r in fixed:
        print(f"  {'[dry] ' if args.dry_run else ''}修正 {r['dir']}  文件={r.get('files')} 行数={r.get('rows')}")
    print(f"\n扫描 {len(results)} 个模型目录：需修正 {len(fixed)}，其余为 normal/已迁移")
    if args.dry_run and fixed:
        print("（dry-run，未写入任何文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
