"""P2 vintage 回放拼接与配对检验 CLI（设计 §5.1，脚本层：唯一文件/DB IO 处）。

挑战者 = 一组历史锚点 campaign 的 OOS test 段拼接（同 recipe 多 vintage，
``qm_rolling_campaigns`` 的 ``model_id`` → ``pred.parquet``）；冠军 = 当前默认
模型同窗 ``pred.parquet``。对比在**共同日期 ∩ 共同池**上逐日配对
（见 ``paired_stats``——跨池比均值必先取共同池）。

输出一份 evidence JSON（stdout；``--out`` 落盘），G2-G6 闸门全部从它取数；
``pred_md5`` 一并带上供 G1 独立性检查。

用法::

  python backend/scripts/eval/walkforward_rollup.py \
      --champion-model-id mdl_cust_train_20260917064612_33659b62_754461be \
      --challenger-model-id mdl_cust_train_20260916000342_b701110b_7ed2e231 \
      [--campaign-id rc_xxx ...] [--k 50] [--out data/rollouts/replay.json]

本地宿主直跑时会自动把容器内 ``/app/models`` 前缀映射回仓库 ``models/``
（复用 ``model_card._user_meta_path`` 的同一条路径解析）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.eval.model_card import _user_meta_path  # noqa: E402
from backend.scripts.eval.model_realized import (  # noqa: E402
    read_test_split,
    resolve_cost_model,
)
from backend.scripts.eval.paired_stats import (  # noqa: E402
    DEFAULT_TOP_K,
    annualized_turnover,
    monthly_ic_stats,
    paired_daily_delta,
    prepare_frame,
    summarize_delta,
)
from backend.shared.utc_datetime import utc_now  # noqa: E402

EXIT_OK = 0
EXIT_CHALLENGER_UNAVAILABLE = 2
EXIT_CHAMPION_UNAVAILABLE = 3


def _file_md5(path: Path, *, chunk: int = 1 << 20) -> str:
    """pred.parquet 的 md5（G1 独立性检查用：逐位相同 = 复制品）。"""
    h = hashlib.md5()  # noqa: S324 - 产物指纹非安全用途
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


async def _load_storage_paths(model_ids: list[str]) -> dict[str, str]:
    """``qm_user_models`` → {model_id: storage_path}（缺行不报错，按不在盘处理）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    if not model_ids:
        return {}
    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT model_id, storage_path FROM qm_user_models "
                    "WHERE model_id = ANY(:ids)"
                ),
                {"ids": list(dict.fromkeys(model_ids))},
            )
        ).fetchall()
    return {str(r[0]): str(r[1] or "") for r in rows}


def _model_dir(model_id: str, storage_path: str) -> Path | None:
    meta = _user_meta_path(model_id, storage_path)
    return meta.parent if meta else None


def _cost_of(model_dir: Path | None) -> float | None:
    """模型 metadata 的费率覆盖（缺 metadata 用 ``CostModel()`` 默认）。"""
    meta: dict[str, Any] | None = None
    if model_dir is not None:
        meta_file = model_dir / "metadata.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = None
    return resolve_cost_model(meta).round_trip_cost()


def _load_segment(model_dir: Path) -> tuple[Any, dict[str, Any]]:
    """单个 vintage 的 OOS 段：pred.parquet test 段 → (frame, 剖面)。"""
    pred = model_dir / "pred.parquet"
    if not pred.is_file():
        raise FileNotFoundError(f"pred.parquet 不在盘: {pred}")
    frame = read_test_split(pred)
    dates = sorted(set(frame["date_key"].astype(str)))
    profile = {
        "pred_md5": _file_md5(pred),
        "n_rows": int(len(frame)),
        "n_days": len(dates),
        "date_span": [dates[0], dates[-1]] if dates else None,
    }
    return frame, profile


