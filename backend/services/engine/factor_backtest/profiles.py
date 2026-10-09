"""T-FB-01 市场档案注册表——「回测中心」一切参数的唯一出处。

每市场声明：qlib provider 解析（经 ``qlib_paths`` 统一链）、universe 模式
（CN 现有 8 池 / US 全列精选池 / HK 60 日成交额 top-N 动态池 / BC 全列 /
FUT curated）、默认窗口、研究费率、基准、实验性标注。

口径纪律：
- **CN 列 = 样本内基准**（挖掘原始市场，用户要求「A股也要别的市场也要对比」），
  其余市场列 = 样本外；BC/FUTURES 结构差异大（7×24 / 合约混合）标实验性。
- 基准（T-FB-19）：CN/HK/US 声明**真实指数**（``benchmarks.BENCHMARK_SOURCES``
  登记 csi300/hsi/spx，读数失败或日历覆盖不足时 engine 如实回落等权并在载荷
  标注）；crypto/futures 无可靠指数序列，声明即等权兜底——**不冒充**指数。
- 研究费率（T-FB-19 审计口径，见各档案上方注释）：按各市场显性交易成本 +
  保守滑点估的双边研究费率；``QM_BACKTEST_COST_BPS_DEFAULT`` 可全局覆盖。
- 窗口默认各市场最近 N 年（``QM_BACKTEST_WINDOW_YEARS`` 可覆盖），起点钳制
  在日历首日之内（BC 只有约 1 年数据，不钳制就是空跑）。

本模块保持**导入轻量**（只依赖 qlib_paths/compat，不拉 qlib 与 routers）：
engine 层负责 qlib.init 与 D.features，profiles 只做静态声明与文件面侦察。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from backend.services.engine.factor_backtest.compat import (
    BASE_COLUMNS,
    CN_MINING_COLUMNS,
)
from backend.shared.qlib_paths import is_qlib_provider_ready, resolve_qlib_provider_uri

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """读整数 env（非法值回落默认）；调用时读取，测试可 monkeypatch。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("env %s=%r 非法，回落默认 %d", name, raw, default)
        return default


@dataclass(frozen=True)
class MarketProfile:
    """单个市场的档案（不可变；所有字段都是声明，不含运行时状态）。"""

    market: str  # 应用侧市场键（API/台账用）
    qlib_market: str  # qlib 市场键（provider 解析用）
    label: str  # 界面中文标签
    in_sample: bool  # True = 挖掘原始市场（样本内基准列）
    universe_mode: str  # cn_pools | all | liquid_top_n | curated
    default_universe: str
    universe_top_n: int  # 0 = 不裁剪（all/curated 模式）
    window_years: int  # 默认回测窗口年数
    cost_bps: int  # 研究口径双边费率（bps；审计口径见各档案上方注释）
    benchmark: (
        str  # 请求基准：csi300/hsi/spx 或 equal_weight（读数失败回落等权并如实标注）
    )
    experimental: bool  # BC/FUTURES 实验性标注
    note: str  # 界面说明（结构差异等）


