#!/usr/bin/env python3
"""把日频特征选择结果注册进训练目录并提交 T+3 / T+5 两个平台训练 job。

流程（全部走已在运行的 API，不绕过闸门）：
1. 读选择 JSON（top 100/200 + 逐条理由）；
2. POST /api/v1/admin/training-data/register-from-research（写各来源库草稿）；
3. 逐个 POST /versions/{id}/publish（草稿 seed 自线上版本，发布=替换不丢老特征）；
4. 解析锚库 l1_factors 的活动发布版本；组装 features（`库:列` 限定）+ versions；
5. POST /api/v1/models/run-training（factor_selection.method='' 关闭自动裁剪，
   auto_feature_filter=false），显式 2016-2024 / 2025 / 2026 split；
6. T+3 完成后再提交 T+5（平台单飞锁，串行）。

用法（宿主机，API 8000 可达）：
    INTERNAL_CALL_SECRET=... python3 backend/scripts/stock_classifier/submit_daily_model_jobs.py \
        --selection data/stock_classifier/feature_selection/daily_feature_selection_2016_2024.json \
        --top 100 --submit
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests

API = "http://127.0.0.1:8000"
USER = "10000001"
TENANT = "default"
HEADERS = {
    "X-User-Id": USER,
    "X-Tenant-Id": TENANT,
    "Content-Type": "application/json",
}


def _session(secret: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({**HEADERS, "X-Internal-Call": secret})
    return s


def _post(s: requests.Session, path: str, body: dict | None = None) -> dict:
    r = s.post(f"{API}{path}", json=body or {}, timeout=300)
    if r.status_code >= 400:
        raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def _get(s: requests.Session, path: str) -> dict:
    r = s.get(f"{API}{path}", timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def _anchor_version(s: requests.Session, anchor: str) -> str:
    data = _get(
        s,
        f"/api/v1/admin/training-data/versions?market=CN&source_dataset={anchor}"
        "&status=published&limit=1",
    )
    versions = data.get("versions") or []
    if not versions:
        raise RuntimeError(f"锚库 {anchor} 没有已发布目录版本")
    return str(versions[0]["version_id"])


def _submit(
    s: requests.Session,
    *,
    horizon: int,
    features: list[str],
    versions: dict[str, str],
    anchor: str,
    name: str,
) -> str:
    payload = {
        "display_name": name,
        "job_name": name,
        "model_type": "lightgbm",
        "train_start": "2016-01-04",
        "train_end": "2024-12-31",
        "valid_start": "2025-01-01",
        "valid_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-11-05",
        "target_horizon_days": horizon,
        "target_mode": "return",
        "execution_price": "open",
        "label_transform": "rank",
        "features": features,
        "factor_source": anchor,
        "factor_catalog_version": versions[anchor],
        # 锚库走 factor_catalog_version，副库走这个 map（与既有跨库脚本同口径）
        "factor_catalog_versions": {
            lib: vid for lib, vid in versions.items() if lib != anchor
        },
        # 选中的特征全量入模：关闭平台默认的 IC/ICIR top-80 裁剪
        "auto_feature_filter": "false",
        "factor_selection": {
            "method": "",
            "n_top": len(features),
            "ic_threshold": 0.01,
            "icir_threshold": 0.15,
            "correlation_threshold": 0.9,
            "pfs_enabled": False,
            "dh_enabled": False,
        },
        "num_boost_round": 1000,
        "early_stopping_rounds": 100,
        "max_time_minutes": 360,
        # 不暂停本机其他非 quantmind 前缀容器（本机收益可忽略、误伤风险实存）
        "pause_others": False,
        "explain": {
            "enable_shap": True,
            "shap_split": "valid",
            "shap_sample_rows": 30000,
        },
        "required_artifacts": [
            "model.lgb",
            "pred.parquet",
            "pred.pkl",
            "metadata.json",
            "config.yaml",
            "result.json",
        ],
        "context": {"market": "CN", "benchmark": "SH000300"},
        "initial_capital": 1000000,
        "benchmark": "SH000300",
        "commission_rate": 0.0003,
        "slippage": 0.0,
        "deal_price": "close",
    }
    out = _post(s, "/api/v1/models/run-training", payload)
    run_id = out.get("runId")
    if not run_id:
        raise RuntimeError(f"提交未返回 runId: {out}")
    missing = out.get("missingFeatures") or []
    if missing:
        print(f"[warn] {len(missing)} 个特征未通过预检（前 10：{missing[:10]}）")
    print(f"[submit] h{horizon} run_id={run_id} valid={out.get('validFeatureCount')}")
    return str(run_id)


def _wait(s: requests.Session, run_id: str, timeout_sec: int = 21600) -> dict:
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout_sec:
        try:
            d = _get(s, f"/api/v1/models/training-runs/{run_id}")
        except Exception as exc:  # noqa: BLE001 - 轮询瞬断不终止
            print(f"[poll] {run_id} 读取失败，重试: {exc}")
            time.sleep(20)
            continue
        status = str(d.get("status") or "")
        progress = d.get("progress")
        if (status, progress) != (last, d.get("progress")):
            print(f"[poll] {run_id} status={status} progress={progress}")
            last = status
        if status in (
            "completed",
            "succeeded",
            "success",
            "failed",
            "cancelled",
            "canceled",
        ):
            return d
        time.sleep(30)
    raise TimeoutError(f"训练 {run_id} 超过 {timeout_sec}s 未完成")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", required=True)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument(
        "--submit", action="store_true", help="真正提交；不传只打印载荷预览"
    )
    ap.add_argument(
        "--out", default="data/stock_classifier/feature_selection/model_jobs.json"
    )
    args = ap.parse_args()
    secret = os.environ.get("INTERNAL_CALL_SECRET", "").strip()
    if not secret:
        raise SystemExit("缺少 INTERNAL_CALL_SECRET")
    sel = json.loads(Path(args.selection).read_text(encoding="utf-8"))
    entries = sel["selected"][str(args.top)]
    # top100 可能因注册跳过/相关性少于 100：注册 top200，按 100→200 顺序补足
    entries_wide = sel["selected"].get("200") or entries
    ordered = list(entries) + [
        e for e in entries_wide if e["code"] not in {x["code"] for x in entries}
    ]
    codes = [e["code"] for e in ordered]
    s = _session(secret)

    # 注册 top200（若存在）以覆盖 top100 的跳过项
    reg_codes = codes
    reg = _post(
        s,
        "/api/v1/admin/training-data/register-from-research",
        {"market": "CN", "dataset": "private", "codes": reg_codes},
    )
    registered = {r["code"]: r for r in reg.get("registered", [])}
    skipped = reg.get("skipped", [])
    if skipped:
        print(f"[register] 跳过 {len(skipped)}：{skipped[:5]}")
    versions = {str(k): str(v) for k, v in (reg.get("versions") or {}).items()}
    # 只发布「真有已注册映射」的来源库：空草稿 publish 会被闸门拒绝
    # （A published catalog needs at least one enabled factor）。
    libs_registered = {str(r["source_dataset"]) for r in registered.values()}
    versions = {lib: vid for lib, vid in versions.items() if lib in libs_registered}
    for vid in sorted(set(versions.values())):
        pub = _post(s, f"/api/v1/admin/training-data/versions/{vid}/publish")
        print(f"[publish] {vid} -> {pub.get('status')}")

    anchor = "l1_factors"
    if anchor not in versions:
        versions[anchor] = _anchor_version(s, anchor)
    missing_top = [e["code"] for e in entries if e["code"] not in registered]
    if missing_top:
        print(
            f"[warn] top{args.top} 中 {len(missing_top)} 个未注册，"
            f"将由 top200 补齐：{missing_top[:10]}"
        )
    feats = [
        f"{registered[e['code']]['source_dataset']}:{registered[e['code']]['feature_key']}"
        for e in ordered
        if e["code"] in registered
    ][: args.top]
    if len(feats) < max(20, args.top // 2):
        raise RuntimeError(
            f"可用特征太少：{len(feats)}/{args.top}（检查刷新字段/注册跳过）"
        )
    libs = sorted({f.split(":", 1)[0] for f in feats})
    print(f"[features] {len(feats)}/{args.top} 可用；来源库 {libs}")
    if not args.submit:
        print(
            json.dumps(
                {"features": feats, "versions": versions, "anchor": anchor},
                ensure_ascii=False,
                indent=1,
            )[:4000]
        )
        return 0

    active = _get(s, "/api/v1/models/training-runs/active")
    active_status = str((active or {}).get("status") or "")
    if active and active_status in (
        "pending",
        "provisioning",
        "running",
        "waiting_callback",
    ):
        print(
            f"[wait] 已有训练在跑：{active.get('runId') or active.get('run_id')} "
            f"status={active_status}，等它结束后再提交（单飞锁）"
        )
        # 不自动取消别人的任务；有活动任务时直接退出，由用户决定
        return 2

    jobs: dict[str, dict] = {}
    for horizon in (3, 5):
        run_id = _submit(
            s,
            horizon=horizon,
            features=feats,
            versions=versions,
            anchor=anchor,
            name=f"CN日频 T+{horizon} · 2016-2024训/2025验/2026测 · sel{args.top}",
        )
        jobs[f"t{horizon}"] = {"run_id": run_id}
        result = _wait(s, run_id)
        jobs[f"t{horizon}"]["status"] = result.get("status")
        jobs[f"t{horizon}"]["result"] = result
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(
                {
                    "selection": args.selection,
                    "top": args.top,
                    "features": feats,
                    "versions": versions,
                    "jobs": jobs,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"[done] h{horizon} status={result.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
