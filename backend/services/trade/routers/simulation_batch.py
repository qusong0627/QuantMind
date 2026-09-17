from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.services.trade_shared.deps import get_db, require_admin
from backend.services.simulation.services.simulation_settler import settler

router = APIRouter(prefix="/api/v1/simulation/batch", tags=["Simulation Batch Operations"])


@router.post("/step")
async def trigger_simulation_step(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    auth: Any = Depends(require_admin),  # H2 加固：原匿名可代任意用户触发日终结算
):
    """
    手动触发一次模拟交易的步进（结算）
    用于测试“单一写权限”和“数据驱动模拟盘”流程
    """
    user_id = payload.get("user_id")
    strategy_id = payload.get("strategy_id")

    if not user_id or not strategy_id:
        raise HTTPException(status_code=400, detail="Missing user_id or strategy_id")

    try:
        result = await settler.run_daily_settlement(db, int(user_id), strategy_id)
        return {"status": "success", "message": f"Daily simulation step completed for user {user_id}", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