async def _resolve_campaigns(campaign_ids: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """campaign_id → (campaign_id, model_id)；未注册/缺 model_id 记 warning 跳过。"""
    from backend.shared.rolling_campaigns import STATUS_REGISTERED, get_campaign

    specs: list[tuple[str, str]] = []
    warnings: list[str] = []
    for cid in campaign_ids:
        row = await get_campaign(cid)
        if row is None:
            warnings.append(f"campaign {cid} 不存在，跳过")
            continue
        if row.get("status") != STATUS_REGISTERED:
            warnings.append(
                f"campaign {cid} 状态 {row.get('status')} ≠ registered（按已登记产物继续）"
            )
        model_id = str(row.get("model_id") or "").strip()
        if not model_id:
            warnings.append(f"campaign {cid} 无 model_id（未注册完成），跳过")
            continue
        specs.append((cid, model_id))
    return specs, warnings


def _assemble_challenger(
    frames: list[Any],
    segments: list[dict[str, Any]],
    warnings: list[str],
) -> Any:
    import pandas as pd

    concat = pd.concat(frames, ignore_index=True)
    clean, notes = prepare_frame(concat)
    if notes["dropped_dup"]:
        warnings.append(
            f"拼接段存在重叠 (日期, 标的) 行 {notes['dropped_dup']} 条（keep last）——"
            "检查 campaign 窗口是否意外重叠"
        )
    return clean, notes


def build_report_sync(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """同步包装（DB 段用 asyncio.run；便于单测与 CLI 共用）。"""
    return asyncio.run(build_report(args))


async def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """证据组装主体（async——rollout 服务在事件循环里直接 await 本函数）。"""
    warnings: list[str] = []

    specs, w = await _resolve_campaigns(list(args.campaign_id or []))
    warnings.extend(w)
    for mid in args.challenger_model_id or []:
        specs.append(("", str(mid)))
    if not specs:
        return {"error": "无挑战者来源（--campaign-id / --challenger-model-id 至少一个）"}, EXIT_CHALLENGER_UNAVAILABLE
    if args.challenger_dir and len(specs) > 1:
        return {"error": "--challenger-dir 仅支持单一挑战者来源"}, EXIT_CHALLENGER_UNAVAILABLE

    try:
        storage = await _load_storage_paths([mid for _, mid in specs] + [args.champion_model_id])
    except Exception as exc:  # noqa: BLE001 - DB 不可用要显式报，不装成「产物不在盘」
        return {"error": f"读 qm_user_models 失败: {exc}"}, EXIT_CHALLENGER_UNAVAILABLE

    # 冠军
    champion_dir = (
        Path(args.champion_dir)
        if args.champion_dir
        else _model_dir(args.champion_model_id, storage.get(args.champion_model_id, ""))
    )
    if champion_dir is None or not champion_dir.is_dir():
        return {
            "error": f"冠军产物不在盘: {args.champion_model_id} "
            f"(dir={champion_dir})；可用 --champion-dir 显式指定"
        }, EXIT_CHAMPION_UNAVAILABLE
    try:
        champion_frame, champion_profile = _load_segment(champion_dir)
    except FileNotFoundError as exc:
        return {"error": str(exc)}, EXIT_CHAMPION_UNAVAILABLE

    # 挑战者各 vintage
    c_frames: list[Any] = []
    segments: list[dict[str, Any]] = []
    for campaign_id, model_id in specs:
        seg_dir = (
            Path(args.challenger_dir)
            if args.challenger_dir
            else _model_dir(model_id, storage.get(model_id, ""))
        )
        if seg_dir is None or not seg_dir.is_dir():
            warnings.append(f"挑战者产物不在盘（model_id={model_id}），该 vintage 跳过")
            continue
        try:
            frame, profile = _load_segment(seg_dir)
        except FileNotFoundError as exc:
            warnings.append(str(exc))
            continue
        frame = frame.assign(model_id=model_id, campaign_id=campaign_id)
        c_frames.append(frame)
        segments.append(
            {"campaign_id": campaign_id or None, "model_id": model_id, "dir": str(seg_dir), **profile}
        )
    if not c_frames:
        return {"error": "挑战者无任何在盘 OOS 段", "warnings": warnings}, EXIT_CHALLENGER_UNAVAILABLE

    challenger_clean, c_notes = _assemble_challenger(c_frames, segments, warnings)
    if challenger_clean.empty:
        return {"error": "挑战者拼接后无有效行"}, EXIT_CHALLENGER_UNAVAILABLE
    champion_clean, h_notes = prepare_frame(champion_frame)

    paired = paired_daily_delta(
        challenger_clean,
        champion_clean,
        challenger_notes=c_notes,
        champion_notes=h_notes,
    )
    delta_summary = summarize_delta(paired["delta"])
    paired_dates = set(paired["delta"])
    c_pair = challenger_clean[challenger_clean["date_key"].isin(paired_dates)]
    h_pair = champion_clean[champion_clean["date_key"].isin(paired_dates)]

    try:
        cost_challenger = _cost_of(Path(segments[-1]["dir"]))
        cost_champion = _cost_of(champion_dir)
    except Exception as exc:  # noqa: BLE001 - 费率解析失败不阻断（回默认）
        warnings.append(f"费率解析失败，回默认: {exc}")
        cost_challenger = cost_champion = None

    report: dict[str, Any] = {
        "object_type": "rollout_replay",
        "source": "walkforward_rollup",
        "generated_at": utc_now().isoformat(),
        "champion": {
            "model_id": args.champion_model_id,
            "dir": str(champion_dir),
            **champion_profile,
        },
        "challenger": {
            "specs": [
                {"campaign_id": cid or None, "model_id": mid} for cid, mid in specs
            ],
            "segments": segments,
        },
        "paired": paired,
        "delta_summary": delta_summary,
        "monthly": {
            "basis": "paired_dates",
            "challenger": monthly_ic_stats(paired["challenger_ic"]),
            "champion": monthly_ic_stats(paired["champion_ic"]),
        },
        "turnover": {
            "basis": "paired_dates",
            "k": int(args.k),
            "challenger": annualized_turnover(
                c_pair, k=args.k, round_trip_cost=cost_challenger
            ),
            "champion": annualized_turnover(
                h_pair, k=args.k, round_trip_cost=cost_champion
            ),
        },
        "warnings": warnings,
    }
    return report, EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description="vintage 回放拼接与 champion/challenger 配对检验（设计 §5.1）"
    )
    parser.add_argument("--champion-model-id", required=True)
    parser.add_argument("--challenger-model-id", action="append", default=[])
    parser.add_argument("--campaign-id", action="append", default=[])
    parser.add_argument("--champion-dir", default=None)
    parser.add_argument("--challenger-dir", default=None)
    parser.add_argument("--k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--out", default=None, help="evidence JSON 落盘路径（默认只打印）")
    args = parser.parse_args()

    report, code = build_report_sync(args)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out and code == EXIT_OK:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
