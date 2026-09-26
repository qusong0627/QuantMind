"""推理持久化与 XGBoost 打分口径的静态护栏（完整链路需 DB/qlib，跑不了单测）。

覆盖三个回归点（详见各测试 docstring）：
1. 单日/每日推理在模型目录缺 ``pred.parquet`` 时必须能建文件，
   否则模拟交易回放建会话被 pred.parquet 门禁永久拦死；
2. 管理员手动推理必须写 ``qm_model_inference_runs``，
   否则「推理历史 / 信号就绪（latest_run_id）」永远看不到最新批次；
3. XGBoost 推理侧 ``iteration_range`` 必须与训练侧同口径，
   且不得传 ``None``（xgboost>=2.0 不接受）。
"""

from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _backend_src(rel: str) -> str:
    return (_BACKEND / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. pred.parquet 缺失时可创建（模拟交易「缺少推理文件」的根因）
# ---------------------------------------------------------------------------


def test_pred_merge_default_stays_non_creating():
    """默认必须仍是「不凭单日数据创建残缺历史」，靠调用方显式选择。"""
    src = _backend_src("services/engine/inference/pred_merge.py")
    assert "create_if_missing: bool = False" in src
    # 文件不存在且未允许创建 → 直接返回 0（静默）。调用方必须显式传 True。
    assert "if not parquet_file.is_file() and not create_if_missing:" in src
    assert "return 0" in src


def test_daily_inference_can_create_missing_pred_parquet():
    """router_service 的回写必须显式 create_if_missing=True。

    回归：旧实现只传 (pred_file, signals) → 模型目录无 pred.parquet 时
    merge 静默返回 0，推理「跑了」但文件永远不出现，模拟交易回放
    （simulation/replay/router.py 的 pred.parquet 门禁）永久 400。
    """
    src = _backend_src("services/engine/inference/router_service.py")
    assert "merge_signals_into_pred(" in src
    assert "create_if_missing=True" in src
    # 新建文件的情形必须有醒目告警，不能静默
    assert "原本缺少 pred.parquet" in src


def test_backfill_still_creates_pred_parquet():
    """一键补全路径同样要允许创建（补历史不能依赖文件先存在）。"""
    src = _backend_src("services/engine/inference/gap_backfill.py")
    assert "create_if_missing=True" in src


# ---------------------------------------------------------------------------
# 2. 管理员手动推理必须落 run 记录
# ---------------------------------------------------------------------------


def test_admin_manual_inference_records_run():
    """管理员 /run-inference 不能只写信号表，必须落 qm_model_inference_runs。

    回归：该端点曾直接调 run_daily_inference_script 并返回 run_id，但不写
    run 表 → engine_signal_scores 有数据、推理历史为空，
    manual_execution_service.get_default_model_hosted_status 取不到
    latest_run_id，信号就绪落到 missing_latest_run。
    """
    src = _backend_src("services/api/routers/admin/model_management.py")
    assert "async def _record_admin_inference_run(" in src
    # 三张表/设置的口径必须齐全
    assert "create_run(" in src
    assert "update_run(" in src
    assert "record_run_to_settings(" in src
    # 成功与失败两条返回分支都要落库
    assert src.count("await _record_admin_inference_run(") >= 2
    # 落库失败不能影响推理结果返回
    assert "落库失败" in src


# ---------------------------------------------------------------------------
# 3. XGBoost iteration_range 与训练侧同口径，且禁止 None
# ---------------------------------------------------------------------------

# 训练侧规范写法（docker/training/model_trainers/predict.py）：
# iteration_range 右开区间，best_iteration 为 0-based，需 +1 才含最优轮。
_TRAINING_CANONICAL = "iteration_range=(0, (n_iter + 1) if n_iter is not None else 0)"
_FORBIDDEN_INFERENCE = "iteration_range=(0, best_iter) if best_iter else None"


def test_training_side_xgb_predict_is_canonical():
    src = (_REPO_ROOT / "docker" / "training" / "model_trainers" / "predict.py").read_text(
        encoding="utf-8"
    )
    assert _TRAINING_CANONICAL in src


def test_inference_template_xgb_iteration_range_matches_training():
    """推理模板不得再传 None，且必须用 +1 的右开区间写法。

    回归：旧写法 `(0, best_iter) if best_iter else None` 在
    best_iteration 缺失/为 0（未开 early stopping 的常见情形）时传 None，
    xgboost>=2.0 不接受 → XGBoost 推理直接抛错而 LightGBM 正常；
    即便接受也少算一轮，与训练打分口径不一致。
    """
    src = _backend_src("services/engine/inference/templates/inference_parquet.py")
    assert _FORBIDDEN_INFERENCE not in src
    assert "iteration_range=(0, _iter_end)" in src
    # 右开区间 +1，且 best_iteration 缺失时退化为 (0, 0)=全部树
    assert "_iter_end = int(best_iter) + 1" in src


def test_embedded_fallback_inference_script_has_same_fix():
    """train.py 内嵌的兜底 inference.py 字符串必须同步修复（同一 bug 的副本）。"""
    src = (_REPO_ROOT / "docker" / "training" / "train.py").read_text(encoding="utf-8")
    assert _FORBIDDEN_INFERENCE not in src
    assert "iteration_range=(0, _iter_end)" in src
