#!/usr/bin/env python3
"""把 .pine 策略转写成 Pyne-Python 模板，并可验证 / 回测 / 装进策略注册表。

背景：本机没有任何 Pine 解释器，PyneCore 的 `pyne compile` 是付费远程 API（已决定不订阅），
所以「让 .pine 跑起来」的唯一路径是让模型把它翻成 Pyne-Python（lab_strategies 那套格式），
再用现有 PyneCore 链路回测。转写产物一律先落到 data/pine_transpile/<id>/，**不直接执行**：

  candidate.py   模型产出的脚本（未验证）
  prompt.md      完整提示词（审计用：模型看到了什么）
  meta.json      模型/耗时/token/校验结果
  report.json    --validate 跑出来的回测统计

校验与执行分开：先静态查（语法 / 危险调用），再 --validate 用真实行情跑一遍。

宿主专用：容器里没有 bwrap、切不出沙箱，也装不了 pynecore（需 Python>=3.11，主环境
3.10）——所以本脚本一律在宿主用系统解释器跑。pynecore 来自仓内 ``pine_lab/`` 运行时
目录（见下方 ``_PINE_SITE``；环境变量 ``QM_PINE_SITE`` 可覆盖）。

用法::

    /usr/bin/python3 scripts/pine_to_pyne.py --id 0001                 # 转写
    /usr/bin/python3 scripts/pine_to_pyne.py --id 0001 --validate 600309.SH
    /usr/bin/python3 scripts/pine_to_pyne.py --id 0001 --install       # 装进 lab_strategies
    /usr/bin/python3 scripts/pine_to_pyne.py --list                    # 看已有产物
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _pine_site() -> Path | None:
    """pynecore 站点目录：容器/主环境都装不了（Requires-Python>=3.11，主环境 3.10），
    只有宿主 ``pine_lab/`` 运行时目录里有一份 → 回测前挂进 sys.path，脚本就能用
    系统 python3 跑（否则得切解释器，容易忘）。优先 ``QM_PINE_SITE`` 显式覆盖。"""
    env = os.environ.get("QM_PINE_SITE")
    if env:
        return Path(env).expanduser()
    lib = ROOT / "pine_lab/.venv/lib"
    if lib.is_dir():
        for cand in sorted(lib.glob("python3.*/site-packages"), reverse=True):
            return cand
    return None


_PINE_SITE = _pine_site()
if _PINE_SITE and _PINE_SITE.is_dir() and str(_PINE_SITE) not in sys.path:
    sys.path.append(str(_PINE_SITE))

OUT_DIR = Path(__file__).resolve().parents[1] / "data/pine_transpile"
# 与 market_lab.STRATEGY_DIR 同一落点（agent_arena 包内 lab_strategies/），
# --install 装进去后模板回测立刻可见。
LAB_DIR = ROOT / "backend/services/agent_arena/lab_strategies"
MODEL = "deepseek-v4-flash"
# --source / --out：agent 改策略时用来跑候选（读指定 Pine、产物落到别的目录）。
# 候选绝不能覆盖正式产物，所以这两项一旦给了，路径就整体改道。
SOURCE_OVERRIDE: Path | None = None
OUT_OVERRIDE: Path | None = None
# 推理模型的 reasoning token 也计入 completion：0001 开思考烧掉 23755 token / 180s 才
# 勉强塞进 32k，0004 同样 32k 直接截断（策略还更短）。转写是照模板的机械活，不需要长
# 思考——默认 thinking=disabled，输出只剩代码，8k 富余。真截断了再回退开思考重试。
MAX_TOKENS = 32000          # 开思考时的上限
MAX_TOKENS_FAST = 8000      # 关思考时只出代码
NO_THINKING = {"thinking": {"type": "disabled"}}


def _out(item_id: str) -> Path:
    """某条策略的产物目录：默认 ``data/pine_transpile/<id>``，``--out`` 时整体改道。"""
    return OUT_OVERRIDE if OUT_OVERRIDE is not None else OUT_DIR / item_id


# 危险调用黑名单：转写产物是要被执行的，先在静态阶段挡一道。
BANNED = [
    (r"\b(import|from)\s+(os|sys|subprocess|socket|shutil|requests|urllib|http|ctypes|multiprocessing)\b",
     "禁止导入标准库/网络模块"),
    (r"\b(eval|exec|compile|__import__|open)\s*\(", "禁止动态执行/文件访问"),
    (r"\b(getattr|setattr|globals|locals|vars|breakpoint)\s*\(", "禁止反射"),
]
REQUIRED = ("@pyne", "from pynecore.lib import", "def main(")

SYSTEM_PROMPT = """你是 Pine Script → Pyne-Python 的转写器。把用户给的 Pine 策略改写成下面这种 Pyne-Python 脚本。

## 目标格式（严格照抄结构）

```python
\"\"\"
@pyne

<中文一句话说明策略逻辑>
\"\"\"
from pynecore.lib import close, input, script, strategy, ta
from pynecore.types import Series


@script.strategy("<中文策略名>", overlay=True, pyramiding=0,
                 default_qty_type=strategy.percent_of_equity, default_qty_value=95,
                 initial_capital=100000,
                 commission_type=strategy.commission.percent, commission_value=0.05,
                 slippage=1)
def main(fast=input(5, "快线周期"), slow=input(20, "慢线周期")):
    f = ta.sma(close, fast)
    s = ta.sma(close, slow)
    up: Series[bool] = f > s
    if up and not up[1]:
        strategy.entry('L', strategy.long)
    elif not up and up[1]:
        strategy.close('L')
```

## 硬性规则

1. 只做多（A股口径）：只用 `strategy.entry('L', strategy.long)` / `strategy.close('L')`，
   **不要做空**；原策略的做空腿直接丢掉，并在注释里说明。
2. 所有可调参数走 `input(默认值, "中文名")` 写在 `main()` 签名里，不要写死数字。
3. 只能用 `pynecore.lib` 与 `pynecore.types`（Series）；禁止 import 任何其它模块。
4. 禁止画图 / 告警 / 表格 / 标签（plot、alert、table、label、line、box 一律不用）。
5. 禁止 `request.security`（多时间框架）、`request.*` 数据请求——取不到就退化成单周期逻辑。
6. 序列变量必须显式标注类型，例如 `up: Series[bool] = f > s`；上一根用 `x[1]`。
   **凡是要用 `[n]` 取历史的值（包括 `ta.*` 的返回值）都必须先赋给带 `Series[...]`
   标注的变量**（`r: Series[float] = ta.rsi(close, n)` 才能写 `r[3]`），
   否则运行时只是普通 float，`[n]` 会抛 TypeError。**不能对表达式直接取历史**
   （`(high + low)[1]` 会抛 TypeError），先赋值给变量再索引。
   `ta.macd` / `ta.bb` / `ta.dmi` / `ta.kc` / `ta.supertrend` 返回**元组**，必须解包：
   `macd_line, signal_line, hist = ta.macd(close, f, s, sig)`。
   **同一个变量在 if/elif/else 里赋值时，声明只能写在链外一次**。`if c: x: Series[float] = a`
   / `else: x = b` 这种写法里，分支里的声明只在那个分支执行，其它分支的 `x = b` 是「覆盖
   当前值」、缓冲没被喂过 → x 恒为 nan（实测这类策略 0 成交）。正确写法是分支里只算普通值、
   链后声明一次：
   ```python
   if c:
       raw = ta.ema(close, n)
   else:
       raw = ta.sma(close, n)
   x: Series[float] = raw
   ```
7. 不加未来函数：判断与开平仓只用当前及历史 bar。
8. 保持策略原意，宁可简化也不要臆造新逻辑；简化处在文件头注释里写清楚。
9. 输出必须是**纯 Python**，Pine 写法一律翻译掉——这是最常见的翻车点（会直接语法错误）：
   - 三元 `cond ? a : b` → `a if cond else b`
   - **`var` 要翻成持久变量**：Pine 的 `var x = ...`（跨 bar 保留、只初始化一次）→
     `x: PersistentSeries[<类型>] = ...`（`from pynecore.types import PersistentSeries`）。
     写成 `Series[...]` 会**每根 bar 重置**，状态永远存不住（实测这类策略 46% 一笔不成交）。
     反之，每根 bar 都要重新计算的值必须用 `Series[...]`——PersistentSeries 的初始值只算一次。
   - 不要 `:=` 赋值（用 `=`）
   - 缺失值用 `float('nan')`；取值用 `nz(x, 默认值)`；判断用 `na(x)`（`na` 是函数，不是值）
