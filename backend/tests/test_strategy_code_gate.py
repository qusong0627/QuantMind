"""T-P0-02 回归：策略代码统一安全闸门（AST 白名单）。

背景：/real-trading/start 的沙箱提交路径此前完全绕过校验，用户可在 trade
服务进程内执行任意代码。闸门落在 sandbox_manager.submit_strategy 与
user_strategy_loader.save_strategy 两处。
"""

import pytest

from backend.shared.strategy_code_gate import validate_strategy_code

_DANGEROUS_CASES = [
    ("import_os", "import os\nos.system('id')\n"),
    ("import_subprocess", "import subprocess\n"),
    ("dunder_class_escape", "x = ().__class__.__bases__\n"),
    ("eval", "eval('1 + 1')\n"),
    ("exec", "exec('x = 1')\n"),
    ("open_file", "f = open('/etc/passwd')\n"),
    ("dunder_import", "__import__('os').system('id')\n"),
]


@pytest.mark.parametrize("name,code", _DANGEROUS_CASES, ids=[c[0] for c in _DANGEROUS_CASES])
def test_dangerous_code_rejected(name, code):
    with pytest.raises(ValueError):
        validate_strategy_code(code)


def test_legit_platform_code_passes():
    """平台合法引用（minibt / backend.*）+ 无 setup 钩子形态应通过。"""
    code = (
        "from minibt import Bt, Strategy\n"
        "from backend.shared.minibt_qdb import load_daily\n"
        "\n"
        "class MyStrategy(Strategy):\n"
        "    def next(self):\n"
        "        pass\n"
    )
    validate_strategy_code(code)  # 不抛即通过


def test_on_tick_style_passes_without_setup_hook():
    code = "def on_tick(context):\n    context.log('hi')\n"
    validate_strategy_code(code)


def test_empty_code_rejected():
    with pytest.raises(ValueError):
        validate_strategy_code("")
    with pytest.raises(ValueError):
        validate_strategy_code(None)


def test_oversize_code_rejected():
    with pytest.raises(ValueError):
        validate_strategy_code("x = 1\n" * 200_000)  # ~1.2MB > 512KB


def test_syntax_error_rejected():
    with pytest.raises(ValueError):
        validate_strategy_code("def broken(:\n")
