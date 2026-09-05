"""
训练节点状态解析回归测试

覆盖 node_manager.NodeStatus._parse 的静态方法崩溃回归：
_parse 是 @staticmethod,曾在收尾调用 cls.assess_readiness 导致
NameError → collect_all 吞异常 → AutoDL 远程节点永远显示 offline,
即使机器与 SSH 完全正常(2026-09-05 修复为 NodeStatus.assess_readiness)。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.engine.training.node_manager import NodeStatus  # noqa: E402


def _fake_collect_output() -> str:
    """模拟远端 SSH 采集命令的标准输出(与 _COLLECT_CMD 的 === 段结构一致)。"""
    return """===SYS===
8
 12:00:00 up 3 days
mem:32000 8000
disk:200000000 90000000
net:100 50
===GPU===
100, 1200, 16000, 62, NVIDIA GeForce RTX 4090
===DOCKER===
qm-train-train_20260901000000_abc123|Up 3 hours
===NET===
0.42
"""


def test_parse_returns_online_with_gpu_and_readiness():
    """_parse 正常输出应解析出 GPU/容器并给出 readiness,不再抛 NameError。"""
    result: dict = {}
    parsed = NodeStatus._parse(_fake_collect_output(), result)
    assert parsed is result
    assert result["online"] is True
    assert result["gpus"] and result["gpus"][0]["name"].startswith("NVIDIA")
    assert result["containers"] and result["containers"][0]["name"].startswith("qm-train")
    assert result["training_active"] is True
    assert "readiness" in result  # 收尾 assess_readiness 已执行


def test_parse_with_empty_output_does_not_crash():
    """最简输出也必须走完 assess_readiness(曾在此崩溃导致全节点 offline)。"""
    result: dict = {}
    parsed = NodeStatus._parse("", result)
    assert parsed is result
    assert result.get("online") is True
    assert "readiness" in result