10. 用到的内置名必须 import 齐（第一行 `from pynecore.lib import ...`）——漏一个就是运行时
   NameError，这是实测最高频的失败。注意 `bar_index` / `time` / `na` / `nz` 是**顶层名字**，
   不在 `ta.` 下（`ta.bar_index` 会报 AttributeError）；`pynecore.lib.math` 里没有
   `isnan` / `nan`，判断缺失用 `na(x)`、字面缺失值写 `float('nan')`。
11. 装饰器参数一律用模板里的 `initial_capital=100000` / `default_qty_value=95` /
    `default_qty_type=strategy.percent_of_equity`，不要沿用原策略的仓位口径
    （小本金 × 1% 仓位在 A 股算出不足 1 股、整场 0 笔成交；固定手数配不同本金
    等于杠杆随本金变，跨策略收益不可比）。静态闸会**校验取值**，写错会被打回重修。
12. 输出**只有一个 python 代码块**，不要解释文字。

## 常用库

- 均线/通道：`ta.sma(close, n)` `ta.ema(close, n)` `ta.rma` `ta.wma` `ta.highest(high, n)` `ta.lowest(low, n)`
- 波动/动量：`ta.atr(n)` `ta.rsi(close, n)` `ta.macd(close, f, s, sig)` `ta.stoch(close, high, low, n)`
- 交叉：`ta.crossover(a, b)` `ta.crossunder(a, b)`
- 其它：`ta.change(x)` `ta.stdev(close, n)` `math.abs/max/min/round`（`from pynecore.lib import math`）
- 缺失值：`nz(x, 默认值)` 取值、`na(x)` 判断（`na` 是函数）；字面缺失值写 `float('nan')`
- 跨 bar 状态（Pine 的 `var`）：`from pynecore.types import PersistentSeries` 后写
  `flag: PersistentSeries[bool] = False`，同样支持 `flag[1]`
- Pine 数组（`array.new_*` / `array.push` / `array.get` / `array.size` / `array.shift`）→
  Python list：`a: Persistent[list] = []`（`from pynecore.types import Persistent`）声明一次，
  然后 `a.append(v)` / `a[i]` / `len(a)` / `a.pop(0)`。**别写 `PersistentSeries[list]`**——
  它的 `a[i]` 是「i 根 bar 前的值」而不是列表下标（实测 0521 因此报
  `TypeError: '>=' not supported between 'list' and 'int'`）。
  定长初始化用 `array.new_bool(n, False)` / `array.new_float(n, 0.0)` / `array.new_int(n, 0)`，
  **不要写 `[False] * n`**——n 在 pyne 里是浮点，list × float 会
  `TypeError: can't multiply sequence by non-int of type 'float'`（实测 0248/0253/0260）。
- 开平仓：`strategy.entry('L', strategy.long)` `strategy.close('L')` `strategy.position_size`
- 常量：`strategy.long` `strategy.percent_of_equity` `strategy.commission.percent`
"""


def _env() -> dict:
    env: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in env:
                env[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return env


def _chat(messages: list[dict], model: str,
          think: bool = False) -> tuple[str, dict | None, bool]:
    """直连 OpenAI 兼容接口（重试 1 次）。返回 (文本, usage, 是否被截断)。

    ``think=False``（默认）关掉推理：转写产物是照模板的代码，长思考只会把额度烧光。
    """
    env = _env()
    base = env.get("OPENAI_API_BASE", "").rstrip("/")
    key = env.get("OPENAI_API_KEY", "")
    if not base or not key:
        raise RuntimeError("OPENAI_API_BASE/OPENAI_API_KEY 缺失（.env）")
    import requests

    payload = {"model": model, "messages": messages,
               "temperature": 0.1,
               "max_tokens": MAX_TOKENS if think else MAX_TOKENS_FAST}
    if not think:
        payload.update(NO_THINKING)
    last: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.post(f"{base}/chat/completions",
                                 headers={"Authorization": f"Bearer {key}"},
                                 json=payload, timeout=600)
            resp.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 0:
                print(f"  ⚠️ 调用失败，重试: {exc}")
    else:
        raise last  # type: ignore[misc]
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = str(msg.get("content") or "").strip() or str(msg.get("reasoning_content") or "").strip()
    usage = data.get("usage") or None
    if usage:
        usage = {k: int(usage.get(k) or 0)
                 for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return content, usage, choice.get("finish_reason") == "length"


def call_llm(system: str, user: str, model: str,
             think: bool = False) -> tuple[str, dict | None, bool]:
    """单轮 system+user 调用。"""
    return _chat([{"role": "system", "content": system},
                  {"role": "user", "content": user}], model, think=think)


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip() + "\n"


def _strip_strings_and_comments(code: str) -> str:
    """把字符串与注释抹成空格（保留换行与列位），供「只看代码」的检查用。

    位置一一对应是刻意的：改写函数要靠它把「干净文本里的下标」映射回原文。
    """
    out: list[str] = []
    i, n = 0, len(code)
    quote = ""
    while i < n:
        ch = code[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                out.append("  ")
                i += 2
            elif code.startswith(quote, i):
                out.append(" " * len(quote))
                i += len(quote)
                quote = ""
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if ch in "\"'":
            quote = ch * 3 if code.startswith(ch * 3, i) else ch
            out.append(" " * len(quote))
            i += len(quote)
            continue
        if ch == "#":
            while i < n and code[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _find_top_level(s: str, target: str, start: int = 0) -> int:
    """在括号/方括号/花括号之外找 target，找不到返回 -1。"""
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == target:
            return i
    return -1


def _ternary_to_if(expr: str) -> str:
    """`cond ? a : b` → `a if cond else b`（递归处理嵌套，Pine 的 ?: 是右结合）。"""
    q = _find_top_level(expr, "?")
    if q < 0:
        s = expr.strip()
        if s.startswith("(") and s.endswith(")"):      # 括号里的嵌套三元
            inner = _ternary_to_if(s[1:-1])
            if inner != s[1:-1]:
                return f"({inner})"
        return expr
    c = _find_top_level(expr, ":", q + 1)
    if c < 0:
        return expr
    cond, a, b = expr[:q].strip(), expr[q + 1:c].strip(), expr[c + 1:].strip()
    return f"({_ternary_to_if(a)}) if ({_ternary_to_if(cond)}) else ({_ternary_to_if(b)})"


def _split_comment(orig: str, clean: str) -> tuple[str, str]:
    """按「干净文本里该位置起全是空白」认出真注释，字符串里的 # 不算。"""
    for i, ch in enumerate(orig):
        if ch == "#" and clean[i:].strip() == "":
            return orig[:i], orig[i:]
    return orig, ""


def _split_prefix(clean_line: str) -> int:
    """赋值 / return 语句的右值起点（行里没有这类前缀时返回 -1）。"""
    m = re.match(r"^\s*return\s+", clean_line)
    if m:
        return m.end()
    m = re.match(r"^\s*[A-Za-z_]\w*\s*(?::\s*[^=\n]+?)?\s*=\s*", clean_line)
    return m.end() if m else -1


def _fix_ternaries(code: str) -> tuple[str, int]:
    """逐行把右值里的 `? :` 翻成 Python 条件表达式。"""
    clean = _strip_strings_and_comments(code)
    out: list[str] = []
    fixed = 0
    for orig, cl in zip(code.splitlines(), clean.splitlines()):
        idx = _split_prefix(cl) if "?" in cl else -1
        if idx < 0:
            out.append(orig)
            continue
        q = _find_top_level(cl, "?", idx)
        if q < 0 or _find_top_level(cl, ":", q + 1) < 0:
            out.append(orig)
            continue
        body, comment = _split_comment(orig[idx:], cl[idx:])
        stripped = body.rstrip()
        pad = body[len(stripped):]          # 注释前的空格要留着，不然注释会贴到代码上
        new_body = _ternary_to_if(stripped)
        if new_body == stripped:
            out.append(orig)
            continue
        out.append(orig[:idx] + new_body + pad + comment)
        fixed += 1
    text = "\n".join(out)
    return text + ("\n" if code.endswith("\n") else ""), fixed


