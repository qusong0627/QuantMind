"""持仓预警契约：表 DDL + 自愈建表 + 阈值/冷却/dedupe 口径（纯函数，可单测）。

**只管一件事：什么时候该吵醒用户。** 判定规则全部收在这里，是因为「分数从 +0.02
掉到 -0.01 要不要提醒」这类判断一旦分散在扫描器、路由、前端三处，现场就会出现
「面板里有这条预警，但没响」——用户不会去查为什么，只会不再相信提醒。

口径（用户已确认）：

- **由正转负**（``prev > 0 and now <= 0``）→ ``critical``。这是「该走了」的信号。
- **跌破自定阈值**（``threshold > 0 and prev >= threshold > now``）→ ``warning``。
  阈值 0 表示不启用该规则（与「由正转负」重合，不重复报）。
- **没有基线不报警**（``prev is None``）——首次见到一只票时它是负分不是「跌了」，
  否则开启哨兵当天所有持仓一起报警（假警报会把真警报淹掉）。
- **基线过期不比较**（隔了 ``MAX_BASELINE_GAP_DAYS`` 天以上）——哨兵停摆一周后
  一次性报几十条「今天跌了」，其实那跌是上周的事，用户按它做决策就是被误导。
- **冷却**：同 (user, symbol, kind) 在 ``COOLDOWN_SECONDS`` 内只报一次，
  冷却桶进 ``dedupe_key``，靠表上唯一索引兜底（并发扫描也不会重复投递）。

表 ``qm_holding_alerts`` 是**留痕 + 状态机**（active/dismissed/executed/expired），
站内通知走既有 `notifications` 管道（type=``holding_alert``）。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "qm_holding_alerts"

#: 预警类型
KIND_SCORE_CROSS_ZERO = "score_cross_zero"
KIND_SCORE_BELOW_THRESHOLD = "score_below_threshold"
KIND_RISK_NEWS = "risk_news"
KIND_RISK_ANOMALY = "risk_anomaly"
KIND_RISK_LIST = "risk_list"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITY_ORDER = {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}

STATUS_ACTIVE = "active"
STATUS_DISMISSED = "dismissed"
STATUS_EXECUTED = "executed"
STATUS_EXPIRED = "expired"
_STATUSES = frozenset(
    {STATUS_ACTIVE, STATUS_DISMISSED, STATUS_EXECUTED, STATUS_EXPIRED}
)

#: 同 (user, symbol, kind) 的提醒冷却（秒）。盘中实时分每 ~1.7min 一圈，
#: 没有冷却会有几十条同一只票的预警；太长又会漏掉「跌下去又跌一层」。
COOLDOWN_SECONDS = 1800

#: 基线最大可比较间隔（天）：超过则视为过期基线，只重新播种不报警。
MAX_BASELINE_GAP_DAYS = 5

#: 单次扫描每个用户的报警上限（防「全市场异动 + 大持仓」一轮刷屏）
MAX_ALERTS_PER_SCAN = 20

#: 用户配置（Redis hash ``qm:holding:sentinel:config:{tenant}:{user}`` 的 ``settings`` 字段）
CONFIG_KEY_PREFIX = "qm:holding:alert:config:"
#: 分数基线（Redis hash，字段=prefix 代码）
BASELINE_KEY_PREFIX = "qm:holding:alert:baseline:"
#: 风险/新闻水位（Redis hash：sentinel_alerts 游标、新闻标签水位、名单快照指纹）
RISK_CURSOR_KEY_PREFIX = "qm:holding:alert:riskcursor:"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    # 跌破该值告警；0 = 关闭该规则（由正转负仍然报）
    "score_threshold": 0.0,
    # 监控范围：默认「持仓 + 手工自选」，候选不监控（几百只票全监控等于没有监控）
    "watch_sim": True,
    "watch_real": True,
    "watch_manual": True,
    # 三通道（站内由后端投递；桌面/声音由前端按这些开关决定播不播）
    "notify_inapp": True,
    "notify_desktop": True,
    "notify_sound": True,
    # 低于该级别只留痕不推送
    "min_severity": SEVERITY_WARNING,
}

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id        VARCHAR(64) NOT NULL,
    symbol         VARCHAR(32) NOT NULL,
    stock_name     VARCHAR(64),
    kind           VARCHAR(32) NOT NULL,
    severity       VARCHAR(16) NOT NULL,
    source         VARCHAR(64) NOT NULL DEFAULT 'holding_sentinel',
    title          VARCHAR(256) NOT NULL,
    content        TEXT NOT NULL DEFAULT '',
    detail         JSONB NOT NULL DEFAULT '{{}}',
    score_prev     DOUBLE PRECISION,
    score_now      DOUBLE PRECISION,
    score_as_of    DATE,
    dedupe_key     VARCHAR(64) NOT NULL,
    status         VARCHAR(16) NOT NULL DEFAULT 'active',
    notified       BOOLEAN NOT NULL DEFAULT FALSE,
    action_url     VARCHAR(512),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_holding_alerts_dedupe ON {TABLE} (dedupe_key);
CREATE INDEX IF NOT EXISTS idx_holding_alerts_user ON {TABLE} (tenant_id, user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_holding_alerts_symbol ON {TABLE} (tenant_id, user_id, symbol, created_at DESC);
"""


