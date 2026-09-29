"""因子身份与查重（廉价层）：名称/公式归一化、代码指纹、重复判定。

用途：
- RD-Agent 挖掘落库前（``scripts/alpha_agent/run_rd_agent.py``）：
  与 ``rd_agent_factors`` 存量 + 同批候选比对，重复的**不入库**；
- 物化器（``backend/scripts/rd_mined_materialize.py``）复用列名映射
  （``feature_column_name``）与 ``code_fingerprint``（检测同 id 代码改写
  → 旧判定失效须重算），**不**走本模块的存量索引。

口径刻意保守（只抓高置信重复）：LaTeX 规范化仅削纯排版噪声
（``\\left``/``\\right``/间距命令/空白），**不**削括号等结构符号；
代码指纹忽略注释、空行与行首缩进。跨写法/跨记号的「换个写法的老因子」
由物化时的值级相关层（阈值 0.9 口径）兜底，不依赖本模块。

纯标准库实现，便于在挖掘子进程/脚本/测试中直接 import。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DuplicateVerdict",
    "code_fingerprint",
    "feature_column_name",
    "find_duplicate",
    "normalize_factor_name",
    "normalize_formula",
    "partition_duplicates",
]

#: LaTeX 纯排版噪声命令：自适应定界符与显式间距命令去括号后无数学含义。
_FORMULA_NOISE_RE = re.compile(
    r"\\left|\\right|\\[;,!]|\\quad|\\qquad|\\thinspace|\\medspace"
)
_NON_SQL_CHAR_RE = re.compile(r"[^A-Za-z0-9_]")
_UNDERSCORE_RUN_RE = re.compile(r"_+")

#: 与训练直读闸门一致：映射列名必须是合法 SQL 标识符（read_range 对 alias 有
#: fullmatch 校验），列名一律 ASCII。
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize_factor_name(name: str | None) -> str:
    """因子名归一：小写、仅保留字母数字（Unicode 字母数字保留，如中文）。"""
    return "".join(ch for ch in str(name or "").strip().lower() if ch.isalnum())


def normalize_formula(formulation: str | None) -> str:
    """LaTeX 公式归一：剥 ``$`` 包裹、去排版噪声与全部空白、统一小写。

    只做**高置信**归一（宁可漏报不可误报）：括号等结构符号原样保留，
    所以 ``\\frac{a}{b}`` 与 ``\\frac{(a)}{b}`` 视为不同写法。
    """
    text = str(formulation or "")
    if not text.strip():
        return ""
    text = text.strip()
    if len(text) >= 2 and text.startswith("$") and text.endswith("$"):
        text = text[1:-1]
    text = _FORMULA_NOISE_RE.sub("", text)
    text = re.sub(r"\s+", "", text)
    return text.lower()


def code_fingerprint(code: str | None) -> str | None:
    """因子代码指纹：逐行去注释、strip 缩进、去空行后 sha1（前 16 位）。

    全为注释/空白时返回 ``None``（空代码不参与比对，避免空对空误判）。
    """
    if not code:
        return None
    lines: list[str] = []
    for raw in str(code).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    joined = "\n".join(lines)
    if not joined:
        return None
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def feature_column_name(name: str | None) -> str:
    """因子名 → ``rd_`` 前缀的 SQL 安全列名（写入 rd_mined 库的列名）。

    规则与 ``alpha_factor_pipeline._sanitize_feature_key`` 同族（非字母数字 →
    ``_``、折叠、小写、截 80），区别有二：前缀 ``rd_``（与 l1 系因子列天然
    不撞）；**含非 ASCII 字符时追加 6 位名 hash** —— 中文名 ASCII 化后会大量
    塌缩（"动量_20" → "20"），不加 hash 会把不同因子映射到同一列名。
    长名先给 hash 腾位再截断，hash 不会被 80 上限削掉（削掉 = 塌缩病复发）。
    """
    raw = str(name or "").strip()
    key = _NON_SQL_CHAR_RE.sub("_", raw).lower()
    key = _UNDERSCORE_RUN_RE.sub("_", key).strip("_")
    if not key:
        key = "f"
    if any(ord(ch) > 127 for ch in raw):
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
        key = f"{key[: 80 - len('rd_') - len(digest) - 1]}_{digest}"
    column = f"rd_{key}"[:80]
    if not _SQL_IDENTIFIER_RE.match(column):  # pragma: no cover - 防御兜底
        column = "rd_f"
    return column


@dataclass(frozen=True)
class DuplicateVerdict:
    """一次重复判定的结果（reason 指明命中哪一层）。"""

    reason: str  # "name" | "formula" | "code"
    matched_id: str | None
    matched_name: str


def _row_fields(row: Mapping[str, Any]) -> tuple[str | None, str, str, str]:
    return (
        str(row.get("factor_id") or "") or None,
        str(row.get("factor_name") or ""),
        str(row.get("factor_formulation") or ""),
        str(row.get("factor_code") or ""),
    )


class _IdentityIndex:
    """名称/公式/代码指纹三张表的查重索引。"""

    def __init__(self) -> None:
        self.names: dict[str, tuple[str | None, str]] = {}
        self.formulas: dict[str, tuple[str | None, str]] = {}
        self.codes: dict[str, tuple[str | None, str]] = {}

    def add(
        self, factor_id: str | None, name: str, formulation: str, code: str
    ) -> None:
        norm_name = normalize_factor_name(name)
        if norm_name:
            self.names.setdefault(norm_name, (factor_id, name))
        norm_formula = normalize_formula(formulation)
        if norm_formula:
            self.formulas.setdefault(norm_formula, (factor_id, name))
        fingerprint = code_fingerprint(code)
        if fingerprint:
            self.codes.setdefault(fingerprint, (factor_id, name))

    def find(self, name: str, formulation: str, code: str) -> DuplicateVerdict | None:
        norm_name = normalize_factor_name(name)
        if norm_name and norm_name in self.names:
            fid, fname = self.names[norm_name]
            return DuplicateVerdict("name", fid, fname)
        norm_formula = normalize_formula(formulation)
        if norm_formula and norm_formula in self.formulas:
            fid, fname = self.formulas[norm_formula]
            return DuplicateVerdict("formula", fid, fname)
        fingerprint = code_fingerprint(code)
        if fingerprint and fingerprint in self.codes:
            fid, fname = self.codes[fingerprint]
            return DuplicateVerdict("code", fid, fname)
        return None


def find_duplicate(
    name: str,
    formulation: str,
    code: str,
    existing: Iterable[Mapping[str, Any]],
) -> DuplicateVerdict | None:
    """与存量因子逐一比对，返回首个命中（名称 → 公式 → 代码指纹）。"""
    index = _IdentityIndex()
    for row in existing or ():
        index.add(*_row_fields(row))
    return index.find(name, formulation, code)


def partition_duplicates(
    candidates: Sequence[Mapping[str, Any]],
    existing: Iterable[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[tuple[Mapping[str, Any], DuplicateVerdict]]]:
    """把候选切分为 (保留, 重复)。

    批内去重：保留的先入索引，后续同指纹候选命中它（matched_id 取候选
    自己的 ``factor_id``，落库前即可指认）。保序。
    """
    index = _IdentityIndex()
    for row in existing or ():
        index.add(*_row_fields(row))
    kept: list[Mapping[str, Any]] = []
    duplicates: list[tuple[Mapping[str, Any], DuplicateVerdict]] = []
    for cand in candidates or ():
        fid, cname, cformula, ccode = _row_fields(cand)
        verdict = index.find(cname, cformula, ccode)
        if verdict is not None:
            duplicates.append((cand, verdict))
            continue
        kept.append(cand)
        index.add(fid, cname, cformula, ccode)
    return kept, duplicates
