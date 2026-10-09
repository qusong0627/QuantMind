"""campaign 终态回流接线契约（P1 · 设计文档 §4.2；验收 ③）。

台账行从 dispatched 走到 registered/failed 只有三条回流路径，缺一条就有 run
把 campaign 永远留在 dispatched 装「在跑」（该窗口的幂等键反过来挡住重派发）：

- ``complete_training_run`` 完成回调（§4.2 主路径：registered / failed 带 model_id）；
- ``cancel_training_run`` 取消（failed: cancelled——取消的 run 不会有完成回调）；
- ``job_reaper._mark_orphaned`` 判死回收（验收 ③：kill -9 API 后重启，僵尸 run
  必须回流，否则台账漂着）。

admin 包因并行改动暂不可导入（global_market_console 冲突），行为级验证受阻，
这里做源码级接线断言——「回调里加回流」这件事本身可被格式化/重构弄丢，计数
守卫拦得住。
"""

from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]


def test_complete_and_cancel_flow_back_with_correct_statuses():
    src = (
        _BACKEND / "services/api/routers/admin/admin_training_utils.py"
    ).read_text(encoding="utf-8")

    assert (
        src.count(
            "from backend.shared.rolling_campaigns import mark_outcome_by_run_safe"
        )
        == 2
    ), "完成回调与取消两处都要回流台账"
    assert src.count("await mark_outcome_by_run_safe(") == 2
    # 完成路径：终态 completed → registered，其余 → failed（带注册出的 model_id）
    assert 'status="registered" if status == "completed" else "failed"' in src
    # 取消路径：run 已被取消，不会再有完成回调
    assert 'status="failed", reason="cancelled"' in src


def test_reaper_orphan_flows_back_to_campaign():
    src = (
        _BACKEND / "services/engine/training/job_reaper.py"
    ).read_text(encoding="utf-8")

    assert src.count("await mark_outcome_by_run_safe(") == 1
    assert 'reason=f"orphaned:{verdict.reason}"' in src
    # 回流必须挂在「真的改到了行」分支里：竞态输家（行已是终态/行已消失）
    # 不得替赢家写台账
    assert src.index("if updated:") < src.index("await mark_outcome_by_run_safe(")
