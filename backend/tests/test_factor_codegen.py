"""补码生成（factor_codegen）的提示词/抠码/静态校验/重试契约。

背景（2026-10-09）：因子库「待评估」的 45 条因子 factor_code 为空串（旧提取
器落库的半成品，工作区已清理），物化/回测/训练三条路都堵。修复方式是按公式+
描述用 LLM 补码再自动回测补 IC。本文件钉死补码侧的安全边界与重试语义：

1. 提示词必含执行契约关键词（daily_pv.h5 / __main__ / result.h5 / ``$`` 前缀
   兼容 / 禁前视）——提示词是生成的唯一输入，契约缺一句，生成就偏一分；
2. ``extract_python_code`` 只认围栏块或整段代码，散文混排交给校验拒（不静默
   截半截）；
3. ``validate_factor_code`` 不执行代码：语法错误、缺入口、禁项（subprocess/
   eval/os.system 等）一律拒绝并给出中文原因；
4. ``generate_factor_code`` 校验不过时把原因回喂重试一次，仍不过抛
   ``FactorCodegenError``（错误信息带原因，供批次行 metadata 上屏）。

LLM 一律替身（无网络）；真实生成质量属验收批次的观测对象，不在单测范围。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

try:
    from backend.services.engine.alpha_agent import factor_codegen as fc
except Exception:  # noqa: BLE001 - 环境相关
    fc = None

pytestmark = pytest.mark.skipif(fc is None, reason="依赖不可用（需容器环境）")

_GOOD_CODE = """\
import numpy as np
import pandas as pd


def _col(df, name):
    return df[f"${name}"] if f"${name}" in df.columns else df[name]


def main(data_path="daily_pv.h5", output_path="result.h5"):
    df = pd.read_hdf(data_path, key="data")
    if not isinstance(df.index, pd.MultiIndex):
        df = df.set_index(["datetime", "instrument"])
    df = df.sort_index()
    close = _col(df, "close")
    factor = close.groupby(level="instrument").pct_change(5)
    factor = pd.Series(
        np.where(np.isfinite(factor), factor, np.nan),
        index=df.index,
        name="momentum_5d",
    )
    factor.to_hdf(output_path, key="data", mode="w")


if __name__ == "__main__":
    main()
