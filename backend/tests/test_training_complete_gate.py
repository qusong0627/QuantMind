"""完成回调门禁语义：软门禁暂留如实提示，run 保持成功。

- admin_training_utils 依赖重，用 importlib 直接加载（缺依赖时跳过，沿用
  test_admin_training_timezone.py 策略；服务器/CI 全依赖环境下实际执行）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REASONS = ["test_rank_ic=-0.0105 非正", "test_rank_icir=-0.1701 低于软门禁阈值 0.05"]


def _load_utils():
    fp = ROOT / "backend/services/api/routers/admin/admin_training_utils.py"
    try:
        spec = importlib.util.spec_from_file_location("atu_gate_test", fp)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


@pytest.fixture()
def atu():
    mod = _load_utils()
    if mod is None or not hasattr(mod, "_registration_outcome"):
        pytest.skip("admin_training_utils cannot be imported in this env")
    return mod


def test_ready_stays_completed(atu):
    status, summary, error = atu._registration_outcome(
        {"status": "ready", "error": "", "model_id": "m1"}
    )
    assert status == "completed" and summary == {} and error == ""


def test_gate_hold_stays_completed_with_truthful_summary(atu):
    status, summary, error = atu._registration_outcome(
        {
            "status": "candidate",
            "error": "",
            "model_id": "m2",
            "gate_reasons": REASONS,
            "message": "门禁msg",
        }
    )
    assert status == "completed" and error == ""
    assert summary["status"] == "质量门禁暂留候选"
    assert "门禁msg" in summary["message"]


def test_gate_hold_without_message_uses_reasons(atu):
    status, summary, error = atu._registration_outcome(
        {"status": "candidate", "error": "", "gate_reasons": REASONS}
    )
    assert status == "completed" and error == ""
    assert REASONS[0] in summary["message"]


def test_failed_stays_failed(atu):
    status, summary, error = atu._registration_outcome(
        {"status": "failed", "error": "boom"}
    )
    assert status == "failed"
    assert summary["status"] == "模型注册失败" and error == "boom"


def test_candidate_without_reasons_stays_failed(atu):
    status, summary, error = atu._registration_outcome(
        {"status": "candidate", "error": ""}
    )
    assert status == "failed"
    assert summary["status"] == "模型注册失败"
