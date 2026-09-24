#!/usr/bin/env python3
"""把「产物已不在盘、DB 却仍记 shap.status=completed」的模型改标 missing。

背景（2026-09-24 CN 全量审计）：CN 66 行模型中 shap.status=completed 的 19 行里，
有 3 行盘上没有模型目录；其中 2 行连 `/data/training_jobs/<run>/` 工作目录也已消失
（不可恢复），另 1 行（mdl_cn_train_20260906023306_57ce74a7_d5a3faa7）job 目录尚存、
待模型目录回填时一并恢复——本脚本**只动不可恢复的那 2 行**。

为什么必须改（消费方读的就是这份存储）：
  - 前端归因面板 `AttributionAnalysisPanel`（electron/src/pages/modelRegistryPanels.tsx）
    直接用 metadata.shap.status 渲染标签：completed → 绿色「分析就绪」，表格恒空；
  - SHAP 端点 GET /{model_id}/shap-summary 对无目录模型直接 404，永远不会把
    「文件不在」修正回 status。

命名空间（2026-09-24 实测的容器挂载，脚本对两个方向都能跑）：
  - 消费方是 quantmind 容器：storage_path 写作 /app/models/...（逐目录 bind mount
    自宿主仓库 models/）；job 目录实际在容器 /data/training_jobs（compose ./data:/data），
    **不在** /app/data——/app/data 是镜像里的空目录，sanity 探针踩过这个坑：
    在容器里按 /app/data 判 job 存亡，会把「可恢复」误判成「不可恢复」。
  - 因此两个根都按「候选列表取第一个可见者」解析，且任一不可见即整体 abort——
    路径判据落在错命名空间时绝不继续。

守卫（任一不满足即拒绝该行，绝不盲改）：
  1) DB 中 shap.status == 'completed'（已是别的状态则不动）；
  2) storage_path 目录不存在（可见命名空间内）；
  3) training_jobs/<run_id>/ 也不存在（尚存者留给目录回填，不在这里改）；
  4) model_id 推导的 run_id 与白名单登记一致（防呆：行被换过就停手）。

用法:
    python backend/scripts/fix_shap_status_missing_artifacts.py --dry-run   # 默认，只报表
    python backend/scripts/fix_shap_status_missing_artifacts.py --apply     # 写库 + 回读复验
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

# 本次审计确认的不可恢复行：model_id → 期望 run_id（从 model_id 推导，不一致即拒绝）
TARGETS: dict[str, str] = {
    "mdl_cn_train_20260825095922_078eb40f_e09a811f": "train_20260825095922_078eb40f",
    "mdl_cn_train_20260826011833_4a1cc0eb_a60396a7": "train_20260826011833_4a1cc0eb",
    # 阳性对照（永远应被守卫 3 跳过）：job 目录尚存（容器 /data/training_jobs 可见），
    # 属可恢复行，留给目录回填。放在这里是为了每次运行都证明守卫真的在判，
    # 而不是无差别 will_fix。
    "mdl_cn_train_20260906023306_57ce74a7_d5a3faa7": "train_20260906023306_57ce74a7",
}
STALE_STATUS = "completed"
NEW_STATUS = "missing"
REPAIR_NOTE = "artifacts_missing_20260924"

# 候选根按序取「第一个可见者」：容器内 / 宿主 repo 两个命名空间都能跑
MODELS_ROOT_CANDIDATES = [Path("/app/models"), PROJECT_ROOT / "models"]
JOBS_ROOT_CANDIDATES = [
    Path("/data/training_jobs"),
    PROJECT_ROOT / "data" / "training_jobs",
]


def _get_engine():
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    if not db_url.startswith("postgresql"):
        db_url = (
            f"postgresql+psycopg2://{os.getenv('DB_USER', 'quantmind')}:"
            f"{os.getenv('DB_PASSWORD', 'quantmind2026')}@{os.getenv('DB_HOST', 'db')}:5432/quantmind"
        )
    return create_engine(db_url, pool_pre_ping=True, future=True)


def _as_jsonb(val: Any) -> dict:
    """psycopg2 下 JSONB 可能是 str。"""
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            out = json.loads(val)
        except json.JSONDecodeError:
            return {}
        return out if isinstance(out, dict) else {}
    return {}


def _first_visible(candidates: list[Path], what: str) -> Path:
    for p in candidates:
        if p.is_dir():
            return p
    print(
        f"**ABORT**：{what}根不可见（候选：{[str(c) for c in candidates]}）——"
        "路径判据落在错的命名空间里，不可据此判定产物缺失。",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _reroot(storage_path: str, models_root: Path) -> Path:
    """把 DB 里的 /app/models/... 路径重挂到当前可见的 models 根。"""
    marker = "/app/models/"
    if storage_path.startswith(marker):
        return models_root / storage_path[len(marker) :]
    host_marker = str(PROJECT_ROOT / "models") + "/"
    if storage_path.startswith(host_marker):
        return models_root / storage_path[len(host_marker) :]
    return Path(storage_path)


def _run_id_from_model_id(model_id: str) -> str | None:
    m = re.match(r"^mdl_cn_(train_\d+_[0-9a-f]+)_", model_id)
    return m.group(1) if m else None


def _check_row(
    conn, model_id: str, expected_run: str, models_root: Path, jobs_root: Path
) -> tuple[str, str | None]:
    """返回 (verdict, detail)。verdict ∈ {'will_fix', 'skip:<原因>', 'error:<原因>'}。"""
    row = conn.execute(
        text(
            "SELECT storage_path, metadata_json FROM qm_user_models WHERE model_id = :mid"
        ),
        {"mid": model_id},
    ).fetchone()
    if row is None:
        return "error:DB 无此 model_id", None
    storage_path, meta_raw = row
    shap = _as_jsonb(meta_raw).get("shap")
    shap = shap if isinstance(shap, dict) else {}
    cur_status = str(shap.get("status") or "")
    if cur_status != STALE_STATUS:
        return (
            f"skip:当前 status={cur_status!r}，非 {STALE_STATUS!r}（可能已被处理）",
            None,
        )

    derived = _run_id_from_model_id(model_id)
    if derived != expected_run:
        return (
            f"error:run_id 推导 {derived!r} != 白名单 {expected_run!r}（行被换过，停手）",
            None,
        )

    storage_dir = _reroot(str(storage_path or ""), models_root)
    job_dir = jobs_root / expected_run
    if storage_dir.is_dir():
        return (
            f"skip:storage_path 目录存在（{storage_dir}），交给端点 file_exists 逻辑",
            None,
        )
    if job_dir.is_dir():
        return f"skip:job 目录尚存（{job_dir}），留给模型目录回填", None
    for probe in (storage_dir / "shap_summary.csv", job_dir / "shap_summary.csv"):
        if probe.is_file():
            return f"error:居然找到 {probe}，与不可恢复判定矛盾", None
    return "will_fix", f"storage_path={storage_dir} 不存在 · job_dir={job_dir} 不存在"


def _apply(conn, model_id: str) -> None:
    # 比较并交换：只有此刻仍是 completed 才写
    conn.execute(
        text(
            """
            UPDATE qm_user_models
               SET metadata_json = jsonb_set(
                       jsonb_set(metadata_json, '{shap,status}', to_jsonb(cast(:new_status AS text))),
                       '{shap,repair_note}', to_jsonb(cast(:note AS text))
                   )
             WHERE model_id = :mid
               AND metadata_json->'shap'->>'status' = :stale
            """
        ),
        {
            "mid": model_id,
            "new_status": NEW_STATUS,
            "note": REPAIR_NOTE,
            "stale": STALE_STATUS,
        },
    )


def _verify(conn, model_id: str) -> bool:
    row = conn.execute(
        text(
            "SELECT metadata_json->'shap'->>'status', metadata_json->'shap'->>'repair_note' "
            "FROM qm_user_models WHERE model_id = :mid"
        ),
        {"mid": model_id},
    ).fetchone()
    return bool(row) and row[0] == NEW_STATUS and row[1] == REPAIR_NOTE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="只报表（默认）")
    g.add_argument("--apply", action="store_true", help="写库并回读复验")
    args = ap.parse_args()

    models_root = _first_visible(MODELS_ROOT_CANDIDATES, "models")
    jobs_root = _first_visible(JOBS_ROOT_CANDIDATES, "training_jobs")
    engine = _get_engine()
    failures = 0
    print(
        f"{'APPLY' if args.apply else 'DRY-RUN'} · 目标 {len(TARGETS)} 行 · "
        f"models_root={models_root} · jobs_root={jobs_root}"
    )
    with engine.begin() as conn:
        for model_id, expected_run in TARGETS.items():
            verdict, detail = _check_row(
                conn, model_id, expected_run, models_root, jobs_root
            )
            print(f"  [{verdict}] {model_id}  {detail or ''}")
            if verdict == "will_fix" and args.apply:
                _apply(conn, model_id)
                ok = _verify(conn, model_id)
                print(f"    → 写入 + 回读复验: {'通过' if ok else '**失败**'}")
                if not ok:
                    failures += 1
            elif verdict.startswith("error"):
                failures += 1
    print(f"完成：{len(TARGETS)} 行处理，{failures} 处异常")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
