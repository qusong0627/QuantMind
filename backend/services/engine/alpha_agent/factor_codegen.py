"""待评估因子「补码」：按公式+描述用 LLM 还原 RD-Agent 因子实现代码。

背景（2026-10-09）：因子库「待评估」页签的存量因子来自 2026-09-13 之前的旧
挖掘批次——旧提取器把 coding 阶段未完成的半成品也落了库（factor_code 为空串，
见修复提交 417ded10），旧任务工作区已按留存策略清理、代码无法找回，物化/回测/
训练三条路都因缺码而堵。本模块负责把这类因子按**挖掘侧同一套执行契约**补出
实现代码：补出的代码经静态校验后写回 factor_code，再由 alpha_agent 路由的
「补码评估」批次自动跑标准回测补 IC。

生成契约（唯一提示词出口，与子进程执行器 ``_run_functional_factor_subprocess``
的两种入口样式对齐；改这里必须同步那张实现）：

- 单文件、只用 pandas/numpy（执行环境不装其他三方库）；
- 读**相对路径** ``daily_pv.h5``（HDF5 key='data'，MultiIndex (datetime,
  instrument)，列名可能带 ``$`` 前缀）；
- 自执行式：``main()`` + ``if __name__ == '__main__':`` 守卫，算完把因子值
  Series 写 ``result.h5``（key='data'）；
- 备选：零参 ``calculate_<name>()`` 函数（有默认参数）。

安全边界：本模块只做**静态**校验（compile + 禁项扫描），不执行代码；真正的
隔离执行在回测链路的子进程里，与本模块无关。
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class FactorCodegenError(RuntimeError):
    """补码生成失败（LLM 不可用 / 输出无代码 / 静态校验不过）。"""


#: 生成的代码里禁止出现的模块（执行环境不需要它们；出现即视为跑偏/危险）。
_FORBIDDEN_IMPORTS = {
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "http",
    "ftplib",
    "ctypes",
    "importlib",
    "multiprocessing",
    "shutil",
    "pickle",
}
#: 禁止调用的内建/属性（动态执行、外联、文件系统破坏）。
_FORBIDDEN_CALLS = {"eval", "exec", "__import__", "compile"}
_FORBIDDEN_ATTRS = {("os", "system"), ("os", "popen")}

_CODE_FENCE_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)

_SYSTEM_PROMPT = """\
你是量化因子实现工程师。请把给定的因子公式/描述翻译成**单个 Python 文件**。
代码将在隔离子进程中执行，必须严格满足以下契约（违反任何一条都会导致评估失败）：

1. 只允许 import pandas、numpy 以及 math/typing 等纯标准库；禁止网络、子进程、
   eval/exec、文件系统操作（下述数据读写除外）。
2. 数据读取：读相对路径 `daily_pv.h5`（HDF5，key='data'，两层 MultiIndex
   (datetime, instrument)）。列名可能带 `$` 前缀（如 `$close`）也可能不带，
   必须用兼容写法：
   ```python
   def _col(df, name):
       return df[f"${name}"] if f"${name}" in df.columns else df[name]
   ```
   常用列：open / high / low / close / volume / amount（amount 缺失时退化为
   close*volume）。
3. 结构归一：若读取后不是 MultiIndex，执行
   `df = df.set_index(["datetime", "instrument"])`，随后 `df = df.sort_index()`。
   时间序列运算用 `df.groupby(level="instrument")`；截面运算用
   `df.groupby(level="datetime")`。
4. 严禁前视：时间序列只能使用当前及更早数据（`shift(n)`、`rolling` 等），
   禁止 `shift(-n)`、禁止任何使用未来数据的写法。
5. 输出：因子值 Series（索引与输入一致、命名为因子名）；把 inf 替换成 nan
   （`np.where(np.isfinite(v), v, np.nan)`）；**不要 fillna 成 0**（缺失保持 nan）。
6. 入口采用自执行式：定义 `def main():`（读数据 → 计算 → 写结果），文件以
   ```python
   if __name__ == "__main__":
       main()
   ```
   收尾。main() 内用 `result.to_hdf("result.h5", key="data", mode="w")` 写出
   （result 为 Series；若算出 DataFrame 请取单列）。main 的参数带默认值
   （`data_path="daily_pv.h5", output_path="result.h5"`）。
7. 代码自包含、可直接运行；公式中的每个中间量先定义再使用；不要输出注释以外
   的多余文字。