def _fix_walrus_assign(code: str) -> tuple[str, int]:
    """Pine 的 `:=` 赋值 → `=`（只改代码区，字符串/注释里的不动）。"""
    clean = _strip_strings_and_comments(code)
    pat = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*:=\s*", re.M)
    out: list[str] = []
    last = fixed = 0
    for m in pat.finditer(code):
        if clean[m.start():m.end()] != code[m.start():m.end()]:
            continue
        out.append(code[last:m.start()])
        out.append(f"{m.group(1)}{m.group(2)} = ")
        last = m.end()
        fixed += 1
    out.append(code[last:])
    return "".join(out), fixed


def depine(code: str) -> tuple[str, list[str]]:
    """机械翻掉最顽固的两处 Pine 残留：`? :` 三元与 `:=`。

    模型连着修两轮仍会漏（0400/0778 就是这么挂的），而这两处改写纯语法、无歧义。
    改写记录进 meta.auto_fixes；静态闸照旧跑，改不干净的仍会被挡下。
    """
    notes: list[str] = []
    code, n = _fix_walrus_assign(code)
    if n:
        notes.append(f"自动改写 {n} 处 `:=` → `=`")
    code, n = _fix_ternaries(code)
    if n:
        notes.append(f"自动改写 {n} 处 `? :` → `if/else`")
    return code, notes


def autofix(code: str) -> tuple[str, list[str]]:
    """候选落盘前的机械改写：Pine 残留（depine）+ 缺 Series 声明（fix_history）
    + 分支里的 Series 声明（fix_conditional_series）+ 列表重复的浮点乘数（fix_list_repeat）
    + 漏 import（fix_imports，放最后——前面几步可能引入新名字）。"""
    code, notes = depine(code)
    code, hist = fix_history(code)
    code, cond = fix_conditional_series(code)
    code, rep = fix_list_repeat(code)
    code, imp = fix_imports(code)
    return code, notes + hist + cond + rep + imp


# --- 取历史的名字必须是 Series 变量 -------------------------------------------
# pynecore 里普通赋值 / 解包 / 未标注的参数都只是「当前值」，对它取历史必抛
# TypeError: 'float' object is not subscriptable。只有 `x: Series[float] = ...`
# 声明的名字才进状态槽、才有历史（实测：`s = ta.sma(close,20)` 后 `s[1]` 挂；
# `a, b, c = ta.bb(...)` 后 `b[1]` 挂；`def f(src, n)` 里 `src[i]` 挂；
# 补上声明后三种都正常）。模型自己连修两轮也修不好（0065/0165/0334/0521/0572），
# 所以照 depine 的路子机械补声明。
_SERIES_FLOAT = "Series[float]"
_PRICE_SERIES = frozenset({"open", "high", "low", "close", "volume", "hl2", "hlc3",
                           "ohlc4", "hlcc4", "bid", "ask"})
# pynecore.lib 的序列（顶层名，`dayofmonth[1]` 合法），不能当普通变量处理。
_LIB_SERIES = frozenset({"bar_index", "last_bar_index", "time", "time_close",
                         "time_tradingday", "hour", "minute", "second", "dayofweek",
                         "dayofmonth", "weekofyear", "month", "year"})
_MULTI_TA = ("bb", "dmi", "kc", "macd", "supertrend")
# 返回标量的调用；返回列表/字符串的调用（split/keys/…）下标是正常 Python，别动。
_SCALAR_CALLS = frozenset({"na", "nz", "fixnan", "input", "timestamp"})
_SCALAR_NS = frozenset({"ta", "math", "strategy", "request", "syminfo", "barstate",
                        "timeframe"})


def _scalar_rhs(node: ast.AST) -> bool:
    """右值是否「只可能是当前值」的标量表达式（容器的下标是正常 Python，别乱加注解）。"""
    if isinstance(node, ast.Constant):
        return not isinstance(node.value, (str, bytes, list, tuple, dict, set))
    if isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.IfExp,
                         ast.Attribute)):
        return True
    if isinstance(node, ast.Name):
        return node.id in _PRICE_SERIES or node.id in _LIB_SERIES
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in _SCALAR_CALLS
        if isinstance(func, ast.Attribute):
            return func.attr in _SCALAR_CALLS or ast.unparse(func.value) in _SCALAR_NS
    return False


_SERIES_TYPES = ("PersistentSeries", "Persistent", "Series")


def _decl_kind(annotation: ast.expr) -> str | None:
    """声明种类；`PersistentSeries` 必须排在 `Persistent`/`Series` 前（startswith 会串）。"""
    text = ast.unparse(annotation)
    for kind in _SERIES_TYPES:
        if text.startswith(kind):
            return kind
    return None


class _Scope:
    """一个函数体的绑定表：谁被 Series 声明过、谁只是当前值。"""

    __slots__ = ("parent", "children", "ann", "ann_text", "params", "unpacks", "assigns", "subs")

    def __init__(self, parent: "_Scope | None"):
        self.parent = parent
        self.children: list[_Scope] = []
        self.ann: set[str] = set()                       # Series[...] 声明（含参数注解）
        self.ann_text: dict[str, str] = {}               # 名字 → 注解原文（区分 Persistent 系）
        self.params: dict[str, ast.arg] = {}             # 未标注的参数
        self.unpacks: dict[str, list[ast.Assign]] = {}   # 解包自 ta.<多返回值>()
        self.assigns: dict[str, list[ast.Assign]] = {}   # 普通赋值
        self.subs: dict[str, list[ast.Subscript]] = {}   # 被下标的名字
        if parent is not None:
            parent.children.append(self)


def _scan_scopes(code: str) -> "_Scope | None":
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None          # 语法错误交给静态闸报，这里不猜

    root = _Scope(None)

    def walk(node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = _Scope(scope)
            for arg in node.args.args + node.args.kwonlyargs:
                kind = _decl_kind(arg.annotation) if arg.annotation is not None else None
                if kind:
                    inner.ann.add(arg.arg)
                    inner.ann_text[arg.arg] = ast.unparse(arg.annotation)
                else:
                    inner.params[arg.arg] = arg
            for stmt in node.body:
                walk(stmt, inner)
            return
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                kind = _decl_kind(node.annotation)
                if kind:
                    scope.ann.add(node.target.id)
                    scope.ann_text[node.target.id] = ast.unparse(node.annotation)
            if node.value is not None:
                walk(node.value, scope)
            return
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                scope.assigns.setdefault(node.targets[0].id, []).append(node)
            elif len(node.targets) == 1 and isinstance(node.targets[0], ast.Tuple):
                if re.match(r"ta\.(%s)\s*\(" % "|".join(_MULTI_TA),
                            ast.unparse(node.value)):
                    for elt in node.targets[0].elts:
                        if isinstance(elt, ast.Name):
                            scope.unpacks.setdefault(elt.id, []).append(node)
            walk(node.value, scope)
            return
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name):
                scope.subs.setdefault(node.value.id, []).append(node)
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(tree, root)
    return root


def _bindings(scope: _Scope, name: str) -> list[tuple[str, ast.AST]]:
    out: list[tuple[str, ast.AST]] = []
    if name in scope.params:
        out.append(("param", scope.params[name]))
    out += [("unpack", node) for node in scope.unpacks.get(name, [])]
    out += [("assign", node) for node in scope.assigns.get(name, [])]
    return out


