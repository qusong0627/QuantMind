from fastapi import APIRouter
from config.settings import settings
from backend.shared.programmatic_trading_disclosure import (
    HFT_ORDERS_PER_DAY,
    HFT_ORDERS_PER_MINUTE,
    HFT_ORDERS_PER_SECOND,
    SOFTWARE_DEVELOPER,
    SOFTWARE_NAME,
    disclosure_lines,
    disclosure_text,
    software_version,
)
from backend.shared.version import get_version_info, check_updates

router = APIRouter(prefix="/api/v1/system", tags=["System"])


@router.get("/version")
async def system_version(force: bool = False):
    """当前运行代码版本与上游更新检查。

    - version/commit/branch：由 deploy/update.sh 写入 version.json（build 时拷入镜像）。
    - update：可选地调用上游平台（默认 gitee）compare API 算出本部署落后提交数。
      容器无外网或未走 update.sh 时省略；force=true 可绕过缓存强制刷新。
      更新检查属增强能力，任何异常都不应影响版本读取接口。
    """
    info = get_version_info()
    try:
        update = await check_updates(force=force)
    except Exception:
        update = None
    return {
        "version": info["version"],
        "edition": settings.edition,
        "commit": info["commit"],
        "branch": info["branch"],
        "update": update,
    }


@router.get("/programmatic-trading-disclosure")
async def programmatic_trading_disclosure():
    """程序化交易报告义务的待填报信息（软件名称/版本号/开发者）。

    只**提供**信息，不代为报告、不做合规判定。用户按券商与交易所要求自行填报；
    口径与阈值出处见 `backend/shared/programmatic_trading_disclosure.py`。
    """
    return {
        "software_name": SOFTWARE_NAME,
        "version": software_version(),
        "developer": SOFTWARE_DEVELOPER,
        "lines": disclosure_lines(),
        "text": disclosure_text(),
        "high_frequency": {
            "orders_per_second": HFT_ORDERS_PER_SECOND,
            "orders_per_minute": HFT_ORDERS_PER_MINUTE,
            "orders_per_day": HFT_ORDERS_PER_DAY,
            "note": (
                "单账户每秒申报+撤单达 300 笔以上、或单日达 20000 笔以上，"
                "可能被认定为高频交易，须额外报告系统服务器所在地、系统测试报告"
                "与故障应急方案。本工具不代为报告。"
            ),
        },
    }


@router.get("/capabilities")
async def get_capabilities():
    """获取当前版本的系统能力与开关"""
    return settings.capabilities
