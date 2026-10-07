"""qm_market_anomalies 落表的 subject 归类（生产缺陷回归：模型级告警整行丢失）。

**缺陷（2026-10-08 实测）**：`_record_sync` 原判据是「非 account_ 就把 subject 写进
`instrument`」，而 `instrument` 是 **varchar(16)** 的证券代码列（与
`qm_sector_constituents.instrument` 同义）。模型类 subject 是**模型 id**——实测
/app/models 里 8 个模型目录名 60~68 字符——每次模型 IC 告警 INSERT 都被 PG 以
`StringDataRightTruncation` 拒绝，**整行丢失**；上游 `build_once` 逐条 try/except
兜住，只留一条 WARNING（每小时两条，自引擎上线起）。后果面：
表里 2829 行**全是 price_surge**、0 条 model_ic_drop，而同期 `risk_events` 审计
（symbol 列 varchar(32)，`subject[:32]` 截断后装得下）**有 979 行**——
即「审计在、台账空」，不是没检测到，是没落上表。

**口径**：`instrument` 只承载证券代码（market/data 族）；account/model 族写 NULL，
完整 subject 落 `details.subject`（对 account 族也是修复：原先只写 NULL，user_id
仅存在于 title/description 文本里）。判据用**白名单**而非黑名单——列宽 16 是硬约束，
新 kind 默认不写，宁可少一列也不静默丢整行。
"""

from __future__ import annotations

import json

import pytest

from backend.services.engine.anomaly_detectors import Detection
from backend.services.engine.anomaly_engine import AnomalyEngine

pytestmark = pytest.mark.unit

# 生产实测最长（/app/models/**/pred.parquet 父目录名 60~68 字符），取 68 那条
MODEL_ID = "mdl_us_train_20260912134006_566f6da2_8d0709f2_random_forest_4555743f"
SYMBOL = "600036.SH"
USER_ID = "88001234"


def _patch_sync_session(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """把 sync_session 换成捕获器，返回**真实下发的绑定参数列表**（不碰库）。"""
    from backend.shared import sync_db

    sink: list[dict] = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _stmt, params):
            sink.append(params)

        def commit(self):
            pass

    monkeypatch.setattr(sync_db, "sync_session", lambda: _Session())
    return sink


def _capture_record(monkeypatch: pytest.MonkeyPatch, detection: Detection) -> dict:
    sink = _patch_sync_session(monkeypatch)
    AnomalyEngine()._record_sync(detection)
    assert len(sink) == 1, "必须恰好下发一条 INSERT"
    return sink[0]


def _detection(kind: str, subject: str) -> Detection:
    # targets 与真实检测器同形（各 detector 都写 targets=(subject,)）——这样测的是
    # **引擎按族裁剪**，而不是测一个本来就没 target 的空元组
    return Detection(
        kind=kind,
        subject=subject,
        severity="warn",
        title=f"{kind} {subject}",
        targets=(subject,) if subject else (),
    )


# ── 核心回归：模型 id 不得进 instrument ─────────────────────────────


def test_model_subject_never_goes_into_instrument(monkeypatch):
    """68 字符模型 id 写 instrument 必被 varchar(16) 拒 ⇒ 只能为 NULL。"""
    params = _capture_record(monkeypatch, _detection("model_ic_drop", MODEL_ID))

    assert params["ins"] is None
    assert len(MODEL_ID) > 16, "夹具必须长于列宽，否则这条测试是假的"
    assert json.loads(params["details"])["subject"] == MODEL_ID, "完整模型 id 必须落 details"


def test_account_subject_stays_out_of_instrument(monkeypatch):
    """账户族保持 NULL（原行为），且 user_id 现在能机器可读地取回。"""
    params = _capture_record(monkeypatch, _detection("account_cancel_ratio", USER_ID))

    assert params["ins"] is None
    assert json.loads(params["details"])["subject"] == USER_ID


# ── 证券代码族：instrument 语义不许被本次改动动摇 ───────────────────


@pytest.mark.parametrize(
    "kind",
    [
        "price_surge",
        "price_limit_up",
        "price_limit_down",
        "volume_surge",
        "data_jump",
        "data_gap",
        "data_zero_volume",
    ],
)
def test_symbol_subject_still_lands_in_instrument(monkeypatch, kind):
    params = _capture_record(monkeypatch, _detection(kind, SYMBOL))

    assert params["ins"] == SYMBOL
    assert len(SYMBOL) <= 16, "证券代码必须落在列宽内（后缀式最长 9）"
    assert json.loads(params["details"])["subject"] == SYMBOL


# ── 结构性守卫：新 kind 默认 fail-safe ──────────────────────────────