def _series_needed(code: str) -> list[tuple[str, str, ast.AST, bool]]:
    """找出「被下标取历史、但 pynecore 里不留历史」的名字。

    返回 (名字, 修法, 绑定节点, 是否「确定是标量但机械改不动」)。
    修法只有三种无歧义的：param（给参数加注解）、unpack（解包后补一行同名声明）、
    assign（普通赋值升级成 Series 赋值）。同一个名字在同一作用域有多种绑定时不机械改，
    只留给静态闸报出来（能确定是标量的话）。
    """
    root = _scan_scopes(code)
    if root is None:
        return []
    out: list[tuple[str, str, ast.AST, bool]] = []

    def resolve(scope: _Scope, name: str):
        while scope is not None:
            if name in scope.ann:
                return None
            binds = _bindings(scope, name)
            if binds:
                kinds = {kind for kind, _ in binds}
                # 混着参数/解包/赋值时判断不了该改哪处；只有全是标量才敢说它需要 Series
                scalar = all(_scalar_rhs(a.value) for a in scope.assigns.get(name, []))
                if len(kinds) > 1:
                    return (binds[0][0], binds[0][1], True) if scalar else None
                kind, node = binds[0]
                if kind == "param":
                    return ("param", node, False)
                if kind == "unpack":
                    return ("unpack", node, False)
                if scalar:
                    return ("assign", scope.assigns[name][0], False)
                return None
            scope = scope.parent
        return None

    def visit(scope: _Scope) -> None:
        for name in scope.subs:
            if name in _PRICE_SERIES:
                continue
            got = resolve(scope, name)
            if got:
                out.append((name, got[0], got[1], got[2]))
        for child in scope.children:
            visit(child)

    visit(root)
    return out


def fix_history(code: str) -> tuple[str, list[str]]:
    """给「被下标取历史」的名字机械补上 Series 声明（见上面实测记录）。"""
    needed = [item for item in _series_needed(code) if not item[3]]
    if not needed:
        return code, []
    lines = code.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    notes: list[str] = []
    for name, kind, node, _ in needed:
        if kind == "param":
            edits.append((node.end_lineno, node.end_col_offset, f": {_SERIES_FLOAT}"))
            notes.append(f"给参数 {name} 补 `: Series[float]`（参数下标取历史）")
        elif kind == "assign":
            target = node.targets[0]
            edits.append((target.end_lineno, target.end_col_offset, f": {_SERIES_FLOAT}"))
            notes.append(f"`{name} = ...` 升级成 `{name}: Series[float] = ...`（普通赋值不留历史）")
        else:
            indent = " " * node.col_offset
            end = len(lines[node.end_lineno - 1].rstrip("\n"))
            edits.append((node.end_lineno, end,
                          f"\n{indent}{name}: {_SERIES_FLOAT} = {name}"))
            notes.append(f"解包值 {name} 后补一行 `{name}: Series[float] = {name}`")
    for lineno, col, text in sorted(edits, reverse=True):
        line = lines[lineno - 1]
        lines[lineno - 1] = line[:col] + text + line[col:]
    return "".join(lines), notes


# --- 条件分支里的 Series 声明 ---------------------------------------------------
# pynecore 把同名普通赋值写成 `x = __state__[N].set(...)`，而 set() 在「本 bar 还没
# add() 过」的空缓冲上返回 na 且什么都不存（transformers/series.py 原话：set() on an
# empty buffer answers na and stores nothing）。所以「声明写在某个分支里、其它分支普通
# 赋值」会让整条链恒为 na——实测 0165 的 main_ma_val 全 bar nan（0 笔成交），0521 的
# os 同理。修法：链前 hoist 一条声明，默认值只为把缓冲喂起来，各分支随后 set() 覆盖。
_CHAIN_DEFAULTS = {"float": "float('nan')", "bool": "False", "int": "0", "str": "''"}


def _ann_elem(annotation: ast.expr) -> str | None:
    """`Series[float]` → 'float'；PersistentSeries / 嵌套容器一律返回 None（不碰）。"""
    m = re.fullmatch(r"Series\[\s*(\w+)\s*\]", ast.unparse(annotation))
    return m.group(1) if m else None


def _if_chain(node: ast.If) -> tuple[list[list[ast.stmt]], bool]:
    """if/elif/else 链的各个分支体，以及末尾是不是 else（所有路径都赋值）。"""
    bodies = [node.body]
    cur = node
    while len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
        cur = cur.orelse[0]
        bodies.append(cur.body)
    if cur.orelse:
        bodies.append(cur.orelse)
        return bodies, True
    return bodies, False


def _bindings_in(node: ast.AST | list) -> list[tuple[str, str, ast.AST]]:
    """子树（或语句列表）里按源码顺序的绑定 (名字, 种类, 节点)；嵌套函数的绑定不算。"""
    out: list[tuple[str, str, ast.AST]] = []

    def go(n: ast.AST) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.append((n.target.id, "ann", n))
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                names = [t] if isinstance(t, ast.Name) else (
                    list(t.elts) if isinstance(t, (ast.Tuple, ast.List)) else [])
                for elt in names:
                    if isinstance(elt, ast.Name):
                        out.append((elt.id, "plain", n))
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            out.append((n.target.id, "plain", n))
        for child in ast.iter_child_nodes(n):
            go(child)

    for stmt in node if isinstance(node, list) else [node]:
        go(stmt)
    return out


def _loads_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
               for n in ast.walk(node))


