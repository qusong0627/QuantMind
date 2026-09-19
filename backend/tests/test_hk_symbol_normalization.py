"""港股代码归一化回归测试（4 位 + .HK 唯一口径）。

修复前的真实缺陷：
1. `to_hk_suffix` 对任何以 `.HK` 结尾的输入直接原样返回，`02057.HK` / `700.HK`
   这类「带后缀但没补零/去掉前导零」的写法会漏过归一化，落到下游查询静默查空；
2. `predict_single_stock` 对 symbol 一律走 `StockCodeUtil.to_prefix`（A 股口径），
   港股 4 位裸码 `2057` 原样透传 → QuantDB/聚合表按 `2057` 查不到 → 404
   「HK 市场无 2057 的历史行情数据」。**推理中心港股页排名列表点行即整片 404**；
3. 用户手输 6 位 `000700` 会被 `to_prefix` 补成 `SZ000700`（深市 A 股）跨市场串号。

零 mock：幂等性用例直接吃真实 HK pred.parquet 的 2738 只 symbol 全集。

跑法：
    docker exec -w /app/backend quantmind python -m pytest \
        tests/test_hk_symbol_normalization.py -q --no-cov
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from backend.shared.stock_utils import StockCodeUtil

pytestmark = pytest.mark.unit


# ---- 1. 规范形态（4 位 + .HK）----------------------------------------------

_CANONICAL_CASES = [
    # 裸数字：任意位宽都要补/裁到 4 位
    ("700", "0700.HK"),
    ("0700", "0700.HK"),
    ("00700", "0700.HK"),
    ("2057", "2057.HK"),
    ("02057", "2057.HK"),
    ("9988", "9988.HK"),
    ("09988", "9988.HK"),
    ("1", "0001.HK"),
    ("00001", "0001.HK"),
    # 已带后缀但非规范：必须重新归一，而不是原样透传
    ("700.HK", "0700.HK"),
    ("0700.HK", "0700.HK"),
    ("00700.HK", "0700.HK"),
    ("02057.HK", "2057.HK"),
    ("9988.HK", "9988.HK"),
    ("09988.HK", "9988.HK"),
    # 小写后缀 / 空白
    ("0700.hk", "0700.HK"),
    (" 0700 ", "0700.HK"),
    # 5 位真码（8 开头的人民币柜台/特殊码）保留 5 位
    ("89888", "89888.HK"),
    ("80737", "80737.HK"),
    ("89888.HK", "89888.HK"),
]


@pytest.mark.parametrize("raw,expected", _CANONICAL_CASES)
def test_to_hk_suffix_canonical(raw: str, expected: str) -> None:
    assert StockCodeUtil.to_hk_suffix(raw) == expected


def test_to_hk_suffix_empty() -> None:
    assert StockCodeUtil.to_hk_suffix("") == ""


# ---- 2. 幂等性：真实港股 universe 上必须是恒等映射 --------------------------
#
# 若归一化对已规范代码不是恒等，下游「归一后再比对」的等值匹配就会把
# 同一只股票拆成两个键（`0700.HK` 与 `0700.HK.HK` 之类），静默查空。


def _hk_universe() -> list[str]:
    """取真实 HK 训练产物里的 symbol 全集（无产物时回退到港股日线目录）。"""
    candidates = sorted(glob.glob("/data/training_jobs/*/pred.parquet"))
    for path in candidates:
        try:
            import duckdb

            con = duckdb.connect()
            rows = con.execute(
                f"SELECT DISTINCT symbol FROM read_parquet('{path}') "
                "WHERE UPPER(CAST(symbol AS VARCHAR)) LIKE '%.HK' LIMIT 1"
            ).fetchall()
            con.close()
            if not rows:
                continue
            con = duckdb.connect()
            syms = [
                str(r[0])
                for r in con.execute(
                    f"SELECT DISTINCT symbol FROM read_parquet('{path}')"
                ).fetchall()
            ]
            con.close()
            hk = [s for s in syms if s.upper().endswith(".HK")]
            if hk:
                return hk
        except Exception:  # noqa: BLE001
            continue
    # 回退：港股日线 parquet 的文件名即 symbol
    for base in ("/data/quanthk/1_kline_data/daily_forward", "/data/quanthk"):
        p = Path(base)
        if not p.is_dir():
            continue
        found = [f.stem for f in p.rglob("*.parquet") if f.stem.upper().endswith(".HK")]
        if found:
            return found
    return []


def test_to_hk_suffix_idempotent_on_real_universe() -> None:
    universe = _hk_universe()
    # 空集合会让断言静默通过（假通过），必须先判参与量
    assert len(universe) > 100, f"港股 symbol 全集取不到（n={len(universe)}）"
    drifted = [s for s in universe if StockCodeUtil.to_hk_suffix(s) != s]
    assert not drifted, (
        f"{len(drifted)}/{len(universe)} 只港股代码归一后漂移，"
        f"样例：{drifted[:10]}"
    )


# ---- 3. 市场感知取数：港股各写法必须命中同一只 ------------------------------
#
# 这条是排名列表点行 404 的根因锁：`predict_single_stock` 必须按 market
# 选口径，而不是一律套 A 股 `to_prefix`。


def test_predict_stock_hk_symbol_forms_converge() -> None:
    """裸 4 位 / 5 位 / 带后缀三种写法归一后必须是同一个 key。"""
    variants = ["2057", "02057", "2057.HK", "02057.HK", " 2057.hk "]
    keys = {StockCodeUtil.to_hk_suffix(v) for v in variants}
    assert keys == {"2057.HK"}, f"港股写法未收敛到同一 key：{keys}"


def test_hk_raw_code_not_misrouted_to_a_share() -> None:
    """6 位港股写法不得被 A 股口径补成 SH/SZ 前缀（跨市场串号）。

    `000700` 在 A 股口径下是深市股票；港股侧必须先按 market=HK 走
    `to_hk_suffix`，绝不能让 `to_prefix` 先咬一口。
    """
    assert StockCodeUtil.to_prefix("000700") == "SZ000700"  # A 股口径（对照）
    assert StockCodeUtil.to_hk_suffix("000700") == "0700.HK"  # 港股口径
