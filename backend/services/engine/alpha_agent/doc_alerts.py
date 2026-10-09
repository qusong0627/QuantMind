"""文档挖掘配额告警（T-FM-14）—— 平台余量跌到预算 10% 以下时告警一次。

触发点只有**结算**：``DocParseService._settle_quota``。预留在 reserve 时是
保守的占位（数不出页数的按单文件上限压），用量真正落地在 settle；失败路径
release 全额退回后条件可能不再成立，所以不在预留/退回那两处判。

三条纪律：

1. **每天最多一次**：``DocQuota.try_lock``（SET NX EX）按配额日去重，一个
   平台日一条告警，不随每次结算刷屏。
2. **绝不弄断主链**：告警是锦上添花的旁路——取锁/发布任何失败都只记日志，
   对调用方**永远不抛**（解析主链照常走完）。
3. **受众是管理员**：走 notification_publisher 的管理员 fanout（与哨兵/体检
   告警同一条链路，QQ 旁路随之生效）。通知类型声明 ``system``——前端 WS
   白名单对未知类型会静默降级成 system，直接声明 system 更诚实。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from backend.services.engine.alpha_agent.doc_quota import (
    DocQuota,
    QuotaStatus,
    get_doc_quota,
)

logger = logging.getLogger(__name__)

#: 去重锁 TTL：一个配额日（北京时间日界）内只告警一次；48h 覆盖跨日留余
ALERT_LOCK_TTL_S = 48 * 3600

TITLE = "MinerU 解析配额告急"

#: 告警通知的类型/级别（类型选择理由见模块 docstring 第 3 条）
ALERT_NOTIFICATION_TYPE = "system"
ALERT_NOTIFICATION_LEVEL = "warning"


def _lock_name(day: str) -> str:
    return f"doc_quota_alert:{day}"


def _default_publisher() -> Callable[..., tuple[int, int]]:
    # 惰性 import：通知链路拖 sync_db/psycopg，模块导入期不背这个包袱
    from backend.shared.notification_publisher import publish_notification_to_admins

    return publish_notification_to_admins


def _content(status: QuotaStatus) -> str:
    return (
        f"平台今日 MinerU 解析配额已用 {status.platform_used}/"
        f"{status.platform_budget} 页，仅剩 {status.platform_remaining} 页"
        "（不足 10%）。继续上传的文档可能被配额闸拦下。请申请 MinerU 提额，"
        "或调整 MINERU_DAILY_PAGE_BUDGET；额度恢复前可暂时关闭文档挖掘"
        "（ENABLE_DOC_MINING=false）止损。"
    )


def maybe_alert_quota_low(
    status: QuotaStatus | None,
    *,
    quota: DocQuota | None = None,
    publisher: Callable[..., tuple[int, int]] | None = None,
) -> bool:
    """余量告急则给管理员发一次通知；返回「本次是否真的发出」。

    非告急（``status.warning`` 为假）或今天已告警过 → False（不发）。
    任何异常都只记日志并返回 False，绝不向调用方抛出。
    """
    if status is None or not status.warning:
        return False
    quota = quota if quota is not None else get_doc_quota()
    name = _lock_name(status.day)
    try:
        acquired = quota.try_lock(name, ttl_s=ALERT_LOCK_TTL_S)
    except Exception as exc:  # noqa: BLE001 —— 去重锁故障宁可漏发，不刷屏
        logger.warning("配额告警去重锁失败（本次跳过）: %s", exc)
        return False
    if not acquired:
        return False
    try:
        send = publisher or _default_publisher()
        sent, admins = send(
            title=TITLE,
            content=_content(status),
            type=ALERT_NOTIFICATION_TYPE,
            level=ALERT_NOTIFICATION_LEVEL,
        )
    except Exception as exc:  # noqa: BLE001 —— 发送失败释放锁，下个结算点重试
        logger.warning("配额告警发送失败（已释放去重锁，稍后重试）: %s", exc)
        try:
            quota.unlock(name)
        except Exception:  # noqa: BLE001 —— 释放失败等 TTL 自愈
            pass
        return False
    logger.warning("MinerU 配额告警已发出：%s（%d/%d 管理员收到）", name, sent, admins)
    return True
