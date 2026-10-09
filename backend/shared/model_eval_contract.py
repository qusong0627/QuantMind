"""模型对比口径（P0-1）与样本外评估契约（P0-4）——读取侧唯一实现。

背景（缺陷实测见 docs/滚动训练与模型生命周期_设计方案.md §2.2）：
- **P0-1**：`POST /models/compare` 读 `model["metadata"]`，而注册表
  `_row_to_model`（model_registry.py）返回的键是 `metadata_json`/`metrics_json`
  → 对比指标恒空。本模块的 ``compare_metrics_of`` / ``features_of`` 是
  对比接口的唯一取值实现（读注册表真实键，容错旧形状）。
- **P0-4**：训练 headline IC 是 train+valid+test **合并窗口**（实测虚高
  1.47×），跨模型族比 ICIR 不成立。唯一可做决策的样本外口径是
  ``metadata.eval_report.by_split.test``（mean IC + t + 天数）；
  扁平 ``metrics.test_*`` 仅作老批次回退且标记 ``is_oos_verified=False``；
  headline / train / valid 段**永不**作为决策输入。
- **产物独立性**：HK「13 子模型复制」事件——指标逐位相同 + pred 产物一致
  即复制品，进入对比/晋升前必须先用 ``artifact_independence`` 指认。

本模块除 ``file_md5`` 外全部是纯函数；不抛异常、缺失一律返回 None，
由消费方决定拒绝/降级（缺失绝不显示成 0）。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OOS_SOURCE_BY_SPLIT = "eval_report.by_split.test"
OOS_SOURCE_FLAT = "metrics.test_*"

# 独立性指纹固定键集：与 docker/training/train.py 的 metrics 字典键一一对应。
# 固定键集（而非整 dict 序列化）避免无关字段（score_direction 等）噪声，
# 且保证「逐位相同」判据跨版本稳定。
_FINGERPRINT_KEYS: tuple[str, ...] = (
    "train_ic",
    "train_rank_ic",
    "train_rank_icir",
    "val_ic",
    "val_rank_ic",
    "val_rank_icir",
    "test_ic",
    "test_rank_ic",
    "test_rank_icir",
)

_MD5_CHUNK = 1 << 20  # 1 MiB


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    """数值抽取：bool 不是数；无法转 float 返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _first_num(sources: list[dict[str, Any]], key: str) -> float | None:
    for src in sources:
        v = _num(src.get(key))
        if v is not None:
            return v
    return None


# ── P0-1：注册表形状取值 ─────────────────────────────────────────────────────


def _registry_metadata(model: dict[str, Any] | None) -> dict[str, Any]:
    """注册表行 → metadata dict。优先 `metadata_json`（真实键），容错旧形状。"""
    m = _as_dict(model)
    return _as_dict(m.get("metadata_json")) or _as_dict(m.get("metadata"))


def compare_metrics_of(model: dict[str, Any] | None) -> dict[str, Any]:
    """对比表指标（P0-1）：读注册表真实键，旧形状（metadata）兼容回退。"""
    m = _as_dict(model)
    md = _registry_metadata(m)
    metrics = _as_dict(m.get("metrics_json")) or _as_dict(md.get("metrics"))
    return {
        "val_ic": metrics.get("val_ic"),
        "val_rank_ic": metrics.get("val_rank_ic"),
        "val_rank_icir": metrics.get("val_rank_icir"),
        "test_ic": metrics.get("test_ic"),
        "test_rank_ic": metrics.get("test_rank_ic"),
        "test_rank_icir": metrics.get("test_rank_icir"),
        "model_type": md.get("model_type"),
        "target_horizon_days": md.get("target_horizon_days"),
        "feature_count": md.get("feature_count"),
        "created_at": str(m.get("created_at") or "")[:19],
        "is_default": bool(m.get("is_default")),
        "status": m.get("status"),
    }


def features_of(model: dict[str, Any] | None) -> set[str]:
    """模型特征列表（P0-1）：注册表 metadata_json.features / feature_columns。"""
    md = _registry_metadata(model)
    feats = md.get("features") or md.get("feature_columns") or []
    if not isinstance(feats, (list, tuple, set)):
        return set()
    return {str(f) for f in feats}


# ── P0-4：样本外口径 ────────────────────────────────────────────────────────


