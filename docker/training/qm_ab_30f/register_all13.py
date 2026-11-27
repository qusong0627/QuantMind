"""把 30 特征 13 模型训练产物注册到 CN 模型库。

用法（在 API 容器内）：
    docker exec quantmind python /app/docker/training/qm_ab_30f/register_all13.py

前置：训练产物在 /data/qm_ab_30f/all13/（result.json + metadata.json + 各模型文件）。
本脚本把该目录链接为 /data/training_jobs/{run_id}（注册器的任务目录），
调用 ModelRegistryService.register_model_from_training_run 注册父模型，
再调用 split_multi_model_entries 拆成 13 条单算法记录。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import yaml

RUN_ID = "qm_ab30_all13_20261127"
TENANT_ID = "default"
USER_ID = "10000001"
SRC = Path("/data/qm_ab_30f/all13")
JOB = Path("/data/training_jobs") / RUN_ID
CFG_PATH = Path("/app/docker/training/qm_ab_30f/all13.yaml")


def _link_job_dir() -> None:
    if not (SRC / "result.json").is_file():
        raise SystemExit(f"训练产物缺失：{SRC / 'result.json'}")
    JOB.parent.mkdir(parents=True, exist_ok=True)
    if JOB.is_symlink():
        if JOB.resolve() == SRC.resolve():
            return
        JOB.unlink()
    elif JOB.exists():
        # 已存在真实目录（可能是上一次失败的注册）：保留，不覆盖用户数据
        print(f"[warn] {JOB} 已存在（非 symlink），直接复用")
        return
    os.symlink(SRC, JOB, target_is_directory=True)
    print(f"[ok] {JOB} -> {SRC}")


def _payloads() -> tuple[dict, dict]:
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    label = cfg.get("label") or {}
    payload = {
        **cfg,
        "display_name": str(cfg.get("job_name") or RUN_ID),
        "target_horizon_days": label.get("target_horizon_days"),
        "target_mode": label.get("target_mode"),
        "label_formula": label.get("label_formula") or "",
        "training_window": label.get("training_window") or "",
    }
    payload["context"] = {
        **(cfg.get("context") or {}),
        "market": "CN",
        "benchmark": (cfg.get("context") or {}).get("benchmark") or "SH000300",
    }
    result = json.loads((SRC / "result.json").read_text(encoding="utf-8"))
    # 注册器从顶层 metrics 取样本外指标；多模型结果若只放在 metadata.metrics，
    # 这里补一份，避免被软门禁误判为「缺少 test_rank_icir」。
    if not isinstance(result.get("metrics"), dict) or not result.get("metrics"):
        meta_metrics = (result.get("metadata") or {}).get("metrics")
        if isinstance(meta_metrics, dict) and meta_metrics:
            result["metrics"] = meta_metrics
    return payload, result


async def main() -> None:
    from backend.shared.model_registry import model_registry_service

    _link_job_dir()
    payload, result = _payloads()
    print("[info] run_id=", RUN_ID, "status=", result.get("status"))
    reg = await model_registry_service.register_model_from_training_run(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        request_payload=payload,
        result_payload=result,
    )
    print("[registration]", json.dumps(reg, ensure_ascii=False, indent=2, default=str)[:4000])
    model_id = str((reg or {}).get("model_id") or "")
    status = str((reg or {}).get("status") or "")
    if model_id and status == "ready":
        children = await model_registry_service.split_multi_model_entries(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            model_id=model_id,
        )
        print("[children]", json.dumps(children, ensure_ascii=False, indent=2, default=str)[:12000])
    else:
        print("[warn] 父模型未 ready，跳过拆分：", model_id, status)


if __name__ == "__main__":
    asyncio.run(main())
