"""MCP 服务与后台管理共用的工具目录 / 权限码（唯一事实源）。

为什么放在 `backend/shared`：
- MCP 服务（独立容器）与后台管理 API 都要知道「有哪些工具、每个工具要什么权限、
  哪些是写操作」；
- 两边各抄一份，第一周就会漂移：管理页开了开关、服务里没这个工具，或者反过来。

MCP 镜像构建时会把本文件复制进镜像（`docker/Dockerfile.mcp`），不复制整个 backend。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 权限码
# ---------------------------------------------------------------------------

MCP_USE = "mcp.use"
MCP_DATA_READ = "mcp.data.read"
MCP_CONTROL_READ = "mcp.control.read"
MCP_TASK_SUBMIT = "mcp.task.submit"
MCP_TASK_CANCEL = "mcp.task.cancel"
MCP_INFERENCE_READ = "mcp.inference.read"
MCP_MODEL_WRITE = "mcp.model.write"
MCP_STRATEGY_WRITE = "mcp.strategy.write"

#: 模拟盘沿用对外 API 已有的两个权限码（现网凭据已带，零迁移）。
TRADE_READ = "trade.read"
TRADE_WRITE = "trade.write"

MCP_PERMISSIONS: tuple[str, ...] = (
    MCP_USE,
    MCP_DATA_READ,
    MCP_CONTROL_READ,
    MCP_TASK_SUBMIT,
    MCP_TASK_CANCEL,
    MCP_INFERENCE_READ,
    MCP_MODEL_WRITE,
    MCP_STRATEGY_WRITE,
)

ALL_API_PERMISSIONS: frozenset[str] = frozenset(
    {*MCP_PERMISSIONS, TRADE_READ, TRADE_WRITE}
)

PERMISSION_LABELS: dict[str, str] = {
    MCP_USE: "连接 MCP",
    MCP_DATA_READ: "读取数据",
    MCP_CONTROL_READ: "读取策略/模型清单",
    MCP_TASK_SUBMIT: "提交任务",
    MCP_TASK_CANCEL: "取消任务",
    MCP_INFERENCE_READ: "读取推理排名",
    MCP_MODEL_WRITE: "模型写操作",
    MCP_STRATEGY_WRITE: "策略写操作",
    TRADE_READ: "模拟盘读取",
    TRADE_WRITE: "模拟盘下单/撤单",
}

#: 管理员签发 key 时用的模板。没有实盘模板——实盘不接 MCP。
PERMISSION_TEMPLATES: dict[str, tuple[str, ...]] = {
    "readonly": (MCP_USE, MCP_DATA_READ, MCP_CONTROL_READ),
    "research": (
        MCP_USE,
        MCP_DATA_READ,
        MCP_CONTROL_READ,
        MCP_TASK_SUBMIT,
        MCP_INFERENCE_READ,
    ),
    "sim_trading": (
        MCP_USE,
        MCP_DATA_READ,
        MCP_CONTROL_READ,
        MCP_TASK_SUBMIT,
        MCP_INFERENCE_READ,
        TRADE_READ,
        TRADE_WRITE,
    ),
}


# ---------------------------------------------------------------------------
# 工具目录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission: str
    category: str
    version: str  # "v1" / "v2"
    write: bool = False
    #: 写工具默认关；只读工具默认开。任务提交虽是写但属于研究基线，默认开。
    default_enabled: bool = True
    destructive: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


TOOLS: tuple[ToolSpec, ...] = (
    # ── v1 基础 ─────────────────────────────────────────────────────────
    ToolSpec("qm_capabilities", "部署能力、实盘开关、当前凭据与可用工具", MCP_USE, "meta", "v1"),
    ToolSpec("qm_list_datasets", "数据集目录（kind/market/as_of/freshness）", MCP_DATA_READ, "data", "v1"),
    ToolSpec("qm_get_dataset_schema", "某数据集的列名与类型", MCP_DATA_READ, "data", "v1"),
    ToolSpec("qm_list_partitions", "某数据集的分区清单与 etag", MCP_DATA_READ, "data", "v1"),
    ToolSpec("qm_query_dataset", "有界查询：标的/日期/列/限量，返回 JSON 行", MCP_DATA_READ, "data", "v1"),
    ToolSpec("qm_get_dataset_file", "取某分区文件下载地址与 etag（不返回文件体）", MCP_DATA_READ, "data", "v1"),
    ToolSpec("qm_list_strategies", "当前用户的策略清单（只读）", MCP_CONTROL_READ, "control", "v1"),
    ToolSpec("qm_list_models", "当前用户的模型清单（只读）", MCP_CONTROL_READ, "control", "v1"),
    ToolSpec("qm_submit_task", "提交训练/回测/因子演化/数据同步任务", MCP_TASK_SUBMIT, "task", "v1", write=True),
    ToolSpec("qm_get_task", "轮询任务状态", MCP_TASK_SUBMIT, "task", "v1"),
    ToolSpec("qm_sim_account", "模拟账户与持仓", TRADE_READ, "trading", "v1"),
    ToolSpec("qm_sim_orders", "模拟委托列表", TRADE_READ, "trading", "v1"),
    ToolSpec("qm_sim_order", "单笔模拟委托", TRADE_READ, "trading", "v1"),
    ToolSpec("qm_sim_trades", "模拟成交列表", TRADE_READ, "trading", "v1"),
    ToolSpec("qm_sim_preview_order", "模拟下单预估（金额/上限，不发单）", TRADE_WRITE, "trading", "v1"),
    ToolSpec(
        "qm_sim_place_order",
        "模拟下单（必须带 client_order_id 幂等键）",
        TRADE_WRITE,
        "trading",
        "v1",
        write=True,
        default_enabled=False,
        destructive=True,
    ),
    ToolSpec(
        "qm_sim_cancel_order",
        "模拟撤单",
        TRADE_WRITE,
        "trading",
        "v1",
        write=True,
        default_enabled=False,
        destructive=True,
    ),
    # ── v2 读 ───────────────────────────────────────────────────────────
    ToolSpec("qm_get_inference_rankings", "推理排名（模型/日期/TopN，分页）", MCP_INFERENCE_READ, "inference", "v2"),
    ToolSpec("qm_get_stock_score", "单票在某模型/日期的评分与截面位置", MCP_INFERENCE_READ, "inference", "v2"),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}

DEFAULT_TOOL_FLAGS: dict[str, bool] = {
    tool.name: bool(tool.default_enabled) for tool in TOOLS
}

#: 全局默认配额。管理员可在后台覆盖。
DEFAULT_LIMITS: dict[str, Any] = {
    "calls_per_minute": 60,
    "task_submits_per_day": 20,
    "sim_orders_per_day": 20,
    "sim_order_max_value": 50_000.0,
    "query_max_rows": 5_000,
    "audit_retention_days": 90,
}

LIMIT_SPECS: dict[str, tuple[float, float, str]] = {
    # key: (min, max, label)
    "calls_per_minute": (1, 10_000, "每分钟工具调用上限"),
    "task_submits_per_day": (0, 1_000, "每日任务提交上限"),
    "sim_orders_per_day": (0, 1_000, "每日模拟下单上限"),
    "sim_order_max_value": (0, 100_000_000, "单笔模拟下单金额上限（元）"),
    "query_max_rows": (1, 20_000, "单次查询行数上限"),
    "audit_retention_days": (7, 730, "审计保留天数"),
}


def tool_catalog_payload() -> list[dict[str, Any]]:
    """给后台管理 UI 的完整目录。"""
    return [tool.as_dict() for tool in TOOLS]
