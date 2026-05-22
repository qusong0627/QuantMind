"""QuantBot RD-Agent 演化循环启动器"""

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from backend.services.engine.quantbot.task_store import QuantBotTaskStore
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    RDAgentFactorPersistence,
)

logger = logging.getLogger(__name__)


# Alpha191 seed factor 模板（简化版）
ALPHA191_SEED_TEMPLATE = (
    "import pandas as pd\n"
    "import numpy as np\n"
    "from qlib.contrib.data.handler import Alpha158\n"
    "\n"
    "class SeedFactor:\n"
    '    """Seed factor generated from user request: {description}"""\n'
    "    def __call__(self, df: pd.DataFrame) -> pd.Series:\n"
    "        return df[\"$close\"].rolling({window}).mean() / df[\"$close\"] - 1\n"
)


class RDAgentLauncher:
    """启动 RD-Agent 因子演化循环"""

    def __init__(self):
        self.task_store = QuantBotTaskStore()
        self.factor_persistence = RDAgentFactorPersistence()
        self._running_tasks: dict[str, asyncio.Task] = {}

    async def launch_evolution(
        self,
        task_id: str,
        user_id: str,
        request: dict[str, Any],
    ) -> None:
        """异步启动 RD-Agent 演化循环"""

        async def _run():
            try:
                await self.task_store.update_status(task_id, "running", progress="正在初始化 RD-Agent...")

                # 1. 生成 seed factors
                seed_path = await self._generate_seed_factors(request)
                logger.info(f"Generated seed factors at {seed_path}")

                await self.task_store.update_status(task_id, "running", progress="正在启动因子演化循环...")

                # 2. 确定 RD-Agent 工作目录
                rd_agent_dir = self._find_rd_agent_dir()
                if not rd_agent_dir:
                    raise RuntimeError("RD-Agent 目录未找到，请确认 rd-agent 已正确部署")

                # 3. 构建命令行
                loop_n = request.get("constraints", {}).get("loop_n", 5)
                cmd = [
                    "python", "-m", "rdagent.app.qlib_rd_loop.factor",
                    "--base-features-path", str(seed_path),
                    "--loop-n", str(loop_n),
                ]

                env = os.environ.copy()
                env["PYTHONPATH"] = f"{rd_agent_dir}:{env.get('PYTHONPATH', '')}"

                await self.task_store.update_status(
                    task_id, "running", progress=f"演化循环进行中 (共 {loop_n} 轮)..."
                )

                # 4. 启动子进程
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=rd_agent_dir,
                    env=env,
                )

                # 5. 监控输出
                factor_ids: list[str] = []
                async for line in proc.stdout:
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    logger.info(f"[RD-Agent] {line_str}")

                    # 尝试从日志中提取因子 ID（如果 RD-Agent 写入了 rd_agent_factors）
                    if "factor_id" in line_str.lower():
                        pass  # 可以在这里解析日志获取 factor_ids

                await proc.wait()

                if proc.returncode != 0:
                    raise RuntimeError(f"RD-Agent 进程退出，返回码 {proc.returncode}")

                # 6. 从 rd_agent_factors 表读取结果
                await self.task_store.update_status(
                    task_id, "running", progress="正在汇总结果..."
                )

                factors = await self._collect_results()
                factor_ids = [f["factor_id"] for f in factors]

                result = {
                    "factors": factors,
                    "total_factors": len(factors),
                    "summary": self._build_summary(factors),
                }

                await self.task_store.update_status(
                    task_id,
                    "completed",
                    progress="完成",
                    result=result,
                    factor_ids=factor_ids,
                )
                logger.info(f"Task {task_id} completed with {len(factors)} factors")

            except Exception as e:
                logger.exception(f"Task {task_id} failed")
                await self.task_store.update_status(
                    task_id, "failed", error_message=str(e)
                )

        task = asyncio.create_task(_run(), name=f"quantbot-{task_id}")
        self._running_tasks[task_id] = task
        task.add_done_callback(
            lambda t: self._running_tasks.pop(task_id, None)
        )

    async def _generate_seed_factors(self, request: dict[str, Any]) -> Path:
        """生成 seed factor 文件"""
        description = request.get("description", "custom factors")
        factor_type = request.get("factor_type", "综合")

        # 根据因子类型生成不同的 seed
        windows = {"value": 20, "momentum": 60, "volatility": 20, "quality": 252, "growth": 60, "technical": 10, "综合": 20}
        window = windows.get(factor_type, 20)

        seed_code = ALPHA191_SEED_TEMPLATE.format(description=description, window=window)

        tmp_dir = Path(tempfile.mkdtemp(prefix="quantbot_seed_"))
        seed_path = tmp_dir / "seed_factor.py"
        seed_path.write_text(seed_code, encoding="utf-8")
        return seed_path

    def _find_rd_agent_dir(self) -> str | None:
        """查找 RD-Agent 目录"""
        candidates = [
            "/app/rd-agent",           # 容器内路径
            "/opt/quantmind/rd-agent", # 宿主机路径
            Path(__file__).parent.parent.parent.parent.parent / "rd-agent",
        ]
        for p in candidates:
            p = Path(p) if isinstance(p, str) else p
            if (p / "rdagent").exists() or (p / "pyproject.toml").exists():
                return str(p)
        return None

    async def _collect_results(self) -> list[dict[str, Any]]:
        """从 rd_agent_factors 表收集最新生成的因子"""
        from backend.shared.database_manager_v2 import get_session
        from sqlalchemy import text

        async with get_session(read_only=True) as session:
            rows = await session.execute(text("""
                SELECT factor_id, factor_name, status, ic_value, sharpe_ratio,
                       annual_return, max_drawdown, created_at
                FROM rd_agent_factors
                ORDER BY created_at DESC
                LIMIT 20
            """))
            data = rows.mappings().all()
            return [dict(r) for r in data]

    def _build_summary(self, factors: list[dict]) -> dict:
        """构建结果摘要"""
        if not factors:
            return {"message": "未生成任何因子"}

        completed = [f for f in factors if f.get("status") == "completed"]
        ic_values = [f["ic_value"] for f in completed if f.get("ic_value") is not None]
        sharpes = [f["sharpe_ratio"] for f in completed if f.get("sharpe_ratio") is not None]

        return {
            "total": len(factors),
            "completed": len(completed),
            "avg_ic": round(sum(ic_values) / len(ic_values), 4) if ic_values else None,
            "best_ic": round(max(ic_values), 4) if ic_values else None,
            "avg_sharpe": round(sum(sharpes) / len(sharpes), 4) if sharpes else None,
            "best_sharpe": round(max(sharpes), 4) if sharpes else None,
        }
