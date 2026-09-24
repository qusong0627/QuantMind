"""TCA 读数面（`scripts/tca_report.py`）的纯函数守卫——不碰库。

守的是**读数纪律**，不是算术（算术在 `test_exec_cost.py`）：执行损耗报告最危险的
失败不是算错，而是**把"没测到"印成"没问题"**。本文件逐条钉住：

1. **空样本不许印 0 bps**——没有基准价时加权滑点算不出来，印成 `0.00` 读者会
   读成"执行完美"；正确输出是「无样本」；
2. **不可定价与 0 bps 分列**——`n_unpriced` 必须与 `n_priced` 并列露出，缺口计数
   不许被吞（缺 73 笔而只印"可定价 0"的报告，读者无从知道是漏采还是真没有）；
3. **样本 <30 只展示不结论**——`MIN_SAMPLE` 纪律必须出现在**终端输出里**，不能只写进
   JSON 字段（隔壁的教训：纪律写进了 scorecard 的字段，`print_report` 从不打印它，
   读终端的人只看到一行光秃秃的均值）；
4. **已知边界原样印出**——手续费口径、成交确认时刻的粒度、基准价缺口，这三条是本
   读数解释力的边界，藏在文档里等于没有；
5. **落盘是原子的**——读到一半的 JSON 比没有更坏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.scripts.tca_report import (
    DEFAULT_DAYS,
    EXIT_ATTENTION,
    EXIT_OK,
    MIN_SAMPLE,
    SMALL_SAMPLE_NOTE,
    build_report,
    main,
    push_line,
    render,
    reports_dir,
    write_report,
)
from backend.shared.exec_cost_source import LoadResult

def _row(**over: object) -> dict[str, object]:
    """一条合并前的成交样本（键集合与 ``TCA_SAMPLE_FIELDS`` 一致）。"""
    base: dict[str, object] = {
        "order_id": "o1",
        "symbol": "600036.SH",
        "side": "buy",
        "fill_px": 10.05,
        "filled": 100.0,
        "ts": "2026-09-23T01:30:00+00:00",
        "date": "2026-09-23",
        "ref_px": 10.0,
        "limit_px": 10.1,
        "wanted": 100.0,
        "decided_ts": "2026-09-23T01:20:00+00:00",
        "submit_ts": "2026-09-23T01:29:00+00:00",
        "fill_ts": "2026-09-23T01:30:00+00:00",
        "path": "llm_decision",
        "agent": "alpha",
        "exchange_trade_id": "T1",
        "price_source": "detail",
        "fees": 0.0,
    }
    base.update(over)
    return base


def _loaded(samples: tuple[dict[str, object], ...] = (), **over: object) -> LoadResult:
    base: dict[str, object] = {
        "zero_fill_orders": 0,
        "window": ("2026-09-23",),
        "coverage": {
            "n_trades": len(samples),
            "n_orphan": 0,
            "n_not_real": 0,
            "n_synth": 0,
            "n_missing_ref": 0,
            "n_missing_limit": 0,
            "n_unknown_path": 0,
            "n_orders": len({str(s.get("order_id")) for s in samples}),
        },
    }
    base.update(over)
    return LoadResult(samples=samples, **base)  # type: ignore[arg-type]


def _report(loaded: LoadResult, **over: object) -> dict[str, object]:
    return build_report(
        loaded,
        days=DEFAULT_DAYS,
        tenant_id="default",
        user_id="10000001",
        generated="2026-09-24 09:05:00",
        **over,
    )


# ── 空样本：不许印 0 bps ────────────────────────────────────────────
def test_render_says_no_sample_instead_of_zero_bps() -> None:
    """没有可定价样本时，输出必须是「无样本」，**绝不出现任何 bps 数字**。"""
    text = "\n".join(render(_report(_loaded())))
    assert "无样本" in text
    assert "bps" not in text


def test_push_line_refuses_to_report_a_number_without_samples() -> None:
    line = push_line(_report(_loaded()))
    assert "无样本" in line or "暂无可读数字" in line
    assert "bps" not in line


# ── 缺口与 0 分列 ──────────────────────────────────────────────────
def test_render_lists_priced_and_unpriced_side_by_side() -> None:
    """缺基准价的那批必须与可定价的同框印出——只印可定价等于把缺口藏起来。"""
    loaded = _loaded(
        samples=(_row(), _row(order_id="o2", ref_px=None, exchange_trade_id="T2")),
        coverage={
            "n_trades": 2,
            "n_orphan": 0,
            "n_not_real": 0,
            "n_synth": 0,
            "n_missing_ref": 1,
            "n_missing_limit": 0,
            "n_unknown_path": 0,
            "n_orders": 2,
        },
    )
    text = "\n".join(render(_report(loaded)))
    assert "可定价 1" in text
    assert "不可定价 1" in text
    assert "bps" in text  # 有可定价样本时才允许出现 bps


def test_render_reports_orphan_and_unknown_path_counters() -> None:
    """覆盖计数逐项露出：拼不上订单的、认不出来路的，都要能被读出来。"""
    loaded = _loaded(
        samples=(_row(path="unknown"),),
        coverage={
            "n_trades": 3,
            "n_orphan": 2,
            "n_not_real": 4,
            "n_synth": 0,
            "n_missing_ref": 1,
            "n_missing_limit": 0,
            "n_unknown_path": 1,
            "n_orders": 1,
        },
    )
    text = "\n".join(render(_report(loaded)))
    assert "拼不上订单" in text
    assert "路径未识别" in text
    assert "非真单" in text


def test_render_marks_synth_fills_so_the_reader_knows_the_price_caliber() -> None:
    """合成成交（柜面均价口径）单列计数——不剔，但读者要知道口径。"""
    loaded = _loaded(
        samples=(_row(price_source="synth", exchange_trade_id="qmt-synth-x"),),
        coverage={
            "n_trades": 1,
            "n_orphan": 0,
            "n_not_real": 0,
            "n_synth": 1,
            "n_missing_ref": 0,
            "n_missing_limit": 0,
            "n_unknown_path": 0,
            "n_orders": 1,
        },
    )
    assert "合成成交" in "\n".join(render(_report(loaded)))


# ── 样本不足的纪律 ─────────────────────────────────────────────────
def test_render_warns_below_min_sample_but_still_shows_the_numbers() -> None:
    """<30 时：数字照印（供参考），但必须带上「只展示不做结论」。"""
    rows = tuple(_row(order_id=f"o{i}", exchange_trade_id=f"T{i}") for i in range(3))
    assert 3 < MIN_SAMPLE
    text = "\n".join(render(_report(_loaded(samples=rows))))
    assert SMALL_SAMPLE_NOTE in text
    assert "bps" in text  # 展示归展示


def test_render_drops_the_small_sample_warning_at_the_threshold() -> None:
    rows = tuple(_row(order_id=f"o{i}", exchange_trade_id=f"T{i}") for i in range(MIN_SAMPLE))
    text = "\n".join(render(_report(_loaded(samples=rows))))
    assert SMALL_SAMPLE_NOTE not in text


# ── 已知边界 ───────────────────────────────────────────────────────
def test_render_always_prints_the_known_boundaries() -> None:
    """边界与样本量无关：空报告和有数报告都要印。"""
    for rep in (_report(_loaded()), _report(_loaded(samples=(_row(),)))):
        text = "\n".join(render(rep))
        assert "手续费" in text
        assert "轮询" in text
        assert "基准价" in text


def test_render_states_the_fee_gap_as_a_measured_fact() -> None:
    """手续费那条边界要带**实测计数**：本窗口有几笔带费用。

    写死一句「手续费不在账」是断言；带上「窗口内 N 笔成交、其中 M 笔有费用」
    才是读数——换了通道/补了字段之后，这句话会自己变准，而不是继续撒谎。
    """
    zero_fee = "\n".join(render(_report(_loaded(samples=(_row(),)))))
    assert "0/1" in zero_fee or "0 笔有费用" in zero_fee
    with_fee = "\n".join(render(_report(_loaded(samples=(_row(fees=12.34),)))))
    assert "1/1" in with_fee or "1 笔有费用" in with_fee


# ── build_report 的取数 ────────────────────────────────────────────
def test_build_report_merges_rows_and_carries_coverage() -> None:
    loaded = _loaded(
        samples=(
            _row(filled=100.0, fill_px=10.0),
            _row(filled=300.0, fill_px=10.4, exchange_trade_id="T2", fees=1.5),
            _row(order_id="o2", exchange_trade_id="T3", fees=0.5),
        ),
        zero_fill_orders=7,
        coverage={
            "n_trades": 3,
            "n_orphan": 0,
            "n_not_real": 0,
            "n_synth": 0,
            "n_missing_ref": 0,
            "n_missing_limit": 0,
            "n_unknown_path": 0,
            "n_orders": 2,
        },
    )
    rep = _report(loaded)
    assert rep["n_rows"] == 3          # 合并前（成交笔数）
    assert rep["n_orders"] == 2        # 合并后（委托笔数）
    assert rep["n_zero_fill"] == 7
    assert rep["coverage"]["n_trades"] == 3
    assert rep["n_fees"] == 2
    assert rep["fees_total"] == 2.0    # 1.5 + 0.5，手续费可加，合成后不丢
    merged = {r["order_id"]: r for r in rep["rows"]}
    assert merged["o1"]["filled"] == 400.0
    assert merged["o1"]["fill_px"] == 10.3  # 100@10.0 + 300@10.4 按量加权


def test_build_report_keeps_the_account_and_window_it_read() -> None:
    """报告要自证"这是哪座账户、哪段窗口"——换账户跑出来的数与别的混在一起就全废了。"""
    rep = _report(_loaded(samples=(_row(),)))
    assert rep["tenant_id"] == "default"
    assert rep["user_id"] == "10000001"
    assert rep["window"] == ["2026-09-23"]
    assert rep["days"] == DEFAULT_DAYS
    assert rep["generated"] == "2026-09-24 09:05:00"


def test_render_separates_the_read_window_from_the_data_range() -> None:
    """两个"窗口"必须分开叫：`days` 是取数范围，`window` 是**取到的成交**在哪几天。

    合成一句「窗口 09-10 ~ 09-11（近 30 天）」读者只会觉得自相矛盾；而真实情况
    （近 30 天里只有这两天有成交）恰恰是读数时要看见的事实——尤其是刚上线基准价
    补写的那几天，成交区间比取数窗口短得多是**预期**，不是故障。
    """
    text = "\n".join(render(_report(_loaded(samples=(_row(),)))))
    assert "取数 近 30 天" in text
    assert "成交区间 2026-09-23" in text
    assert "窗口 2026-09-23" not in text, "旧的含糊写法（把数据区间叫成窗口）回来了"

    # 空样本时 `window` 也是空的（取数侧按**取到的成交**的日期集合构造，
    # 见 exec_cost_source.load_samples 末段）——这里必须显式传空，不能吃夹具默认值：
    # 夹具的 `window=("2026-09-23",)` 是给"有样本"的用例准备的。
    empty = "\n".join(render(_report(_loaded(window=()))))
    assert "成交区间 无（窗口内没有成交）" in empty


# ── 落盘 ───────────────────────────────────────────────────────────
def test_write_report_lands_both_files_and_leaves_no_temp_behind(tmp_path: Path) -> None:
    rep = _report(_loaded(samples=(_row(),)))
    json_path, md_path = write_report(rep, tmp_path, stamp="2026-09-24")
    assert json_path.name == "2026-09-24_tca.json"
    assert md_path.name == "2026-09-24_tca.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["n_orders"] == rep["n_orders"]
    assert md_path.read_text(encoding="utf-8").strip() == "\n".join(render(rep)).strip()
    assert not list(tmp_path.glob("*.tmp"))


def test_write_report_overwrites_the_same_day_in_place(tmp_path: Path) -> None:
    """同日重跑覆盖同一份（报告是快照，不是流水）。"""
    first = _report(_loaded(samples=(_row(),)))
    second = _report(_loaded(samples=(_row(), _row(order_id="o2", exchange_trade_id="T2"))))
    write_report(first, tmp_path, stamp="2026-09-24")
    json_path, _ = write_report(second, tmp_path, stamp="2026-09-24")
    assert json.loads(json_path.read_text(encoding="utf-8"))["n_orders"] == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-09-24_tca.json",
        "2026-09-24_tca.md",
    ]


def test_reports_dir_honors_the_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QM_REPORTS_DIR", str(tmp_path))
    assert reports_dir() == tmp_path / "tca"


# ── main 的分派（注入假取数，不碰库）──────────────────────────────
_MANY = tuple(
    _row(order_id=f"o{i}", exchange_trade_id=f"T{i}") for i in range(MIN_SAMPLE)
)


def _patch_load(monkeypatch, samples) -> None:
    async def _fake_load(**_kw: object) -> LoadResult:
        return _loaded(samples=samples)

    monkeypatch.setattr("backend.scripts.tca_report.load_samples", _fake_load)


def test_main_prints_the_report_and_lands_both_files(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _patch_load(monkeypatch, _MANY)
    rc = main(["--days", "3", "--out", str(tmp_path)])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "执行损耗" in out
    assert "bps" in out
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 2
    assert all(f.endswith((".json", ".md")) for f in files)
    assert files[0][:-5] == files[1][:-3]  # 同一戳的 json/md 成对


def test_main_flags_a_thin_sample_with_the_attention_code(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    """样本不足是**注意**（cron 据此决定要不要推给人看），不是错误。"""
    _patch_load(monkeypatch, (_row(),))
    rc = main(["--days", "3", "--out", str(tmp_path)])
    assert rc == EXIT_ATTENTION
    assert SMALL_SAMPLE_NOTE in capsys.readouterr().out


def test_main_no_save_prints_only(monkeypatch, capsys, tmp_path: Path) -> None:
    _patch_load(monkeypatch, ())
    rc = main(["--no-save", "--out", str(tmp_path)])
    assert rc == EXIT_ATTENTION  # 无样本同样是"注意"
    assert "无样本" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_main_json_mode_emits_parseable_json(monkeypatch, capsys, tmp_path: Path) -> None:
    _patch_load(monkeypatch, _MANY)
    main(["--json", "--no-save", "--out", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_orders"] == MIN_SAMPLE
    assert payload["sample"]["n"] == MIN_SAMPLE


def test_main_push_sends_the_summary_line(monkeypatch, tmp_path: Path) -> None:
    """``--push`` 推的是 ``push_line`` 的摘要（推送面与终端面同一口径）。"""
    _patch_load(monkeypatch, _MANY)
    sent: list[dict[str, object]] = []

    async def _fake_publish(**kwargs: object) -> bool:
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "backend.shared.notification_publisher.publish_notification_async", _fake_publish
    )
    rc = main(["--push", "--no-save", "--out", str(tmp_path), "--user", "10000001"])
    assert rc == EXIT_OK
    assert len(sent) == 1
    assert sent[0]["user_id"] == "10000001"
    assert sent[0]["title"] == "执行损耗 TCA"
    # 摘要面与终端面同一口径：推送内容就是 push_line 的输出（`generated` 不参与渲染，
    # 故这里可以逐字比对，不需要放宽成"包含某几个词"）。
    expected = push_line(
        build_report(
            _loaded(samples=_MANY),
            days=DEFAULT_DAYS,
            tenant_id="default",
            user_id="10000001",
        )
    )
    assert sent[0]["content"] == expected


def test_main_push_failure_does_not_fail_the_run(monkeypatch, tmp_path: Path) -> None:
    """推送是旁路：通知中心挂了，报告仍然要出得来。"""
    _patch_load(monkeypatch, _MANY)

    async def _boom(**_kwargs: object) -> bool:
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(
        "backend.shared.notification_publisher.publish_notification_async", _boom
    )
    assert main(["--push", "--no-save", "--out", str(tmp_path)]) == EXIT_OK


def test_main_rejects_a_bad_day_count() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--days", "abc"])
    assert exc.value.code == 2


# ── 调度接线（P1.6 日读数）──────────────────────────────────────────
# 这一段守的是「读数面到底会不会每天自己跑一次」。报告本身算得再对，
# 调度断了也只是多了一个没人知道存在的 CLI——与隔壁「纪律写进字段但从不打印」
# 同源的失败：能力在，出口不在。
def test_beat_schedule_entry_points_at_the_registered_task() -> None:
    from backend.services.engine.qlib_app.celery_config import (
        TCA_REPORT_DAYS,
        beat_schedule,
    )
    from backend.services.engine.tasks.celery_tasks import tca_daily_report

    entry = beat_schedule["tca-daily-report"]  # KeyError = 接线断了，用例名即诊断
    assert entry["task"] == tca_daily_report.name, "beat 指的任务名与注册的任务名不一致"
    assert entry["kwargs"]["days"] == TCA_REPORT_DAYS
    schedule = entry["schedule"]
    # 16:40 = 收盘后且晚于 eval_scores(16:00)/advice_generator(16:20)；1-6 = 周日不跑。
    # 逐字段断言而不是比对 crontab 的 repr：repr 会随 celery 版本变，那时用例会
    # 因为"格式变了"而红，而不是因为"时间变了"——红灯指错地方和没有红灯一样坏。
    assert (schedule._orig_minute, schedule._orig_hour) == ("40", "16")
    assert schedule._orig_day_of_week == "1-6"


def test_beat_schedule_entry_is_switched_by_its_own_env(monkeypatch) -> None:
    """开关必须真的能关掉这条调度；否则 ``TCA_REPORT_ENABLED`` 只是文档里的装饰。

    顺带钉住**独立开关**这一点：借别的任务的开关会让注册表把「本就没开」
    报成「停摆」（见 celery_config 里 NEWS_TAG_ROLLUP_ENABLED 的同款注释）。

    这里用 ``runpy.run_path`` 在**独立命名空间**里重放一遍模块，而不是
    ``importlib.reload``：reload 会换掉模块级的 ``celery_app`` 对象，而
    ``celery_tasks`` 抓的是旧的那个——测试顺序一变，"谁持有哪个 app"就成了隐形的
    全局状态。``run_path`` 不碰 ``sys.modules``，验的是同一段代码，副作用为零。
    """
    import runpy

    path = (
        Path(__file__).resolve().parents[1]
        / "services/engine/qlib_app/celery_config.py"
    )
    monkeypatch.setenv("TCA_REPORT_ENABLED", "false")
    ns = runpy.run_path(str(path))
    assert "tca-daily-report" not in ns["beat_schedule"]

    monkeypatch.setenv("TCA_REPORT_ENABLED", "true")
    ns = runpy.run_path(str(path))
    assert "tca-daily-report" in ns["beat_schedule"]


def test_daily_task_lands_the_report_and_flags_a_thin_sample(
    monkeypatch, tmp_path: Path
) -> None:
    from backend.services.engine.tasks import celery_tasks

    landed: dict[str, object] = {}

    async def _fake_collect(**_kwargs: object) -> dict[str, object]:
        return _report(_loaded((_row(),)))

    def _fake_write(rep, out_dir, *, stamp):  # noqa: ANN001, ANN202
        landed["rep"] = rep
        landed["dir"] = out_dir
        landed["stamp"] = stamp
        return tmp_path / "x.json", tmp_path / "x.md"

    seen: list[str] = []
    monkeypatch.setattr(
        "backend.shared.scheduler_registry.heartbeat",
        lambda key, **kw: seen.append(key) or True,
    )
    monkeypatch.setattr("backend.scripts.tca_report.collect", _fake_collect)
    monkeypatch.setattr("backend.scripts.tca_report.write_report", _fake_write)

    result = celery_tasks.tca_daily_report(days=7, tenant_id="default")

    assert seen == ["tca_report"], "任务必须先给调度注册表写心跳（C07 的唯一信号）"
    assert result["status"] == "success"
    assert result["days"] == 7
    assert result["n_orders"] == 1
    assert landed["stamp"] == "2026-09-24", "文件名按产出日（同日重跑覆盖同一份）"
    # 1 笔 < 30：薄样本如实标记，但**不算失败**（"还没攒够"≠"算错了"）
    assert result["attention"] is True
    assert result["json"].endswith("x.json")


def test_daily_task_reports_failure_as_a_result_not_an_exception(monkeypatch) -> None:
    """取数炸了要给 celery 一个**可读的失败结果**，不是抛出去。

    抛出去的话 celery 只是把任务标红，而 beat 每天照跑——运维看到的是"有失败"
    而不是"TCA 读数面从某天起就没出过报告"。
    """
    from backend.services.engine.tasks import celery_tasks

    async def _boom(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(
        "backend.shared.scheduler_registry.heartbeat", lambda *a, **k: True
    )
    monkeypatch.setattr("backend.scripts.tca_report.collect", _boom)

    result = celery_tasks.tca_daily_report(days=3)
    assert result["status"] == "failed"
    assert "ledger unreachable" in result["error"]


def test_registry_entry_agrees_with_the_beat_schedule() -> None:
    """注册表与 beat 是同一件事的两处声明（C07 按前者判活，市场按后者触发）。

    ``switch_env`` 必须是 beat 真正读的那个变量名——写错了 C07 会拿一个没人看的
    环境变量去判断"任务开着没"，于是要么把在跑的任务报成关闭，要么把停摆报成正常。
    """
    from backend.shared.scheduler_registry import JOBS_BY_KEY

    spec = JOBS_BY_KEY["tca_report"]
    assert spec.kind == "celery_beat" and spec.owner == "celery"
    assert spec.switch_env == "TCA_REPORT_ENABLED"
    assert spec.switch_default_on is True
    assert spec.heartbeat_ttl == 345600, "周一至周六的调度，TTL 必须覆盖周末间隔"
    assert "tca_daily_report" not in (spec.rerun or ""), "重跑走控制台，别让人手敲 celery"
