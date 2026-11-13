""".pine → Pyne-Python 转写：静态检查与产物读取。

转写产物是要被执行的代码，静态检查是唯一一道拦截（危险导入 / 动态执行 / 结构缺失），
漏检等于把模型输出直接喂给回测进程，所以这里逐条锁定。

运行（容器）：python3 -m pytest backend/tests/test_pine_to_pyne.py -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.agent_arena import pine_library as pl  # noqa: E402
from scripts import pine_to_pyne as ptp  # noqa: E402

GOOD = '''"""
@pyne

双均线交叉。
"""
from pynecore.lib import close, input, script, strategy, ta
from pynecore.types import Series


@script.strategy("双均线交叉", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000)
def main(fast=input(5, "快线"), slow=input(20, "慢线")):
    f = ta.sma(close, fast)
    s = ta.sma(close, slow)
    up: Series[bool] = f > s
    if up and not up[1]:
        strategy.entry('L', strategy.long)
    elif not up and up[1]:
        strategy.close('L')
'''


class TestExtractCode:
    def test_fenced_block(self):
        assert ptp.extract_code("说明\n```python\nx = 1\n```\n尾注") == "x = 1\n"

    def test_plain_text_falls_back(self):
        assert ptp.extract_code("x = 1") == "x = 1\n"


class TestCallLlm:
    @staticmethod
    def _patch(monkeypatch, seen: dict, finish: str = "stop"):
        import requests

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "x"}, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(json or {})
            return FakeResp()

        monkeypatch.setattr(ptp, "_env", lambda: {"OPENAI_API_BASE": "http://x",
                                                  "OPENAI_API_KEY": "k"})
        monkeypatch.setattr(requests, "post", fake_post)

    def test_default_disables_thinking(self, monkeypatch):
        """转写是照模板的机械活：开思考会把 completion 额度烧光——0001 烧 23755 token，
        0004 直接 32k 截断。默认必须关思考。"""
        seen: dict = {}
        self._patch(monkeypatch, seen)
        ptp.call_llm("system", "user", "m")
        assert seen["thinking"] == {"type": "disabled"}
        assert seen["max_tokens"] == ptp.MAX_TOKENS_FAST

    def test_think_mode_keeps_generous_cap(self, monkeypatch):
        seen: dict = {}
        self._patch(monkeypatch, seen)
        ptp.call_llm("system", "user", "m", think=True)
        assert "thinking" not in seen
        assert seen["max_tokens"] == ptp.MAX_TOKENS
        assert ptp.MAX_TOKENS >= 16000

    def test_truncation_returned_not_raised(self, monkeypatch):
        """截断要能回传给 transpile 做「开思考重试」，不能在底层就抛掉。"""
        seen: dict = {}
        self._patch(monkeypatch, seen, finish="length")
        content, usage, truncated = ptp.call_llm("system", "user", "m")
        assert truncated is True
        assert content == "x" and usage["completion_tokens"] == 2


class TestTranspileLadder:
    """关思考优先、截断才开思考重试。"""

    @staticmethod
    def _fake_pine(monkeypatch, tmp_path):
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path)
        monkeypatch.setattr(ptp, "_load_pine", lambda _: {
            "title": "甲", "category": "c", "version": 5, "source": "//@version=5\n"})

    def test_clean_run_does_not_think(self, tmp_path, monkeypatch):
        self._fake_pine(monkeypatch, tmp_path)
        calls: list[bool] = []

        def fake(system, user, model, think=False):
            calls.append(think)
            return f"```python\n{GOOD}```", {"completion_tokens": 9}, False

        monkeypatch.setattr(ptp, "call_llm", fake)
        meta = ptp.transpile("0004")
        assert calls == [False]
        assert meta["think"] is False and meta["problems"] == []

    def test_truncation_retries_with_thinking(self, tmp_path, monkeypatch):
        self._fake_pine(monkeypatch, tmp_path)
        calls: list[bool] = []

        def fake(system, user, model, think=False):
            calls.append(think)
            return f"```python\n{GOOD}```", None, not think

        monkeypatch.setattr(ptp, "call_llm", fake)
        meta = ptp.transpile("0004")
        assert calls == [False, True]
        assert meta["think"] is True and meta["problems"] == []

    def test_double_truncation_raises(self, tmp_path, monkeypatch):
        self._fake_pine(monkeypatch, tmp_path)
        monkeypatch.setattr(ptp, "call_llm", lambda *a, **k: ("", None, True))
        with pytest.raises(RuntimeError, match="截断"):
            ptp.transpile("0004")


class TestRepair:
    """回测报错 → 让模型改一版。只写文件，执行永远归沙箱。"""

    @staticmethod
    def _setup(tmp_path, monkeypatch):
        d = tmp_path / "0004"
        d.mkdir(parents=True)
        (d / "candidate.py").write_text("旧代码\n", encoding="utf-8")
        (d / "meta.json").write_text(json.dumps({"title": "甲", "problems": []}),
                                     encoding="utf-8")
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path)
        monkeypatch.setattr(ptp, "_load_pine", lambda _: {
            "title": "甲", "category": "c", "version": 5, "source": "//@version=5\n"})
        return d

    def test_rewrites_candidate_and_records_history(self, tmp_path, monkeypatch):
        d = self._setup(tmp_path, monkeypatch)
        seen: dict = {}

        def fake_chat(messages, model, think=False):
            seen["messages"] = messages
            seen["think"] = think
            return f"```python\n{GOOD}```", {"completion_tokens": 7}, False

        monkeypatch.setattr(ptp, "_chat", fake_chat)
        meta = ptp.repair("0004", "TypeError: 'float' object is not subscriptable")

        assert (d / "candidate.py").read_text(encoding="utf-8").strip() == GOOD.strip()
        assert meta["problems"] == []
        assert len(meta["repairs"]) == 1
        assert "TypeError" in meta["repairs"][0]["error"]
        assert seen["think"] is False                      # 修复同样关思考
        assert "TypeError" in seen["messages"][-1]["content"]   # 报错进了提示词

    def test_static_problems_recorded(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(ptp, "_chat",
                            lambda *a, **k: ("```python\ndef main():\n    pass\n```", None, False))
        meta = ptp.repair("0004", "boom")
        assert any("缺少必需结构" in p for p in meta["problems"])

    def test_truncated_repair_raises(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(ptp, "_chat", lambda *a, **k: ("", None, True))
        with pytest.raises(RuntimeError, match="截断"):
            ptp.repair("0004", "boom")

    def test_missing_candidate_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path / "nope")
        with pytest.raises(RuntimeError, match="没有候选可修"):
            ptp.repair("0004", "boom")


class TestDepine:
    """模型连着修两轮仍会漏 `? :` 与 `:=`（0400/0778 就这么挂的），改成机械改写。"""

    def test_ternary_becomes_condition_expression(self):
        code = "x: Series[float] = candle_range > 0 ? body / candle_range : 0.0\n"
        got, notes = ptp.depine(code)
        assert got == "x: Series[float] = (body / candle_range) if (candle_range > 0) else (0.0)\n"
        assert notes == ["自动改写 1 处 `? :` → `if/else`"]

    def test_nested_ternary_inside_parens(self):
        code = "s = close > open ? volume : (close < open ? -volume : 0.0)\n"
        got, _ = ptp.depine(code)
        assert "?" not in got and got.count("if") == 2

    def test_trailing_comment_survives(self):
        got, _ = ptp.depine("x = a ? 1 : 2  # 注释里的 ? 不算\n")
        assert got == "x = (1) if (a) else (2)  # 注释里的 ? 不算\n"

    def test_string_with_question_mark_untouched(self):
        code = 'msg = "a ? b"\nx = 1\n'
        assert ptp.depine(code) == (code, [])

    def test_walrus_assign_becomes_equals(self):
        got, notes = ptp.depine("def main():\n    x = 1\n    x := x + 1\n")
        assert ":=" not in got and "x = x + 1" in got
        assert notes == ["自动改写 1 处 `:=` → `=`"]

    def test_clean_code_untouched(self):
        assert ptp.depine(GOOD) == (GOOD, [])


class TestStaticCheck:
    def test_template_passes(self):
        assert ptp.static_check(GOOD) == []

    @pytest.mark.parametrize("code,frag", [
        ("import os\n" + GOOD, "禁止导入"),
        ("from subprocess import run\n" + GOOD, "禁止导入"),
        ("x = eval('1')\n" + GOOD, "禁止动态执行"),
        ("y = open('/etc/passwd')\n" + GOOD, "禁止动态执行"),
        ("z = globals()\n" + GOOD, "禁止反射"),
    ])
    def test_dangerous_calls_blocked(self, code, frag):
        probs = ptp.static_check(code)
        assert any(frag in p for p in probs), probs

    def test_missing_structure_flagged(self):
        probs = ptp.static_check("def main():\n    pass\n")
        assert any("缺少必需结构" in p for p in probs)

    def test_syntax_error_flagged(self):
        probs = ptp.static_check("@pyne\nfrom pynecore.lib import input\ndef main(:\n")
        assert any("语法错误" in p for p in probs)

    def test_pynecore_input_is_not_python_input(self):
        """模板里的 input() 是 pynecore.lib 的，不能误判成 Python 内建 input。"""
        assert not any("禁止动态执行" in p for p in ptp.static_check(GOOD))


class TestImportChecks:
    """漏 import 是实测最高频的运行时失败（NameError），必须在静态闸就抓住。"""

    def test_bare_name_without_import_flagged(self):
        code = GOOD.replace("up: Series[bool] = f > s", "up: Series[bool] = f > s and not na(f)")
        assert any("用了 na 但没从 pynecore.lib 导入" in p for p in ptp.static_check(code))

    def test_imported_name_passes(self):
        code = GOOD.replace("from pynecore.lib import close, input, script, strategy, ta",
                            "from pynecore.lib import close, input, na, script, strategy, ta")
        code = code.replace("up: Series[bool] = f > s", "up: Series[bool] = f > s and not na(f)")
        assert not any("没从 pynecore.lib 导入" in p for p in ptp.static_check(code))

    def test_local_variable_is_not_flagged(self):
        code = GOOD.replace("up: Series[bool] = f > s", "session = 1\n    up: Series[bool] = f > s")
        assert not any("session" in p for p in ptp.static_check(code))

    def test_comment_mention_is_not_flagged(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    # 这里要判断 na 但只是注释\n    f = ta.sma(close, fast)")
        assert not any("用了 na" in p for p in ptp.static_check(code))

    def test_ta_bar_index_namespace_flagged(self):
        code = GOOD.replace("up: Series[bool] = f > s", "up: Series[bool] = ta.bar_index > 0")
        assert any("bar_index 不在 ta 下" in p for p in ptp.static_check(code))

    def test_math_isnan_flagged(self):
        code = GOOD.replace("up: Series[bool] = f > s", "up: Series[bool] = math.isnan(f)")
        assert any("没有 isnan" in p for p in ptp.static_check(code))

    def test_single_target_for_multi_return_ta(self):
        """ta.macd 返回元组，赋给单个变量后下标拿到的是「另一个指标」不是历史值。"""
        code = GOOD.replace("up: Series[bool] = f > s",
                            "m: Series[float] = ta.macd(close, 12, 26, 9)")
        assert any("返回元组" in p for p in ptp.static_check(code))

    def test_unpacked_multi_return_passes(self):
        code = GOOD.replace("up: Series[bool] = f > s",
                            "m, sig, hist = ta.macd(close, 12, 26, 9)")
        assert not any("返回元组" in p for p in ptp.static_check(code))

    def test_expression_history_index_flagged(self):
        code = GOOD.replace("up: Series[bool] = f > s",
                            "up: Series[bool] = (high + low + close)[1] > f")
        assert any("对表达式直接取历史" in p for p in ptp.static_check(code))

    def test_list_literal_index_is_not_flagged(self):
        """`arr[0]` 这种普通下标不能被误伤。"""
        code = GOOD.replace("up: Series[bool] = f > s", "up: Series[bool] = f[0] > s")
        assert not any("对表达式直接取历史" in p for p in ptp.static_check(code))


class TestTypeImports:
    """PersistentSeries 是 2026-09-09 才加进提示词的名字，漏 import 是运行时 NameError。"""

    def test_missing_persistent_series_import_flagged(self):
        code = GOOD.replace("up: Series[bool] = f > s",
                            "flag: PersistentSeries[bool] = False\n    up: Series[bool] = f > s")
        assert any("PersistentSeries 但没从 pynecore.types 导入" in p
                   for p in ptp.static_check(code))

    def test_imported_persistent_series_passes(self):
        code = GOOD.replace("from pynecore.types import Series",
                            "from pynecore.types import PersistentSeries, Series")
        code = code.replace("up: Series[bool] = f > s",
                            "flag: PersistentSeries[bool] = False\n    up: Series[bool] = f > s")
        assert not any("PersistentSeries" in p for p in ptp.static_check(code))

    def test_persistent_series_does_not_require_plain_series(self):
        """`PersistentSeries[bool]` 里含 Series 字样，别误报成缺 Series 导入。"""
        code = GOOD.replace("from pynecore.types import Series",
                            "from pynecore.types import PersistentSeries")
        code = code.replace("up: Series[bool] = f > s",
                            "flag: PersistentSeries[bool] = False\n"
                            "    up: PersistentSeries[bool] = f > s")
        assert not any("Series" in p for p in ptp.static_check(code)), ptp.static_check(code)


class TestFixHistory:
    """pynecore 里只有 `x: Series[...]` 声明的名字才有历史，普通赋值/解包/参数都没有。

    实测（600309.SH 真回测）：`s = ta.sma(close,20)` 后 `s[1]`、`a,b,c = ta.bb(...)`
    后 `b[1]`、`def f(src, n)` 里 `src[i]` 全都抛 TypeError: 'float' object is not
    subscriptable；补上声明后三种都正常。模型连修两轮也修不好，所以机械补。
    """

    def test_plain_assign_upgraded(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    f = ta.sma(close, fast)\n    g = ta.sma(close, slow)")
        code = code.replace("    up: Series[bool] = f > s", "    up: Series[bool] = f > g[1]")
        got, notes = ptp.fix_history(code)
        assert "    g: Series[float] = ta.sma(close, slow)" in got
        assert notes == ["`g = ...` 升级成 `g: Series[float] = ...`（普通赋值不留历史）"]

    def test_unpack_gets_same_name_declaration(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    mid, up_band, low_band = ta.bb(close, fast, 2.0)")
        code = code.replace("    up: Series[bool] = f > s", "    up: Series[bool] = up_band[1] > s")
        got, notes = ptp.fix_history(code)
        assert ("    mid, up_band, low_band = ta.bb(close, fast, 2.0)\n"
                "    up_band: Series[float] = up_band\n") in got
        assert notes == ["解包值 up_band 后补一行 `up_band: Series[float] = up_band`"]

    def test_unannotated_param_annotated(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    def sum_n(src, n):\n"
                            "        total = 0.0\n"
                            "        for i in range(n):\n"
                            "            total += src[i]\n"
                            "        return total\n"
                            "    f = sum_n(close, fast)")
        got, _ = ptp.fix_history(code)
        assert "def sum_n(src: Series[float], n):" in got

    def test_container_subscript_untouched(self):
        """`parts = sess.split('-')` 的下标是正常 Python，加了注解反而会把策略搞坏。"""
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            '    parts = "09-15".split("-")\n'
                            "    f = ta.sma(close, int(parts[0]))")
        assert ptp.fix_history(code) == (code, [])

    def test_already_declared_is_untouched(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    f: Series[float] = ta.sma(close, fast)")
        assert ptp.fix_history(code) == (code, [])

    def test_builtin_price_series_untouched(self):
        code = GOOD.replace("    up: Series[bool] = f > s", "    up: Series[bool] = close[1] > s")
        assert ptp.fix_history(code) == (code, [])

    def test_idempotent(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    mid, up_band, low_band = ta.bb(close, fast, 2.0)")
        code = code.replace("    up: Series[bool] = f > s", "    up: Series[bool] = up_band[1] > s")
        once, _ = ptp.fix_history(code)
        assert ptp.fix_history(once) == (once, [])

    def test_syntax_error_left_alone(self):
        bad = "def main(:\n"
        assert ptp.fix_history(bad) == (bad, [])


class TestConditionalSeries:
    """分支里的 Series 声明只在那个分支执行，其它分支的赋值走 set()，空缓冲上 set()
    返回 na（pynecore transformers/series.py 原话），整个变量恒为 na。实测 0165 的
    main_ma_val、0521 的 os 全 bar nan → 0 笔成交。改法：链前 hoist 一条声明。
    """

    IF_ELSE = ("    if fast > slow:\n"
               "        x: Series[float] = close * 2.0\n"
               "    else:\n"
               "        x = close * 3.0\n")

    def test_if_else_hoisted_before_chain(self):
        code = GOOD.replace("    up: Series[bool] = f > s", self.IF_ELSE + "    up: Series[bool] = x > s")
        got, notes = ptp.fix_conditional_series(code)
        assert "    x: Series[float] = float('nan')\n    if fast > slow:" in got
        assert "        x = close * 2.0\n" in got
        assert "x: Series[float] = close * 2.0" not in got
        assert notes == ["`x` 的 Series 声明从分支里提到 if 链之前"
                         "（分支里的声明只在那一支生效，其它分支的取值/历史会错位）"]

    def test_if_elif_else_all_branches_plain(self):
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    if fast == 1:\n"
                            "        x: Series[float] = close\n"
                            "    elif fast == 2:\n"
                            "        x = open\n"
                            "    else:\n"
                            "        x = high\n"
                            "    up: Series[bool] = x > s")
        got, _ = ptp.fix_conditional_series(code)
        assert "    x: Series[float] = float('nan')\n    if fast == 1:" in got
        assert "        x = close\n" in got
        assert got.count("Series[float]") == 1

    def test_all_branches_annotated_hoisted(self):
        """两个分支各写一条声明也不行：读的是最后注册的槽，先注册那支的历史冻住
        （实测 probe_Y：z[1] 永远停在第一根 bar）——要全部降级成普通赋值。"""
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    if fast == 1:\n"
                            "        x: Series[float] = close\n"
                            "    else:\n"
                            "        x: Series[float] = open\n"
                            "    up: Series[bool] = x > s")
        got, notes = ptp.fix_conditional_series(code)
        assert "    x: Series[float] = float('nan')\n    if fast == 1:" in got
        assert got.count("Series[float]") == 1
        assert "        x = close\n" in got and "        x = open\n" in got
        assert len(notes) == 1

    def test_helper_returning_in_every_branch_untouched(self):
        """纯辅助函数按类型算完就 return，没有跨 bar 语义，别报也别改（0059 的 ma_func）。"""
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    def pick(mode):\n"
                            "        if mode == 1:\n"
                            "            x: Series[float] = close\n"
                            "            return x\n"
                            "        else:\n"
                            "            x: Series[float] = open\n"
                            "            return x\n"
                            "    f = ta.sma(close, pick(fast))")
        assert ptp.fix_conditional_series(code) == (code, [])
        assert not any("Series 声明写在 if 分支里" in p for p in ptp.static_check(code))

    def test_no_else_reported_not_fixed(self):
        """没有 else：有路径不赋值，hoist 后那条路径会变 na（原语义是保留旧值），不敢机械改。"""
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    if fast > slow:\n"
                            "        x: Series[float] = close\n"
                            "    elif fast < slow:\n"
                            "        x = open\n"
                            "    up: Series[bool] = x > s")
        assert ptp.fix_conditional_series(code) == (code, [])
        assert any("x" in p and "Series 声明写在 if 分支里" in p
                   for p in ptp.static_check(code))

    def test_duplicate_declaration_reported(self):
        """0521 的写法：顶层一条 PersistentSeries + 分支里又一条 Series——同名两条声明。"""
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    x: PersistentSeries[float] = 0.0\n"
                            "    if fast > slow:\n"
                            "        x: Series[float] = close\n"
                            "    else:\n"
                            "        x = open\n"
                            "    up: Series[bool] = x > s")
        assert ptp.fix_conditional_series(code) == (code, [])
        assert any("x" in p and "Series 声明写在 if 分支里" in p
                   for p in ptp.static_check(code))

    def test_read_before_chain_reported_not_fixed(self):
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    y: Series[float] = x * 2.0\n"
                            "    if fast > slow:\n"
                            "        x: Series[float] = close\n"
                            "    else:\n"
                            "        x = open\n"
                            "    up: Series[bool] = y > s")
        assert ptp.fix_conditional_series(code) == (code, [])
        assert any("x" in p and "Series 声明写在 if 分支里" in p
                   for p in ptp.static_check(code))

    def test_same_branch_add_then_set_untouched(self):
        """同一分支里 add 之后再 set 是安全的（缓冲已经喂过），不能误报。"""
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    if fast > slow:\n"
                            "        x: Series[float] = close\n"
                            "        x = x * 2.0\n"
                            "    up: Series[bool] = x > s")
        assert ptp.fix_conditional_series(code) == (code, [])
        assert not any("x" in p and "Series 声明写在 if 分支里" in p
                       for p in ptp.static_check(code))

    def test_lone_conditional_declaration_untouched(self):
        """只有一个分支里声明、没有别处赋值：没有 set() 问题，保守不动。"""
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    if fast > slow:\n"
                            "        x: Series[float] = close\n"
                            "    up: Series[bool] = x > s")
        assert ptp.fix_conditional_series(code) == (code, [])
        assert not any("Series 声明写在 if 分支里" in p for p in ptp.static_check(code))

    def test_idempotent(self):
        code = GOOD.replace("    up: Series[bool] = f > s", self.IF_ELSE + "    up: Series[bool] = x > s")
        once, _ = ptp.fix_conditional_series(code)
        assert ptp.fix_conditional_series(once) == (once, [])

    def test_autofix_includes_conditional(self):
        code = GOOD.replace("    up: Series[bool] = f > s", self.IF_ELSE + "    up: Series[bool] = x > s")
        got, notes = ptp.autofix(code)
        assert "    x: Series[float] = float('nan')\n    if fast > slow:" in got
        assert any("提到 if 链之前" in n for n in notes)


class TestPersistentHistory:
    """Persistent 没有历史（`p[1]` 抛 TypeError）；Persistent[list] 的 `[i]` 是列表下标。"""

    def test_persistent_with_history_read_flagged(self):
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    p: Persistent[float] = 0.0\n"
                            "    up: Series[bool] = p[1] > s")
        assert any("p" in x and "没有历史" in x for x in ptp.static_check(code))

    def test_persistent_list_index_not_flagged(self):
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    a: Persistent[list] = []\n"
                            "    a.append(close)\n"
                            "    up: Series[bool] = a[0] > s")
        assert not any("没有历史" in x for x in ptp.static_check(code))

    def test_persistent_series_history_read_passes(self):
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    p: PersistentSeries[float] = 0.0\n"
                            "    up: Series[bool] = p[1] > s")
        assert not any("没有历史" in x for x in ptp.static_check(code))

    def test_fix_history_does_not_duplicate_declaration(self):
        """0521 踩过：fix_history 只认 Series，给 PersistentSeries 声明的名字又补一条 Series。"""
        code = GOOD.replace("    up: Series[bool] = f > s",
                            "    p: PersistentSeries[float] = 0.0\n"
                            "    up: Series[bool] = p[1] > s")
        assert ptp.fix_history(code) == (code, [])


class TestListContainer:
    """Pine 数组装进 PersistentSeries 时 `a[i]` 是历史取值不是列表下标——实测 0521 报
    `TypeError: '>=' not supported between instances of 'list' and 'int'`。"""

    def test_persistent_series_list_flagged(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    a: PersistentSeries[list] = []\n"
                            "    f = ta.sma(close, fast)")
        assert any("PersistentSeries[list]" in p and "Persistent[list]" in p
                   for p in ptp.static_check(code))

    def test_persistent_list_passes(self):
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    a: Persistent[list] = []\n"
                            "    f = ta.sma(close, fast)")
        assert not any("PersistentSeries[list]" in p for p in ptp.static_check(code))


class TestListRepeat:
    """`[初值] * n` 的乘数在 pyne 里是浮点——input 参数是浮点、`int()` 被翻成 safe_int()
    也返回浮点——`list * float` 直接 TypeError（实测 0248/0253/0260 全挂在这）。"""

    def _code(self, stmt: str) -> str:
        return GOOD.replace("    f = ta.sma(close, fast)",
                            f"    {stmt}\n    f = ta.sma(close, fast)")

    def test_float_multiplier_rewritten(self):
        code, notes = ptp.fix_list_repeat(self._code("a: Persistent[list] = [False] * slow"))
        assert "a: Persistent[list] = [False for _ in range(slow)]" in code
        assert any("range(slow)" in n for n in notes), notes

    def test_pine_int_cast_rewritten(self):
        code, _ = ptp.fix_list_repeat(self._code("a: Persistent[list] = [False] * int(slow)"))
        assert "[False for _ in range(int(slow))]" in code

    def test_reversed_order_rewritten(self):
        code, _ = ptp.fix_list_repeat(self._code("a: Persistent[list] = slow * [0.0]"))
        assert "[0.0 for _ in range(slow)]" in code

    def test_int_literal_left_alone(self):
        src = self._code("a: Persistent[list] = [False] * 20")
        assert ptp.fix_list_repeat(src) == (src, [])

    def test_multi_element_reported_not_fixed(self):
        src = self._code("a: Persistent[list] = [False, True] * slow")
        assert ptp.fix_list_repeat(src) == (src, [])
        assert any("array.new_bool" in p for p in ptp.static_check(src)), ptp.static_check(src)

    def test_non_constant_element_reported(self):
        src = self._code("a: Persistent[list] = [close] * slow")
        assert ptp.fix_list_repeat(src) == (src, [])
        assert any("array.new_bool" in p for p in ptp.static_check(src))

    def test_autofix_integration(self):
        code, notes = ptp.autofix(self._code("a: Persistent[list] = [False] * slow"))
        assert "[False for _ in range(slow)]" in code
        assert any("range(slow)" in n for n in notes), notes

    def test_idempotent(self):
        once, _ = ptp.fix_list_repeat(self._code("a: Persistent[list] = [False] * slow"))
        assert ptp.fix_list_repeat(once) == (once, [])


class TestImportFix:
    """漏 import 是实测最高频的失败（模型修两轮仍会漏），能机械补的就别留给模型。"""

    def test_lib_name_added(self):
        src = GOOD.replace("    f = ta.sma(close, fast)",
                           "    f = ta.sma(close, fast)\n    tf = timeframe.period")
        code, notes = ptp.fix_imports(src)
        assert "from pynecore.lib import close, input, script, strategy, ta, timeframe" in code
        assert any("timeframe" in n for n in notes), notes

    def test_type_name_appended(self):
        src = GOOD.replace("    f = ta.sma(close, fast)",
                           "    a: Persistent[list] = []\n    f = ta.sma(close, fast)")
        code, notes = ptp.fix_imports(src)
        assert "from pynecore.types import Series, Persistent" in code
        assert any("Persistent" in n for n in notes)

    def test_type_import_line_inserted(self):
        src = GOOD.replace("from pynecore.types import Series\n", "")
        code, _ = ptp.fix_imports(src)
        assert "from pynecore.types import Series" in code

    def test_defined_name_not_imported(self):
        src = GOOD.replace("    f = ta.sma(close, fast)",
                           '    session = "09:00"\n    f = ta.sma(close, fast)')
        assert ptp.fix_imports(src) == (src, [])

    def test_static_check_clean_after_fix(self):
        src = GOOD.replace("    f = ta.sma(close, fast)",
                           "    f = ta.sma(close, fast)\n    tf = timeframe.period")
        code, _ = ptp.fix_imports(src)
        assert not any("导入" in p for p in ptp.static_check(code))

    def test_autofix_integration(self):
        src = GOOD.replace("    f = ta.sma(close, fast)",
                           "    f = ta.sma(close, fast)\n    tf = timeframe.period")
        code, notes = ptp.autofix(src)
        assert "timeframe" in code.splitlines()[5]
        assert any("timeframe" in n for n in notes)

    def test_idempotent(self):
        src = GOOD.replace("    f = ta.sma(close, fast)",
                           "    f = ta.sma(close, fast)\n    tf = timeframe.period")
        once, _ = ptp.fix_imports(src)
        assert ptp.fix_imports(once) == (once, [])


class TestHistoryProblems:
    def test_reports_ambiguous_history_read(self):
        """名字既当参数又被赋值（都还是标量）时机械改不了，静态闸要报出来让模型改。"""
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            "    def scale(src, n):\n"
                            "        src = src * 2.0\n"
                            "        return src[n]\n"
                            "    f = scale(close, fast)")
        probs = ptp.static_check(code)
        assert any("src" in p and "Series" in p for p in probs), probs

    def test_container_binding_is_not_reported(self):
        """`parts = sess.split('-')` 的下标是正常 Python，静态闸不能误报。"""
        code = GOOD.replace("    f = ta.sma(close, fast)",
                            '    parts = "09-15".split("-")\n'
                            "    f = ta.sma(close, int(parts[0]))")
        assert not any("parts" in p for p in ptp.static_check(code))


class TestSizingCheck:
    """口径闸校验**取值**：跨策略比较的前提是同一本金 + 同一仓位类型。"""

    def test_template_sizing_passes(self):
        assert not any("仓位口径" in p for p in ptp.static_check(GOOD))

    def test_tiny_percent_position_flagged(self):
        """小本金 × 小百分比：A 股按手成交会算出不足 1 股，整场 0 笔。"""
        code = (GOOD.replace("initial_capital=100000", "initial_capital=1000")
                    .replace("default_qty_value=95", "default_qty_value=1"))
        assert any("仓位口径必须与库内统一" in p for p in ptp.static_check(code))

    def test_fixed_qty_flagged(self):
        """固定手数配不同本金 = 杠杆随本金变（实测 58 份偏离里最要命的一类）。"""
        code = (GOOD.replace("default_qty_type=strategy.percent_of_equity",
                             "default_qty_type=strategy.fixed")
                    .replace("default_qty_value=95", "default_qty_value=1"))
        assert any("percent_of_equity" in p for p in ptp.static_check(code))

    def test_missing_sizing_flagged(self):
        code = GOOD.replace(
            "                 default_qty_type=strategy.percent_of_equity,"
            " default_qty_value=95,\n                 initial_capital=100000", "")
        assert any("缺少统一口径的仓位参数" in p for p in ptp.static_check(code))

    def test_wrong_capital_flagged(self):
        code = GOOD.replace("initial_capital=100000", "initial_capital=10000")
        assert any("10000" in p for p in ptp.static_check(code))


class TestRefreshProblems:
    """复用已有候选时必须用**当前**闸门重算——否则收紧规则后重跑等于白跑。"""

    def test_recomputes_and_writes_meta(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path)
        d = tmp_path / "0001"
        d.mkdir()
        (d / "candidate.py").write_text(
            GOOD.replace("initial_capital=100000", "initial_capital=10000"),
            encoding="utf-8")
        (d / "meta.json").write_text(json.dumps({"title": "甲", "problems": []}),
                                     encoding="utf-8")

        meta = ptp._refresh_problems("0001", {"title": "甲", "problems": []})

        assert meta["problems"] and "仓位口径" in meta["problems"][0]
        saved = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert saved["problems"] == meta["problems"] and saved["title"] == "甲"

    def test_unchanged_problems_returns_same_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path)
        d = tmp_path / "0001"
        d.mkdir()
        (d / "candidate.py").write_text(GOOD, encoding="utf-8")
        meta = {"title": "甲", "problems": []}
        assert ptp._refresh_problems("0001", meta) is meta
        assert not (d / "meta.json").exists()      # 没变就别写盘

    def test_missing_candidate_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path)
        meta = {"problems": ["旧的"]}
        assert ptp._refresh_problems("0001", meta) is meta


class TestListTranspile:
    def test_reads_meta_and_report(self, tmp_path, monkeypatch):
        d = tmp_path / "pine_transpile/0001"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(
            {"title": "甲", "model": "m", "problems": ["缺少必需结构：def main("]}),
            encoding="utf-8")
        (d / "candidate.py").write_text("x", encoding="utf-8")
        (d / "report.json").write_text(json.dumps({
            "symbol": "600309.SH", "trades": 7,
            "stats": {"Net profit": {"pct": 12.5},
                      "Max equity drawdown": {"pct": 8.0},
                      "Buy & hold return": {"pct": 30.0}}}), encoding="utf-8")
        monkeypatch.setattr(pl, "transpile_dir", lambda: tmp_path / "pine_transpile")

        got = pl.list_transpile()["0001"]
        assert got["title"] == "甲"
        assert got["problems"] == ["缺少必需结构：def main("]
        assert got["has_candidate"] and got["has_report"]
        assert got["net_pct"] == 12.5 and got["bh_pct"] == 30.0 and got["trades"] == 7

    def test_missing_dir_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pl, "transpile_dir", lambda: tmp_path / "nope")
        assert pl.list_transpile() == {}

    def test_broken_json_is_skipped(self, tmp_path, monkeypatch):
        d = tmp_path / "pine_transpile/0002"
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{ 不是 json", encoding="utf-8")
        monkeypatch.setattr(pl, "transpile_dir", lambda: tmp_path / "pine_transpile")
        assert pl.list_transpile() == {}


class TestCandidateOverride:
    """`--source` / `--out`：agent 改策略的候选绝不覆盖正式产物。"""

    def test_default_out_is_official(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path / "official")
        monkeypatch.setattr(ptp, "OUT_OVERRIDE", None)
        assert ptp._out("0001") == tmp_path / "official/0001"

    def test_out_override_points_at_candidate_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ptp, "OUT_DIR", tmp_path / "official")
        monkeypatch.setattr(ptp, "OUT_OVERRIDE", tmp_path / "chat/0001/job1")
        assert ptp._out("0001") == tmp_path / "chat/0001/job1"

    def test_load_pine_keeps_library_metadata(self, tmp_path, monkeypatch):
        """候选只换源码：标题/分类仍取正式策略，界面才认得出是哪一条。"""
        src = tmp_path / "candidate.pine"
        src.write_text('//@version=5\nstrategy("改过的")\n', encoding="utf-8")
        monkeypatch.setattr(ptp, "SOURCE_OVERRIDE", src)
        monkeypatch.setattr(pl, "get_source", lambda i: {
            "id": i, "title": "原策略", "category": "趋势",
            "source": '//@version=5\nstrategy("原")\n'})

        got = ptp._load_pine("0001")
        assert got["title"] == "原策略" and got["category"] == "趋势"
        assert got["source_kind"] == "candidate" and "改过的" in got["source"]

    def test_load_pine_survives_unknown_id(self, tmp_path, monkeypatch):
        src = tmp_path / "candidate.pine"
        src.write_text("//@version=5\n", encoding="utf-8")
        monkeypatch.setattr(ptp, "SOURCE_OVERRIDE", src)

        def boom(_):
            raise ValueError("没有这条策略")

        monkeypatch.setattr(pl, "get_source", boom)
        assert ptp._load_pine("x001")["source_kind"] == "candidate"

    def test_transpile_writes_to_override_not_official(self, tmp_path, monkeypatch):
        official, out = tmp_path / "official", tmp_path / "chat/0001/job1"
        src = tmp_path / "candidate.pine"
        src.write_text('//@version=5\nstrategy("改过的")\n', encoding="utf-8")
        monkeypatch.setattr(ptp, "OUT_DIR", official)
        monkeypatch.setattr(ptp, "OUT_OVERRIDE", out)
        monkeypatch.setattr(ptp, "SOURCE_OVERRIDE", src)
        monkeypatch.setattr(ptp, "call_llm",
                            lambda *a, **k: (f"```python\n{GOOD}\n```", {"total_tokens": 1}, False))

        meta = ptp.transpile("0001")
        assert (out / "candidate.py").exists() and (out / "meta.json").exists()
        assert meta["id"] == "0001"
        assert not official.exists()          # 正式产物一个字节都没动
