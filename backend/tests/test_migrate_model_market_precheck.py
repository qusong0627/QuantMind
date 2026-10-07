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
        library_ready=True, missing_columns=["f_l2_net_inflow", "f_l2_bid_ratio"], hash_ok=True
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