"""


def _factor(**overrides) -> dict:
    base = {
        "factor_id": "f-1",
        "factor_name": "Momentum_5D",
        "market": "a_share",
        "factor_formulation": r"\frac{close_t - close_{t-5}}{close_{t-5}}",
        "metadata": {"description": "五日动量", "category": "momentum"},
    }
    base.update(overrides)
    return base


# ── 提示词 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_messages_contain_execution_contract():
    """契约关键词一个不能少（提示词是生成质量的唯一闸门）。"""
    messages = fc.build_codegen_messages(_factor())
    assert messages[0]["role"] == "system"
    system = messages[0]["content"]
    for keyword in (
        "daily_pv.h5",
        "result.h5",
        "__main__",
        "$",
        "groupby(level=",
        "shift(-n)",
        "nan",
    ):
        assert keyword in system, f"契约关键词缺失: {keyword}"

    user = messages[1]["content"]
    assert "Momentum_5D" in user
    assert "close" in user  # 公式透传


@pytest.mark.unit
def test_messages_tolerate_missing_formulation():
    """公式缺失时提示词明说「按名称与描述推断」，不出现 None 字样。"""
    messages = fc.build_codegen_messages(
        _factor(factor_formulation="", metadata={"description": "x"})
    )
    user = messages[1]["content"]
    assert "None" not in user
    assert "推断" in user


# ── 抠码 ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_extract_fenced_code():
    text = f"以下是实现：\n```python\n{_GOOD_CODE}```\n说明文字。"
    code = fc.extract_python_code(text)
    assert code is not None
    assert "def main" in code
    assert not code.startswith("```")


@pytest.mark.unit
def test_extract_bare_python_without_fence():
    code = fc.extract_python_code(_GOOD_CODE)
    assert code == _GOOD_CODE.strip()


@pytest.mark.unit
def test_extract_garbage_returns_none():
    assert fc.extract_python_code("我不知道怎么写这个因子。") is None
    assert fc.extract_python_code("") is None


# ── 静态校验 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_validate_good_code_passes():
    ok, reason = fc.validate_factor_code(_GOOD_CODE)
    assert ok, reason


@pytest.mark.unit
def test_validate_calculate_style_passes_without_main_guard():
    code = (
        "import pandas as pd\n\n"
        "def calculate_x(data_path='daily_pv.h5'):\n"
        "    return pd.read_hdf(data_path, key='data')['close']\n"
    )
    ok, _ = fc.validate_factor_code(code)
    assert ok


@pytest.mark.unit
def test_validate_syntax_error_rejected():
    ok, reason = fc.validate_factor_code("def main(:\n    pass\n")
    assert not ok
    assert "语法错误" in reason


@pytest.mark.unit
def test_validate_missing_entry_rejected():
    ok, reason = fc.validate_factor_code("X = 1\n")
    assert not ok
    assert "缺少入口" in reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "snippet",
    [
        "import subprocess\n\n\ndef calculate_x():\n    return 1\n",
        "import requests\n\n\ndef calculate_x():\n    return 1\n",
        "def calculate_x():\n    eval('1')\n",
        "import os\n\n\ndef calculate_x():\n    os.system('rm -rf /')\n",
    ],
)
def test_validate_forbidden_constructs_rejected(snippet):
    ok, reason = fc.validate_factor_code(snippet)
    assert not ok
    assert "禁止" in reason


# ── 生成（LLM 替身） ─────────────────────────────────────────────────


@pytest.fixture()
def fake_chat(monkeypatch):
    """替身 ``llm_client.chat``：按脚本依次回吐；记录每次 messages。"""
    llm = pytest.importorskip("backend.services.engine.alpha_agent.llm_client")
    calls: list[list[dict]] = []
    replies: list[object] = []

    async def _chat(messages, **kwargs):
        calls.append(messages)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm, "chat", _chat)
    return type("FakeChat", (), {"calls": calls, "replies": replies})


@pytest.mark.unit
def test_generate_ok_first_try(fake_chat):
    fake_chat.replies.append(f"```python\n{_GOOD_CODE}```")
    code = asyncio.run(fc.generate_factor_code(_factor(), config=object()))
    assert "def main" in code
    assert len(fake_chat.calls) == 1


@pytest.mark.unit
def test_generate_retries_with_reason_after_bad_code(fake_chat):
    """首次输出缺入口 → 第二次消息带原因回喂，成功返回且只多一次调用。"""
    fake_chat.replies.append("```python\nX = 1\n```")
    fake_chat.replies.append(f"```python\n{_GOOD_CODE}```")
    code = asyncio.run(fc.generate_factor_code(_factor(), config=object()))
    assert "def main" in code
    assert len(fake_chat.calls) == 2
    feedback = fake_chat.calls[1][-1]
    assert feedback["role"] == "user"
    assert "缺少入口" in feedback["content"]


@pytest.mark.unit
def test_generate_raises_after_max_attempts(fake_chat):
    fake_chat.replies.append("没有代码")
    fake_chat.replies.append("还是没有代码")
    with pytest.raises(fc.FactorCodegenError) as excinfo:
        asyncio.run(fc.generate_factor_code(_factor(), config=object()))
    assert "代码块" in str(excinfo.value)
    assert len(fake_chat.calls) == 2


@pytest.mark.unit
def test_generate_wraps_llm_exception(fake_chat):
    fake_chat.replies.append(RuntimeError("boom"))
    with pytest.raises(fc.FactorCodegenError) as excinfo:
        asyncio.run(fc.generate_factor_code(_factor(), config=object()))
    assert "LLM 调用失败" in str(excinfo.value)


@pytest.mark.unit
def test_no_module_import_of_llm_client_at_import_time():
    """factor_codegen 顶层不得 import llm_client（保持轻依赖、可单测）。"""
    import backend.services.engine.alpha_agent.factor_codegen as module

    source = module.__file__
    with open(source, encoding="utf-8") as handle:
        head = handle.read().split("async def generate_factor_code", 1)[0]
    assert "import chat" not in head
    assert "llm_client import chat" not in head
    # 顺带保证替身在 sys.modules 有迹可循（importorskip 用）
    assert "backend.services.engine.alpha_agent" in sys.modules
