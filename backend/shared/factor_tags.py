"""因子六维标签派生 + 表达式要素抽取（T-FE-07，唯一实现，勿在别处重写）。

用途：因子注册中心（``rd_agent_factors``）的最低元数据面——六维标签
（数据源/频率/家族/变换/目标/成熟度）与表达式依赖要素（字段/算子/窗口），
供 API 装配（``routers/alpha_agent.py`` 因子库列表/详情）与注册报告消费。

口径边界：
- 本模块在 ``shared/``，**不得 import 任何服务模块**（跨服务边界）；抽取
  是**字符串级**（无 AST、无 eval、线性扫描），FM-DSL 的 AST 级结构面在
  ``factorminer/structure.py``（T-FM-06）、统计派复杂度计数在
  ``statistical/complexity.py``（T-FE-05）——本口径是「要素级」，不冒充
  结构级，三者互不重写。
- **缺失一律 ``unknown``，不猜**；唯一例外是两条有单源依据的派生：
  ``fm:`` 前缀 → 数据源 ``factorminer``（``FM_CODE_PREFIX`` 是 FM-DSL 的
  唯一标记）；频率缺省 ``daily``（平台当前全量日频——接入分钟因子时须在
  写侧标 ``metadata.frequency``）。
- 成熟度（maturity）由生命周期状态机（``shared/factor_lifecycle.py``）
  派生后传入，本模块不持第二套映射（单一 source of truth）。
- 超长文本按不可信输入设计：总量截断 + 括号扫描限长，防御性上限见常量；
  表达式本属内部产物（数百字符），上限只在对抗场景生效。

纯标准库实现，便于在挖掘子进程/脚本/引擎/测试中直接 import。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "TAG_DIMENSIONS",
    "derive_tags",
    "extract_expression_deps",
    "read_metadata",
]

#: 六维标签为最小集（§1.2：因子注册中心的最低元数据）。
TAG_DIMENSIONS: tuple[str, ...] = (
    "data_source",
    "frequency",
    "family",
    "transform",
    "target",
    "maturity",
)

_UNKNOWN = "unknown"

#: FM-DSL 表达式前缀（与 ``factorminer.dsl.FM_CODE_PREFIX`` 同值——shared
#: 不能 import 引擎模块，此常量在双端各一处，改动须两处同步）。
_FM_PREFIX = "fm:"

#: 字段引用（FM-DSL 与 qlib 中缀同用 ``$`` 前缀）。
_FIELD_RE = re.compile(r"\$([A-Za-z_]\w*)")

#: 函数调用：标识符紧跟可选的空白与左括号（与 complexity.py 同形态）。
_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")

#: 数值字面量（含小数与科学计数法）；前缀是词字符或点的数字串属标识符
#: （``turn_1``/``Ref20``），不计——与 complexity.py 同一条排除规则。
_NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")

#: 对抗性输入防御：表达式超过此长度只处理前缀（标签用途，截断无损大局）。
_MAX_EXPR_CHARS = 65536

#: 单次调用参数的括号扫描上限（未闭合调用不吞整段文本）。
_MAX_PAREN_SCAN = 512


def read_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """行 → 元数据 dict；兼容 ``metadata`` dict 与 ``metadata_json``（JSONB
    字符串/ dict 形态）；坏 JSON 一律退化空 dict（标签面不对脏数据抛错）。"""
    raw = row.get("metadata")
    if not isinstance(raw, Mapping):
        raw = row.get("metadata_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _call_args(text: str, open_idx: int) -> str:
    """从 ``(`` 处做平衡括号扫描，取参数文本（未闭合/超限宽容截断）。"""
    depth = 0
    limit = min(len(text), open_idx + _MAX_PAREN_SCAN)
    for i in range(open_idx, limit):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    return text[open_idx + 1 : limit]


def _to_number(raw: str) -> int | float:
    """数值字面量文本 → int（无小数点/指数）或 float。"""
    if "." in raw or "e" in raw or "E" in raw:
        return float(raw)
    return int(raw)


def extract_expression_deps(code: str | None) -> dict[str, list[Any]]:
    """表达式 → 依赖要素 ``{fields, operators, windows}``（保序去重）。

    - ``fields``：``$name`` 引用（行情字段；FM-DSL 与 qlib 同语法）；
    - ``operators``：函数调用名（``ts_mean(`` → ``ts_mean``）；
    - ``windows``：**调用参数内**的数值字面量（``Ref($close,5)`` → 5）——
      裸字面量（``-1``/``1e-8``）不算窗口，它们不是窗口参数。

    纯函数；永不抛（垃圾输入退化空集）；同输入恒同输出。
    """
    text = str(code or "")[:_MAX_EXPR_CHARS]

    fields: list[str] = []
    for name in _FIELD_RE.findall(text):
        if name not in fields:
            fields.append(name)

    operators: list[str] = []
    windows: list[Any] = []
    for match in _CALL_RE.finditer(text):
        name = match.group(1)
        if name not in operators:
            operators.append(name)
        args = _call_args(text, match.end() - 1)
        for raw in _NUM_RE.findall(args):
            value = _to_number(raw)
            if value not in windows:
                windows.append(value)

    return {"fields": fields, "operators": operators, "windows": windows}


def derive_tags(
    row: Mapping[str, Any],
    *,
    maturity: str | None = None,
    family_hint: str | None = None,
) -> dict[str, str]:
    """因子行 → 六维标签 dict（键恒为 :data:`TAG_DIMENSIONS` 全集）。

    派生链（写侧权威优先，读侧推导兜底，缺则 ``unknown``）：
    - data_source：``metadata.source`` → ``metadata.algorithm`` → ``fm:``
      前缀 → ``factorminer`` → ``unknown``；
    - frequency：``metadata.frequency`` → ``daily``（平台当前事实）；
    - family：``metadata.family``（写侧权威）→ ``family_hint``（读侧推导，
      如 FM 分类函数）→ ``unknown``；
    - transform：表达式调用名序列 ``"/"`` 连接 → ``unknown``；
    - target：``metadata.target`` → ``metadata.horizon`` →
      ``fwd_ret_{h}d`` → ``fwd_ret_1d``（平台训练/基准的次日收益口径）；
    - maturity：由调用方传（生命周期状态机派生）→ ``unknown``。
    """
    meta = read_metadata(row)
    code = str(row.get("factor_code") or "")

    source = meta.get("source") or meta.get("algorithm")
    if not source and code.startswith(_FM_PREFIX):
        source = "factorminer"
    if not source:
        source = _UNKNOWN

    frequency = str(meta.get("frequency") or "daily")

    family = meta.get("family") or family_hint or _UNKNOWN

    operators = extract_expression_deps(code)["operators"]
    transform = "/".join(operators) if operators else _UNKNOWN

    target = meta.get("target")
    if not target:
        horizon = meta.get("horizon")
        target = f"fwd_ret_{horizon}d" if horizon not in (None, "") else "fwd_ret_1d"

    return {
        "data_source": str(source),
        "frequency": str(frequency),
        "family": str(family),
        "transform": str(transform),
        "target": str(target),
        "maturity": str(maturity) if maturity else _UNKNOWN,
    }
