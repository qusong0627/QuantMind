"""经典因子库夜间增量更新的三个助手函数。

背景（2026-09-14 事故）：alpha360 / tdxgs / jq110 三库只在 09-13 一次性
手动构建过，之后没有任何调度更新它们；CUSTOM 合并（`build_factor_custom_
dataset.rebuild`）每日读这三库，源缺哪天就少哪天的列 —— 产物从 281 列
缩到 159 列，模型 273 维灌不进去，5 个推理任务全灭。现在夜间链
（`market_sync_scheduler.run_market_sync('CUSTOM')`）先补三库再合并，
本文件钉住补库决策的三块积木：

    update_start()          —— 回看起点（--start 双语义：计算输入起点+写出下界）
    latest_source_date()    —— 上游（daily_forward）走到哪天
    libraries_up_to_date()  —— 三库对最新源日是否都已有分区（都齐则整段跳过）

`LIB_UPDATE_LOOKBACK_DAYS` 的**数值下界**另有专测：它必须深到把 EMAC_120
（ewm span=120，无限记忆）的截断种子残余压进 float32 噪声 —— 否则补出来
的分区与全历史构建在数值上不一致，训练集里埋接缝。
"""

from __future__ import annotations

import pandas as pd

from backend.scripts.build_factor_library import (
    ALL_LIBS,
    LIB_UPDATE_LOOKBACK_DAYS,
    latest_source_date,
    libraries_up_to_date,
    update_start,
)


def _touch_lib_partition(root, lib: str, dt: str) -> None:
    d = root / "6_ml_datasets" / lib / f"dt={dt}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "data.parquet").write_bytes(b"x")


class TestUpdateStart:
    def test_is_lookback_days_before_reference(self):
        start = update_start("2026-09-24")
        delta = pd.Timestamp("2026-09-24") - pd.Timestamp(start)
        assert delta.days == LIB_UPDATE_LOOKBACK_DAYS

    def test_returns_compact_date_string(self):
        # build_libraries 直接拿它做字符串比较（`dt_str < start`），格式反了就全写
        assert update_start("2026-09-24").isdigit()
        assert len(update_start("2026-09-24")) == 8

    def test_lookback_covers_emac120_seed_decay(self):
        """EMAC_120 种子残余 (1−2/121)^n 必须 < 1e-6（float32 有效位以内）。

        取 5/7 的自然日→交易日折算（含春节等假期只会更保守地多给交易日）。
        n≈895 时残余 ~4e-7；回看窗砍到一年（n≈260）残余 ~1.4e-2 —— 那正是
        补出来的 EMAC_120 会与全历史构建肉眼可见地不一致的量级。
        """
        trading_days = LIB_UPDATE_LOOKBACK_DAYS * 5 / 7
        residual = (1 - 2 / 121) ** trading_days
        assert residual < 1e-6


class TestLatestSourceDate:
    def test_reads_max_daily_forward_partition(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path))
        for d in ("20260918", "20260923", "20260922"):
            (tmp_path / "1_kline_data" / "daily_forward" / f"dt={d}").mkdir(
                parents=True
            )

        assert latest_source_date() == "20260923"

    def test_none_when_source_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path))
        (tmp_path / "1_kline_data").mkdir()

        assert latest_source_date() is None


class TestLibrariesUpToDate:
    def test_false_until_every_lib_has_latest_partition(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path))
        _touch_lib_partition(tmp_path, ALL_LIBS[0], "20260923")

        assert not libraries_up_to_date("20260923")

        for lib in ALL_LIBS[1:]:
            _touch_lib_partition(tmp_path, lib, "20260923")
        assert libraries_up_to_date("20260923")

    def test_older_partitions_do_not_count(self, tmp_path, monkeypatch):
        """库停在昨天 = 没覆盖今天 —— 跳过条件是逐位相等，不是 >=。"""
        monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path))
        for lib in ALL_LIBS:
            _touch_lib_partition(tmp_path, lib, "20260922")

        assert not libraries_up_to_date("20260923")