_PROFILES: tuple[MarketProfile, ...] = (
    # 费率审计（双边，bps）：佣金万分之 2.5×2 + 卖出印花税 5 + 过户费 ~0.2
    # + 保守滑点 ~7 ≈ 20。与既有关卡 20bps 口径一致，CN 数字跨批可比。
    MarketProfile(
        market="a_share",
        qlib_market="CN",
        label="沪深A股",
        in_sample=True,
        universe_mode="cn_pools",
        default_universe="csi300",
        universe_top_n=0,
        window_years=3,
        cost_bps=20,
        benchmark="csi300",
        experimental=False,
        note="挖掘原始市场（样本内基准列）；股票池沿用现有 8 池",
    ),
    # 费率审计（双边，bps）：印花税 0.1%×2 = 20 + 交易费/征费/交收费 ~0.4
    # + 保守滑点 ~5 ≈ 25（港股显性成本全球主要市场最高档）。
    MarketProfile(
        market="hong_kong",
        qlib_market="HK",
        label="港股",
        in_sample=False,
        universe_mode="liquid_top_n",
        default_universe="liquid_top500",
        universe_top_n=500,
        window_years=3,
        cost_bps=25,
        benchmark="hsi",
        experimental=False,
        note="动态池：过去 60 日日均成交额 top-N（避免静态名单的生存者偏差）",
    ),
    # 费率审计（双边，bps）：佣金 $0（主流零佣）+ SEC 卖出规费/TAF ~0.3
    # + 保守滑点 ~5×2 ≈ 10。
    MarketProfile(
        market="us_stock",
        qlib_market="US",
        label="美股",
        in_sample=False,
        universe_mode="all",
        default_universe="all",
        universe_top_n=0,
        window_years=3,
        cost_bps=10,
        benchmark="spx",
        experimental=False,
        note="缓存即为精选流动池（517 只），全列参与",
    ),
    # 费率审计（双边，bps）：Binance 现货 taker 0.1%×2 = 20（资金费不计入
    # 日频换手口径）。
    MarketProfile(
        market="crypto",
        qlib_market="CRYPTO",
        label="加密货币",
        in_sample=False,
        universe_mode="all",
        default_universe="all",
        universe_top_n=0,
        window_years=1,
        cost_bps=20,
        benchmark="equal_weight",
        experimental=True,
        note="7×24 日历且历史仅约 1 年（窗口自动钳制）；实验性",
    ),
    # 费率审计（双边，bps）：手续费+滑点单边 ~2.5，按主力合约估 ≈ 5；待
    # 主力清单定稿后随之一并复核。
    MarketProfile(
        market="futures",
        qlib_market="FUTURES",
        label="期货",
        in_sample=False,
        universe_mode="curated",
        default_universe="curated_main",
        universe_top_n=0,
        window_years=3,
        cost_bps=5,
        benchmark="equal_weight",
        experimental=True,
        note="混合 T+D/现货类合约，主力清单尚待人工定稿；实验性",
    ),
)

_BY_MARKET: dict[str, MarketProfile] = {p.market: p for p in _PROFILES}


def list_market_profiles() -> list[MarketProfile]:
    """全部市场档案（声明序：CN 在前 = 基准列在前）。"""
    return list(_PROFILES)


def get_market_profile(market: str) -> MarketProfile:
    """按应用侧市场键取档案；未知市场抛 KeyError（路由层翻 400）。"""
    try:
        return _BY_MARKET[market]
    except KeyError:
        raise KeyError(f"未知市场: {market!r}") from None


def columns_for_market(market: str) -> frozenset[str]:
    """目标市场的**分类用**列集。

    CN 用 39 列挖掘契约（因子挖掘时看到的那套列）；其余市场用 bin 实测
    基础 7 列。别拿 CN bin 的 8 列当契约——``change`` 不在挖掘契约内，
    因子从未依赖它。
    """
    return CN_MINING_COLUMNS if market == "a_share" else BASE_COLUMNS


def effective_universe_top_n(profile: MarketProfile) -> int:
    """生效的动态池大小（env ``QM_BACKTEST_UNIVERSE_TOP_N`` 覆盖档案值）。"""
    if profile.universe_mode != "liquid_top_n":
        return profile.universe_top_n
    return _env_int("QM_BACKTEST_UNIVERSE_TOP_N", profile.universe_top_n)


def effective_window_years(profile: MarketProfile) -> int:
    """生效窗口年数（env ``QM_BACKTEST_WINDOW_YEARS`` 覆盖全部市场）。"""
    return _env_int("QM_BACKTEST_WINDOW_YEARS", profile.window_years)


def effective_cost_bps(profile: MarketProfile) -> int:
    """生效研究费率（env ``QM_BACKTEST_COST_BPS_DEFAULT``）。"""
    return _env_int("QM_BACKTEST_COST_BPS_DEFAULT", profile.cost_bps)


def effective_min_days() -> int:
    """insufficient 阈值（env ``QM_BACKTEST_MIN_DAYS``，默认 120 个有效日）。"""
    return _env_int("QM_BACKTEST_MIN_DAYS", 120)


# ── 文件面侦察（provider 布局读取，皆为 KiB 级元数据 IO）────────────────


