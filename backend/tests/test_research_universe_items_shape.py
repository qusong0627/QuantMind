"""候选池接口的 `items` 必须是真数组，而不是被尾逗号包出来的单元素 tuple。

实测 2026-09：`/api/v1/research/universe?date=...` 返回
`{"data": {"items": [[{...}, {...}]], "summary": {"total": 3271}}}` ——
`items` 是一个长度 1 的数组，里面才是那 3271 行。成因是 `get_research_universe_by_date`
里构造 `items` 的列表推导式后面多了一个逗号，`x = [ ... ],` 在 Python 里是
`x = ([...],)`，静默变成 tuple。

前端 `researchService.getResearchUniverseByDate` 直接 `candidates: data.items`，
页面再 `.map()` 出行；于是候选池计数显示 3271、表格一行都不会渲染 ——
这就是投研平台「没有数据了」的表象之一。

这里不去 mock 整条取数链路（要造 pred.parquet、PG、QuantDB 三份数据），
而是直接扫源码里这一类**静默改变数据形状**的写法，顺带覆盖全后端同类笔误。
判据见 `_tuple_wrapped_literals`：右侧是单元素 Tuple，且该元素源码末尾那一行
紧随其后真的是逗号。显式写法 `x = (a,)` 和多行 `x = (\\n a,\\n)` 的尾巴是
`)` 或空行，不会被误报。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "node_modules", "__pycache__", "data", "logs"}


def _tuple_wrapped_literals(path: Path) -> list[tuple[int, tuple[str, ...]]]:
    """返回 `x = <字面量>,` 的 (行号, 赋值目标) 列表。"""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (SyntaxError, UnicodeDecodeError):
        return []
    lines = text.splitlines()
    found: list[tuple[int, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Tuple):
            continue
        if len(node.value.elts) != 1:
            continue
        inner = node.value.elts[0]
        if inner.end_lineno is None:
            continue
        names = tuple(t.id for t in node.targets if isinstance(t, ast.Name))
        if not names:
            continue
        tail = lines[inner.end_lineno - 1][inner.end_col_offset :]
        if not tail.lstrip().startswith(","):
            continue
        if isinstance(
            inner,
            (ast.List, ast.ListComp, ast.Dict, ast.DictComp, ast.Set, ast.SetComp),
        ):
            found.append((node.lineno, names))
    return found


def _scan_backend() -> list[str]:
    offenders: list[str] = []
    for path in BACKEND_ROOT.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        for lineno, names in _tuple_wrapped_literals(path):
            rel = path.relative_to(BACKEND_ROOT)
            offenders.append(f"{rel}:{lineno} -> {', '.join(names)}")
    return offenders


class TestNoTrailingCommaTuples:
    def test_backend_has_no_tuple_wrapped_literal_assignments(self):
        offenders = _scan_backend()
        assert not offenders, (
            "以下赋值被尾逗号包成了单元素 tuple，会静默改变接口数据形状：\n  "
            + "\n  ".join(offenders)
        )

    def test_detector_is_not_vacuous(self, tmp_path):
        # 这条测试是上一条的地基：判定器自己必须先能命中已知坏例，
        # 否则「扫描无结果」只是没扫到而已。
        bad = tmp_path / "bad.py"
        bad.write_text(
            "def f(rows):\n"
            "    items = [\n"
            "        {'a': r}\n"
            "        for r in rows\n"
            "    ],\n"
            "    return items\n",
            encoding="utf-8",
        )
        good = tmp_path / "good.py"
        good.write_text(
            "def f(rows):\n"
            "    items = [\n"
            "        {'a': r}\n"
            "        for r in rows\n"
            "    ]\n"
            "    explicit = (items,)\n"
            "    return items, explicit\n",
            encoding="utf-8",
        )

        assert _tuple_wrapped_literals(bad) == [(2, ("items",))]
        assert _tuple_wrapped_literals(good) == []

    def test_research_service_items_is_plain_list(self):
        # 定点：投研平台候选池那个赋值点单独钉一次，避免有人用 `# noqa` 绕开全量扫描
        target = BACKEND_ROOT / "services" / "api" / "routers" / "research_service.py"
        assert _tuple_wrapped_literals(target) == []


@pytest.mark.parametrize(
    "snippet",
    ["items = [r for r in rows],", "payload = {'a': 1},"],
)
def test_detector_catches_other_literal_kinds(snippet, tmp_path):
    src = tmp_path / "snippet.py"
    src.write_text(f"def f(rows):\n    {snippet}\n    return 1\n", encoding="utf-8")
    assert _tuple_wrapped_literals(src), f"未命中: {snippet}"


def test_bare_name_packing_is_deliberately_not_flagged(tmp_path):
    """`items = rows,` 是合法的 tuple 打包，判据只盯**字面量容器**。

    两者的区别在于看不看得见：`rows,` 一眼就是元组，而 `[r for r in rows],`
    长得跟正常列表推导一模一样，形状却在没人注意的地方变了 —— 后者才是要拦的。
    """
    src = tmp_path / "snippet.py"
    src.write_text("def f(rows):\n    items = rows,\n    return items\n", encoding="utf-8")

    assert _tuple_wrapped_literals(src) == []