示例（动量因子，展示格式与风格）：
```python
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
```\
"""


def build_codegen_messages(factor: dict[str, Any]) -> list[dict[str, str]]:
    """按因子行组装 LLM 消息（公式+描述+类别+市场 → 补码提示词）。"""
    meta = factor.get("metadata") or {}
    name = factor.get("factor_name") or "unnamed_factor"
    formulation = (
        factor.get("factor_formulation")
        or meta.get("factor_formulation")
        or meta.get("formulation")
        or ""
    )
    description = meta.get("description") or ""
    category = meta.get("category") or ""
    market = factor.get("market") or "a_share"

    user = f"""因子名称：{name}
因子公式（LaTeX，可能缺失）：{formulation or "（缺失，按名称与描述推断）"}
因子描述：{description or "（无）"}
类别：{category or "（未标注）"}
市场：{market}

请输出该因子的完整实现代码：单个 ```python 代码块，不要输出其他内容。"""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def extract_python_code(text: str) -> str | None:
    """从 LLM 输出中抠出 Python 代码（围栏块优先；无围栏时整段兜底）。

    兜底路径不做智取——把整段交给 :func:`validate_factor_code` 用语法树筛，
    散文混排会在 parse 阶段被拒，绝不静默截取「看起来像代码」的半截。
    """
    match = _CODE_FENCE_RE.search(text or "")
    if match:
        code = match.group(1).strip()
        return code or None
    stripped = (text or "").strip()
    if stripped.startswith(("import ", "from ", "def ", "class ", "#")) and (
        "def " in stripped or "import " in stripped
    ):
        return stripped
    return None


def is_main_guard(test: ast.expr) -> bool:
    """``if __name__ == "__main__":`` 判定。"""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def validate_factor_code(code: str) -> tuple[bool, str]:
    """静态校验：语法可编译 + 有可调用入口 + 无禁项。

    不执行代码。Returns: ``(ok, reason)``——ok=False 时 reason 是给用户看
    （也回喂给 LLM 重试）的中文拒绝原因。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"代码语法错误: {exc.msg} (line {exc.lineno})"

    has_main_guard = False
    has_calculate = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and is_main_guard(node.test):
            has_main_guard = True
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("calculate_"):
            has_calculate = True
    if not has_main_guard and not has_calculate:
        return False, ("缺少入口：既没有 __main__ 自执行守卫，也没有 calculate_* 函数")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_IMPORTS:
                    return False, f"禁止导入 {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _FORBIDDEN_IMPORTS:
                return False, f"禁止导入 {node.module}"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
                return False, f"禁止调用 {func.id}"
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in _FORBIDDEN_ATTRS
            ):
                return False, f"禁止调用 {func.value.id}.{func.attr}"
    return True, ""


async def generate_factor_code(
    factor: dict[str, Any],
    *,
    config: Any,
    max_attempts: int = 2,
) -> str:
    """按因子公式/描述生成实现代码；静态校验不过时带原因重试。

    Args:
        factor: rd_agent_factors 行（factor_name/factor_formulation/metadata...）。
        config: LLMConfig（调用方已解析：用户 Profile 优先、env 兜底）。
        max_attempts: 含首次的最大尝试次数；每次失败把校验原因回喂后重发。

    Raises:
        FactorCodegenError: LLM 不可用、输出无代码，或重试后仍校验不过。
    """
    from backend.services.engine.alpha_agent.llm_client import chat

    messages = build_codegen_messages(factor)
    last_reason = "未知原因"
    for attempt in range(1, max_attempts + 1):
        try:
            text = await chat(
                messages,
                max_tokens=3000,
                temperature=0.2,
                timeout=120,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 —— 一律收成可上屏的补码失败
            raise FactorCodegenError(f"LLM 调用失败: {exc}") from exc

        code = extract_python_code(text)
        if code:
            ok, reason = validate_factor_code(code)
            if ok:
                return code
            last_reason = reason
        else:
            last_reason = "LLM 输出中没有找到 Python 代码块"

        logger.warning(
            "[factor-codegen] 第 %s/%s 次生成未通过：%s (factor=%s)",
            attempt,
            max_attempts,
            last_reason,
            factor.get("factor_name"),
        )
        if attempt < max_attempts:
            messages = [
                *messages,
                {"role": "assistant", "content": text[:6000]},
                {
                    "role": "user",
                    "content": (
                        f"上面的代码未通过校验：{last_reason}。"
                        "请修正后重新输出完整可运行的单文件实现"
                        "（仍只输出一个 ```python 代码块）。"
                    ),
                },
            ]
    raise FactorCodegenError(f"代码校验未通过: {last_reason}")
