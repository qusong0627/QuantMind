"""各市场数据中枢的 DuckDB 视图挂载单元测试。

回归背景：基类 ``QuantDBDataHub.query()`` 在 Catalog 报错后会以
``self._mount_views(conn, force=True)`` 重试一次；港股/美股/期货/加密四个
子类此前把签名写成 ``_mount_views(self, conn)``，重试路径直接抛
``TypeError``，把真正的错误盖掉（数据目录晚挂载时表现为莫名其妙的类型错误）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.data_platform.quantbc_hub import QuantBCDataHub
from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.services.engine.data_platform.quantfutures_hub import QuantFuturesDataHub
from backend.services.engine.data_platform.quanthk_hub import QuantHKDataHub
from backend.services.engine.data_platform.quantus_hub import QuantUSDataHub

_CATALOG_ERROR = (
    "Catalog Error: Table with name qdb_daily_unadjusted does not exist!\n"
    'Did you mean "duckdb_indexes"?'
)

_SUBCLASSES = [QuantHKDataHub, QuantUSDataHub, QuantFuturesDataHub, QuantBCDataHub]


def _fake_conn(errors: list[Exception] | None = None) -> MagicMock:
    """conn.execute(...).fetchdf() 形态的假连接：先抛 errors，随后返回结果。"""
    cursor = MagicMock()
    cursor.fetchdf.return_value = pd.DataFrame({"dt": [20260720]})
    conn = MagicMock()
    conn.execute.side_effect = [*(errors or []), cursor]
    return conn


@pytest.mark.parametrize("hub_cls", _SUBCLASSES)
def test_subclass_hub_retries_view_mount_on_catalog_error(tmp_path, hub_cls):
    # 空数据目录：挂载视图不会产生额外的 execute 调用
    hub = hub_cls(data_dir=tmp_path)
    conn = _fake_conn([Exception(_CATALOG_ERROR)])

    with patch.object(hub, "_get_duck_conn", return_value=conn):
        out = hub.query("SELECT DISTINCT dt FROM qdb_daily_unadjusted ORDER BY dt")

    assert list(out["dt"]) == [20260720]
    assert conn.execute.call_count == 2  # 首查失败 → 重建视图 → 重试一次


def test_base_hub_retries_view_mount_on_catalog_error(tmp_path):
    hub = QuantDBDataHub(data_dir=tmp_path)
    conn = _fake_conn([Exception(_CATALOG_ERROR)])

    with patch.object(hub, "_get_duck_conn", return_value=conn):
        out = hub.query("SELECT DISTINCT dt FROM qdb_daily_unadjusted ORDER BY dt")

    assert list(out["dt"]) == [20260720]


def test_unrelated_query_error_propagates(tmp_path):
    hub = QuantDBDataHub(data_dir=tmp_path)
    conn = MagicMock()
    conn.execute.side_effect = RuntimeError("Binder Error: unrelated failure")

    with patch.object(hub, "_get_duck_conn", return_value=conn):
        with pytest.raises(RuntimeError, match="unrelated failure"):
            hub.query("SELECT 1")


def test_mount_views_force_rebuilds_for_every_market(tmp_path):
    # force=True 必须绕开"本连接已挂载"标记：数据目录补齐后基类 query()
    # 正是靠它重建视图，否则该连接会一直报 Table does not exist
    part = tmp_path / "1_kline_data" / "daily_forward" / "dt=20260720"
    part.mkdir(parents=True)
    pd.DataFrame({"symbol": ["600036.SH"], "close": [1.0]}).to_parquet(
        part / "data.parquet"
    )

    for hub_cls in [QuantDBDataHub, *_SUBCLASSES]:
        hub = hub_cls(data_dir=tmp_path)
        conn = MagicMock()

        hub._mount_views(conn)  # 首次：建视图
        mounted = conn.execute.call_count
        assert mounted >= 1

        hub._mount_views(conn)  # 已挂载：跳过
        assert conn.execute.call_count == mounted

        hub._mount_views(conn, force=True)  # 强制重建
        assert conn.execute.call_count > mounted