def ensure_holding_alerts_table() -> bool:
    """幂等建表（存在即零 DDL 快路径；失败仅告警不抛出）。"""
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    try:
        with sync_session() as session:
            exists = session.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}
            ).scalar()
        if exists is not None:
            return True
        with sync_session() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            for statement in _CREATE_SQL.strip().split(";\n"):
                if statement.strip():
                    session.execute(text(statement))
            session.commit()
        logger.info("[HoldingAlertContract] %s 表已创建", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[HoldingAlertContract] 自愈建表失败（不阻断）: %s", exc)
        return False


async def ensure_holding_alerts_table_async() -> bool:
    """trade 服务启动期自愈（与 :func:`ensure_holding_alerts_table` 等价）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            exists = (
                await session.execute(
                    _text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}
                )
            ).scalar()
        if exists is not None:
            return True
        async with get_session() as session:
            await session.execute(_text("SET LOCAL lock_timeout = '3s'"))
            for statement in _CREATE_SQL.strip().split(";\n"):
                if statement.strip():
                    await session.execute(_text(statement))
            await session.commit()
        logger.info("[HoldingAlertContract] %s 表已创建（async）", TABLE)
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断启动
        logger.warning("[HoldingAlertContract] 自愈建表失败（不阻断）: %s", exc)
        return False


# ── 用户配置 ─────────────────────────────────────────────────────────


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def parse_alert_config(raw: Mapping[str, Any] | str | None) -> dict[str, Any]:
    """用户配置 → 规整后的 dict（缺键用默认值，坏值退回默认值而不是抛）。

    前端 PUT 的是人改过的一部分字段；Redis 里存的可能是半年前写的老结构。
    任何一处不认得的取值都不能让哨兵停摆——退回默认值并在面板上如实显示，
    比「配置坏了所以不提醒」可接受得多（用户至少能看到开关是关的）。
    """
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            logger.warning("[HoldingAlertContract] 配置 JSON 解析失败，用默认值")
            raw = {}
    src: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

    out: dict[str, Any] = {}
    for key, default in DEFAULT_CONFIG.items():
        if key == "score_threshold":
            try:
                value = float(src.get(key, default))
            except (TypeError, ValueError):
                value = float(default)
            # 阈值语义是「跌到 0 以下多深算事」，负值没有意义；上限 1 覆盖满仓分
            out[key] = min(max(value, 0.0), 1.0)
            continue
        if key == "min_severity":
            text = str(src.get(key, default) or "").strip().lower()
            out[key] = text if text in SEVERITY_ORDER else str(default)
            continue
        out[key] = _coerce_bool(src.get(key, default), bool(default))
    return out


def meets_min_severity(severity: str, min_severity: str) -> bool:
    """严重度是否达到推送门槛（只留痕的不推）。"""
    return SEVERITY_ORDER.get(str(severity), 0) >= SEVERITY_ORDER.get(
        str(min_severity), 0
    )


def notification_level(severity: str) -> str:
    """预警严重度 → 站内通知 level（notifications 表只认 info/warning/error/success）。"""
    sev = str(severity or "").lower()
    if sev == SEVERITY_CRITICAL:
        return "error"
    if sev == SEVERITY_WARNING:
        return "warning"
    return "info"


# ── 判定（纯函数）────────────────────────────────────────────────────


def baseline_is_comparable(
    baseline_as_of: str | None,
    current_as_of: str | None,
    max_gap_days: int = MAX_BASELINE_GAP_DAYS,
) -> bool:
    """基线是否还值得比（隔太久 → 重新播种，不报「今天跌了」）。"""
    a = str(baseline_as_of or "")[:10]
    b = str(current_as_of or "")[:10]
    if not a or not b:
        return False
    from datetime import date

    try:
        da = date.fromisoformat(a)
        db = date.fromisoformat(b)
    except ValueError:
        return False
    return abs((db - da).days) <= int(max_gap_days)


def evaluate_score_transition(
    prev: float | None, now: float | None, threshold: float = 0.0
) -> tuple[str, str] | None:
    """分数迁移 → ``(kind, severity)``；不该报返回 ``None``。

    规则见模块 docstring。任一侧缺分返回 None——「没有分数」不是「分数变差了」，
    拿它报警等于在数据缺口上编故事。
    """
    if prev is None or now is None:
        return None
    prev_f = float(prev)
    now_f = float(now)
    if prev_f > 0 and now_f <= 0:
        return KIND_SCORE_CROSS_ZERO, SEVERITY_CRITICAL
    threshold_f = float(threshold or 0.0)
    if threshold_f > 0 and prev_f >= threshold_f > now_f:
        return KIND_SCORE_BELOW_THRESHOLD, SEVERITY_WARNING
    return None


def cooldown_bucket(now_ts: float, cooldown_seconds: int = COOLDOWN_SECONDS) -> int:
    """冷却桶编号：同一桶内的重复命中视为同一条预警。"""
    window = max(1, int(cooldown_seconds))
    return int(float(now_ts) // window)


def make_holding_dedupe_key(
    *,
    tenant_id: str,
    user_id: str,
    symbol: str,
    kind: str,
    bucket: int,
) -> str:
    """幂等键（含冷却桶）：唯一索引兜底，并发扫描也不会重复投递。"""
    raw = f"{tenant_id}|{user_id}|{symbol}|{kind}|{int(bucket)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def alert_action_url(symbol: str | None = None) -> str:
    """站内通知点击落点（交易台 → 持仓监控）。

    ``tab=position`` 必须与前端页签 id 逐字一致，且该 id 在前端深链白名单里
    （``pages/trading/utils/activeTab.ts`` 的 ``DEEP_LINKABLE``）。写成复数
    ``positions`` 时前端只会静默回落「系统健康」——点了不报错、也到不了持仓页。
    """
    base = "/trading?tab=position"
    sym = str(symbol or "").strip()
    return f"{base}&symbol={sym}" if sym else base


def build_alert_title(
    kind: str, stock_name: str | None, symbol: str, *, freq: str = "daily"
) -> str:
    """预警标题（人读的，一眼能看出是什么事）。"""
    name = str(stock_name or "").strip() or symbol
    tag = "（日频分）" if str(freq) != "realtime" else ""
    if kind == KIND_SCORE_CROSS_ZERO:
        return f"{name} 分数由正转负{tag}"
    if kind == KIND_SCORE_BELOW_THRESHOLD:
        return f"{name} 分数跌破阈值{tag}"
    if kind == KIND_RISK_NEWS:
        return f"{name} 出现重大利空"
    if kind == KIND_RISK_ANOMALY:
        return f"{name} 盘中异动"
    if kind == KIND_RISK_LIST:
        return f"{name} 进入排除名单"
    return f"{name} 持仓预警"


def format_score(value: float | None) -> str:
    return "—" if value is None else f"{float(value):+.3f}"


def build_alert_content(
    *,
    kind: str,
    symbol: str,
    score_prev: float | None = None,
    score_now: float | None = None,
    extra: str = "",
) -> str:
    """预警正文（把数字原样写出来，用户自己判断，不做措辞安抚）。"""
    parts: list[str] = []
    if kind in {KIND_SCORE_CROSS_ZERO, KIND_SCORE_BELOW_THRESHOLD}:
        parts.append(
            f"{symbol} 信号分 {format_score(score_prev)} → {format_score(score_now)}"
        )
    else:
        parts.append(str(symbol))
    if extra:
        parts.append(str(extra).strip())
    parts.append("可在此处一键卖出（需手动确认）。")
    return " ".join(p for p in parts if p)


def normalize_status(status: Any) -> str:
    text = str(status or "").strip().lower()
    return text if text in _STATUSES else STATUS_ACTIVE


def dedupe_alert_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一轮扫描内按 (symbol, kind) 去重，保留更严重的那条。"""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("symbol")), str(row.get("kind")))
        prev = best.get(key)
        if prev is None or SEVERITY_ORDER.get(
            str(row.get("severity")), 0
        ) > SEVERITY_ORDER.get(str(prev.get("severity")), 0):
            best[key] = row
    return list(best.values())