def _cond_series_hits(code: str) -> list[tuple[str, int, bool, str]]:
    """(名字, 声明行号, 是否机械可修, 严重度)：Series 声明落在 if 链的分支体里，同名另有绑定。

    两种坏法：
    - ``mixed``（有分支写普通赋值）：那个分支走 set()，空缓冲上返回 na → **整个变量恒为 na**，
      实测 0165 的 main_ma_val 全 bar nan、0 笔成交。
    - ``multi``（每个分支都写声明）：值本身没问题，但读的是最后注册的槽，先注册那支的历史会
      冻住（实测 probe_Y：z[1] 永远停在第一根 bar）——不读历史就暂时看不出来。

    只有「链有 else、每个分支都赋值、所有绑定都在链内、链前没读过」才敢机械改。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: list[tuple[str, int, bool, str]] = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        fn_binds = _bindings_in(fn.body)
        for idx, stmt in enumerate(fn.body):
            if not isinstance(stmt, ast.If):
                continue
            bodies, has_else = _if_chain(stmt)
            chain_names = {b[0] for body in bodies for b in _bindings_in(body)}
            for name in sorted(chain_names):
                # 分支体的直接语句里的 Series 声明（候选）
                cand = [(i, s) for i, body in enumerate(bodies) for s in body
                        if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                        and s.target.id == name and _ann_elem(s.annotation) is not None
                        and s.value is not None]
                if not cand:
                    continue
                # 同一分支内的其它绑定是安全的（add 过再 set），只看别的分支 / 链外
                same_branch = {id(b[2]) for b in _bindings_in(bodies[cand[0][0]])}
                others = [(n, k, node) for n, k, node in fn_binds
                          if n == name and id(node) not in same_branch]
                if not others:
                    continue
                # 每支都当场 return（纯辅助函数，如 ma_func 里按类型算完就返回）：
                # 没有跨 bar 语义，别报也别改。
                bind_bodies = [i for i, body in enumerate(bodies)
                               if name in {b[0] for b in _bindings_in(body)}]
                if bind_bodies and all(bodies[i] and isinstance(bodies[i][-1], (ast.Return, ast.Raise))
                                       for i in bind_bodies):
                    continue
                anns_all = [b for b in fn_binds if b[0] == name and b[1] == "ann"]
                elems = {_ann_elem(b[2].annotation) for b in anns_all}
                fixable = (has_else and len(elems) == 1 and None not in elems
                           and len(anns_all) == len(cand)      # 声明都是分支体的直接语句
                           and all(stmt.lineno <= node.lineno <= stmt.end_lineno
                                   for _, _, node in others)
                           and all(name in {b[0] for b in _bindings_in(body)} for body in bodies)
                           and not any(_loads_name(s, name) for s in fn.body[:idx]))
                severity = "mixed" if any(k == "plain" for _, k, _ in others) else "multi"
                out.append((name, cand[0][1].lineno, fixable, severity))
    return out


def fix_conditional_series(code: str) -> tuple[str, list[str]]:
    """把「分支里声明 Series、其它分支普通赋值」机械改成链前声明 + 分支赋值。"""
    hits = [h for h in _cond_series_hits(code) if h[2]]
    if not hits:
        return code, []
    tree = ast.parse(code)
    lines = code.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def abs_off(lineno: int, col: int) -> int:
        return starts[lineno - 1] + col

    edits: list[tuple[int, int, str]] = []
    notes: list[str] = []
    done: set[tuple[str, int]] = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for stmt in fn.body:
            if not isinstance(stmt, ast.If):
                continue
            hoist: list[str] = []
            for name, lineno, _, _ in hits:
                if (name, lineno) in done or lineno < stmt.lineno or lineno > stmt.end_lineno:
                    continue
                ann_nodes = [n for n in ast.walk(stmt)
                             if isinstance(n, ast.AnnAssign)
                             and isinstance(n.target, ast.Name) and n.target.id == name
                             and _ann_elem(n.annotation) is not None and n.value is not None]
                if not ann_nodes:
                    continue
                elems = {_ann_elem(n.annotation) for n in ann_nodes}
                if len(elems) != 1:
                    continue
                done.add((name, lineno))
                elem = next(iter(elems))
                for ann_node in ann_nodes:
                    edits.append(
                        (abs_off(ann_node.target.end_lineno, ann_node.target.end_col_offset),
                         abs_off(ann_node.value.lineno, ann_node.value.col_offset), " = "))
                hoist.append(f"{name}: Series[{elem}] = {_CHAIN_DEFAULTS[elem]}")
                notes.append(f"`{name}` 的 Series 声明从分支里提到 if 链之前"
                             f"（分支里的声明只在那一支生效，其它分支的取值/历史会错位）")
            if hoist:
                indent = " " * stmt.col_offset
                edits.append((abs_off(stmt.lineno, stmt.col_offset), abs_off(stmt.lineno, stmt.col_offset),
                              "".join(f"{line}\n{indent}" for line in hoist)))
    if not edits:
        return code, []
    for start, end, text in sorted(edits, reverse=True):
        code = code[:start] + text + code[end:]
    return code, notes


def _cond_series_problems(code: str) -> list[str]:
    """fix_conditional_series 改不动的那部分：让模型自己把声明提到链外。"""
    out: list[str] = []
    for name, lineno, fixable, severity in _cond_series_hits(code):
        if fixable:
            continue
        if severity == "mixed":
            why = ("分支里的声明只在那个分支执行，其它分支的赋值走 set()，空缓冲上 set() "
                   "返回 na——整个变量恒为 na")
        else:
            why = ("每个分支各写一条声明，读的是最后注册的槽，先注册那支的历史会冻住"
                   "（z[1] 停在第一根 bar）")
        out.append(f"`{name}`（第 {lineno} 行）的 Series 声明写在 if 分支里、同名另有赋值：{why}。"
                   f"改成：各分支只算普通值，链后写一条 `{name}: Series[float] = <值>`；"
                   f"或链前写 `{name}: Series[float] = float('nan')` 再让分支赋值")
    return out


def _is_int_literal(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, int)
            and not isinstance(node.value, bool))


def _list_and_mult(node: ast.BinOp) -> tuple[ast.List, ast.expr]:
    """`[x] * n` 的两侧：返回 (列表字面量, 乘数)。"""
    if isinstance(node.left, ast.List):
        return node.left, node.right
    return node.right, node.left  # type: ignore[return-value]


def _list_repeat_hits(code: str) -> list[tuple[ast.BinOp, bool, str]]:
    """`[初值] * n`：n 是浮点时 Python 直接 `TypeError: can't multiply sequence by non-int`。

    pyne 脚本里「Pine int」运行期就是浮点——`input()` 的参数、`int(x)` 转换（被翻成
    `safe_int()`，返回 float）都是，只有字面量是原生 int（实测 probe：`[False] * 20` 过、
    `[False] * n` / `[False] * int(n)` 都抛 TypeError；0248/0253/0260 全挂在这）。
    返回 (节点, 是否可机械修, 说明)；单个常量元素的列表能机械改成 `[x for _ in range(n)]`。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: list[tuple[ast.BinOp, bool, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)):
            continue
        if not (isinstance(node.left, ast.List) or isinstance(node.right, ast.List)):
            continue
        lst, mult = _list_and_mult(node)
        if isinstance(mult, ast.List) or _is_int_literal(mult):
            continue  # 字面量乘数是原生 int，合法
        why = ("列表乘法的乘数在 pyne 里是浮点（input 参数、int() 转换都是浮点），"
               "list × float 会 TypeError")
        fixable = len(lst.elts) == 1 and isinstance(lst.elts[0], ast.Constant)
        out.append((node, fixable, why))
    return out


def fix_list_repeat(code: str) -> tuple[str, list[str]]:
    """`[初值] * n` → `[初值 for _ in range(n)]`（range 的实参会被原生截断成 int）。"""
    hits = [h for h in _list_repeat_hits(code) if h[1]]
    if not hits:
        return code, []
    lines = code.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def abs_off(lineno: int, col: int) -> int:
        return starts[lineno - 1] + col

    edits: list[tuple[int, int, str]] = []
    notes: list[str] = []
    for node, _, _ in hits:
        lst, mult = _list_and_mult(node)
        elem_src = ast.get_source_segment(code, lst.elts[0])
        mult_src = ast.get_source_segment(code, mult)
        old_src = ast.get_source_segment(code, node)
        if not (elem_src and mult_src and old_src):
            continue
        new = f"[{elem_src} for _ in range({mult_src})]"
        edits.append((abs_off(node.lineno, node.col_offset),
                      abs_off(node.end_lineno, node.end_col_offset), new))
        notes.append(f"`{old_src}` → `{new}`（乘数是浮点，list × float 会 TypeError）")
    if not edits:
        return code, []
    for start, end, text in sorted(edits, reverse=True):
        code = code[:start] + text + code[end:]
    return code, notes


def _list_repeat_problems(code: str) -> list[str]:
    """fix_list_repeat 改不动的那部分（多元素/非常量元素列表重复）。"""
    out: list[str] = []
    for node, fixable, why in _list_repeat_hits(code):
        if fixable:
            continue
        src = ast.get_source_segment(code, node) or "?"
        out.append(f"`{src}`（第 {node.lineno} 行）：{why}。改成 "
                   f"`array.new_bool/new_float/new_int(n, 初值)`（`from pynecore.lib import array`）"
                   f"或 `[初值 for _ in range(n)]`")
    return out


# pynecore.lib 里必须显式导入的名字——漏一个就是运行时 NameError（实测最高频的翻车点）。
_LIB_NAMES = (
    "close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4",
    "na", "nz", "fixnan", "bar_index", "last_bar_index", "time", "time_close",
    "time_tradingday", "timestamp", "hour", "minute", "second",
    "dayofweek", "dayofmonth", "weekofyear", "month", "year", "math", "ta", "array", "matrix",
    "map", "strategy", "input", "script", "barstate", "timeframe", "syminfo", "session",
    "request", "color", "alert", "label", "line", "box", "table",
)
_DEF_RE = re.compile(r"^\s*def\s+(\w+)\s*\(([^)]*)\)", re.M)
_TARGET_RE = re.compile(r"^\s*(\w+)\s*(?::[^=\n]+)?=", re.M)
_IMPORT_RE = re.compile(r"from\s+pynecore\.lib\s+import\s+([^\n]+)")
# 补 import 用：拆成「前缀 + 名字列表」两组，替换时只重写名字那截
_LIB_IMPORT_RE = re.compile(r"(from\s+pynecore\.lib\s+import\s+)([^\n]+)")
# 这些 ta 函数在 pynecore 里返回元组，赋给单个变量后下标会拿到「另一个指标」而不是历史值。
_MULTI_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::\s*[^=\n]+?)?\s*=\s*"
                       r"ta\.(bb|dmi|kc|macd|supertrend)\s*\(", re.M)
_EXPR_INDEX_RE = re.compile(r"\)\s*\[\s*\d+\s*\]")


