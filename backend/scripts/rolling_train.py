#!/usr/bin/env python3
"""滚动训练 CLI（P1 · 设计文档 §4.8）——窗口干跑 / 手工派发。

用法：
  ① 干跑（默认）：只按交易日历算窗口 plan 并打印 JSON，不连任何服务。
     验收口径 ④ 要求与 golden fixture（backend/tests/fixtures/rollingWindowGolden.json）
     一致：
       python backend/scripts/rolling_train.py --dry-run --market CN
  ② 对表：自定义 anchor / 本地日历文件（与 golden 测试同款输入）：
       python backend/scripts/rolling_train.py --dry-run --anchor 2026-10-01 \\
           --calendar-file /tmp/calendar.json
  ③ 手工派发（走 internal 端点，与 beat 定时**同一入口、同一服务端守卫**）：
       python backend/scripts/rolling_train.py --dispatch --market CN
     幂等按 trigger 分账：manual 与 schedule 是同窗口的两条独立 campaign
     记录，不会被对方的「本月已派发」标记拦下（同窗口会再训一次）。

约定：真实派发的窗口一律由**服务端**计算（本 CLI 只传 anchor，不传 plan），
与 --dry-run 共用 backend.shared.training.rolling_window 同一实现，口径不漂移。

退出码：0 已派发或幂等命中（dispatched/duplicate）；1 未派发（busy/数据未就绪
等 skipped）或请求失败；2 参数或窗口计算错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared.training.rolling_window import (  # noqa: E402
    DEFAULT_EXECUTION_LAG_DAYS,
    WindowCalculationError,
    WindowPolicy,
    compute_window,
    parse_day,
    resolve_anchor,
)

#: 找日历的回看跨度：默认窗口（756+126+63+2×purge ≈ 957 交易日 ≈ 4 年）留 25% 余量。
_CALENDAR_LOOKBACK_DAYS = 5 * 366
_DEFAULT_RECIPE_ID = "cn_nativetft_base"
_DEFAULT_HORIZON_DAYS = 5
_DISPATCH_PATH = "/api/v1/internal/rolling/dispatch"


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _load_recipe(recipe_id: str) -> dict | None:
    path = (
        _REPO_ROOT / "backend" / "shared" / "training" / "recipes" / f"{recipe_id}.json"
    )
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_calendar_file(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    days = data.get("trading_days") if isinstance(data, dict) else data
    if not isinstance(days, list) or not days:
        raise WindowCalculationError(f"日历文件缺少 trading_days 列表: {path}")
    return [str(day) for day in days]


def _calendar_from_market(market: str, anchor: date) -> list[date]:
    from backend.shared.trading_calendar import trading_days_xcal

    start = anchor - timedelta(days=_CALENDAR_LOOKBACK_DAYS)
    days = trading_days_xcal(market, start, anchor)
    if not days:
        raise WindowCalculationError(
            f"交易日历不可用（{market} {start} ~ {anchor}）"
            "——检查 exchange_calendars 覆盖年限（xcal_coverage）"
        )
    return days


def _anchor_from_data(recipe: dict | None, market: str, horizon_days: int) -> date:
    """无 --anchor 时从因子源分区定锚（与调度器同一条数据滞后守卫）。"""
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    factor_market = str((recipe or {}).get("factor_market") or market)
    factor_source = str((recipe or {}).get("factor_source") or "l1_factors")
    reader = QuantDBFactorReader(market=factor_market)
    available = reader.available_dates(factor_source) or []
    anchor = resolve_anchor(available, horizon_days=horizon_days)
    if anchor is None:
        raise WindowCalculationError(
            f"因子源 {factor_source}（{factor_market}）可用分区不足一个标签跨度，无法定锚"
        )
    return anchor


def _build_policy(args: argparse.Namespace, recipe: dict | None) -> tuple[WindowPolicy, int]:
    overrides: dict = dict((recipe or {}).get("window_policy") or {})
    for key in ("train_days", "valid_days", "test_days", "purge_days"):
        value = getattr(args, key)
        if value is not None:
            overrides[key] = value
    if args.mode is not None:
        overrides["mode"] = args.mode
    horizon = args.horizon_days
    if horizon is None:
        horizon = int((recipe or {}).get("target_horizon_days") or _DEFAULT_HORIZON_DAYS)
    return WindowPolicy.from_dict(overrides), horizon


def _dispatch(args: argparse.Namespace, anchor: date | None) -> dict:
    from backend.shared.auth import get_internal_call_secret

    secret = get_internal_call_secret()
    if not secret:
        raise WindowCalculationError(
            "INTERNAL_CALL_SECRET 未配置（fail-closed，拒绝派发）"
        )
    base = args.url or os.getenv("QUANTMIND_API_BASE_URL") or "http://127.0.0.1:8000"
    url = base.rstrip("/") + _DISPATCH_PATH
    body: dict = {
        "market": args.market,
        "recipe_id": args.recipe_id,
        "trigger": "manual",
        "dry_run": False,
    }
    if anchor is not None:
        body["anchor_date"] = anchor.isoformat()
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Call-Secret": secret,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = raw
        return {"ok": False, "http_status": exc.code, "error": detail}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "error": f"无法连接 {url}: {exc}"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="滚动训练窗口干跑 / 手工派发（P1）",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", action="store_true", help="只算窗口并打印（默认）"
    )
    mode_group.add_argument(
        "--dispatch",
        action="store_true",
        help="走 internal 端点手工派发一轮（trigger=manual，与调度侧幂等分账；同窗口会再训一次）",
    )
    parser.add_argument("--market", default="CN", help="市场代码（默认 CN）")
    parser.add_argument(
        "--recipe-id", default=_DEFAULT_RECIPE_ID, help="配方 ID（默认 cn_nativetft_base）"
    )
    parser.add_argument("--anchor", default=None, help="锚定交易日 YYYY-MM-DD")
    parser.add_argument(
        "--calendar-file",
        type=Path,
        default=None,
        help='本地交易日历 JSON（{"trading_days": [...]})，仅 --dry-run 可用',
    )
    parser.add_argument("--horizon-days", type=int, default=None, help="标签跨度（默认取 recipe）")
    parser.add_argument("--train-days", type=int, default=None)
    parser.add_argument("--valid-days", type=int, default=None)
    parser.add_argument("--test-days", type=int, default=None)
    parser.add_argument("--mode", choices=["sliding", "expanding"], default=None)
    parser.add_argument("--purge-days", type=int, default=None, help="净化带显式覆盖")
    parser.add_argument(
        "--url", default=None, help="API 基址（默认 QUANTMIND_API_BASE_URL 或 127.0.0.1:8000）"
    )
    parser.add_argument("--timeout", type=int, default=60, help="HTTP 超时秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dispatch = bool(args.dispatch)

    if dispatch and args.calendar_file is not None:
        _eprint("真实派发的窗口由服务端计算；--calendar-file 仅用于 --dry-run")
        return 2
    if dispatch and any(
        getattr(args, key) is not None
        for key in ("train_days", "valid_days", "test_days", "mode", "purge_days")
    ):
        _eprint("真实派发的窗口策略以 recipe/服务端为准，不接受本地覆写参数")
        return 2

    recipe = _load_recipe(args.recipe_id)

    try:
        if args.anchor is not None:
            anchor: date | None = parse_day(args.anchor)
        elif dispatch:
            anchor = None  # 服务端自行定锚
        else:
            policy_pre, horizon_pre = _build_policy(args, recipe)
            anchor = _anchor_from_data(recipe, args.market, horizon_pre)

        if dispatch:
            result = _dispatch(args, anchor)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            # 端点成功体是 execute_dispatch 裁决（status=…），没有 "ok" 键；
            # 只有错误体（连接失败/HTTPError）才带 ok=False。【审查 F4】
            return 0 if str(result.get("status") or "") in ("dispatched", "duplicate") else 1

        policy, horizon = _build_policy(args, recipe)
        if args.calendar_file is not None:
            calendar: list = _load_calendar_file(args.calendar_file)
        else:
            calendar = _calendar_from_market(args.market, anchor)
        window = compute_window(
            anchor,
            calendar,
            policy,
            horizon_days=horizon,
            execution_lag_days=DEFAULT_EXECUTION_LAG_DAYS,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "dry-run",
                    "market": args.market,
                    "recipe_id": args.recipe_id,
                    "anchor_date": window.anchor_date.isoformat(),
                    "horizon_days": horizon,
                    "policy": policy.to_dict(),
                    "plan": window.to_plan(),
                    "split_fields": window.to_split_fields(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except WindowCalculationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