def _calendar_bounds(provider: str) -> tuple[str | None, str | None]:
    """日历首末交易日（``calendars/day.txt``）；缺失返回 (None, None)。"""
    path = Path(provider) / "calendars" / "day.txt"
    try:
        lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    except OSError:
        return None, None
    if not lines:
        return None, None
    return lines[0], lines[-1]


def _instrument_count(provider: str) -> int | None:
    """标的数（``instruments/all.txt`` 行数）；缺失返回 None。"""
    path = Path(provider) / "instruments" / "all.txt"
    try:
        with path.open() as fh:
            return sum(1 for ln in fh if ln.strip())
    except OSError:
        return None


def _first_instrument(provider: str) -> str | None:
    """all.txt 首个标的 id（bin 列侦察的取样标的）。"""
    path = Path(provider) / "instruments" / "all.txt"
    try:
        with path.open() as fh:
            for ln in fh:
                token = ln.split("\t", 1)[0].strip()
                if token:
                    return token
    except OSError:
        return None
    return None


def _bin_columns(provider: str) -> list[str] | None:
    """features/<首个标的>/ 下的列文件（``close.day.bin`` → ``close``）。

    取样一只标的即可——bin 布局中所有标的的列文件一致（五市场同构实测）。
    目录名先按 all.txt 原样试，再按小写回落：非 CN provider 的 feature 目录
    是小写形态（HK ``hk_0001.HK`` → ``hk_0001.hk``；US/BC/FUT 同型），不加
    回落四个非 CN 市场的列侦察全落空（2026-10-09 实测）。
    """
    inst = _first_instrument(provider)
    if not inst:
        return None
    for candidate in (inst, inst.lower()):
        feat_dir = Path(provider) / "features" / candidate
        try:
            names = sorted(
                p.name[: -len(".day.bin")] for p in feat_dir.glob("*.day.bin")
            )
        except OSError:
            continue
        if names:
            return names
    return None


def default_window(profile: MarketProfile) -> tuple[str, str]:
    """档案默认窗口：日历末日往前 N 年（N = env 覆盖后的档案值），起点钳日历首日。"""
    provider = resolve_qlib_provider_uri(profile.qlib_market)
    start_bound, end_bound = _calendar_bounds(provider)
    if end_bound is None:
        # provider 未就绪时别造窗口——调用方会先走 profile_status 判 unavailable
        return "", ""
    end = _parse_date(end_bound)
    start = _shift_years(end, -effective_window_years(profile))
    if start_bound and start < _parse_date(start_bound):
        start = _parse_date(start_bound)
    return start.isoformat(), end.isoformat()


def _parse_date(value: str):
    from datetime import date

    y, m, d = (int(x) for x in value.split("-")[:3])
    return date(y, m, d)


def _shift_years(day, years: int):
    """日期年份平移（2/29 → 2/28 钳制），不依赖 pandas。"""
    from datetime import date

    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def profile_status(profile: MarketProfile) -> dict:
    """档案 + 数据面实况（``GET /markets`` 的行；供 UI 与 unavailable 判据）。"""
    provider = resolve_qlib_provider_uri(profile.qlib_market)
    ready = is_qlib_provider_ready(provider)
    calendar_start, calendar_end = _calendar_bounds(provider) if ready else (None, None)
    return {
        "market": profile.market,
        "qlib_market": profile.qlib_market,
        "label": profile.label,
        "in_sample": profile.in_sample,
        "experimental": profile.experimental,
        "note": profile.note,
        "provider": provider,
        "ready": ready,
        "calendar_start": calendar_start,
        "calendar_end": calendar_end,
        "instruments": _instrument_count(provider) if ready else None,
        "columns": sorted(columns_for_market(profile.market)),
        "bin_columns": _bin_columns(provider) if ready else None,
        "universe_mode": profile.universe_mode,
        "default_universe": profile.default_universe,
        "universe_top_n": effective_universe_top_n(profile),
        "window_years": effective_window_years(profile),
        "cost_bps": effective_cost_bps(profile),
        "benchmark": profile.benchmark,
        "min_days": effective_min_days(),
    }