def test_unknown_kind_defaults_to_no_instrument(monkeypatch):
    """白名单外的未来 kind：默认不写 instrument（少一列 ≠ 丢整行）。"""
    params = _capture_record(monkeypatch, _detection("strategy_drift", "s" * 64))

    assert params["ins"] is None
    assert json.loads(params["details"])["subject"] == "s" * 64


# ── 同族第二个落点：intel 总线的 targets（MAX_TARGET_LEN=24）────────


def _capture_publish(monkeypatch: pytest.MonkeyPatch, detection: Detection) -> dict:
    """跑一次 _default_publish，返回**真实下发的总线事件**（不碰 Redis）。"""
    from backend.services.engine import anomaly_engine as ae
    from backend.shared import intel_events

    sent: list[dict] = []

    class _Client:
        def close(self):
            pass

    monkeypatch.setattr(ae, "_main_redis", lambda: _Client())
    monkeypatch.setattr(
        intel_events, "publish_event", lambda _c, event, **_kw: (sent.append(event), "1-1")[1]
    )
    ae.AnomalyEngine()._default_publish(detection)
    assert len(sent) == 1
    return sent[0]


def test_model_alert_passes_the_bus_contract(monkeypatch):
    """68 字符模型 id 当 target 会被总线拒（MAX_TARGET_LEN=24）⇒ 必须走 payload。

    旧实现 model_ic_drop 自引擎上线起一条都没上过总线：publish_event 抛
    IntelEventError 被 build_once 兜成一条 WARNING。这条用**真契约校验器**验。
    """
    from backend.shared.intel_events import IntelEventError, validate_event

    event = _capture_publish(monkeypatch, _detection("model_ic_drop", MODEL_ID))

    normalized = validate_event(event)  # 不抛 = 真能上总线
    assert normalized["targets"] == []
    assert normalized["payload"]["subject"] == MODEL_ID
    # 反事实：旧形状（模型 id 进 targets）必须仍然被契约拒——证明修复点是对的
    with pytest.raises(IntelEventError, match="target 超长"):
        validate_event({**event, "targets": [MODEL_ID]})


def test_symbol_alert_keeps_symbol_target(monkeypatch):
    """证券级告警仍按 symbol 路由（不动摇既有消费端行为）。"""
    from backend.shared.intel_events import validate_event

    event = _capture_publish(monkeypatch, _detection("price_surge", SYMBOL))

    assert validate_event(event)["targets"] == [SYMBOL]


def test_account_alert_targets_empty_and_subject_in_payload(monkeypatch):
    """账户族同理：user_id 不是证券代码——targets 空、subject 进 payload。"""
    from backend.shared.intel_events import validate_event

    event = _capture_publish(monkeypatch, _detection("account_concentration", USER_ID))

    normalized = validate_event(event)
    assert normalized["targets"] == []
    assert normalized["payload"]["subject"] == USER_ID


# ── 同族第三个落点：否决路径（拿 model_id 去扫模拟账户）────────────────


def test_deny_for_model_kind_skips_holder_scan(monkeypatch):
    """模型族 deny：不扫持有人、不写锁；审计 message 带完整模型 id。

    旧判据「非账户即标的」会把 68 字符模型 id 丢进 `_symbol_holders` 全量扫
    `simulation:account:*`，扫完必然空手（审计列 symbol 还会把 id 截到 32）。
    """
    from backend.services.engine import anomaly_engine as ae
    from backend.services.trade_shared import redis_client as trade_redis_mod

    monkeypatch.setattr(trade_redis_mod.redis_client, "client", object())  # 不走 connect
    sink = _patch_sync_session(monkeypatch)
    engine = ae.AnomalyEngine()
    scanned: list[str] = []
    monkeypatch.setattr(engine, "_symbol_holders", lambda s: scanned.append(s) or [])

    result = engine._default_deny(_detection("model_ic_drop", MODEL_ID))

    assert result["locked"] == []
    assert scanned == [], "模型 id 不是证券代码，不得拿去扫持有人"
    assert len(sink) == 1 and MODEL_ID in sink[0]["msg"], "审计要能无损认出模型"


def test_every_engine_kind_is_classified():
    """引擎能产出的每个 kind 必须显式归类——新增 kind 不许靠默认值碰运气。

    白名单（symbol 族）∪ 非 symbol 族前缀（account_/model_）必须恰好等于
    anomaly_detectors 里的 KIND_* 全量；少一个就红（提示补归类）。
    """
    from backend.services.engine import anomaly_detectors as det
    from backend.services.engine import anomaly_engine as ae

    kinds = {v for k, v in vars(det).items() if k.startswith("KIND_")}
    classified = set(ae.SYMBOL_SUBJECT_KINDS) | {
        k for k in kinds if k.startswith(ae.NON_SYMBOL_KIND_PREFIXES)
    }
    assert kinds == classified, f"未归类的 kind: {sorted(kinds - classified)}"
