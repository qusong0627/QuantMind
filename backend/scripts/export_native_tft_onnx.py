#!/usr/bin/env python3
"""NativeTFT .pth → model.onnx 导出（实时推理链进的唯一门）。

背景：实时推理链（``inference/realtime_service.py::_ensure_session``）只吃
``model_dir/model.onnx``；而 ``onnx_exporter`` 的导出链**不含 pytorch**（其注释：
"pytorch 走训练侧 torch.onnx.export 携带模型类"）。NativeTFT 的模型类源码就在
``inference/native_tft_model.py``（批量推理重建用的同一份），本脚本用它把训练产出的
``model.pth`` 导出成实时链可加载的 ONNX——补上的正是"训练侧导出"那一步
（历史模型训练时没做，于是实时链切到 NativeTFT 时会在导出环节报
「框架不在导出链: pytorch」）。

输入契约（与 ``realtime_core.compute_cycle`` 的时序装配对齐）：
  input  float32 [batch, step_len, d_feat]   （batch 动态；step_len/d_feat 来自 metadata）
  output float32 [batch]

导出后**自带对拍自检**：onnxruntime 加载 + 随机输入 torch/ort 输出逐值比对
（max|Δ| 超门限即视为导出不可信，删除产物并退出非零）。绝不产出"能加载但算错"的 onnx。

用法：
  python3 backend/scripts/export_native_tft_onnx.py <model_dir> [--force] [--opset 17]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

TOLERANCE = 1e-4


def _load_meta(model_dir: Path) -> dict:
    path = model_dir / "metadata.json"
    if not path.is_file():
        raise SystemExit(f"metadata.json 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def export(model_dir: Path, *, opset: int = 17, force: bool = False) -> Path:
    import torch

    from backend.services.engine.inference.native_tft_model import (
        load_native_tft_state_dict,
    )
    from backend.services.engine.inference.realtime_core import sequence_len_of

    out_path = model_dir / "model.onnx"
    if out_path.is_file() and not force:
        raise SystemExit(f"已存在 {out_path}（需覆盖加 --force）")
    weight_path = model_dir / "model.pth"
    if not weight_path.is_file():
        raise SystemExit(f"权重不存在: {weight_path}")
    meta = _load_meta(model_dir)
    if str(meta.get("model_type") or "").lower() != "nativetft":
        raise SystemExit(f"model_type={meta.get('model_type')!r} 不是 nativetft，本脚本不适用")
    step_len = sequence_len_of(meta)
    input_dim = int(
        (meta.get("model_arch") or {}).get("input_dim")
        or len(meta.get("feature_columns") or [])
    )
    if input_dim <= 0:
        raise SystemExit("metadata 缺 input_dim（model_arch.input_dim / feature_columns 均不可用）")

    predictor = load_native_tft_state_dict(str(weight_path), meta)
    net = predictor.model
    net.eval()

    dummy = torch.zeros(1, step_len, input_dim, dtype=torch.float32)
    kwargs = {
        "input_names": ["input"],
        "output_names": ["output"],
        "dynamic_axes": {"input": {0: "batch"}, "output": {0: "batch"}},
        "opset_version": int(opset),
    }
    try:
        torch.onnx.export(net, dummy, str(out_path), dynamo=False, **kwargs)
    except TypeError:
        # 旧版 torch 无 dynamo 开关（默认即 TorchScript 导出器）
        torch.onnx.export(net, dummy, str(out_path), **kwargs)

    # 自检：加载 + 对拍（能加载但算错的 onnx 比没有还危险——静默喂错分数）
    import onnxruntime as ort

    session = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
    shape = session.get_inputs()[0].shape
    rng = np.random.default_rng(20261008)
    sample = rng.standard_normal((8, step_len, input_dim)).astype(np.float32)
    got = np.asarray(session.run(None, {"input": sample})[0], dtype=np.float32).reshape(-1)
    with torch.no_grad():
        want = net(torch.from_numpy(sample)).detach().cpu().numpy().reshape(-1)
    if got.shape != want.shape:
        out_path.unlink(missing_ok=True)
        raise SystemExit(f"对拍失败：输出形状 {got.shape} ≠ torch {want.shape}（已删除产物）")
    diff = float(np.max(np.abs(got - want)))
    if not np.isfinite(diff) or diff > TOLERANCE:
        out_path.unlink(missing_ok=True)
        raise SystemExit(f"对拍失败：max|Δ|={diff:.3e} > {TOLERANCE}（已删除产物）")

    print(f"[ok] 导出 {out_path}")
    print(f"[ok] input shape={shape}（动态维为符号）output={session.get_outputs()[0].shape}")
    print(f"[ok] torch/ort 对拍 max|Δ|={diff:.2e}（n=8, step_len={step_len}, d={input_dim}）")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="NativeTFT .pth → model.onnx（实时链用）")
    ap.add_argument("model_dir", type=Path)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 model.onnx")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()
    export(args.model_dir, opset=args.opset, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
