"""市场真实性验收：把「离线好看、实盘翻车」的三类缺陷变成响亮的失败。

这个模块不产生任何交易信号，它只回答一个问题 —— **这份数据/这个回测，
有没有在偷偷给自己加分？** 三类缺陷的共同点是都不会自己报错：

1. **年代指示器（era indicator）**
   schema 断点让某列在老分区全空、新分区全有，树模型第一个分裂就学它。
   实测：`features_daily` 在 20260914 由 50 列扩到 78 列，新增的 30 列
   NULL 率恰好 99.744%，有值的 27,806 行**正好等于** 20260914 起的 5 个交易日。
   模型的「预测能力」里混进了「这是哪一年」。

2. **缺失率越界**
   高缺失列未剔除就进模型，等于让模型在稀疏噪声上过拟合。

3. **回测窗口过短仍报年化**
   `(1+r)^(1/years)-1` 作用在 12 个交易日上，会把 2% 放大成 ~64%。
   窗口长度本身就是要披露的口径，不是实现细节。

判据一律是**「是不是构成时间分隔」**，不是「像不像异常值」——
后者会把真实的制度变更（涨跌停改革、ST 新规）误杀成缺陷。

对应回归测试：`backend/tests/test_market_fidelity.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "EraIndicator",
    "FidelityError",
    "FidelityReport",
    "Finding",
    "Severity",
    "availability_island",
    "check_backtest_window",
    "era_switch",
    "scan_era_indicators",
    "scan_hardcoded_limit_thresholds",
    "scan_missing_rates",
]

Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

_SEVERITY_ORDER: dict[str, int] = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

#: 某一日中「整列全有」/「整列全空」的容差。parquet 的 NULL 率是精确 0/1，
#: 留一点余量是为了兼容 pandas 侧聚合引入的浮点噪声。
_ALL_PRESENT = 1e-9
_ALL_MISSING = 1.0 - 1e-9


class FidelityError(AssertionError):
    """门禁未通过。继承 AssertionError，便于 pytest 与 `assert` 两种调用方式。"""


@dataclass(frozen=True, slots=True)
class Finding:
    """一条违反市场真实性的发现。

    `severity` 用**纯 str** 而非 Enum —— 见 python-str-enum-isinstance-trap：
    `isinstance(x, str)` 对 str-Enum 恒真，会让下游的字符串比较静默走错分支。
    """

    rule: str
    severity: Severity
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class EraIndicator:
    """某列在时间轴上恰好切换一次「全空 ↔ 全有」。

    `switch_date` 是**切换后**的第一个日期；`missing_side` 指明哪一侧是空的：
    `"before"` = 该列是后加的（老分区全空），`"after"` = 该列被删了。
    """

    column: str
    switch_date: str
    missing_side: Literal["before", "after"]
    n_dates: int


@dataclass(frozen=True, slots=True)
class FidelityReport:
    """发现集合 + 门禁。

    `n_checked` 是**实际参与检查的对象数**。零项参与必须能失败 ——
    没扫描出任何东西不等于数据是干净的（verification-vacuous-pass-guard）。
    """

    findings: tuple[Finding, ...] = ()
    n_checked: int = 0

    def __post_init__(self) -> None:
        # 允许调用方传 list；frozen 下用 object.__setattr__ 归一。
        if not isinstance(self.findings, tuple):
            object.__setattr__(self, "findings", tuple(self.findings))

    @property
    def critical(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "CRITICAL")

    def summary(self) -> str:
        head = (
            f"市场真实性检查：{self.n_checked} 项参与，"
            f"{len(self.findings)} 条发现（CRITICAL {len(self.critical)}）"
        )
        ordered = sorted(
            self.findings, key=lambda f: _SEVERITY_ORDER.get(f.severity, 9)
        )
        lines = [head]
        lines.extend(
            f"  [{f.severity}] {f.rule} · {f.subject} — {f.detail}" for f in ordered
        )
        return "\n".join(lines)

    def gate(self, *, require_checks: bool = False) -> None:
        """不通过就抛 `FidelityError`。HIGH 只点名不阻断，否则门禁会被绕过。"""
        if require_checks and self.n_checked <= 0:
            raise FidelityError(
                f"市场真实性门禁：未检查任何对象（n_checked={self.n_checked}）。"
                "零项参与不等于通过。"
            )
        if self.critical:
            raise FidelityError(
                f"市场真实性门禁未通过（{len(self.critical)} 条 CRITICAL）：\n{self.summary()}"
            )


# ─────────────────────────── 年代指示器 ───────────────────────────


def era_switch(
    dates: Sequence[str],
    null_fractions: Sequence[float],
) -> tuple[str, str] | None:
    """逐日缺失率 → `(切换日, 空在哪一侧)`；不构成年代分隔则 `None`。

    这是**两条取数路径的公共判据**：pandas 面板（`scan_era_indicators`）与
    parquet footer 元数据（`check_market_fidelity.py`，读 null count 不读数据）。
    判据只有一份，两边就不可能判得不一样。

    `missing_side` 语义见 `EraIndicator`：`"before"` = 该列是后加的。
    """
    n = len(dates)
    if n < 2 or len(null_fractions) != n:
        return None

    # 每一天都必须「全有」或「全空」；出现中间值即不是年代分隔。
    if not all(f <= _ALL_PRESENT or f >= _ALL_MISSING for f in null_fractions):
        return None

    present = [f <= _ALL_PRESENT for f in null_fractions]
    switches = [i for i in range(1, n) if present[i] != present[i - 1]]
    if len(switches) != 1:
        return None

    idx = switches[0]
    return str(dates[idx]), ("before" if not present[0] else "after")


def availability_island(
    dates: Sequence[str],
    null_fractions: Sequence[float],
    *,
    min_series: int = 60,
    max_present_share: float = 0.9,
    clustering: float = 0.5,
) -> tuple[str, int, int] | None:
    """列的全部信息锁在时间轴的一小段里 → `(孤岛起始日, 孤岛天数, 总天数)`。

    **`era_switch` 的补充，不是重复。** 它只认「恰好一次切换」，于是漏掉最坏的一种
    形态：老段全空、中段有值、之后又全空。实测 `l1_factors.ind_netflow_rank_20` ——
    全空 2430 天 → 有值 34 天 → 全空 137 天。两次切换，`era_switch` 返回 `None`，
    但任何用到该列的模型都被锚定到 2026 年初那 34 天。

    判定用四个**可解释**的量，不靠"像不像异常"：

    - **两端闭合**（关键）：最长可用段必须同时有不可用日在它前面**和**后面。
      少了这一条，任何长预热期的列都会被误判 —— 实测 alpha360 会 366/368 全红。
      长预热期的可用段一直延伸到序列末尾，那不是孤岛。
    - `max_present_share`：可用日占比超过它 → 是「基本可用」，不是孤岛；
    - `clustering`：最长连续可用段 / 可用日总数。散布的空洞会把连续段切碎
      （最长段远小于可用日总数），孤岛则接近 1.0。这才是「时间聚集」的正解 ——
      用「最长段 / 总天数」会把正常的高频缺失误杀。
    - `min_series`：短序列不做此判定。5 个日期里「缺 1 天」占 20%，
      与「一个月的断档」在比例上无法区分。

    整列全空返回 `None` —— 那是缺失率规则的活，不重复记账。
    """
    n = len(dates)
    if n < min_series or len(null_fractions) != n:
        return None

    present = [f <= 0.5 for f in null_fractions]
    n_present = sum(present)
    if n_present == 0 or n_present / n >= max_present_share:
        return None

    best_start = best_len = 0
    i = 0
    while i < n:
        if not present[i]:
            i += 1
            continue
        j = i
        while j < n and present[j]:
            j += 1
        if j - i > best_len:
            best_start, best_len = i, j - i
        i = j

    # 两端闭合：可用段不许贴在序列的任一端上。贴了就是预热期 / 中途丢失，
    # 由 era_switch + 预热期规则负责，不是孤岛。
    if best_start == 0 or best_start + best_len == n:
        return None
    if best_len / n_present < clustering:
        return None
    return str(dates[best_start]), best_len, n


def scan_era_indicators(
    df,
    *,
    date_col: str,
    feature_cols: Iterable[str] | None = None,
    min_dates: int = 3,
) -> list[EraIndicator]:
    """找出「在时间轴上恰好切换一次全空/全有」的列。

    只看**逐日的整列缺失状态**：某日缺失率必须是 0 或 1，才可能构成年代分隔；
    零散缺失（真·随机缺失）和单日内部分缺失一律不算 —— 那会误杀真信号。

    日期不足 `min_dates` 时直接返回空：两个日期总有「一次切换」，
    那不是发现，是数学必然。
    """
    if date_col not in df.columns:
        raise ValueError(
            f"date_col {date_col!r} 不在 DataFrame 中，无法判定时间分隔；"
            f"现有列：{sorted(map(str, df.columns))[:20]}"
        )

    if feature_cols is None:
        cols = [c for c in df.columns if c != date_col]
    else:
        cols = list(feature_cols)
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"feature_cols 中的 {missing} 不在 DataFrame 中")

    dates = sorted(df[date_col].dropna().unique())
    if len(dates) < min_dates:
        return []

    found: list[EraIndicator] = []
    for col in cols:
        per_date = df.groupby(date_col, dropna=True)[col].apply(
            lambda s: float(s.isna().mean())
        )
        # 当日整列缺席（例如全表无该日）按「全空」计 —— 保守取 1.0，
        # 缺一天不该让整列免检。
        fracs = [float(per_date.get(d, 1.0)) for d in dates]

        hit = era_switch([str(d) for d in dates], fracs)
        if hit is not None:
            found.append(
                EraIndicator(
                    column=str(col),
                    switch_date=hit[0],
                    missing_side=hit[1],  # type: ignore[arg-type]
                    n_dates=len(dates),
                )
            )
    return found


# ─────────────────────────── 缺失率 ───────────────────────────


def scan_missing_rates(
    df,
    *,
    feature_cols: Iterable[str] | None = None,
    max_rate: float = 0.5,
) -> list[Finding]:
    """列缺失率越界即 HIGH。（年代指示器是 CRITICAL，两者不重复记账。）"""
    cols = list(df.columns) if feature_cols is None else list(feature_cols)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"feature_cols 中的 {missing} 不在 DataFrame 中")
    if len(df) == 0:
        return []

    out: list[Finding] = []
    for col in cols:
        rate = float(df[col].isna().mean())
        if rate > max_rate:
            out.append(
                Finding(
                    rule="missing_rate",
                    severity="HIGH",
                    subject=str(col),
                    detail=f"缺失率 {rate:.1%} > 上限 {max_rate:.1%}，不得直接入模",
                )
            )
    return out


# ─────────────────────────── 回测窗口 ───────────────────────────


def check_backtest_window(
    dates: Iterable[str],
    *,
    min_trading_days: int = 60,
    context: str = "",
) -> list[Finding]:
    """窗口过短或为空 → CRITICAL。

    短窗本身不违规，**短窗报年化**才违规。这里拦在源头，因为下游
    `(1+r)^(1/years)-1` 无法从收益率反推窗口长度。
    """
    n = len(set(dates)) if dates is not None else 0
    prefix = f"{context}：" if context else ""

    if n == 0:
        return [
            Finding(
                rule="backtest_window",
                severity="CRITICAL",
                subject=context or "backtest",
                detail=f"{prefix}回测窗口为空，无任何交易日，结果不可解释",
            )
        ]

    if n < min_trading_days:
        return [
            Finding(
                rule="backtest_window",
                severity="CRITICAL",
                subject=context or "backtest",
                detail=(
                    f"{prefix}仅 {n} 个交易日 < 最少 {min_trading_days} 天；"
                    f"按 (1+r)^(1/years)-1 年化会把短期噪声放大成假收益"
                ),
            )
        ]

    return []


# ────────────────── 源码扫描：自写涨跌停阈值 ──────────────────

#: 权威模块自身持有制度常量，不得被自扫误伤。
#: `market_fidelity.py` 也在内 —— 它必须写下这些字面量才能扫描它们。
_AUTHORITATIVE = frozenset({"local_market_data.py", "market_fidelity.py"})

#: 显式豁免，必须附理由：`# fidelity: allow-limit-threshold — 说明`。
#: 刻意**避开 ruff 的 noqa 指令语法** —— 那套前缀属于 ruff，本门禁若沿用，
#: ruff 会把每一处合法豁免判成非法指令并全仓告警，等于用 lint 噪声换豁免。
#: 测试 `test_hardcoded_limit_scanner_does_not_honour_ruff_noqa` 钉死了这条分家。
_ALLOW_RE = re.compile(r"#\s*fidelity\s*:\s*allow-limit-threshold", re.IGNORECASE)

#: ST 判定写死。ST 的 5% 板永远拦不住 9.5% 的阈值，方向反了。
_ST_FALSE_RE = re.compile(r"\bis_st\s*=\s*False\b")

_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")

#: 「接近涨跌停」的硬编码阈值。含人为留的余量，正是这些余量让制度判定失真：
#: 20%/30% 板上一根 12% 的**非**涨停阴线会被 9.8 误剔，ST 的 5% 涨停反而漏网。
_LIMIT_LITERALS = frozenset(
    round(v, 4)
    for v in (
        4.8,
        4.9,  # ST 5% 板
        9.5,
        9.8,
        9.9,  # 主板 10% 板
        19.5,
        19.8,  # 创业板/科创板 20% 板
        29.5,
        29.8,  # 北交所 30% 板
        0.048,
        0.049,
        0.095,
        0.098,
        0.099,
        0.195,
        0.198,
        0.295,
        0.298,
    )
)

_COMPUTE_LIMITS_HINT = "请改用 compute_limits / limit_pct（唯一权威实现）"


def _is_limit_literal(token: str) -> bool:
    try:
        return round(float(token), 4) in _LIMIT_LITERALS
    except ValueError:
        return False


def _scan_source(text: str, subject_prefix: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _ALLOW_RE.search(raw):
            continue
        # 只判代码部分；注释里提到 9.8 不算实现缺陷。
        code = raw.split("#", 1)[0]
        subject = f"{subject_prefix}:{lineno}"

        if _ST_FALSE_RE.search(code):
            findings.append(
                Finding(
                    rule="hardcoded_limit_threshold",
                    severity="HIGH",
                    subject=subject,
                    # 措辞刻意不写死：「初始化」和「except 分支静默降级」都长这样，
                    # 扫描器分辨不了，需要人去读上下文。别把线索当判决。
                    detail=(
                        f"is_st=False 字面量：ST 的 5% 涨跌停会漏判"
                        f"（写死，或 except 分支静默降级）；{_COMPUTE_LIMITS_HINT}"
                    ),
                )
            )
            continue

        for m in _NUM_RE.finditer(code):
            if _is_limit_literal(m.group(1)):
                findings.append(
                    Finding(
                        rule="hardcoded_limit_threshold",
                        severity="HIGH",
                        subject=subject,
                        detail=f"硬编码涨跌停阈值 {m.group(1)}（未区分板块/ST/制度日期）；{_COMPUTE_LIMITS_HINT}",
                    )
                )
                break
    return findings


def _iter_py_files(paths: Iterable) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
        elif p.suffix == ".py":
            files.append(p)
    return files


def scan_hardcoded_limit_thresholds(paths: Iterable) -> list[Finding]:
    """扫描源码中绕过权威实现、自写的涨跌停阈值。

    读文件用 `errors="replace"` —— 扫描器不该因为一个非 UTF-8 文件整体崩掉，
    但也不该 try/except 静默跳过（那正是「零项参与＝通过」的变体）。
    """
    findings: list[Finding] = []
    for p in _iter_py_files(paths):
        if p.name in _AUTHORITATIVE:
            continue
        # subject 用完整相对路径 —— 只给文件名，运维时无法定位到具体哪一处。
        try:
            label = str(p.relative_to(Path.cwd()))
        except ValueError:
            label = str(p)
        findings.extend(
            _scan_source(p.read_text(encoding="utf-8", errors="replace"), label)
        )
    return findings
