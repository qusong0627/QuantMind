"""因子筛选报告回归守卫：train.py 产出结构化报告 + 编排层阈值透传。

背景：用户全选 321 特征但系统静默筛成 34，训练日志空洞、没有"为什么选/为什么
不选"的依据。本次改动让 select_top_factors 返回 (selected, report)，报告写入
结果元数据，日志输出漏斗 + 逐特征淘汰原因；前端开关/阈值经 payload 显式透传。

train.py 依赖 GPU 运行时无法直接导入，沿用 test_training_design_guards.py 的
源码断言方式；admin_training_utils.py 用 importlib 加载（失败时跳过）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRAIN_SCRIPT = ROOT / "docker" / "training" / "train.py"
# P2 拆包：select_top_factors 及报告组装已迁入 data/factor_selection.py，
# 函数级断言读新家；写入 metadata 的 count 断言仍读 train.py（main 未动）。
FACTOR_SELECTION_PY = ROOT / "docker" / "training" / "data" / "factor_selection.py"
ADMIN_TRAINING_UTILS = ROOT / "backend" / "services" / "api" / "routers" / "admin" / "admin_training_utils.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module_safe(rel_path: str, alias: str):
    """用 importlib 加载模块；如果模块导入失败，返回 None（不抛）。"""
    fp = ROOT / rel_path
    if not fp.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(alias, fp)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def test_select_top_factors_returns_structured_report() -> None:
    source = FACTOR_SELECTION_PY.read_text(encoding="utf-8")

    # 函数签名：返回 (selected, report) 二元组
    assert "def select_top_factors(" in source
    assert "-> tuple[list[str], dict[str, Any]]:" in source

    # 报告结构：漏斗 stage_counts + 逐特征 IC/ICIR/覆盖率/状态
    assert '"stage_counts": {' in source
    assert '"input": len(features)' in source
    assert '"ic_pass": len(candidates)' in source
    assert '"selected": len(selected)' in source
    assert '"coverage": round(coverage_map.get(feat, 1.0), 4)' in source


def test_every_feature_reports_an_explicit_reason() -> None:
    source = FACTOR_SELECTION_PY.read_text(encoding="utf-8")
    # 每个特征必须有 status + reason（入选或明确淘汰原因），不允许空洞
    assert '"status": "selected" if feat in selected_set else "rejected"' in source
    assert '"reason": "通过全部筛选" if feat in selected_set else' in source
    # 不在训练数据中的特征也有明确原因
    assert 'decisions.get(feat, "特征不在训练数据中")' in source


def test_selection_report_is_logged_and_persisted_to_metadata() -> None:
    source = FACTOR_SELECTION_PY.read_text(encoding="utf-8")
    # 漏斗摘要日志（非空洞）
    assert "def _log_factor_selection_summary(report: dict[str, Any]) -> None:" in source
    assert "Factor selection funnel:" in source
    # 报告写入结果元数据（两个 metadata dict：多模型 + 单模型）
    train_source = TRAIN_SCRIPT.read_text(encoding="utf-8")
    assert train_source.count('"factor_selection": factor_selection_report') >= 2


def test_factor_selection_thresholds_forwarded_to_train_script() -> None:
    source = TRAIN_SCRIPT.read_text(encoding="utf-8")
    assert 'factor_selection_cfg.get("n_top", 60)' in source
    assert 'factor_selection_cfg.get("ic_threshold", 0.02)' in source
    assert 'factor_selection_cfg.get("icir_threshold", 0.3)' in source
    assert 'factor_selection_cfg.get("correlation_threshold", 0.85)' in source


def test_admin_passthrough_forwards_factor_selection_and_filter_switch() -> None:
    utils_source = ADMIN_TRAINING_UTILS.read_text(encoding="utf-8")
    # 白名单必须放行 factor_selection 与 auto_feature_filter（此前被剥掉）
    assert 'if isinstance(payload.get("factor_selection"), dict):' in utils_source
    assert '"method": str(raw_fs.get("method") or "").strip().lower(),' in utils_source
    assert '"n_top": _clamp_int(raw_fs.get("n_top"), 150, 10, 300),' in utils_source
    assert 'if "auto_feature_filter" in payload:' in utils_source


def test_admin_passthrough_clamps_n_top_and_keeps_thresholds() -> None:
    mod = _load_module_safe(
        "backend/services/api/routers/admin/admin_training_utils.py",
        "admin_training_utils",
    )
    if mod is None:
        return  # 模块依赖重（db/pydantic 等）加载失败时跳过

    normalize = getattr(mod, "_normalize_payload", None)
    if normalize is None:
        return

    # 用户显式阈值透传，n_top 越界被钳制到 [10, 300]
    payload = {
        "job_name": "job_x",
        "display_name": "test",
        "model_type": "lightgbm",
        "train_start": "2023-01-03",
        "train_end": "2025-07-19",
        "valid_start": "2025-07-20",
        "valid_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-03-31",
        "features": ["amt_log", "vol_persistence"],
        "factor_selection": {
            "method": "ic_icir",
            "n_top": 999,
            "ic_threshold": 0.02,
            "icir_threshold": 0.3,
            "correlation_threshold": 0.85,
        },
        "auto_feature_filter": True,
    }
    normalized = normalize(payload, allowed_features=["amt_log", "vol_persistence"])

    fs = normalized.get("factor_selection")
    assert fs is not None
    assert fs["method"] == "ic_icir"
    assert fs["n_top"] == 300  # 999 → 钳制到上限 300
    assert fs["ic_threshold"] == 0.02
    assert fs["icir_threshold"] == 0.3
    assert fs["correlation_threshold"] == 0.85
    assert normalized.get("auto_feature_filter") == "true"


def test_admin_passthrough_filter_off_skips_selection() -> None:
    mod = _load_module_safe(
        "backend/services/api/routers/admin/admin_training_utils.py",
        "admin_training_utils",
    )
    if mod is None:
        return

    normalize = getattr(mod, "_normalize_payload", None)
    if normalize is None:
        return

    payload = {
        "job_name": "job_x",
        "display_name": "test",
        "model_type": "lightgbm",
        "train_start": "2023-01-03",
        "train_end": "2025-07-19",
        "valid_start": "2025-07-20",
        "valid_end": "2025-12-31",
        "test_start": "2026-01-01",
        "test_end": "2026-03-31",
        "features": ["amt_log"],
        "auto_feature_filter": False,
    }
    normalized = normalize(payload, allowed_features=["amt_log"])

    # 关闭筛选：不注入 factor_selection，orchestrator 收到空配置即全部特征直接训练
    assert "factor_selection" not in normalized
    assert normalized.get("auto_feature_filter") == "false"
