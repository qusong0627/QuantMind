"""派发留痕的失败原因：读 ExecutionResult 的真实字段，而不是不存在的 message。

背景（2026-09-24 排查 5 个全失败的推理任务）：`ExecutionResult`
（`backend/services/engine/inference/script_runner.py`）**没有 message 字段**，
旧实现读 `exec_res.message` 恒得空串 —— `qm_model_inference_dispatch_logs.
reason_detail` 整月为空，失败原因只能翻容器日志。真正的出口是
`error` / `failure_stage` / `exit_code`（三者在 script_runner 的每个早退
分支都已填好：主脚本缺失、维度门禁、子进程 exit≠0）。

回归面：即使字段全空，也必须给出非空 ``unknown failure``——留痕表里一列
空白的「失败」和没有留痕等价，正是本文件存在的理由。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.services.engine.tasks.celery_tasks import _failure_detail


@dataclass
class _Res:
    """ExecutionResult 的最小形状（只含 _failure_detail 读的字段）。"""

    success: bool = False
    error: str = ""
    failure_stage: str = ""
    exit_code: int | None = 0


def test_combines_stage_error_and_exit_code():
    # Arrange：主脚本缺失分支的实际取值（script_runner L1268 附近）
    res = _Res(
        error="主模型推理脚本不存在: /data/models/mdl_x/inference.py",
        failure_stage="main_script",
        exit_code=2,
    )

    # Act
    detail = _failure_detail(res)

    # Assert：三要素齐全，顺序稳定（stage 在前，便于按前缀筛查）
    assert detail.startswith("stage=main_script")
    assert "主模型推理脚本不存在" in detail
    assert detail.endswith("exit=2")


def test_error_only_still_reports_error():
    detail = _failure_detail(_Res(error="CUDA out of memory"))
    assert detail == "CUDA out of memory"


def test_success_exit_code_is_not_confusing_noise():
    """exit_code=0（或 None）不进摘要 —— 失败留痕里出现 exit=0 会误导排查。"""
    assert _failure_detail(_Res(error="x", exit_code=0)) == "x"
    assert _failure_detail(_Res(error="x", exit_code=None)) == "x"


def test_all_fields_empty_is_never_a_blank_string():
    """回归钉：旧实现读不存在的 .message → 空串；全空字段也必须非空。"""
    detail = _failure_detail(_Res())
    assert detail == "unknown failure"


def test_result_object_without_message_attr_is_fine():
    """ExecutionResult 没有 message 字段——传真实对象形状时不得抛 AttributeError。"""

    class _RealShape:
        success = False
        exit_code = 1
        error = "boom"
        failure_stage = "subprocess"

    assert "boom" in _failure_detail(_RealShape())