def resolve_oos_metrics(
    metadata: dict[str, Any] | None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """样本外指标解析（唯一口径）：by_split.test 优先 → 扁平 test_* 回退。

    Returns
    -------
    dict : ``{rank_ic, rank_icir, t_stat, n_days, win_rate, source, is_oos_verified}``
        - ``source`` = ``OOS_SOURCE_BY_SPLIT`` 时 ``is_oos_verified=True``
          （contract 列齐全，可进决策/闸门）；
        - ``source`` = ``OOS_SOURCE_FLAT`` 为老批次回退（无 t/天数，
          不进晋升证据链）；
        - 全缺失 → ``source=None``，各值 None（缺失绝不回退 headline）。
    """
    md = _as_dict(metadata)
    mj = _as_dict(metrics)

    # 1) 契约口径：eval_report.by_split.test
    eval_report = _as_dict(md.get("eval_report"))
    by_split = _as_dict(eval_report.get("by_split"))
    test_seg = _as_dict(by_split.get("test"))
    rank_ic = _num(test_seg.get("mean"))
    if rank_ic is not None:
        return {
            "rank_ic": rank_ic,
            "rank_icir": _num(test_seg.get("icir")),
            "t_stat": _num(test_seg.get("t_stat")),
            "n_days": _num(test_seg.get("n_days")),
            "win_rate": _num(test_seg.get("win_rate")),
            "source": OOS_SOURCE_BY_SPLIT,
            "is_oos_verified": True,
        }

    # 2) 老批次回退：扁平 test_*（metrics_json → metadata.metrics → metadata）
    flat_sources = [mj, _as_dict(md.get("metrics")), md]
    flat_ic = _first_num(flat_sources, "test_rank_ic")
    if flat_ic is not None:
        return {
            "rank_ic": flat_ic,
            "rank_icir": _first_num(flat_sources, "test_rank_icir"),
            "t_stat": None,
            "n_days": None,
            "win_rate": None,
            "source": OOS_SOURCE_FLAT,
            "is_oos_verified": False,
        }

    # 3) 缺失：绝不回退 headline（合并窗口）/ train / valid 段
    return {
        "rank_ic": None,
        "rank_icir": None,
        "t_stat": None,
        "n_days": None,
        "win_rate": None,
        "source": None,
        "is_oos_verified": False,
    }


# ── 产物独立性预检 ──────────────────────────────────────────────────────────


def metrics_bitwise_equal(
    metrics_a: dict[str, Any] | None, metrics_b: dict[str, Any] | None
) -> bool | None:
    """固定指纹键逐位比较；无共同可比键返回 None（不可判定）。"""
    a, b = _as_dict(metrics_a), _as_dict(metrics_b)
    comparable = 0
    for key in _FINGERPRINT_KEYS:
        if key not in a or key not in b:
            continue
        comparable += 1
        va, vb = a.get(key), b.get(key)
        if va is None and vb is None:
            continue
        fa, fb = _num(va), _num(vb)
        if fa is None or fb is None or fa != fb:
            return False
    return True if comparable else None


def file_md5(path: str | Path | None) -> str | None:
    """流式 md5；路径缺失/不可读返回 None（不抛出）。"""
    if not path:
        return None
    p = Path(path)
    try:
        if not p.is_file():
            return None
        digest = hashlib.md5()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(_MD5_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        logger.warning("file_md5 读取失败: %s (%s)", path, exc)
        return None


def resolve_pred_path(model: dict[str, Any] | None) -> Path | None:
    """模型存储目录下的 pred.parquet 路径；不存在返回 None。"""
    md = _registry_metadata(_as_dict(model))
    storage = str(_as_dict(model).get("storage_path") or "").strip()
    if not storage:
        return None
    base = Path(storage)
    if not base.is_absolute():
        base = Path("/app") / base
    candidate = base / "pred.parquet"
    if candidate.is_file():
        return candidate
    # 少数批次 pred 落在 metadata.files 声明处
    files = _as_dict(md.get("files"))
    declared = files.get("pred") or files.get("pred.parquet")
    if declared:
        p = Path(str(declared))
        if p.is_file():
            return p
    return None


def artifact_independence(
    side_a: dict[str, Any], side_b: dict[str, Any]
) -> dict[str, Any]:
    """产物独立性裁决（纯函数）。

    Parameters
    ----------
    side_a / side_b : dict
        各含 ``model_id``、``metrics``（原始指标 dict，可为 None）、
        ``pred_md5``（可为 None）。

    Returns
    -------
    dict : ``{verdict, reasons, metrics_equal, pred_md5_equal, pred_md5_a, pred_md5_b}``

    裁决语义（G1 准入证据）：
    - ``identical``：指标逐位相同 ∧ pred md5 相同 → 复制品，拒绝；
    - ``metrics_copied``：指标逐位相同（md5 缺失或不同）→ 疑似指标复制，拒绝；
    - ``pred_copied``：md5 相同但指标不同 → 异常（同产物异指标），需人工查；
    - ``independent``：指标不同 ∧ (md5 不同 or 缺失) → 通过；
    - ``inconclusive``：既无可比指标又无 md5 → 不可判定（不得当通过用）。
    """
    id_a = str(_as_dict(side_a).get("model_id") or "a")
    id_b = str(_as_dict(side_b).get("model_id") or "b")
    metrics_equal = metrics_bitwise_equal(
        _as_dict(side_a).get("metrics"), _as_dict(side_b).get("metrics")
    )
    md5_a = _as_dict(side_a).get("pred_md5")
    md5_b = _as_dict(side_b).get("pred_md5")
    md5_a = str(md5_a) if md5_a else None
    md5_b = str(md5_b) if md5_b else None
    md5_equal = (md5_a == md5_b) if (md5_a and md5_b) else None

    reasons: list[str] = []
    if metrics_equal is True:
        reasons.append(f"指标指纹逐位相同（{id_a} vs {id_b}）")
    elif metrics_equal is False:
        reasons.append("指标指纹不同")
    else:
        reasons.append("指标指纹不可比（无共同键）")
    if md5_equal is True:
        reasons.append("pred.parquet md5 相同")
    elif md5_equal is False:
        reasons.append("pred.parquet md5 不同")
    else:
        reasons.append("pred.parquet md5 缺失，无法比对")

    if metrics_equal is True and md5_equal is True:
        verdict = "identical"
        reasons.append("裁决=复制品：拒绝进入对比/晋升")
    elif metrics_equal is True:
        verdict = "metrics_copied"
        reasons.append("裁决=疑似指标复制：拒绝进入对比/晋升")
    elif md5_equal is True:
        verdict = "pred_copied"
        reasons.append("裁决=同 pred 产物但指标不同：需人工核查")
    elif metrics_equal is False:
        verdict = "independent"
        reasons.append("裁决=相互独立：通过独立性预检")
    else:
        verdict = "inconclusive"
        reasons.append("裁决=不可判定：不得作为通过证据")

    return {
        "verdict": verdict,
        "reasons": reasons,
        "metrics_equal": metrics_equal,
        "pred_md5_equal": md5_equal,
        "pred_md5_a": md5_a,
        "pred_md5_b": md5_b,
    }
