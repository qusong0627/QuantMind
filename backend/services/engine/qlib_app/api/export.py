"""Qlib 回测导出路由"""

import csv
import io
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.services.engine.qlib_app import get_qlib_service
from backend.services.engine.qlib_app.api.export_utils import (
    _build_quick_trade_rows,
    _to_finite_float,
)
from backend.services.engine.qlib_app.api.identity import _identity_from_request
from backend.shared.export_disclaimer import write_csv_disclaimer

router = APIRouter(tags=["qlib"])


@router.get("/export/{backtest_id}/csv")
@router.get("/export/{backtest_id}/pdf")
@router.get("/export/{backtest_id}/excel")
async def export_backtest(
    request: Request,
    backtest_id: str,
    tenant_id: str | None = Query(None, description="租户ID（已废弃，自动使用认证身份）"),
    style: str = Query(
        "quick",
        pattern="^(quick|legacy)$",
        description="导出格式：quick=快速回测同款交易流水，legacy=旧版指标+交易明细",
    ),
    service: Any = Depends(get_qlib_service),
) -> StreamingResponse:
    """导出回测结果（CSV格式，兼容 pdf/excel 路径）"""

    auth_user_id, auth_tenant_id = _identity_from_request(request, provided_tenant_id=tenant_id)
    result = await service.get_result(
        backtest_id,
        tenant_id=auth_tenant_id,
        user_id=auth_user_id,
    )
    if not result:
        raise HTTPException(status_code=404, detail="回测结果不存在")

    r = result if isinstance(result, dict) else result.dict() if hasattr(result, "dict") else vars(result)

    output = io.StringIO()
    writer = csv.writer(output)

    if style == "legacy":
        writer.writerow(["指标", "值"])
        scalar_fields = [
            "backtest_id",
            "status",
            "symbol",
            "strategy_name",
            "start_date",
            "end_date",
            "initial_capital",
            "final_capital",
            "total_return",
            "annual_return",
            "max_drawdown",
            "volatility",
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "alpha",
            "beta",
            "information_ratio",
            "benchmark_return",
            "total_trades",
            "win_rate",
            "profit_factor",
        ]
        for field in scalar_fields:
            val = r.get(field)
            if val is not None:
                writer.writerow([field, val])

        trades_legacy = r.get("trade_list") or []
        if trades_legacy:
            writer.writerow([])
            writer.writerow(["=== 交易记录 ==="])
            writer.writerow(
                list(trades_legacy[0].keys())
                if isinstance(trades_legacy[0], dict)
                else ["date", "action", "price", "quantity", "pnl"]
            )
            for t in trades_legacy:
                if isinstance(t, dict):
                    writer.writerow(list(t.values()))
        filename = f"backtest_{backtest_id[:8]}.csv"
    else:
        writer.writerow(["日期", "代码", "方向", "成交价", "成交量", "成交金额", "手续费", "权益余额"])
        trades = r.get("trades") or r.get("trade_list") or []
        equity_curve = r.get("equity_curve") or []
        initial_capital = _to_finite_float(r.get("initial_capital"))
        if initial_capital is None and isinstance(r.get("config"), dict):
            initial_capital = _to_finite_float(r["config"].get("initial_capital"))

        rows = _build_quick_trade_rows(
            trades=[x for x in trades if isinstance(x, dict)],
            equity_curve=[x for x in equity_curve if isinstance(x, dict)],
            initial_capital=initial_capital,
        )
        for row in rows:
            writer.writerow(
                [
                    row["date"] or "",
                    row["symbol"] or "",
                    row["action"],
                    f"{float(row['display_price']):.2f}",
                    str(int(row["qty_int"])),
                    f"{float(row['amount']):.2f}",
                    f"{float(row['commission']):.2f}",
                    f"{float(row['equity_balance']):.2f}" if row["equity_balance"] is not None else "",
                ]
            )
        filename = f"backtest_{backtest_id[:8]}_trades.csv"

    # 免责段压尾（生成时间 + 数据区间 + 不构成投资建议）。
    #
    # 区间取回测自身的 start/end_date —— 这是回测覆盖面的事实来源；两者缺失时
    # `normalize_data_range` 返回 None，该行整行不出现，不写空值也不写半截区间。
    # 导出件会离开应用（转发、存档、打印），屏幕上的免责条跟不出去，所以必须进文件。
    write_csv_disclaimer(
        writer,
        data_range=(r.get("start_date"), r.get("end_date")),
    )

    output.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