def _imported_names(code: str) -> set[str]:
    names: set[str] = set()
    for m in _IMPORT_RE.finditer(code):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if name:
                names.add(name)
    return names


def _defined_names(code: str) -> set[str]:
    names = {m[0] for m in _DEF_RE.findall(code)}
    for _, params in _DEF_RE.findall(code):
        for part in params.split(","):
            name = part.strip().split(":")[0].split("=")[0].strip()
            if name and name.isidentifier():
                names.add(name)
    names.update(_TARGET_RE.findall(code))
    return names


def _missing_lib_names(code: str) -> list[str]:
    """用了但没导入的 pynecore.lib 顶层名（漏一个就是运行时 NameError）。"""
    imported = _imported_names(code)
    defined = _defined_names(code)
    clean = _strip_strings_and_comments(code)
    return [name for name in _LIB_NAMES
            if name not in imported and name not in defined
            and re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", clean)]


def _import_problems(code: str) -> list[str]:
    return [f"用了 {name} 但没从 pynecore.lib 导入" for name in _missing_lib_names(code)]


_TYPES_RE = re.compile(r"from\s+pynecore\.types\s+import\s+([^\n]+)")


def _missing_type_names(code: str) -> list[str]:
    imported: set[str] = set()
    for m in _TYPES_RE.finditer(code):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if name:
                imported.add(name)
    clean = _strip_strings_and_comments(code)
    return [name for name in ("Series", "Persistent", "PersistentSeries")
            if name not in imported and re.search(rf"\b{name}\s*\[", clean)]


def _type_import_problems(code: str) -> list[str]:
    """`PersistentSeries` 漏 import 也是运行时 NameError——规则里新加的名字，单独查一道。"""
    return [f"用了 {name} 但没从 pynecore.types 导入"
            f"（`from pynecore.types import {name}`）" for name in _missing_type_names(code)]


def fix_imports(code: str) -> tuple[str, list[str]]:
    """漏 import 的 lib / types 名机械补进已有 import 行——实测最高频的失败，模型修两轮仍会漏。

    只动 import 行本身，其它一个字不改；补的名字都是 pynecore.lib 的顶层名，
    代码里用到却没导入时必然 NameError，补上只可能变好。
    """
    notes: list[str] = []
    missing = _missing_lib_names(code)
    if missing:
        m = _LIB_IMPORT_RE.search(code)
        if m:
            names = [n.strip() for n in m.group(2).split(",") if n.strip()]
            added = [n for n in missing if n not in names]
            if added:
                code = (code[:m.start()] + m.group(1) + ", ".join(names + added)
                        + code[m.end():])
                notes.append(f"补 import：{'、'.join(added)}")
    missing_t = _missing_type_names(code)
    if missing_t:
        m = _TYPES_RE.search(code)
        if m:
            names = [n.strip() for n in m.group(1).split(",") if n.strip()]
            added = [n for n in missing_t if n not in names]
            if added:
                code = (code[:m.start()] + "from pynecore.types import "
                        + ", ".join(names + added) + code[m.end():])
                notes.append(f"补 types import：{'、'.join(added)}")
        else:
            anchor = _LIB_IMPORT_RE.search(code)
            if anchor:
                code = (code[:anchor.end()] + "\nfrom pynecore.types import "
                        + ", ".join(missing_t) + code[anchor.end():])
                notes.append(f"补 types import：{'、'.join(missing_t)}")
    return code, notes


def _list_container_problems(code: str) -> list[str]:
    """`PersistentSeries[list]` 装列表：`a[i]` 会被当成「i 根 bar 前的值」而不是列表下标。"""
    out: list[str] = []
    for m in re.finditer(r"^\s*(\w+)\s*:\s*PersistentSeries\s*\[\s*list\s*\]", code, re.M):
        out.append(f"`{m.group(1)}: PersistentSeries[list]` 装不了列表："
                   f"它的 `{m.group(1)}[i]` 是「i 根 bar 前的值」不是列表下标（实测 0521 报 "
                   f"TypeError: '>=' not supported between 'list' and 'int'）。"
                   f"改成 `{m.group(1)}: Persistent[list] = []`"
                   f"（from pynecore.types import Persistent）")
    return out


def _namespace_problems(code: str) -> list[str]:
    clean = _strip_strings_and_comments(code)
    out: list[str] = []
    if re.search(r"\bta\.bar_index\b", clean):
        out.append("bar_index 不在 ta 下：从 pynecore.lib 导入后用裸名 bar_index")
    if re.search(r"\bmath\.(isnan|is_nan|nan|inf)\b", clean):
        out.append("pynecore.lib.math 没有 isnan/nan：判断缺失用 na(x)，字面缺失值写 float('nan')")
    for m in _MULTI_RE.finditer(clean):
        out.append(f"ta.{m.group(2)}() 返回元组，不能赋给单个变量：解包成多个变量"
                   f"（如 `a, b, c = ta.{m.group(2)}(...)`）再取历史")
    if _EXPR_INDEX_RE.search(clean):
        out.append("对表达式直接取历史（如 `(high + low)[1]`）：先赋给带 Series[...] 标注的变量再索引")
    return out


MANDATED_CAPITAL = 100000
MANDATED_QTY_PCT = 95
MANDATED_QTY_TYPE = "percent_of_equity"
MANDATED_DECORATOR = (f"initial_capital={MANDATED_CAPITAL} / "
                      f"default_qty_value={MANDATED_QTY_PCT} / "
                      f"default_qty_type=strategy.{MANDATED_QTY_TYPE}")


def _sizing_problems(code: str) -> list[str]:
    """本金 × 仓位口径必须**取值**统一，不只是「写了」。

    1001 条策略要横向比，就得同一本金、同一仓位口径：固定手数/现金额配不同本金
    等于杠杆随本金变，收益率根本不可比（实测 189 份候选里 62 份偏离，
    0001 就是 10000 本金 + 100% 仓位）；本金过小还会算出不足 1 股、整场 0 笔成交，
    看起来像「策略不灵」其实是白跑。
    """
    clean = _strip_strings_and_comments(code)
    num = r"([0-9_]+(?:\.[0-9]+)?)"
    cap = re.search(rf"initial_capital\s*=\s*{num}", clean)
    qty = re.search(rf"default_qty_value\s*=\s*{num}", clean)
    typ = re.search(r"default_qty_type\s*=\s*strategy\.(\w+)", clean)
    if not (cap and qty and typ):
        return [f"装饰器缺少统一口径的仓位参数：必须写 {MANDATED_DECORATOR}"]
    got = (f"initial_capital={cap.group(1)} / default_qty_value={qty.group(1)} / "
           f"default_qty_type=strategy.{typ.group(1)}")
    if (typ.group(1) == MANDATED_QTY_TYPE
            and float(cap.group(1).replace("_", "")) == MANDATED_CAPITAL
            and float(qty.group(1).replace("_", "")) == MANDATED_QTY_PCT):
        return []
    return [f"仓位口径必须与库内统一：当前 {got}，改成 {MANDATED_DECORATOR}。"
            f"固定手数/现金额配不同本金 = 杠杆随本金变，跨策略收益不可比"]


def _history_problems(code: str) -> list[str]:
    """fix_history 改不动的那部分：名字有多处绑定，只有模型能决定怎么改。"""
    out: list[str] = []
    for name, _, _, ambiguous in _series_needed(code):
        if ambiguous:
            out.append(f"对 {name} 取历史但它没有 Series 声明、又有多处赋值/参数："
                       f"改成 `{name}: Series[float] = ...` 声明一次，"
                       f"其它地方用 `{name} = ...` 重新赋值")
    return out


def _persistent_history_problems(code: str) -> list[str]:
    """`Persistent` 没有历史（`p[1]` 抛 TypeError）——被下标读到时得换 PersistentSeries。

    `Persistent[list]` 的 `[i]` 是正常列表下标，不算历史读，别误报。
    """
    root = _scan_scopes(code)
    if root is None:
        return []
    out: list[str] = []

    def visit(scope: _Scope) -> None:
        for name in scope.subs:
            cur = scope
            while cur is not None:
                if name in cur.ann:
                    text = cur.ann_text.get(name, "")
                    if text.startswith("Persistent[") and not text.startswith("Persistent[list"):
                        out.append(f"对 {name} 取历史（{name}[n]）但它是 `{text}`（没有历史）："
                                   f"改成 PersistentSeries[...]")
                    break
                cur = cur.parent
        for child in scope.children:
            visit(child)

    visit(root)
    return out


def static_check(code: str) -> list[str]:
    """语法 + 危险调用 + 导入/命名空间静态检查。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    for pat, why in BANNED:
        hit = re.search(pat, code)
        if hit:
            problems.append(f"{why}：{hit.group(0).strip()}")
    for need in REQUIRED:
        if need not in code:
            problems.append(f"缺少必需结构：{need}")
    problems += _namespace_problems(code)
    problems += _import_problems(code)
    problems += _type_import_problems(code)
    problems += _history_problems(code)
    problems += _persistent_history_problems(code)
    problems += _cond_series_problems(code)
    problems += _list_container_problems(code)
    problems += _list_repeat_problems(code)
    problems += _sizing_problems(code)
    try:
        compile(code, "<candidate>", "exec")
    except SyntaxError as exc:
        problems.append(f"语法错误：{exc.msg}（第 {exc.lineno} 行）")
    return problems


def _refresh_problems(item_id: str, meta: dict) -> dict:
    """用**当前**闸门重算 problems 并回写 meta.json。

    闸门会随规则收紧（2026-09-09 起校验本金/仓位取值）——不重算的话，
    复用已有候选的老产物会拿旧结论绕过新规则，重跑一遍等于白跑。
    """
    cand = _out(item_id) / "candidate.py"
    if not cand.exists():
        return meta
    problems = static_check(cand.read_text(encoding="utf-8"))
    if problems == (meta.get("problems") or []):
        return meta
    meta = {**meta, "problems": problems,
            "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (_out(item_id) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _load_pine(item_id: str) -> dict:
    """默认读策略库的生效源码；``--source`` 时读指定文件（agent 改策略的候选）。"""
    if SOURCE_OVERRIDE is not None:
        from scripts.pine_library_index import _meta

        from backend.services.agent_arena import pine_library as pl

        text = SOURCE_OVERRIDE.read_text(encoding="utf-8", errors="replace")
        try:
            base = pl.get_source(item_id)     # 标题/分类仍取正式策略，候选只换源码
        except ValueError:
            base = {}
        fresh = _meta(text)
        return {**base, "id": item_id, "version": fresh.get("version") or base.get("version"),
                "lines": fresh.get("lines"), "indent_ok": fresh.get("indent_ok"),
                "source_kind": "candidate", "source": text}
    from backend.services.agent_arena import pine_library as pl

    return pl.get_source(item_id)


def _pine_prompt(got: dict) -> str:
    return (f"策略标题：{got.get('title') or got.get('file')}\n"
            f"分类：{got.get('category')}\n"
            f"Pine 版本：v{got.get('version') or '?'}\n\n"
            f"```pine\n{got['source']}\n```")


REPAIR_PROMPT = """你上一次的转写没通过静态检查 / 回测。下面列出的问题**必须逐条真的修掉**，
输出完整脚本（一个 python 代码块，不要解释），其它逻辑保持原样。

## 必须修掉的问题

```
{error}
```

## 当前脚本

```python
{code}
```

## 修法（照这些例子改，别只改注释）

- 取历史值：`ta.atr(n)[1]`、`(high + low)[1]` 都会抛 TypeError。先赋值再索引：
  `atr_val: Series[float] = ta.atr(n)`，然后写 `atr_val[1]`。
- 取历史的名字必须是 Series 变量（普通赋值/解包/参数都只剩当前值）：
  `s = ta.sma(close, 20)` 之后写 `s[1]` 会抛 TypeError，改成
  `s: Series[float] = ta.sma(close, 20)`；`a, b, c = ta.bb(close, 20, 2)` 之后要取
  `b[1]`，紧跟解包补一行 `b: Series[float] = b`；辅助函数的参数被下标时加注解
  `def f(src: Series[float], n: int)`。
- 元组返回：`m = ta.macd(close, 12, 26, 9)` 拿到的是元组，下标取到的是别的指标；
  改成 `macd_line, signal_line, hist = ta.macd(close, 12, 26, 9)`。
- 缺 import：用到的名字（na / nz / bar_index / time / timeframe / timestamp …）都要写进
  `from pynecore.lib import ...`，用不到的就从 import 里删掉。`ta.bar_index` 不存在，
  用 `from pynecore.lib import bar_index` 后写裸名 `bar_index`。
- 缺失值：`math.isnan` / `math.nan` 不存在——判断用 `na(x)`，字面值写 `float('nan')`。
- Pine 的 `var`（跨 bar 保留状态）必须用 `PersistentSeries`：
  `flag: Series[bool] = False` 每根 bar 都会重置，状态永远存不住（实测这类策略 0 成交）；
  改成 `flag: PersistentSeries[bool] = False`（`from pynecore.types import PersistentSeries`）。
  注意：PersistentSeries 的初始值只算一次，**每根 bar 都要重算的值仍用 `Series[...]`**。
- 分支里声明 Series：`if c: x: Series[float] = a` / `else: x = b` 里分支里的声明只在那个
  分支执行，其它分支的 `x = b` 撞上没喂过的空缓冲 → x 恒为 nan（实测 0 成交）。改成
  分支只算普通值、链后声明一次：
  `if c: raw = a` / `else: raw = b` / `x: Series[float] = raw`。
- 列表容器：`a: PersistentSeries[list] = []` 的 `a[i]` 是「i 根 bar 前的值」，不是列表
  下标（会拿到 list 或 na）——改成 `a: Persistent[list] = []`
  （`from pynecore.types import Persistent`），`a.append(v)` / `a[i]` / `len(a)` 照常用。
- 列表实参别传进 `Series[...]` 标注的参数：参数被标注成 Series 后，`x[i]` 变成「i 根 bar
  前的值」——传进去的是列表就整个返回列表，`bar_index - x[i]` 报
  `TypeError: unsupported operand type(s) for -: 'float' and 'list'`（实测 0159）。
  列表参数的注解直接去掉（裸参数就是普通 Python 值，`x[i]` 是正常下标）。
- Pine 语法残留：`cond ? a : b` → `a if cond else b`；`x := v` → `x = v`；`var x = ...`
  按上面的持久变量规则翻成 `PersistentSeries`。
- 仓位：装饰器统一 `initial_capital=100000` / `default_qty_value=95`（percent_of_equity）。
- 列表初始化：**别写 `[初值] * n`**——pyne 里 n 是浮点（`input()` 的参数、`int()` 转换都是
  浮点：`int()` 被翻成 `safe_int()` 返回 float），list × float 直接
  `TypeError: can't multiply sequence by non-int of type 'float'`（实测 0248/0253/0260）。
  改成 `array.new_bool(n, False)` / `array.new_float(n, 0.0)` / `array.new_int(n, 0)`
  （`from pynecore.lib import array`），或 `[False for _ in range(n)]`（range 的实参会被截断）。
"""


def transpile(item_id: str, model: str = MODEL) -> dict:
    got = _load_pine(item_id)
    user = _pine_prompt(got)
    out = _out(item_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.md").write_text(
        f"# system\n\n{SYSTEM_PROMPT}\n\n# user\n\n{user}\n", encoding="utf-8")

    t0 = time.time()
    think_used = False
    raw, usage, truncated = call_llm(SYSTEM_PROMPT, user, model, think=False)
    if truncated:
        # 关思考还截断（罕见）→ 开思考重来一次：慢得多但保真度更高
        print("  ⚠️ 关思考输出被截断，改用思考模式重试")
        think_used = True
        raw, usage, truncated = call_llm(SYSTEM_PROMPT, user, model, think=True)
    if truncated:
        raise RuntimeError("模型输出被 max_tokens 截断（关思考与开思考都试过了）")
    code = extract_code(raw)
    code, fixes = autofix(code)
    (out / "candidate.py").write_text(code, encoding="utf-8")
    problems = static_check(code)
    meta = {"id": item_id, "title": got.get("title"), "category": got.get("category"),
            "pine_version": got.get("version"), "pine_indent_ok": got.get("indent_ok"),
            "model": model, "think": think_used,
            "elapsed_sec": round(time.time() - t0, 1), "usage": usage,
            "auto_fixes": fixes, "problems": problems,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return meta


def repair(item_id: str, error: str, model: str = MODEL) -> dict:
    """把回测报错喂回模型，改一版候选（只写文件，不执行——执行归沙箱）。"""
    out = _out(item_id)
    cand = out / "candidate.py"
    meta_path = out / "meta.json"
    if not cand.exists() or not meta_path.exists():
        raise RuntimeError(f"没有候选可修：{cand}（先转写一次）")
    code = cand.read_text(encoding="utf-8")
    user = _pine_prompt(_load_pine(item_id))
    t0 = time.time()
    raw, usage, truncated = _chat(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": user},
         {"role": "user", "content": REPAIR_PROMPT.format(error=error[-2000:], code=code)}],
        model, think=False)
    if truncated:
        raise RuntimeError("修复输出被 max_tokens 截断")
    new_code = extract_code(raw)
    new_code, fixes = autofix(new_code)
    cand.write_text(new_code, encoding="utf-8")
    problems = static_check(new_code)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["problems"] = problems
    meta.setdefault("repairs", []).append({
        "error": error[-500:], "usage": usage, "problems": problems,
        "auto_fixes": fixes,
        "elapsed_sec": round(time.time() - t0, 1),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def validate(item_id: str, symbol: str, adj: str = "backward") -> dict:
    from backend.services.agent_arena import market_lab as ml

    out = _out(item_id)
    cand = out / "candidate.py"
    if not cand.exists():
        raise SystemExit(f"没有候选文件：{cand}（先跑 --id {item_id}）")
    t0 = time.time()
    res = ml.run_script_file(cand, symbol, adj=adj, want_indicators=True)
    stats = res.get("stats") or {}
    trades = res.get("trades") or []
    report = {"id": item_id, "symbol": symbol, "adj": adj,
              "elapsed_sec": round(time.time() - t0, 1),
              "stats": stats, "trades": len(trades),
              # 逐笔与净值一并落盘：界面要画净值曲线 / 列成交，重跑一次回测没必要。
              "trade_rows": trades, "equity": res.get("equity") or [],
              # 策略真正算过的指标序列（ema/macd/rsi…）：界面叠到 K 线主图与副图上。
              # 只有沙箱里跑得出来的产物才有——容器侧永不执行模型代码。
              "indicators": res.get("indicators") or {},
              "meta": res.get("meta"),
              "validated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return report


def install(item_id: str) -> dict:
    """把候选装进 lab_strategies/（复制文件 + 打印注册表条目）。"""
    out = _out(item_id)
    cand = out / "candidate.py"
    if not cand.exists():
        raise SystemExit(f"没有候选文件：{cand}")
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8")) if (out / "meta.json").exists() else {}
    if meta.get("problems"):
        raise SystemExit(f"静态检查未通过，先修：{meta['problems']}")
    slug = re.sub(r"[^a-z0-9_]", "", f"pine_{item_id}_{(meta.get('title') or '')}".lower())[:40]
    dst = LAB_DIR / f"{slug}.py"
    dst.write_text(cand.read_text(encoding="utf-8"), encoding="utf-8")
    name = meta.get("title") or item_id
    snippet = (f'    "{slug}": {{\n'
               f'        "file": "{slug}.py", "name": "{name}",\n'
               f'        "desc": "由 .pine 转写（{item_id}），参数未逐一核对。",\n'
               f'        "params": [],\n'
               f'    }},')
    return {"installed": str(dst), "registry_entry": snippet}


def _list() -> int:
    if not OUT_DIR.exists():
        print("还没有转写产物")
        return 0
    for d in sorted(OUT_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = {}
        if (d / "meta.json").exists():
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        rep = (d / "report.json").exists()
        ok = "静态OK" if meta and not meta.get("problems") else (
            "静态FAIL" if meta else "未转写")
        print(f"{d.name}  {ok:8s} 回测{'有' if rep else '无'}  {meta.get('title', '')}")
    return 0


def _redirect_out(out: str) -> None:
    """``--out <dir>``：把**这一条的产物目录**整体指过去（id 不变）。

    对话候选落在 ``data/pine_library/chat/<id>/<job_id>/``——正式产物
    ``data/pine_transpile/<id>/`` 一个字节都不动。
    """
    global OUT_OVERRIDE

    OUT_OVERRIDE = Path(out).resolve()


def _pin_source(path: str) -> None:
    """``--source <file>``：从该文件读 Pine，而不是策略库的生效源码。"""
    global SOURCE_OVERRIDE

    SOURCE_OVERRIDE = Path(path).resolve()


def main() -> int:
    ap = argparse.ArgumentParser(description=".pine → Pyne-Python 转写与验证")
    ap.add_argument("--id", help="策略库 id（如 0001）")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--validate", metavar="SYMBOL", help="用该标的跑一次真实回测")
    ap.add_argument("--adj", default="backward")
    ap.add_argument("--install", action="store_true", help="装进 lab_strategies")
    ap.add_argument("--force", action="store_true", help="已有候选时强制重新转写")
    ap.add_argument("--no-transpile", action="store_true",
                    help="只校验已有候选（不调模型）。队列 worker 的沙箱回测段走这个。")
    ap.add_argument("--repair", metavar="ERROR",
                    help="用回测报错修一版候选（需已有 candidate.py；只写文件不执行）")
    ap.add_argument("--source", metavar="PINE",
                    help="改读该 Pine 文件（默认读策略库生效源码）——候选绝不覆盖正式产物")
    ap.add_argument("--out", metavar="DIR",
                    help="这一条的产物目录（默认 data/pine_transpile/<id>；候选指到 chat/ 下）")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        return _list()
    if args.out:
        _redirect_out(args.out)
    if args.source:
        _pin_source(args.source)
    if not args.id:
        ap.print_help()
        return 1

    if args.install:
        print(json.dumps(install(args.id), ensure_ascii=False, indent=2))
        return 0

    cand = _out(args.id) / "candidate.py"
    meta_path = _out(args.id) / "meta.json"
    if args.repair:
        try:
            meta = repair(args.id, args.repair, model=args.model)
        except RuntimeError as exc:
            print(f"修复失败：{exc}")
            return 3
        last = meta["repairs"][-1]
        print(f"修复完成（{last['elapsed_sec']}s，{last['usage']}）")
    elif args.no_transpile:
        if not cand.exists() or not meta_path.exists():
            print(f"没有候选文件：{cand}（先转写一次）")
            return 2
        meta = _refresh_problems(
            args.id, json.loads(meta_path.read_text(encoding="utf-8")))
    elif cand.exists() and meta_path.exists() and not args.force:
        meta = _refresh_problems(
            args.id, json.loads(meta_path.read_text(encoding="utf-8")))
        print(f"复用已有候选：{meta.get('title')}（--force 可重新转写）")
    else:
        try:
            meta = transpile(args.id, model=args.model)
        except RuntimeError as exc:
            # 队列 worker 拿最后一行当界面上的失败原因，所以这里只打一行、不打栈
            print(f"转写失败：{exc}")
            return 3
        print(f"转写完成：{meta['title']}（{meta['elapsed_sec']}s，{meta['usage']}）")
    if meta["problems"]:
        print("静态检查未通过：")
        for p in meta["problems"]:
            print("  -", p)
        return 2
    print("静态检查通过")

    if args.validate:
        rep = validate(args.id, args.validate, adj=args.adj)
        s = rep["stats"]
        net = (s.get("Net profit") or {}).get("pct")
        dd = (s.get("Max equity drawdown") or {}).get("pct")
        bh = (s.get("Buy & hold return") or {}).get("pct")
        print(f"回测 {args.validate}：净收益 {net:.1f}% · 最大回撤 {dd:.1f}% · "
              f"买入持有 {bh:.1f}% · 成交 {rep['trades']} 笔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
