#!/usr/bin/env python3
"""特征选择台账（registry）与自动经验/坑生成器。

设计目标：让每一次「选特征 → 训练 → 样本外」都留下可对账的记录，并自动
提炼经验与坑，但**不自动改门槛/配额**——自动学习只学到「观察」，参数变更
必须由人升 protocol_version 后重跑，避免在评估同一段数据上越调越过拟合。

目录结构（root 默认 ``/data/stock_classifier/feature_selection/registry``）::

    registry/
      index.json          # 每次 run 的摘要
      features.json       # 每个特征跨 run 的历史（成长视图）
      lessons.json        # 合并后的经验/坑清单
      lessons.jsonl       # 追加式原始事件（审计用）
      lessons.md          # 人读版
      runs/<run_id>.json  # 单次 run 全量记录（选中特征+选择指标+OOS+audit）
      runs/<run_id>_oos.csv  # 特征级 OOS 明细（attach-oos 时写）

口径：
- 经验/坑只从 ``valid`` 窗口生成；``test`` 窗口只做最终核验，``do_not_apply_to``
  恒含 ``"test"``。
- 同一条经验跨 >= 2 个独立 run 复现才标 ``confirmed``，单次只标 ``open``。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_PROTOCOL_VERSION = "v1"
PROTOCOL_FILE = Path(__file__).with_name("protocol_v1.json")
DEFAULT_REGISTRY = Path("/data/stock_classifier/feature_selection/registry")

# 选择指标 → (取值键, 方向, 目标口径)
# 方向 +1 表示越大越好，-1 表示越小越好；target=signed 对 OOS ICIR 符号，
# target=abs 只对 OOS |ICIR|（强度）。
_METRIC_SPECS: dict[str, tuple[str, int, str]] = {
    "selection_ic5": ("ic5", 1, "signed"),
    "selection_icir5": ("icir5", 1, "signed"),
    "selection_ic1_cc": ("ic1_cc", 1, "signed"),
    "selection_pos5": ("pos5", 1, "abs"),
    "selection_coverage": ("coverage", 1, "abs"),
    "selection_gain_rank_inv": ("gain_rank", -1, "abs"),
    "selection_cap_sign_agree": ("cap_sign_agree", 1, "abs"),
    "selection_cap_mean": ("cap_ic", 1, "signed"),
}

_MIN_FEATURE_OOS = 0.15
_MIN_GROUP_N = 5
_MIN_LIBRARY_N = 5
_MIN_METRIC_N = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _to_float(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def make_run_id(generated_at: str, top: int, existing: set[str] | None = None) -> str:
    """生成稳定 run_id；同一秒同 top 重复跑时追加 _2/_3。"""
    try:
        dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    base = f"sel_{dt:%Y%m%d_%H%M%S}_top{int(top)}"
    if not existing or base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def protocol_hash(protocol: dict | None) -> str:
    raw = json.dumps(protocol or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_protocol_snapshot(path: Path | None = None) -> dict:
    target = Path(path) if path else PROTOCOL_FILE
    return json.loads(target.read_text(encoding="utf-8"))


def _cap_sign_agree(selection: dict) -> int | None:
    cap = selection.get("cap_ic")
    ic5 = _to_float(selection.get("ic5"))
    if not isinstance(cap, list) or ic5 is None:
        return None
    sign = 1 if ic5 >= 0 else -1
    hits = [
        1 if (_to_float(x) is not None and (1 if float(x) >= 0 else -1) == sign) else 0
        for x in cap
    ]
    return int(sum(hits))


def _cap_mean(selection: dict) -> float | None:
    cap = selection.get("cap_ic")
    if not isinstance(cap, list):
        return None
    vals = [_to_float(x) for x in cap]
    vals = [x for x in vals if x is not None]
    return sum(vals) / len(vals) if vals else None


def _reason_counts(rejected: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rejected:
        reason = str((item or {}).get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def normalize_model_jobs(jobs: dict | None) -> dict:
    """把 submit 脚本的 model_jobs.json（或裸 horizon map）压成台账结构。"""
    if not isinstance(jobs, dict):
        return {}
    raw = jobs.get("jobs") if isinstance(jobs.get("jobs"), dict) else jobs
    out: dict[str, dict] = {}
    for horizon, job in raw.items():
        if not isinstance(job, dict):
            continue
        run_id = job.get("run_id") or job.get("runId")
        result = job.get("result")
        if isinstance(result, dict):
            result = {k: v for k, v in result.items() if k != "logs"}
        out[str(horizon)] = {
            "run_id": str(run_id) if run_id else "",
            "status": str(job.get("status") or ""),
            "result": result if isinstance(result, dict) else {},
        }
    return out


def build_run_record(
    selection: dict,
    top: int,
    protocol_snapshot: dict | None = None,
    model_jobs: dict | None = None,
    existing_ids: set[str] | None = None,
    data_issues: list[dict] | None = None,
) -> dict:
    """选特征输出 → 台账 run 记录（纯函数，不改入参）。"""
    top_key = str(int(top))
    entries = (selection.get("selected") or {}).get(top_key)
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"selection 缺少 selected[{top_key}]")
    metrics = selection.get("metrics") or {}
    protocol = deepcopy(protocol_snapshot or {})
    selection_protocol = deepcopy(selection.get("protocol") or {})

    features: list[dict] = []
    for entry in entries:
        code = str(entry.get("code") or "")
        if not code:
            continue
        metric = deepcopy(metrics.get(code) or {})
        chosen = {k: v for k, v in metric.items() if k not in ("group", "library")}
        if chosen.get("cap_sign_agree") is None:
            agree = _cap_sign_agree(chosen)
            if agree is not None:
                chosen["cap_sign_agree"] = agree
        if chosen.get("cap_mean") is None:
            cap_mean = _cap_mean(chosen)
            if cap_mean is not None:
                chosen["cap_mean"] = cap_mean
        features.append(
            {
                "code": code,
                "group": str(entry.get("group") or metric.get("group") or ""),
                "library": str(entry.get("library") or metric.get("library") or ""),
                "source": str(entry.get("source") or ""),
                "selection": chosen,
                "oos": {},
                "lifecycle": "selected",
            }
        )

    rejected = deepcopy(selection.get("rejected") or [])
    windows = deepcopy(protocol.get("windows") or {})
    if not windows:
        windows = {k: selection_protocol.get(k) for k in ("train", "valid", "test")}
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": make_run_id(
            str(selection.get("generated_at") or ""), top, existing=existing_ids
        ),
        "created_at": _now(),
        "generated_at": str(selection.get("generated_at") or ""),
        "protocol_version": str(
            protocol.get("protocol_version") or DEFAULT_PROTOCOL_VERSION
        ),
        "protocol_hash": protocol_hash(protocol),
        "protocol": protocol,
        "selection_protocol": selection_protocol,
        "selection_label": str(
            selection_protocol.get("selection_label")
            or protocol.get("selection_label")
            or ""
        ),
        "windows": windows,
        "selected_top": int(top),
        "features": features,
        "rejected_count": len(rejected),
        "rejected_reasons": _reason_counts(rejected),
        "model_jobs": normalize_model_jobs(model_jobs or {}),
        "data_issues": deepcopy(data_issues or []),
        "oos_meta": {},
        "audit": {},
        "calibration": {
            "allowed": False,
            "tuning_window": "valid",
            "test_window": "report_only",
            "min_runs_for_weight_update": 3,
            "reason": (
                "P0 只记录与诊断，不自动改门槛/权重；权重校准需独立 run 累积后"
                "人工升 protocol_version。"
            ),
        },
    }


def merge_model_jobs(record: dict, jobs: dict) -> dict:
    """把训练 job 合并进台账（返回新对象，不改原记录）。"""
    out = deepcopy(record)
    current = out.setdefault("model_jobs", {})
    for horizon, job in normalize_model_jobs(jobs).items():
        merged = dict(current.get(horizon) or {})
        merged.update({k: v for k, v in job.items() if v not in (None, "")})
        current[horizon] = merged
    return out


def merge_oos(record: dict, oos: dict) -> dict:
    """把 OOS 指标按 code 挂到特征上；未知 code 忽略。"""
    out = deepcopy(record)
    valid = oos.get("valid") or {}
    test = oos.get("test") or {}
    for feature in out.get("features") or []:
        code = feature["code"]
        attached = {
            window: payload[code]
            for window, payload in (("valid", valid), ("test", test))
            if code in payload
        }
        if attached:
            feature["oos"] = attached
    out["oos_meta"] = deepcopy(oos.get("meta") or {})
    return out


def _rank_average(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if den <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / den


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """平均秩 Spearman；样本 < 3 或方差为 0 返回 None。"""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return _pearson(_rank_average(xs), _rank_average(ys))


def _metric_value(selection: dict, key: str) -> float | None:
    if key == "cap_ic":
        return _cap_mean(selection)
    return _to_float(selection.get(key))


def audit_window(record: dict, window: str) -> dict:
    """选择指标 vs OOS 表现的诊断（不调参）。"""
    rows: list[dict] = []
    missing = 0
    for feature in record.get("features") or []:
        selection = feature.get("selection") or {}
        oos = (feature.get("oos") or {}).get(window) or {}
        sel_icir = _to_float(selection.get("icir5"))
        oos_icir = _to_float(oos.get("icir5"))
        if sel_icir is None or oos_icir is None:
            missing += 1
            continue
        rows.append(
            {
                "code": feature["code"],
                "group": feature.get("group", ""),
                "library": feature.get("library", ""),
                "sel_icir": sel_icir,
                "oos_icir": oos_icir,
                "abs_sel": abs(sel_icir),
                "abs_oos": abs(oos_icir),
                "same_sign": (sel_icir >= 0) == (oos_icir >= 0),
            }
        )

    n = len(rows)
    if n == 0:
        return {"window": window, "n": 0, "missing": missing}

    def _bucket(key: str) -> list[dict]:
        buckets: dict[str, list[dict]] = {}
        for row in rows:
            buckets.setdefault(str(row[key]), []).append(row)
        out = []
        for name, group_rows in sorted(buckets.items()):
            out.append(
                {
                    "name": name,
                    "n": len(group_rows),
                    "sign_hit_rate": sum(1 for r in group_rows if r["same_sign"])
                    / len(group_rows),
                    "spearman_signed": spearman(
                        [r["sel_icir"] for r in group_rows],
                        [r["oos_icir"] for r in group_rows],
                    ),
                    "mean_abs_oos": sum(r["abs_oos"] for r in group_rows)
                    / len(group_rows),
                }
            )
        return out

    ordered = sorted(rows, key=lambda r: r["abs_sel"], reverse=True)
    top_n = max(1, int(math.ceil(n * 0.25)))
    bottom = ordered[-top_n:]
    top = ordered[:top_n]

    effectiveness: dict[str, dict] = {}
    for name, (key, direction, target) in _METRIC_SPECS.items():
        pairs = []
        for feature in record.get("features") or []:
            selection = feature.get("selection") or {}
            oos = (feature.get("oos") or {}).get(window) or {}
            value = _metric_value(selection, key)
            oos_icir = _to_float(oos.get("icir5"))
            if value is None or oos_icir is None:
                continue
            pairs.append((value * direction, oos_icir))
        if len(pairs) < _MIN_METRIC_N:
            continue
        xs = [p[0] for p in pairs]
        ys_signed = [p[1] for p in pairs]
        ys_abs = [abs(p[1]) for p in pairs]
        if target == "signed":
            rho_signed = spearman(xs, ys_signed)
            rho_abs = spearman([abs(x) for x in xs], ys_abs)
        else:
            rho_signed = spearman(xs, ys_signed)
            rho_abs = spearman(xs, ys_abs)
        effectiveness[name] = {
            "direction": direction,
            "target": target,
            "n": len(pairs),
            "spearman_signed": rho_signed,
            "spearman_abs": rho_abs,
        }

    return {
        "window": window,
        "n": n,
        "missing": missing,
        "mean_icir": sum(r["oos_icir"] for r in rows) / n,
        "mean_abs_icir": sum(r["abs_oos"] for r in rows) / n,
        "median_abs_icir": sorted(r["abs_oos"] for r in rows)[n // 2],
        "sign_hit_rate": sum(1 for r in rows if r["same_sign"]) / n,
        "spearman_signed": spearman(
            [r["sel_icir"] for r in rows], [r["oos_icir"] for r in rows]
        ),
        "spearman_abs": spearman(
            [r["abs_sel"] for r in rows], [r["abs_oos"] for r in rows]
        ),
        "top_quartile_mean_abs_oos": sum(r["abs_oos"] for r in top) / len(top),
        "bottom_quartile_mean_abs_oos": sum(r["abs_oos"] for r in bottom) / len(bottom),
        "by_group": _bucket("group"),
        "by_library": _bucket("library"),
        "metric_effectiveness": effectiveness,
    }


def _lesson(
    record: dict,
    *,
    kind: str,
    scope: str,
    key: str,
    statement: str,
    evidence: dict,
    confidence: str,
) -> dict:
    raw = f"{kind}|{scope}|{key}|{record.get('run_id')}"
    lesson_id = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return {
        "lesson_id": lesson_id,
        "run_id": record.get("run_id"),
        "created_at": _now(),
        "protocol_version": record.get("protocol_version"),
        "window": "valid",
        "kind": kind,
        "scope": scope,
        "key": key,
        "statement": statement,
        "evidence": evidence,
        "confidence": confidence,
        "status": "open",
        "times_seen": 1,
        "seen_in_runs": [record.get("run_id")],
        "do_not_apply_to": ["test"],
    }


def _confidence(*, strong: bool, n: int) -> str:
    if strong and n >= 20:
        return "high"
    if n >= 10:
        return "medium"
    return "low"


def build_lessons(record: dict, window: str = "valid") -> list[dict]:
    """从 valid 窗口自动提炼经验/坑；test 窗口永不参与（防过拟合）。"""
    if window != "valid":
        return []
    audit = audit_window(record, window)
    if audit.get("n", 0) == 0:
        return []

    lessons: list[dict] = []
    for feature in record.get("features") or []:
        selection = feature.get("selection") or {}
        oos = (feature.get("oos") or {}).get(window) or {}
        sel = _to_float(selection.get("icir5"))
        out = _to_float(oos.get("icir5"))
        if sel is None or out is None:
            continue
        same_sign = (sel >= 0) == (out >= 0)
        if not same_sign and abs(out) >= _MIN_FEATURE_OOS:
            lessons.append(
                _lesson(
                    record,
                    kind="pitfall",
                    scope="feature",
                    key=feature["code"],
                    statement=(
                        f"选择窗 ICIR {sel:+.2f}，验证窗 OOS ICIR {out:+.2f}，方向翻转"
                    ),
                    evidence={"selection_icir5": sel, "oos_icir5": out},
                    confidence=_confidence(
                        strong=abs(sel) >= 0.3 and abs(out) >= 0.3,
                        n=int(oos.get("n") or 0),
                    ),
                )
            )
        elif same_sign and abs(out) >= 0.5 * abs(sel):
            lessons.append(
                _lesson(
                    record,
                    kind="lesson",
                    scope="feature",
                    key=feature["code"],
                    statement=(
                        f"选择窗 ICIR {sel:+.2f}，验证窗 OOS ICIR {out:+.2f}，方向与强度都保留"
                    ),
                    evidence={"selection_icir5": sel, "oos_icir5": out},
                    confidence=_confidence(
                        strong=abs(out) >= 0.3, n=int(oos.get("n") or 0)
                    ),
                )
            )
        elif same_sign:
            lessons.append(
                _lesson(
                    record,
                    kind="lesson",
                    scope="feature",
                    key=feature["code"],
                    statement=(
                        f"选择窗 ICIR {sel:+.2f}，验证窗 OOS ICIR {out:+.2f}，方向保留但强度衰减过半"
                    ),
                    evidence={"selection_icir5": sel, "oos_icir5": out},
                    confidence=_confidence(
                        strong=abs(sel) >= 0.3, n=int(oos.get("n") or 0)
                    ),
                )
            )

    for bucket_name, scope, min_n in (
        ("by_group", "group", _MIN_GROUP_N),
        ("by_library", "library", _MIN_LIBRARY_N),
    ):
        for bucket in audit.get(bucket_name) or []:
            if bucket["n"] < min_n:
                continue
            hit = bucket["sign_hit_rate"]
            if hit < 0.5:
                kind, verdict = "pitfall", "方向命中率低于掷硬币"
            elif hit > 0.65:
                kind, verdict = "lesson", "方向命中率显著高于掷硬币"
            else:
                continue
            lessons.append(
                _lesson(
                    record,
                    kind=kind,
                    scope=scope,
                    key=bucket["name"],
                    statement=(
                        f"{bucket['name']} 的 {bucket['n']} 个特征在验证窗 {verdict}"
                        f"（同号率 {hit:.0%}）"
                    ),
                    evidence={
                        "n": bucket["n"],
                        "sign_hit_rate": hit,
                        "spearman_signed": bucket["spearman_signed"],
                    },
                    confidence=_confidence(
                        strong=hit < 0.35 or hit > 0.75, n=bucket["n"]
                    ),
                )
            )

    for name, metric in (audit.get("metric_effectiveness") or {}).items():
        rho_signed = metric.get("spearman_signed")
        rho_abs = metric.get("spearman_abs")
        n_metric = int(metric.get("n") or 0)
        if n_metric < _MIN_METRIC_N:
            continue
        if rho_signed is not None and rho_signed >= 0.3:
            lessons.append(
                _lesson(
                    record,
                    kind="lesson",
                    scope="metric",
                    key=name,
                    statement=(
                        f"{name} 与验证窗 OOS ICIR 正相关（Spearman {rho_signed:+.2f}），"
                        "可作为下轮排序信号"
                    ),
                    evidence={"spearman_signed": rho_signed, "spearman_abs": rho_abs},
                    confidence=_confidence(strong=rho_signed >= 0.5, n=n_metric),
                )
            )
        elif rho_signed is not None and rho_signed <= -0.2:
            lessons.append(
                _lesson(
                    record,
                    kind="pitfall",
                    scope="metric",
                    key=name,
                    statement=(
                        f"{name} 与验证窗 OOS ICIR 负相关（Spearman {rho_signed:+.2f}），"
                        "当前排序方向可能反了"
                    ),
                    evidence={"spearman_signed": rho_signed, "spearman_abs": rho_abs},
                    confidence=_confidence(strong=rho_signed <= -0.4, n=n_metric),
                )
            )
        if rho_abs is not None and rho_abs <= 0.1:
            lessons.append(
                _lesson(
                    record,
                    kind="lesson",
                    scope="metric",
                    key=name,
                    statement=(
                        f"{name} 对验证窗 OOS 强度几乎没有预测力（Spearman {rho_abs:+.2f}），"
                        "下轮不应单独依赖它"
                    ),
                    evidence={"spearman_abs": rho_abs, "n": n_metric},
                    confidence=_confidence(strong=rho_abs <= 0.0, n=n_metric),
                )
            )

    for issue in record.get("data_issues") or []:
        lessons.append(
            _lesson(
                record,
                kind="pitfall",
                scope="data",
                key=str(issue.get("key") or "unknown"),
                statement=str(issue.get("statement") or ""),
                evidence=deepcopy(issue.get("evidence") or {}),
                confidence=str(issue.get("confidence") or "medium"),
            )
        )
    return lessons


def merge_lessons(existing: list[dict], new: list[dict]) -> list[dict]:
    """按 (kind, scope, key) 合并；跨 run 复现 >=2 次标 confirmed。"""
    out = [deepcopy(x) for x in existing or []]
    index = {(x.get("kind"), x.get("scope"), x.get("key")): x for x in out}
    for lesson in new or []:
        key = (lesson.get("kind"), lesson.get("scope"), lesson.get("key"))
        current = index.get(key)
        if current is None:
            item = deepcopy(lesson)
            seen_new = list(item.get("seen_in_runs") or [])
            if item.get("run_id") and item["run_id"] not in seen_new:
                seen_new.append(item["run_id"])
            item["seen_in_runs"] = seen_new
            item["times_seen"] = len(seen_new) or 1
            item["status"] = "open"
            out.append(item)
            index[key] = item
            continue
        seen = list(current.get("seen_in_runs") or [])
        run_ids = list(lesson.get("seen_in_runs") or [])
        if lesson.get("run_id") and lesson["run_id"] not in run_ids:
            run_ids.append(lesson["run_id"])
        for run_id in run_ids:
            if run_id and run_id not in seen:
                seen.append(run_id)
        current["seen_in_runs"] = seen
        current["times_seen"] = len(seen)
        current["status"] = "confirmed" if len(seen) >= 2 else "open"
        current["evidence"] = deepcopy(lesson.get("evidence") or {})
        current["statement"] = lesson.get("statement") or current.get("statement")
        current["confidence"] = lesson.get("confidence") or current.get("confidence")
        current["updated_at"] = _now()
    out.sort(key=lambda x: str(x.get("created_at") or ""))
    return out


def _sign(value: float | None) -> str:
    if value is None:
        return "—"
    return "+" if value >= 0 else "−"


def render_audit_md(record: dict, out: Path) -> None:
    """单次 run 的 OOS 审计报告。"""
    lines = [
        f"# 特征选择台账：{record.get('run_id')}",
        "",
        f"> 协议 {record.get('protocol_version')}（hash {record.get('protocol_hash')}）"
        f"　选择标签 {record.get('selection_label')}　Top {record.get('selected_top')}",
        "",
        "## 一、窗口与任务",
        "",
        f"- 训练：{(record.get('windows') or {}).get('train')}",
        f"- 验证：{(record.get('windows') or {}).get('valid')}（经验只从这里生成）",
        f"- 测试：{(record.get('windows') or {}).get('test')}（**只报不调**）",
        "",
        "| Horizon | run_id | 状态 |",
        "|---|---|---|",
    ]
    for horizon, job in sorted((record.get("model_jobs") or {}).items()):
        lines.append(
            f"| {horizon} | `{job.get('run_id') or '—'}` | {job.get('status') or '—'} |"
        )
    eval_rows = []
    for horizon, job in sorted((record.get("model_jobs") or {}).items()):
        evaluation = job.get("eval") or {}
        split = evaluation.get("by_split") or {}
        if not split:
            continue
        train = split.get("train") or {}
        valid = split.get("valid") or {}
        test = split.get("test") or {}
        long_short = evaluation.get("long_short") or {}
        eval_rows.append(
            f"| {horizon} | {_fmt(train.get('mean'))} | {_fmt(valid.get('mean'))} | "
            f"{_fmt(test.get('mean'))} | {_fmt(test.get('icir'))} | "
            f"{_pct(test.get('win_rate'))} | {_fmt(long_short.get('sharpe'))} | "
            f"{_pct(long_short.get('max_drawdown'))} | "
            f"{_fmt(evaluation.get('groups_monotonicity'))} |"
        )
    if eval_rows:
        lines += [
            "",
            "### 模型级 OOS（eval_report）",
            "",
            "| Horizon | train rankIC | valid rankIC | test rankIC | test ICIR | test win | 全样本 LS Sharpe | LS MaxDD | groups mono |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ] + eval_rows
    audit_valid = (record.get("audit") or {}).get("valid") or {}
    audit_test = (record.get("audit") or {}).get("test") or {}
    lines += [
        "",
        "## 二、OOS 总览",
        "",
        "| 窗口 | n | 缺失 | 同号率 | 选择 vs OOS 符号 Spearman | 强度 Spearman | 平均 |OOS ICIR| |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, audit in (("valid（学习）", audit_valid), ("test（只报）", audit_test)):
        if not audit.get("n"):
            continue
        lines.append(
            f"| {name} | {audit['n']} | {audit.get('missing', 0)} | "
            f"{audit.get('sign_hit_rate', 0):.0%} | "
            f"{_num(audit.get('spearman_signed'))} | {_num(audit.get('spearman_abs'))} | "
            f"{audit.get('mean_abs_icir', 0):.3f} |"
        )
    lines += [
        "",
        "## 三、选择指标有没有预测力（valid）",
        "",
        "| 指标 | n | 与 OOS ICIR 符号相关 | 与 OOS 强度相关 | 方向 | 目标 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name, metric in sorted((audit_valid.get("metric_effectiveness") or {}).items()):
        lines.append(
            f"| {name} | {metric['n']} | {_num(metric.get('spearman_signed'))} | "
            f"{_num(metric.get('spearman_abs'))} | {metric['direction']:+d} | "
            f"{metric['target']} |"
        )
    lines += [
        "",
        "## 四、分组/分库的同号率（valid）",
        "",
        "| 维度 | 名称 | n | 同号率 | Spearman | 平均 |OOS ICIR| |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for dim, key in (("类别", "by_group"), ("来源库", "by_library")):
        for bucket in audit_valid.get(key) or []:
            lines.append(
                f"| {dim} | {bucket['name']} | {bucket['n']} | "
                f"{bucket['sign_hit_rate']:.0%} | {_num(bucket.get('spearman_signed'))} | "
                f"{bucket.get('mean_abs_oos', 0):.3f} |"
            )
    lines += [
        "",
        "## 五、特征明细",
        "",
        "| 因子 | 类别 | 库 | 选择 ICIR | valid OOS ICIR | test OOS ICIR | 符号 | gain 排名 |",
        "|---|---|---|---:|---:|---:|:---:|---:|",
    ]
    for feature in record.get("features") or []:
        selection = feature.get("selection") or {}
        oos = feature.get("oos") or {}
        sel = _to_float(selection.get("icir5"))
        valid = _to_float((oos.get("valid") or {}).get("icir5"))
        test = _to_float((oos.get("test") or {}).get("icir5"))
        same = (
            "—"
            if sel is None or valid is None
            else ("✓" if (sel >= 0) == (valid >= 0) else "✗")
        )
        lines.append(
            f"| `{feature['code']}` | {feature.get('group')} | {feature.get('library')} | "
            f"{_fmt(sel)} | {_fmt(valid)} | {_fmt(test)} | {same} | "
            f"{selection.get('gain_rank', '—')} |"
        )
    lines += [
        "",
        "## 六、防过拟合声明",
        "",
        "- 经验/坑只从 **valid** 窗口生成；test 窗口只做最终核验，不参与任何排序或调参。",
        "- 本报告不自动改门槛/配额；改协议必须升 `protocol_version` 并重跑。",
        "- 单次 run 的经验只标 `open`；跨 >= 2 个独立 run 复现才标 `confirmed`。",
        "",
    ]
    _write_text(out, "\n".join(lines))


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:+.3f}"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def render_lessons_md(lessons: list[dict], out: Path) -> None:
    def _table(items: list[dict]) -> list[str]:
        rows = [
            "| 状态 | 类型 | 范围 | 对象 | 结论 | 置信度 | 出现次数 |",
            "|---|---|---|---|---|---|---:|",
        ]
        for item in items:
            rows.append(
                f"| {item.get('status')} | {item.get('kind')} | {item.get('scope')} | "
                f"`{item.get('key')}` | {item.get('statement')} | "
                f"{item.get('confidence')} | {item.get('times_seen', 1)} |"
            )
        return rows

    confirmed = [x for x in lessons if x.get("status") == "confirmed"]
    pitfalls = [x for x in lessons if x.get("kind") == "pitfall"]
    seen_ids: set[str] = set()
    priority: list[dict] = []
    for item in sorted(
        pitfalls + confirmed,
        key=lambda x: (
            x.get("kind") != "pitfall",
            {"high": 0, "medium": 1, "low": 2}.get(str(x.get("confidence")), 3),
            -int(x.get("times_seen") or 1),
        ),
    ):
        lesson_id = str(item.get("lesson_id") or item.get("key"))
        if lesson_id in seen_ids:
            continue
        seen_ids.add(lesson_id)
        priority.append(item)
    non_feature_open = [
        x
        for x in lessons
        if x.get("status") == "open"
        and x.get("kind") == "lesson"
        and x.get("scope") in ("metric", "group", "library", "data")
    ]
    feature_open = [
        x for x in lessons if x.get("status") == "open" and x.get("scope") == "feature"
    ]
    feature_open.sort(
        key=lambda x: -abs(float((x.get("evidence") or {}).get("oos_icir5") or 0))
    )

    lines = [
        "# 特征选择经验与坑（自动生成）",
        "",
        "> 只从验证窗生成；test 只做最终核验。单次 `open`，跨 run 复现才 `confirmed`。",
        "",
        "## 概览",
        "",
        f"- 经验 {sum(1 for x in lessons if x.get('kind') == 'lesson')} 条，"
        f"坑 {len(pitfalls)} 条；"
        f"`confirmed` {len(confirmed)} 条，`open` {sum(1 for x in lessons if x.get('status') == 'open')} 条。",
        f"- 特征级 {sum(1 for x in lessons if x.get('scope') == 'feature')} 条，"
        f"类别/库/指标/数据级 {sum(1 for x in lessons if x.get('scope') != 'feature')} 条。",
        "- 完整清单（含 evidence）见 `lessons.json`；这里是按严重度排序的摘要。",
        "",
        "## 一、优先处理：confirmed + 所有坑",
        "",
    ]
    lines += _table(priority[:60]) if priority else ["（暂无）"]
    lines += ["", "## 二、指标/类别/库级 open 经验", ""]
    lines += _table(non_feature_open) if non_feature_open else ["（暂无）"]
    lines += ["", "## 三、特征级 open（按 |OOS ICIR| 前 30）", ""]
    lines += _table(feature_open[:30]) if feature_open else ["（暂无）"]
    lines += [
        "",
        "## 怎么用",
        "",
        "1. `confirmed` 的 pitfall 优先处理；`open` 的先看 evidence，不急着改协议。",
        "2. 任何门槛/权重变更都要升 `protocol_version`，并在新 run 里用验证窗复核。",
        "3. test 窗口的数字只用于最终验收，不能拿来挑经验。",
        "",
    ]
    _write_text(out, "\n".join(lines))


def load_model_eval(run_id: str, root: Path | str = "/data/training_jobs") -> dict:
    """从训练产物目录读取 eval_report.json / shap_summary.csv 摘要。"""
    base = Path(root) / str(run_id)
    out: dict = {}
    eval_path = base / "eval_report.json"
    if eval_path.exists():
        report = json.loads(eval_path.read_text(encoding="utf-8"))
        out["by_split"] = report.get("by_split") or {}
        out["rank_ic"] = report.get("rank_ic") or {}
        out["long_short"] = {
            key: value
            for key, value in (report.get("long_short") or {}).items()
            if key != "curve"
        }
        out["groups_monotonicity"] = (report.get("groups") or {}).get("monotonicity")
        out["n_days"] = report.get("n_days")
    shap_path = base / "shap_summary.csv"
    if shap_path.exists():
        with shap_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        out["shap_top"] = [
            {
                "feature": row.get("feature"),
                "mean_abs_shap": _to_float(row.get("mean_abs_shap")),
            }
            for row in rows[:20]
        ]
    return out


def _enrich_model_jobs(record: dict, model_root: Path | str) -> dict:
    for job in (record.get("model_jobs") or {}).values():
        run_id = str(job.get("run_id") or "")
        if not run_id:
            continue
        try:
            evaluation = load_model_eval(run_id, model_root)
        except Exception:  # noqa: BLE001 - 产物缺失不影响台账
            continue
        if evaluation:
            job["eval"] = evaluation
    return record


class SelectionRegistry:
    """文件台账：runs/*.json + index.json + features.json + lessons.*。"""

    def __init__(self, root: Path | str = DEFAULT_REGISTRY):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def _run_path(self, run_id: str) -> Path:
        return self.root / "runs" / f"{run_id}.json"

    def get(self, run_id: str) -> dict:
        return json.loads(self._run_path(run_id).read_text(encoding="utf-8"))

    def list_runs(self) -> list[dict]:
        index = self.root / "index.json"
        if not index.exists():
            return []
        return json.loads(index.read_text(encoding="utf-8")).get("runs") or []

    def register(
        self,
        selection: dict,
        top: int,
        protocol_snapshot: dict | None = None,
        model_jobs: dict | None = None,
        data_issues: list[dict] | None = None,
    ) -> dict:
        existing = {p.stem for p in (self.root / "runs").glob("*.json")}
        record = build_run_record(
            selection,
            top=top,
            protocol_snapshot=protocol_snapshot,
            model_jobs=model_jobs,
            existing_ids=existing,
            data_issues=data_issues,
        )
        prior = {
            str(run.get("protocol_version")): run.get("protocol_hash")
            for run in self.list_runs()
        }
        prior_hash = prior.get(str(record.get("protocol_version")))
        if prior_hash and prior_hash != record.get("protocol_hash"):
            record["protocol_drift"] = {
                "version": record.get("protocol_version"),
                "expected_hash": prior_hash,
                "actual_hash": record.get("protocol_hash"),
                "warning": "同一 protocol_version 的协议内容发生变化；应升版本号。",
            }
        self._write_run(record)
        return record

    def upsert(
        self,
        selection: dict,
        top: int,
        protocol_snapshot: dict | None = None,
        model_jobs: dict | None = None,
        data_issues: list[dict] | None = None,
    ) -> dict:
        """幂等登记：同一 generated_at+top 的 run 已存在时合并，不新建 _2。"""
        base_id = make_run_id(str(selection.get("generated_at") or ""), top)
        if not self._run_path(base_id).exists():
            return self.register(
                selection,
                top=top,
                protocol_snapshot=protocol_snapshot,
                model_jobs=model_jobs,
                data_issues=data_issues,
            )
        record = merge_model_jobs(self.get(base_id), model_jobs or {})
        if protocol_snapshot:
            new_hash = protocol_hash(protocol_snapshot)
            old_hash = record.get("protocol_hash")
            if old_hash and old_hash != new_hash:
                record["protocol_drift"] = {
                    "version": str(
                        protocol_snapshot.get("protocol_version")
                        or DEFAULT_PROTOCOL_VERSION
                    ),
                    "expected_hash": old_hash,
                    "actual_hash": new_hash,
                    "warning": "同一 protocol_version 的协议内容发生变化；应升版本号。",
                }
            record["protocol"] = deepcopy(protocol_snapshot)
            record["protocol_version"] = str(
                protocol_snapshot.get("protocol_version") or DEFAULT_PROTOCOL_VERSION
            )
            record["protocol_hash"] = new_hash
        if data_issues:
            merged_issues = {
                str(x.get("key") or ""): x for x in (record.get("data_issues") or [])
            }
            for issue in data_issues:
                merged_issues[str(issue.get("key") or "")] = issue
            record["data_issues"] = list(merged_issues.values())
        self._write_run(record)
        return record

    def attach_jobs(
        self, run_id: str, jobs: dict, model_root: str | Path = "/data/training_jobs"
    ) -> dict:
        record = merge_model_jobs(self.get(run_id), jobs)
        record = _enrich_model_jobs(record, model_root)
        self._write_run(record)
        return record

    def attach_oos(self, run_id: str, oos: dict) -> dict:
        record = merge_oos(self.get(run_id), oos)
        record["audit"] = {
            window: audit_window(record, window)
            for window in ("valid", "test")
            if any((f.get("oos") or {}).get(window) for f in record["features"])
        }
        self._write_run(record)
        self._write_oos_csv(record)
        return record

    def add_lessons(self, run_id: str, lessons: list[dict]) -> list[dict]:
        merged = merge_lessons(self._load_lessons(), lessons)
        _atomic_write_json(
            self.root / "lessons.json",
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": _now(),
                "lessons": merged,
            },
        )
        with (self.root / "lessons.jsonl").open("a", encoding="utf-8") as handle:
            for lesson in lessons or []:
                handle.write(
                    json.dumps(
                        {"run_id": run_id, **lesson}, ensure_ascii=False, default=str
                    )
                    + "\n"
                )
        render_lessons_md(merged, self.root / "lessons.md")
        return merged

    def _load_lessons(self) -> list[dict]:
        path = self.root / "lessons.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("lessons") or []

    def _write_run(self, record: dict) -> None:
        _atomic_write_json(self._run_path(record["run_id"]), record)
        self._rebuild()

    def _write_oos_csv(self, record: dict) -> None:
        path = self.root / "runs" / f"{record['run_id']}_oos.csv"
        fields = [
            "code",
            "group",
            "library",
            "selection_ic5",
            "selection_icir5",
            "oos_valid_ic5",
            "oos_valid_icir5",
            "oos_test_ic5",
            "oos_test_icir5",
            "oos_valid_n",
            "oos_test_n",
        ]
        rows = []
        for feature in record.get("features") or []:
            selection = feature.get("selection") or {}
            valid = (feature.get("oos") or {}).get("valid") or {}
            test = (feature.get("oos") or {}).get("test") or {}
            rows.append(
                {
                    "code": feature["code"],
                    "group": feature.get("group", ""),
                    "library": feature.get("library", ""),
                    "selection_ic5": selection.get("ic5"),
                    "selection_icir5": selection.get("icir5"),
                    "oos_valid_ic5": valid.get("ic5"),
                    "oos_valid_icir5": valid.get("icir5"),
                    "oos_test_ic5": test.get("ic5"),
                    "oos_test_icir5": test.get("icir5"),
                    "oos_valid_n": valid.get("n"),
                    "oos_test_n": test.get("n"),
                }
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def _rebuild(self) -> None:
        runs: list[dict] = []
        features: dict[str, dict] = {}
        for path in sorted((self.root / "runs").glob("*.json")):
            if path.name.endswith("_oos.csv"):
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            run_id = record.get("run_id") or path.stem
            audit = record.get("audit") or {}
            oos_valid = audit.get("valid") or {}
            runs.append(
                {
                    "run_id": run_id,
                    "created_at": record.get("created_at"),
                    "protocol_version": record.get("protocol_version"),
                    "protocol_hash": record.get("protocol_hash"),
                    "selection_label": record.get("selection_label"),
                    "top": record.get("selected_top"),
                    "n_features": len(record.get("features") or []),
                    "windows": record.get("windows") or {},
                    "model_jobs": {
                        h: {"run_id": j.get("run_id"), "status": j.get("status")}
                        for h, j in (record.get("model_jobs") or {}).items()
                    },
                    "oos_status": _oos_status(record),
                    "valid_sign_hit_rate": oos_valid.get("sign_hit_rate"),
                    "valid_spearman_signed": oos_valid.get("spearman_signed"),
                }
            )
            for feature in record.get("features") or []:
                entry = features.setdefault(
                    feature["code"],
                    {
                        "code": feature["code"],
                        "library": feature.get("library", ""),
                        "group": feature.get("group", ""),
                        "runs": [],
                        "history": [],
                        "latest": {},
                        "lifecycle": feature.get("lifecycle", "selected"),
                    },
                )
                if run_id not in entry["runs"]:
                    entry["runs"].append(run_id)
                selection = feature.get("selection") or {}
                oos = feature.get("oos") or {}
                history = {
                    "run_id": run_id,
                    "selection_icir5": selection.get("icir5"),
                    "selection_ic5": selection.get("ic5"),
                    "oos_valid_icir5": (oos.get("valid") or {}).get("icir5"),
                    "oos_test_icir5": (oos.get("test") or {}).get("icir5"),
                }
                existing_history = [
                    h for h in entry["history"] if h.get("run_id") != run_id
                ]
                entry["history"] = existing_history + [history]
                entry["latest"] = history
        _atomic_write_json(
            self.root / "index.json",
            {"schema_version": SCHEMA_VERSION, "updated_at": _now(), "runs": runs},
        )
        _atomic_write_json(
            self.root / "features.json",
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": _now(),
                "features": dict(sorted(features.items())),
            },
        )


def _oos_status(record: dict) -> str:
    features = record.get("features") or []
    if not features:
        return "none"
    done = sum(1 for f in features if (f.get("oos") or {}).get("valid"))
    if done == 0:
        return "none"
    return "done" if done == len(features) else "partial"


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="特征选择台账 / 自动经验坑")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register", help="登记一次选特征结果")
    p.add_argument("--selection", required=True)
    p.add_argument("--top", type=int, default=100)
    p.add_argument("--protocol", default=str(PROTOCOL_FILE))
    p.add_argument("--jobs", default="", help="submit 脚本产出的 model_jobs.json")
    p.add_argument("--data-issues", default="", help="数据问题的 JSON 列表")

    p = sub.add_parser("attach-jobs", help="补挂/更新训练 job")
    p.add_argument("--run-id", required=True)
    p.add_argument("--jobs", required=True)

    p = sub.add_parser("attach-oos", help="挂 OOS 指标并生成审计")
    p.add_argument("--run-id", required=True)
    p.add_argument("--oos", required=True)

    p = sub.add_parser("learn", help="从 valid 窗口自动生成经验/坑")
    p.add_argument("--run-id", required=True)

    p = sub.add_parser("audit", help="渲染单次 run 审计报告")
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)

    sub.add_parser("list", help="列出所有 run")

    args = ap.parse_args()
    store = SelectionRegistry(args.registry)

    if args.cmd == "register":
        data_issues = _load_json(args.data_issues) if args.data_issues else None
        record = store.register(
            _load_json(args.selection),
            top=args.top,
            protocol_snapshot=_load_json(args.protocol),
            model_jobs=_load_json(args.jobs) if args.jobs else None,
            data_issues=data_issues,
        )
        print(
            f"registered {record['run_id']} features={len(record['features'])} "
            f"protocol={record['protocol_version']} hash={record['protocol_hash']}"
        )
    elif args.cmd == "attach-jobs":
        record = store.attach_jobs(args.run_id, _load_json(args.jobs))
        print(f"attached jobs -> {record['run_id']} {sorted(record['model_jobs'])}")
    elif args.cmd == "attach-oos":
        record = store.attach_oos(args.run_id, _load_json(args.oos))
        audit = (record.get("audit") or {}).get("valid") or {}
        print(
            f"attached oos -> {record['run_id']} valid n={audit.get('n', 0)} "
            f"sign_hit={audit.get('sign_hit_rate')} "
            f"spearman={audit.get('spearman_signed')}"
        )
    elif args.cmd == "learn":
        record = store.get(args.run_id)
        lessons = build_lessons(record, window="valid")
        merged = store.add_lessons(args.run_id, lessons)
        fresh = sum(1 for x in lessons if x.get("status") == "open")
        print(
            f"learned {len(lessons)} (new/open {fresh}); ledger {len(merged)} "
            f"open={sum(1 for x in merged if x.get('status') == 'open')} "
            f"confirmed={sum(1 for x in merged if x.get('status') == 'confirmed')}"
        )
    elif args.cmd == "audit":
        render_audit_md(store.get(args.run_id), Path(args.out))
        print(f"wrote {args.out}")
    elif args.cmd == "list":
        runs = store.list_runs()
        if not runs:
            print("(empty)")
        for run in runs:
            print(
                f"{run['run_id']}  {run.get('protocol_version')}  "
                f"n={run.get('n_features')}  oos={run.get('oos_status')}  "
                f"jobs={','.join(sorted((run.get('model_jobs') or {}).keys()))}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
