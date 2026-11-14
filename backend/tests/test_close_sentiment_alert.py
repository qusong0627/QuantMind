"""收盘情绪 QQ 提示：内容口径 + 同日去重/发送分支（不打真 QQ）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.services.live_trading.services import close_sentiment_alert as mod

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 13, 15, 10, tzinfo=CST)


class TestBuildMessage:
    def test_composes_live_and_prior_close(self, monkeypatch):
        monkeypatch.setattr(mod, "_index_line", lambda now: "上证 -0.59%｜深成 -2.05%")
        monkeypatch.setattr(
            mod, "_live_block", lambda now: ["今日实时：状态 偏弱｜建议仓位系数 0.7"]
        )
        monkeypatch.setattr(
            mod,
            "_stage_block",
            lambda now: ["上一交易日阶段（截至 2026-11-12 收盘）：六块 -8/±12（防守）"],
        )
        title, content = mod.build_message(NOW)
        assert title == "收盘情绪提示｜11-13"
        assert "【收盘情绪提示｜2026-11-13 15:10】" in content
        assert "上证 -0.59%" in content
        assert "今日实时：状态 偏弱" in content
        assert "上一交易日阶段" in content
        assert "复盘前先看" in content
        assert "次日凌晨补齐" in content

    def test_missing_everything_still_returns_a_prompt(self, monkeypatch):
        monkeypatch.setattr(mod, "_index_line", lambda now: "")
        monkeypatch.setattr(
            mod, "_live_block", lambda now: ["今日实时：盘中 regime 不可得"]
        )
        monkeypatch.setattr(
            mod, "_stage_block", lambda now: ["上一交易日阶段：数据缺口"]
        )
        title, content = mod.build_message(NOW)
        assert title.startswith("收盘情绪提示")
        assert "数据缺口" in content


class TestSend:
    def test_already_sent_skips(self, monkeypatch):
        monkeypatch.setattr(mod, "_already_sent", lambda day: True)
        out = mod.send_close_sentiment(NOW)
        assert out["status"] == "skipped"
        assert out["reason"] == "already_sent"

    def test_success_marks_sent(self, monkeypatch):
        marked = []
        monkeypatch.setattr(mod, "_already_sent", lambda day: False)
        monkeypatch.setattr(mod, "build_message", lambda now: ("t", "c"))
        monkeypatch.setattr(mod, "_mark_sent", lambda day: marked.append(day))

        def _alert(**kwargs):
            return True

        import backend.shared.qq_notify as qq

        monkeypatch.setattr(qq, "alert_async", _alert)
        out = mod.send_close_sentiment(NOW)
        assert out["status"] == "sent"
        assert marked == ["20261113"]

    def test_qq_not_configured_does_not_mark(self, monkeypatch):
        marked = []
        monkeypatch.setattr(mod, "_already_sent", lambda day: False)
        monkeypatch.setattr(mod, "build_message", lambda now: ("t", "c"))
        monkeypatch.setattr(mod, "_mark_sent", lambda day: marked.append(day))

        import backend.shared.qq_notify as qq

        monkeypatch.setattr(qq, "alert_async", lambda **kwargs: False)
        out = mod.send_close_sentiment(NOW)
        assert out["status"] == "skipped"
        assert out["reason"] == "qq_not_configured"
        assert marked == []
