from __future__ import annotations
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from typing import Literal,  TypeVar, Callable, Any,Concatenate
    
    from collections.abc import (
        Hashable,
        Mapping,
        MutableMapping,
        Sequence,
    )
    OPTTargetType = Literal[
        'result', 'profit', 'profit_rate', 'adjusted_sortino', 'autocorr_penalty', 'avg_loss', 'avg_return',
        'avg_win', 'best', 'cagr', 'calmar', 'common_sense_ratio', 'comp', 'compare', 'compsum', 'conditional_value_at_risk',
        'consecutive_losses', 'consecutive_wins', 'cpc_index', 'cvar', 'distribution', 'drawdown_details', 'expected_return',
        'expected_shortfall', 'exposure', 'gain_to_pain_ratio', 'geometric_mean', 'ghpr', 'greeks',
        'implied_volatility', 'information_ratio', 'kelly_criterion', 'kurtosis', 'max_drawdown',
        'monthly_returns', 'omega', 'outlier_loss_ratio', 'outlier_win_ratio', 'outliers', 'payoff_ratio',
        'pct_rank', 'profit_factor', 'profit_ratio', 'r2', 'r_squared', 'rar', 'recovery_factor',
        'remove_outliers', 'risk_of_ruin', 'risk_return_ratio', 'rolling_greeks', 'rolling_sharpe',
        'rolling_sortino', 'rolling_volatility', 'ror', 'serenity_index', 'sharpe', 'skew', 'smart_sharpe',
        'smart_sortino', 'sortino', 'tail_ratio', 'to_drawdown_series', 'ulcer_index', 'ulcer_performance_index',
        'upi', 'value_at_risk', 'var', 'volatility', 'warn', 'win_loss_ratio', 'win_rate', 'worst']
    OPTMethodType = Literal['ga', 'optuna']
    IncludeStyle = Literal["all", "last"]
    EngineType = Literal["cython", "numba"] | None
    BTPlotType = Literal["all", "last"]
    MaModeType = Literal[
        "dema", "ema", "fwma", "hma", "linreg", "midpoint", "pwma", "rma",
        "sinwma", "sma", "swma", "t3", "tema", "trima", "vidya", "wma", "zlma"
    ]
    DevType = Literal["stdev", "art", "variance", "smoothrng"]
    RSRSMethodType = Literal['r1', 'r2', 'r3']
    LabelStyle = Literal["normal", "bold"]
    PRFNameTtype = Literal[
        "2crows", "3blackcrows", "3inside", "3linestrike", "3outside",
        "3starsinsouth", "3whitesoldiers", "abandonedbaby", "advanceblock",
        "belthold", "breakaway", "closingmarubozu", "concealbabyswall",
        "counterattack", "darkcloudcover", "doji", "dojistar", "dragonflydoji",
        "engulfing", "eveningdojistar", "eveningstar", "gapsidesidewhite",
        "gravestonedoji", "hammer", "hangingman", "harami", "haramicross",
        "highwave", "hikkake", "hikkakemod", "homingpigeon", "identical3crows",
        "inneck", "inside", "invertedhammer", "kicking", "kickingbylength",
        "ladderbottom", "longleggeddoji", "longline", "marubozu", "matchinglow",
        "mathold", "morningdojistar", "morningstar", "onneck", "piercing",
        "rickshawman", "risefall3methods", "separatinglines", "shootingstar",
        "shortline", "spinningtop", "stalledpattern", "sticksandwich", "takuri",
        "tasukigap", "thrusting", "tristar", "unique3river", "upsidegap2crows",
        "xsidegap3methods"
    ]
    ExpandingMethodType = Literal["single", "table"]

    # pandas typing
    T = TypeVar("T")

    Axis = int | Literal["index", "columns", "rows"]
    IgnoreRaise = Literal["ignore", "raise"]
    FillnaOptions = Literal["backfill", "bfill", "ffill", "pad"]
    InterpolateOptions = Literal[
        "linear",
        "time",
        "index",
        "values",
        "nearest",
        "zero",
        "slinear",
        "quadratic",
        "cubic",
        "barycentric",
        "polynomial",
        "krogh",
        "piecewise_polynomial",
        "spline",
        "pchip",
        "akima",
        "cubicspline",
        "from_derivatives",
    ]
    import numpy as np
    from datetime import date
    PythonScalar = str | float | bool
    DatetimeLikeScalar = TypeVar("Period") | TypeVar(
        "Timestamp") | TypeVar("Timedelta")
    PandasScalar = TypeVar("Period") | TypeVar(
        "Timestamp") | TypeVar("Timedelta") | TypeVar("Interval")
    Scalar = PythonScalar | PandasScalar | np.datetime64 | np.timedelta64 | date
    ArrayLike = TypeVar("ExtensionArray") | np.ndarray
    AnyArrayLike = ArrayLike | TypeVar("Index") | TypeVar("Series")
    IndexLabel = Hashable | Sequence[Hashable]
    Frequency = str | TypeVar("BaseOffset")
    ValueKeyFunc = Callable[[TypeVar("Series")],
                                     TypeVar("Series") | AnyArrayLike] | None
    IndexKeyFunc = Callable[[
        TypeVar("Index")], TypeVar("Index") | AnyArrayLike] | None
    UpdateJoin = Literal["left"]
    NaPosition = Literal["first", "last"]
    AggFuncTypeBase = Callable | str
    AggFuncTypeDict = MutableMapping[
        Hashable, AggFuncTypeBase | list[AggFuncTypeBase]
    ]
    AggFuncType = AggFuncTypeBase | list[AggFuncTypeBase] | AggFuncTypeDict
    MergeHow = Literal["left", "right", "inner", "outer", "cross"]
    MergeValidate = Literal[
        "one_to_one",
        "1:1",
        "one_to_many",
        "1:m",
        "many_to_one",
        "m:1",
        "many_to_many",
        "m:m",
    ]
    JoinValidate = Literal[
        "one_to_one",
        "1:1",
        "one_to_many",
        "1:m",
        "many_to_one",
        "m:1",
        "many_to_many",
        "m:m",
    ]
    Suffixes = tuple[str | None, str | None]
    DropKeep = Literal["first", "last", False]
    SortKind = Literal["quicksort", "mergesort", "heapsort", "stable"]
    PythonFuncType = Callable[[Any], Any]
    NaAction = Literal["ignore"]
    QuantileInterpolation = Literal["linear",
                                    "lower", "higher", "midpoint", "nearest"]
    WindowingRankType = Literal["average", "min", "max"]

