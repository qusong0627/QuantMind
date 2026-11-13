"""策略库索引：缩进判据。

「可编译」这个标记直接决定用户能不能挑到策略，判据必须两头都不误：旧版爬虫把块体拍平
的要抓出来，没有块语句的合法短脚本不能误伤（0970 就是全用 when= 被误标过的）。

运行（容器）：python3 -m pytest backend/tests/test_pine_library_index.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.pine_library_index import _indent_ok, _meta  # noqa: E402


class TestIndentOk:
    def test_flattened_block_is_flagged(self):
        """旧版爬虫把 if 体拍到第 0 列——编译不了，必须判 False。"""
        assert _indent_ok(["//@version=5", 'strategy("t")',
                           "if close > open", "x := 1"]) is False

    def test_properly_indented_block_passes(self):
        assert _indent_ok(["//@version=5", 'strategy("t")',
                           "if close > open", "    x := 1"]) is True

    def test_no_block_statements_is_not_a_defect(self):
        """全用 when= / 表达式的脚本一行不缩进也合法（0970 被误伤过）。"""
        assert _indent_ok(["//@version=5", 'strategy("t")',
                           "x = ta.sma(close, 5)",
                           'strategy.entry("L", strategy.long, when = x)']) is True

    def test_function_definition_body(self):
        assert _indent_ok(["f(x) =>", "    x * 2"]) is True
        assert _indent_ok(["f(x) =>", "x * 2"]) is False

    def test_comment_before_body_does_not_fool_it(self):
        assert _indent_ok(["if a", "// 注释", "    x := 1"]) is True
        assert _indent_ok(["if a", "// 注释", "x := 1"]) is False

    def test_for_while_else_switch(self):
        for head in ("for i = 0 to 3", "while a", "else", "switch x"):
            assert _indent_ok([head, "    y := 1"]) is True, head
            assert _indent_ok([head, "y := 1"]) is False, head


class TestMeta:
    def test_reports_version_lines_and_strategy(self):
        got = _meta("//@version=5\nstrategy(\"t\")\nif a\n    b := 1\n")
        assert got["version"] == 5 and got["lines"] == 4
        assert got["indent_ok"] is True and got["has_strategy"] is True

    def test_missing_version_is_zero(self):
        assert _meta("x = 1\n")["version"] == 0
