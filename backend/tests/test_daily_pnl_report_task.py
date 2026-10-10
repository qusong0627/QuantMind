"""收盘收益报表 → QQ 推送：报表组装 + 数据收集 + 发送状态机。

钉死的三件事：
  1. 报表金额与卡片同源（总资产 / 当日盈亏 = 总资产 − 日初权益，
     pct 走 ``resolve_daily_pnl_pct`` 同公式）；分母不可得显示 ``—``，不落 0；
  2. 当日无台账行（节假日 / 桥停更）→ 不发送、不造假报表，稍后周期继续等；
  3. QQ 推送失败不算成功（``sent=False``），调用方据此不落 done 标记。

运行（容器）：python3 -m pytest backend/tests/test_daily_pnl_report_task.py -q
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from backend.services.trade.services import daily_pnl_report_task as task


def _row(
    day: str,
    total: float,
    base: float,
    *,
    key: str = "tdx-default-10000001",
    cash: float = 0.0,
    mv: float = 0.0,
    positions: int = 0,
    last: str = "15:00",
):
    return SimpleNamespace(
        account_id=key,
        snapshot_date=date.fromisoformat(day),
        last_snapshot_at=datetime.fromisoformat(f"{day}T{last}:00"),
        total_asset=total,
        day_open_equity=base,
        cash=cash,
        market_value=mv,
        position_count=positions,
    )


class _FakeScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, stmt, *args, **kwargs):
        return _FakeScalarsResult(self.rows)


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def exists(self, key):
        return 1 if key in self.store else 0

    def delete(self, key):
        self.store.pop(key, None)


def _fake_redis():
    """包裹形态同 trade_shared RedisClient：真身挂在 ``.client`` 上。"""
    return SimpleNamespace(client=_FakeRedis())


# ── 报表组装（纯函数）─────────────────────────────────────────────────


class TestBuildReport:
    def _tdx(self):
        return {
            "key": "tdx",
            "label": "通达信桥",
            "total_asset": 918_397.51,
            "cash": 889_591.51,
            "market_value": 28_806.0,
            "position_count": 4,
            # 当日盈亏 = 总资产 − 日初权益，与卡片同一个数
            "day_pnl": 441.86,
            "day_pnl_pct": 0.0481,
        }

    def _qmt(self):
        return {
            "key": "qmt",
            "label": "迅投 QMT",
            "total_asset": 23_834_878.76,
            "cash": 1_000_000.0,
            "market_value": 22_834_878.76,
            "position_count": 12,
            "day_pnl": -12_345.6,
            "day_pnl_pct": -0.0517,
        }

    def test_two_channels_render_amounts_and_total(self):
        # Act
        built = task.build_report([self._tdx(), self._qmt()], date(2026, 10, 8))

        # Assert
        assert built is not None
        title, content = built
        assert title == "收盘收益 · 2026-10-08"
        assert "**通达信桥**" in content
        assert "总资产 ¥918,397.51 ｜ 当日 +¥441.86（+0.05%）" in content
        assert "持仓 4 只 ｜ 现金 ¥889,591.51 ｜ 市值 ¥28,806.00" in content
        assert "**迅投 QMT**" in content
        assert "当日 -¥12,345.60（-0.05%）" in content
        assert "两账户合计 ¥24,753,276.27 ｜ 当日 -¥11,903.74" in content

    def test_single_channel_has_no_total_line(self):
        built = task.build_report([self._tdx()], date(2026, 10, 8))

        assert built is not None
        _, content = built
        assert "通达信桥" in content
        assert "两账户合计" not in content

    def test_empty_channels_returns_none(self):
        """无数据＝不建报表（调用方据此不发送），绝不发空表或 ¥0 表。"""
        assert task.build_report([], date(2026, 10, 8)) is None

    def test_missing_day_open_renders_dash_not_zero(self):
        """日初权益不可得 → 当日一行显示 ``—``；0 的语义是"打平"，不许冒充缺失。"""
        channel = {**self._tdx(), "day_pnl": None, "day_pnl_pct": None}

        _, content = task.build_report([channel], date(2026, 10, 8))

        assert "当日 —" in content
        assert "¥0.00" not in content.split("持仓")[0].split("总资产")[1]

    def test_zero_pnl_keeps_sign_positive(self):
        """打平显示 +¥0.00（不出现 -¥0.00 的负零读法）。"""
        channel = {**self._tdx(), "day_pnl": -0.001, "day_pnl_pct": -0.0001}

        _, content = task.build_report([channel], date(2026, 10, 8))

        assert "当日 +¥0.00（+0.00%）" in content


# ── 数据收集：当日行 + 家族读 ─────────────────────────────────────────


class TestCollectDayChannels:
    @pytest.mark.asyncio
    async def test_takes_only_channels_with_today_row(self):
        """qmt 只有昨日行 → 今日报表不得把旧值当今日收益。"""
        # Arrange
        db = _FakeSession(
            [
                _row(
                    "2026-10-08",
                    918_397.51,
                    917_955.65,
                    cash=889_591.51,
                    mv=28_806.0,
                    positions=4,
                ),
                _row(
                    "2026-10-07",
                    23_834_878.76,
                    23_800_000.0,
                    key="qmt-default-10000001",
                ),
            ]
        )

        # Act
        channels = await task.collect_day_channels(db, date(2026, 10, 8))

        # Assert
        assert [c["key"] for c in channels] == ["tdx"]
        assert channels[0]["day_pnl"] == pytest.approx(441.86, abs=0.005)
        assert channels[0]["position_count"] == 4

    @pytest.mark.asyncio
    async def test_skips_zero_equity_row(self):
        """当日行总资产为 0（空快照/读错）→ 跳过该通道，不发 ¥0 报表。"""
        db = _FakeSession(
            [
                _row("2026-10-08", 918_397.51, 917_955.65),
                _row("2026-10-08", 0.0, 0.0, key="qmt-default-10000001"),
            ]
        )

        channels = await task.collect_day_channels(db, date(2026, 10, 8))

        assert [c["key"] for c in channels] == ["tdx"]

    @pytest.mark.asyncio
    async def test_missing_day_open_yields_none_pnl(self):
        db = _FakeSession([_row("2026-10-08", 918_397.51, 0.0, last="09:00")])

        channels = await task.collect_day_channels(db, date(2026, 10, 8))

        assert channels[0]["day_pnl"] is None
        assert channels[0]["day_pnl_pct"] is None


# ── 发送状态机：真实推送 / 失败 / 无数据 ──────────────────────────────


class TestRunDailyReport:
    def _patch_channels(self, monkeypatch, channels):
        async def fake_collect(db, day):
            return channels

        monkeypatch.setattr(task, "collect_day_channels", fake_collect)

    def _patch_fanout(self, monkeypatch, *, delivered=1, audience=1, exc=None):
        """隔离通知面登记（审计 H11）：记录调用，不碰真库。"""
        calls: list[dict] = []

        async def fake_fanout(**kwargs):
            calls.append(kwargs)
            if exc is not None:
                raise exc
            return delivered, audience

        import backend.shared.notification_publisher as np

        monkeypatch.setattr(np, "publish_notification_to_admins_async", fake_fanout)
        return calls

    @pytest.mark.asyncio
    async def test_sends_and_stores_report(self, monkeypatch):
        # Arrange
        sent: dict = {}

        def fake_notify(title, content, channel="default"):
            sent["title"], sent["content"] = title, content
            return True

        self._patch_fanout(monkeypatch)
        self._patch_channels(
            monkeypatch,
            [
                {
                    "key": "tdx",
                    "label": "通达信桥",
                    "total_asset": 918_397.51,
                    "cash": 889_591.51,
                    "market_value": 28_806.0,
                    "position_count": 4,
                    "day_pnl": 441.86,
                    "day_pnl_pct": 0.0481,
                },
            ],
        )
        import backend.shared.qq_notify as qq

        monkeypatch.setattr(qq, "notify", fake_notify)
        redis = _fake_redis()

        # Act
        result = await task.run_daily_pnl_report(
            redis, today=date(2026, 10, 8), db=object()
        )

        # Assert
        assert result["sent"] is True
        assert sent["title"] == "收盘收益 · 2026-10-08"
        assert "¥918,397.51" in sent["content"]
        assert "trade:daily-pnl-report:20261008" in redis.client.store

    @pytest.mark.asyncio
    async def test_notify_failure_is_not_success(self, monkeypatch):
        """QQ 未送达 → sent=False（调用方据此不落 done，下一周期重试）。"""
        self._patch_fanout(monkeypatch)
        self._patch_channels(
            monkeypatch,
            [
                {
                    "key": "tdx",
                    "label": "通达信桥",
                    "total_asset": 1.0,
                    "cash": 1.0,
                    "market_value": 0.0,
                    "position_count": 0,
                    "day_pnl": None,
                    "day_pnl_pct": None,
                },
            ],
        )
        import backend.shared.qq_notify as qq

        monkeypatch.setattr(qq, "notify", lambda *a, **k: False)
        redis = _fake_redis()

        result = await task.run_daily_pnl_report(
            redis, today=date(2026, 10, 8), db=object()
        )

        assert result["sent"] is False

    @pytest.mark.asyncio
    async def test_no_rows_skips_without_sending(self, monkeypatch):
        self._patch_fanout(monkeypatch)
        self._patch_channels(monkeypatch, [])
        called: list = []
        import backend.shared.qq_notify as qq

        monkeypatch.setattr(qq, "notify", lambda *a, **k: called.append(1) or True)
        redis = _fake_redis()

        result = await task.run_daily_pnl_report(
            redis, today=date(2026, 10, 8), db=object()
        )

        assert result["sent"] is False
        assert result["skipped"] == "no_ledger_rows"
        assert called == [], "无数据不得推 QQ"
        assert "trade:daily-pnl-report:20261008" not in redis.client.store, (
            "无数据不得落报表（只允许登记通知面的痕）"
        )


# ── 通知面登记（审计 H11）：发送/失败都落痕，每（日, 结果）至多一条 ──


def _channel_tdx():
    return {
        "key": "tdx",
        "label": "通达信桥",
        "total_asset": 918_397.51,
        "cash": 889_591.51,
        "market_value": 28_806.0,
        "position_count": 4,
        "day_pnl": 441.86,
        "day_pnl_pct": 0.0481,
    }


class TestNotificationRegistration:
    def _setup(self, monkeypatch, *, notify_result, channels, **fanout_kw):
        async def fake_collect(db, day):
            return channels

        monkeypatch.setattr(task, "collect_day_channels", fake_collect)
        import backend.shared.qq_notify as qq

        monkeypatch.setattr(qq, "notify", lambda *a, **k: notify_result)
        calls = TestRunDailyReport()._patch_fanout(monkeypatch, **fanout_kw)
        return calls

    @pytest.mark.asyncio
    async def test_delivered_report_registers_a_success_row(self, monkeypatch):
        """H11 核心：送达后通知面能查到「已送达」（此前只有 QQ 侧/done 键）。"""
        calls = self._setup(monkeypatch, notify_result=True, channels=[_channel_tdx()])
        redis = _fake_redis()

        result = await task.run_daily_pnl_report(
            redis, today=date(2026, 10, 8), db=object()
        )

        assert result["sent"] is True
        assert len(calls) == 1
        row = calls[0]
        assert row["level"] == "success"
        assert row["type"] == "trading"
        assert "已送达" in row["title"] and "2026-10-08" in row["title"]
        assert "¥918,397.51" in row["content"]
        assert row["qq_alert"] is False, "报表本身已走 QQ，登记不得二次推送"
        assert "trade:daily-pnl-report:registered:20261008:sent" in redis.client.store

    @pytest.mark.asyncio
    async def test_failed_delivery_registers_an_error_row(self, monkeypatch):
        """QQ 未送达 → 通知面落 error 痕并放行 QQ 旁路（手机才是告警面）。"""
        calls = self._setup(monkeypatch, notify_result=False, channels=[_channel_tdx()])
        redis = _fake_redis()

        result = await task.run_daily_pnl_report(
            redis, today=date(2026, 10, 8), db=object()
        )

        assert result["sent"] is False
        assert len(calls) == 1
        row = calls[0]
        assert row["level"] == "error"
        assert "未送达" in row["title"]
        assert "重试" in row["content"]
        assert row["qq_alert"] is True
        assert "trade:daily-pnl-report:registered:20261008:unsent" in redis.client.store

    @pytest.mark.asyncio
    async def test_repeated_failed_attempts_register_once_per_day(self, monkeypatch):
        """未送达时任务每周期重试：登记键把「逐次刷屏」压成一日一条。"""
        calls = self._setup(monkeypatch, notify_result=False, channels=[_channel_tdx()])
        redis = _fake_redis()

        await task.run_daily_pnl_report(redis, today=date(2026, 10, 8), db=object())
        await task.run_daily_pnl_report(redis, today=date(2026, 10, 8), db=object())

        assert len(calls) == 1, "同（日, 结果）至多登记一条"

    @pytest.mark.asyncio
    async def test_unsent_then_delivered_registers_both_outcomes(self, monkeypatch):
        """先败后成：通知面保留完整时间线（先 error 后 success），互不吞并。"""
        state = {"ok": False}

        async def fake_collect(db, day):
            return [_channel_tdx()]

        monkeypatch.setattr(task, "collect_day_channels", fake_collect)
        import backend.shared.qq_notify as qq

        monkeypatch.setattr(qq, "notify", lambda *a, **k: state["ok"])
        calls = TestRunDailyReport()._patch_fanout(monkeypatch)
        redis = _fake_redis()

        await task.run_daily_pnl_report(redis, today=date(2026, 10, 8), db=object())
        state["ok"] = True
        await task.run_daily_pnl_report(redis, today=date(2026, 10, 8), db=object())

        assert [c["level"] for c in calls] == ["error", "success"]

    @pytest.mark.asyncio
    async def test_no_data_day_registers_a_warning_row(self, monkeypatch):
        """桥停更日：报表发不出也不该无声——通知面落一条「无数据」（一日一条）。"""
        calls = self._setup(monkeypatch, notify_result=True, channels=[])
        redis = _fake_redis()

        result = await task.run_daily_pnl_report(
            redis, today=date(2026, 10, 8), db=object()
        )
        await task.run_daily_pnl_report(redis, today=date(2026, 10, 8), db=object())

        assert result["skipped"] == "no_ledger_rows"
        assert len(calls) == 1
        row = calls[0]
        assert row["level"] == "warning"
        assert "无数据" in row["title"]
        assert row["qq_alert"] is False, "数据迟到是常事，先站内留痕不惊动手机"
        assert "trade:daily-pnl-report:registered:20261008:nodata" in redis.client.store

    @pytest.mark.asyncio
    async def test_registration_failure_never_breaks_sending(self, monkeypatch):
        """登记是旁路：fanout 炸了，QQ 送达判定与报表落盘都不受影响。"""
        self._setup(
            monkeypatch,
            notify_result=True,
            channels=[_channel_tdx()],
            exc=RuntimeError("db down"),
        )
        redis = _fake_redis()

        result = await task.run_daily_pnl_report(
            redis, today=date(2026, 10, 8), db=object()
        )

        assert result["sent"] is True
        assert "trade:daily-pnl-report:20261008" in redis.client.store
        assert (
            "trade:daily-pnl-report:registered:20261008:sent" not in redis.client.store
        )

    @pytest.mark.asyncio
    async def test_failed_registration_releases_the_claim_for_retry(self, monkeypatch):
        """登记本身失败（库不可达/无管理员）→ 释放占位，下个周期补登记。"""
        calls = self._setup(
            monkeypatch,
            notify_result=True,
            channels=[_channel_tdx()],
            exc=RuntimeError("db down"),
        )
        redis = _fake_redis()

        await task.run_daily_pnl_report(redis, today=date(2026, 10, 8), db=object())
        assert len(calls) == 1

        # 下一次运行（登记恢复正常）必须还能登记，而不是被失败的占位键挡住
        monkeypatch.undo()
        self._setup(monkeypatch, notify_result=True, channels=[_channel_tdx()])
        await task.run_daily_pnl_report(redis, today=date(2026, 10, 8), db=object())

        assert "trade:daily-pnl-report:registered:20261008:sent" in redis.client.store


# ── 配置解析 ─────────────────────────────────────────────────────────


class TestParseTime:
    def test_valid(self):
        assert task.parse_report_time("15:10") == (15, 10)

    def test_invalid_falls_back_to_close_plus_ten(self):
        """非法值回落 15:10（结算 finalize 15:05 之后，收盘值已定）。"""
        assert task.parse_report_time("25:99") == (15, 10)
        assert task.parse_report_time("") == (15, 10)
        assert task.parse_report_time(None) == (15, 10)
