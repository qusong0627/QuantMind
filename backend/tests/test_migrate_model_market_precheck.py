"""模型市场迁移的「迁移后就绪校验」裁决口径（纯函数，无 IO）。

**缺陷（2026-10-08 复核）**：脚本原先的裁决是
``ready = status.ready and not missing and hash_ok``——把 `factor_schema_hash` 不同
也算硬失败，而平台口径早在 2026-09-20（b3e3a61b）就改成了「**缺列才硬失败、漂移只
提示**」，同款注释在 `script_runner` 预检与 `data_loader` 执行侧都写着（哈希只覆盖
列名集合，因子库每加一列就会变：实测 quantcustom 273→282）。本脚本是那轮改动漏掉的
第三处，后果是**成功的迁移被渲染成 `precheck: FAIL` + 退出码 1**——而这个脚本存在的
理由正是 CUSTOM（`/data/quantcustom`）→ CN 迁移，迁移不改数据根，库一加列 pin 就过期
（平台实测 quantcustom 273→282）：届时运维拿到的唯一校验信号是假红，会据此做多余回滚。
（实测 2026-10-08：当前 6 个 CN 模型的 pin 与库哈希一致、CUSTOM 模型已全部迁完，
所以这是**潜伏**缺陷而非正在发作——但判据必须与门禁同口径，否则下次库加列就中招。）

判据（与推理门禁同口径）：库不可用 / 执行侧要读的列取不到 ⇒ FAIL；仅哈希不同 ⇒ PASS
但必须把漂移写进 `notes`（可见，不必可阻断）。

**2026-10-08 评审补的两条（本文件后半）**：
1. 只测 ``evaluate_precheck`` 钉不住**接线**——装配（`build_verdict`）或退出码口径
   （`exit_code_for`）被改回内联判据时，前面 5 条全绿而 CLI 语义已变。故补装配层用例。
2. 空 `factor_field_sources` 的模型要让检查回退到 `feature_columns`，否则缺列检查
   被整个跳过（实测 87 个 metadata.json 里 17 个属此类，含 1 个 136 列的 CN 模型）。
   故抽出 `execution_feature_columns` 并钉住回退口径。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _evaluate(**kw):
    from backend.scripts.migrate_model_market import evaluate_precheck

    return evaluate_precheck(**kw)


def test_hash_drift_alone_is_not_fatal():
    """pin 过期（列名集合变了）但列取得到：必须 PASS——这正是被误报的那个场景。"""
    verdict = _evaluate(library_ready=True, missing_columns=[], hash_ok=False)

    assert verdict["precheck"] == "PASS", "漂移不得阻断（会渲染成假红的迁移失败）"
    assert verdict["schema_hash_ok"] is False, "漂移仍须可见"
    assert any("漂移" in n for n in verdict["notes"]), "漂移必须写进 notes"


def test_missing_columns_is_fatal():
    """执行侧要读的列取不到 ⇒ 硬失败，且 note 要指名道姓（便于定位）。"""
    verdict = _evaluate(
        library_ready=True,
        missing_columns=["f_l2_net_inflow", "f_l2_bid_ratio"],
        hash_ok=True,
    )

    assert verdict["precheck"] == "FAIL"
    joined = " ".join(verdict["notes"])
    assert "f_l2_net_inflow" in joined and "f_l2_bid_ratio" in joined


def test_library_not_ready_is_fatal():
    verdict = _evaluate(library_ready=False, missing_columns=[], hash_ok=True)

    assert verdict["precheck"] == "FAIL"
    assert any("不可用" in n for n in verdict["notes"])


def test_clean_library_passes_without_notes():
    verdict = _evaluate(library_ready=True, missing_columns=[], hash_ok=True)

    assert verdict["precheck"] == "PASS"
    assert verdict["notes"] == []
    assert verdict["schema_hash_ok"] is True


def test_missing_columns_win_over_drift_when_both_present():
    """两者同时出现时结论仍是 FAIL，且两条原因都要说清楚（别只报漂移）。"""
    verdict = _evaluate(library_ready=True, missing_columns=["f_x"], hash_ok=False)

    assert verdict["precheck"] == "FAIL"
    joined = " ".join(verdict["notes"])
    assert "f_x" in joined and "漂移" in joined


def test_execution_columns_fall_back_to_feature_columns():
    """空映射的老模型必须回退到 feature_columns——否则缺列检查整个被跳过。

    实测（2026-10-08，`/app/models/users`）：87 个 metadata.json 里 17 个
    `factor_field_sources` 为空，且 17 个都有 feature_columns（HK 模型各 8 列，
    另有 1 个 CN 模型 136 列）。只遍历映射的旧实现对这批模型恒 `missing=[]`，
    预检绿灯、迁移后推理缺列。
    """
    from backend.scripts.migrate_model_market import execution_feature_columns

    assert execution_feature_columns(
        {"feature_columns": ["a", "b"], "factor_field_sources": {"x": "a"}}
    ) == ["a", "b"], "执行读的是 feature_columns，不是映射键"
    assert execution_feature_columns(
        {"feature_columns": [], "factor_field_sources": {"x": "col_x", "y": "col_y"}}
    ) == ["x", "y"], "没写 feature_columns 的老模型回退映射键"
    assert execution_feature_columns({"features": ["old_key"]}) == ["old_key"], (
        "旧键 features"
    )
    assert execution_feature_columns({}) == [], (
        "两处都空：没有可检查的列（调用方如实报空）"
    )


def test_build_verdict_wires_precheck_into_exit_code():
    """装配层口径：漂移单独出现 ⇒ PASS 且退出码 0——**接线本身**要被钉住。

    单测 `evaluate_precheck` 时，把 `build_verdict` 里的展开丢掉（或退回内联判据）
    不会有任何用例变红，而 CLI 的 PASS⇒0 语义已经变了（评审指出的正是这个形状）。
    """
    from backend.scripts.migrate_model_market import build_verdict, exit_code_for

    verdict = build_verdict(
        data_dir="/app/models/x",
        factor_source="l1_factors",
        coverage="2020-01-01~2026-09-30",
        missing_columns=[],
        library_ready=True,
        hash_ok=False,
    )

    assert verdict["precheck"] == "PASS", "漂移不得阻断（装配层同样口径）"
    assert verdict["schema_hash_ok"] is False and verdict["notes"], "漂移仍须可见"
    assert exit_code_for({"verification": verdict}) == 0

    bad = build_verdict(
        data_dir="/app/models/x",
        factor_source="l1_factors",
        coverage="c",
        missing_columns=[f"f{i}" for i in range(12)],
        library_ready=True,
        hash_ok=True,
    )

    assert bad["precheck"] == "FAIL"
    assert len(bad["missing_mapped_fields"]) == 10, "裁决字段最多列 10 个列名"
    assert exit_code_for({"verification": bad}) == 1
    assert exit_code_for({}) == 0 and exit_code_for(None) == 0, "无裁决的路径不改退出码"


def test_truncated_note_reports_the_total_count():
    """notes 截断必须带总数：只列 5 个而不说还有多少，会把「缺 12 列」读成「缺 5 列」。"""
    verdict = _evaluate(
        library_ready=True, missing_columns=[f"f{i}" for i in range(12)], hash_ok=True
    )

    note = next(n for n in verdict["notes"] if "取不到" in n)
    assert "f0" in note and "f11" not in note, "notes 只列前 5 个"
    assert "12" in note, "截断时必须报总列数"
