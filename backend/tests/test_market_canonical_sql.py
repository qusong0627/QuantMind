"""市场口径单源防漂移：SQL ``qm_market_of`` ≡ Python ``_canonical_market``。

§5.5/P2 演练出的缺陷类别：同一「模型属于哪个市场」的问题，Python 侧一套别名表
（``_MARKET_ALIASES``），SQL 侧又是裸值谓词 —— 未知值（'CUSTOM'）两侧分叉，
默认切换/唯一索引/读路径各说各话（详见 data/upgrade_v1.1.4.sql）。

本测试把两侧钉在同一张金样表上：库里逐值跑 ``qm_market_of(jsonb)``，与
``_canonical_market`` 比对。别名集任何一侧改动而另一侧没跟 → 本测试红。
（另钉两级取值链 metadata.market → context.market：与 ``_model_market_of``
在无 model_id 前缀依赖的输入上等价——函数参数只有 jsonb，看不到 model_id。）

要求 v1.1.4 已应用（函数存在）；缺失时直接失败而非 skip——函数缺失本身就是要
修的漂移（db_init/迁移/ensure_tables 三处同源 DDL 必居其一）。
"""

from __future__ import annotations

from typing import Any

import pytest

#: 金样（值, 期望口径）：覆盖全部别名 + 大小写/空白 + 未知/缺省 → CN。
GOLDEN: list[tuple[Any, str]] = [
    # CN 及别名
    ("CN", "CN"), ("cn", "CN"), ("CN ", "CN"), (" A ", "CN"),
    ("A", "CN"), ("A_SHARE", "CN"), ("a股", "CN"), ("A股", "CN"),
    ("CHINA", "CN"), ("SSE", "CN"), ("XSHG", "CN"), ("SZSE", "CN"),
    # HK
    ("HK", "HK"), ("hk", "HK"), ("HONG_KONG", "HK"), ("港股", "HK"),
    ("HKEX", "HK"), ("XHKG", "HK"),
    # US
    ("US", "US"), ("us", "US"), ("美股", "US"), ("NYSE", "US"),
    ("XNYS", "US"), ("NASDAQ", "US"), ("XNAS", "US"), ("AMEX", "US"),
    # CRYPTO
    ("CRYPTO", "CRYPTO"), ("加密", "CRYPTO"), ("加密货币", "CRYPTO"), ("24/7", "CRYPTO"),
    # FUTURES
    ("FUTURES", "FUTURES"), ("期货", "FUTURES"), ("CME", "FUTURES"), ("SHFE", "FUTURES"),
    # 未知/缺省 → CN
    ("CUSTOM", "CN"), ("whatever", "CN"), ("", "CN"), (None, "CN"),
    # 两级取值链：metadata.market 空 → context.market
    ({"market": "", "context": {"market": "HK"}}, "HK"),
    ({"market": None, "context": {"market": "hong_kong"}}, "HK"),
    ({"market": "US", "context": {"market": "HK"}}, "US"),
    ({"context": {"market": "美股"}}, "US"),
    ({"context": {"market": "CUSTOM"}}, "CN"),
    ({}, "CN"),
]


def _expected_python(raw: Any) -> str:
    from backend.shared.model_registry import _canonical_market

    if isinstance(raw, dict):
        # 与 _model_market_of 同序：metadata.market → metadata.context.market → ''（无 model_id 用）
        meta = raw
        value = meta.get("market") or ""
        if not value:
            ctx = meta.get("context")
            value = (ctx or {}).get("market") if isinstance(ctx, dict) else ""
        return _canonical_market(value)
    return _canonical_market(raw)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_function_matches_python_canonical_market():
    import json

    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as session:
            exists = (
                await session.execute(
                    text("SELECT 1 FROM pg_proc WHERE proname = 'qm_market_of'")
                )
            ).scalar()
            assert exists, (
                "qm_market_of 不在库中——v1.1.4 未应用（db_init/迁移/ensure_tables 三处同源）"
            )
            mismatches: list[str] = []
            for raw, expected in GOLDEN:
                if isinstance(raw, dict):
                    sql_val = (
                        await session.execute(
                            text("SELECT qm_market_of(CAST(:m AS JSONB))"),
                            {"m": json.dumps(raw, ensure_ascii=False)},
                        )
                    ).scalar()
                elif raw is None:
                    sql_val = (
                        await session.execute(text("SELECT qm_market_of(NULL)"))
                    ).scalar()
                else:
                    sql_val = (
                        await session.execute(
                            text(
                                "SELECT qm_market_of(jsonb_build_object("
                                "'market', CAST(:m AS TEXT)))"
                            ),
                            {"m": raw},
                        )
                    ).scalar()
                py_val = _expected_python(raw)
                if str(sql_val) != str(expected) or str(py_val) != str(expected):
                    mismatches.append(
                        f"{raw!r}: sql={sql_val!r} py={py_val!r} 期望={expected!r}"
                    )
            assert not mismatches, "SQL/Python 市场口径分叉：\n" + "\n".join(mismatches)
    finally:
        await close_database()
