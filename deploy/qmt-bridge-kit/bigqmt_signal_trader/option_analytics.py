"""Local Black-Scholes option analytics for Big-QMT clients.

Big QMT exposes ``bsm_price``/``bsm_iv`` over RPC, but some terminals return
``0.0`` from ``get_option_iv`` and expose no Greeks API.  These functions are
deliberately client-side and dependency-free: callers get deterministic IV and
Delta/Gamma/Vega/Theta/Rho without changing the QMT-side runtime.

Rates and volatility are decimals (``0.02`` = 2%).  ``vega`` and ``rho`` are
per 1.00 absolute change; the result also includes the more practical
``vega_1pct`` and ``rho_1pct`` fields.  Theta is returned per year and per day.
"""

import math


_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def normalize_option_type(option_type):
    """Return ``C`` or ``P`` for common QMT/English/Chinese spellings."""
    text = str(option_type or "").strip().upper()
    if text in ("C", "CALL", "认购", "购"):
        return "C"
    if text in ("P", "PUT", "认沽", "沽"):
        return "P"
    raise ValueError("option_type must be C/CALL or P/PUT, got %r" % option_type)


def _normal_cdf(value):
    return 0.5 * (1.0 + math.erf(value / _SQRT_2))


def _normal_pdf(value):
    return math.exp(-0.5 * value * value) / _SQRT_2PI


def _inputs(option_type, underlying_price, strike_price, time_to_expiry, sigma=None):
    kind = normalize_option_type(option_type)
    spot = float(underlying_price)
    strike = float(strike_price)
    years = float(time_to_expiry)
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("underlying_price must be finite and positive")
    if not math.isfinite(strike) or strike <= 0:
        raise ValueError("strike_price must be finite and positive")
    if not math.isfinite(years) or years <= 0:
        raise ValueError("time_to_expiry must be finite and positive")
    if sigma is not None:
        sigma = float(sigma)
        if not math.isfinite(sigma) or sigma <= 0:
            raise ValueError("sigma must be finite and positive")
    return kind, spot, strike, years, sigma


def _d1_d2(spot, strike, years, rate, dividend, sigma):
    root_years = math.sqrt(years)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend + 0.5 * sigma * sigma) * years
    ) / (sigma * root_years)
    return d1, d1 - sigma * root_years


def black_scholes_price(
    option_type,
    underlying_price,
    strike_price,
    time_to_expiry,
    risk_free_rate,
    sigma,
    dividend_yield=0.0,
):
    """Return a European option price under Black-Scholes-Merton."""
    kind, spot, strike, years, sigma = _inputs(
        option_type, underlying_price, strike_price, time_to_expiry, sigma
    )
    rate = float(risk_free_rate)
    dividend = float(dividend_yield)
    d1, d2 = _d1_d2(spot, strike, years, rate, dividend, sigma)
    discounted_spot = spot * math.exp(-dividend * years)
    discounted_strike = strike * math.exp(-rate * years)
    if kind == "C":
        return discounted_spot * _normal_cdf(d1) - discounted_strike * _normal_cdf(d2)
    return discounted_strike * _normal_cdf(-d2) - discounted_spot * _normal_cdf(-d1)


def _price_bounds(kind, spot, strike, years, rate, dividend):
    discounted_spot = spot * math.exp(-dividend * years)
    discounted_strike = strike * math.exp(-rate * years)
    if kind == "C":
        return max(0.0, discounted_spot - discounted_strike), discounted_spot
    return max(0.0, discounted_strike - discounted_spot), discounted_strike


def implied_volatility(
    option_type,
    underlying_price,
    strike_price,
    option_price,
    time_to_expiry,
    risk_free_rate,
    dividend_yield=0.0,
    tolerance=1e-10,
    max_iterations=200,
    max_sigma=10.0,
):
    """Solve Black-Scholes implied volatility with bounded bisection.

    Bisection is slower than a pure Newton step but cannot diverge on a low
    vega, deep in/out-of-the-money contract.  Invalid market prices are rejected
    with the no-arbitrage interval in the error instead of returning a plausible
    but meaningless number.
    """
    kind, spot, strike, years, _ = _inputs(
        option_type, underlying_price, strike_price, time_to_expiry
    )
    observed = float(option_price)
    rate = float(risk_free_rate)
    dividend = float(dividend_yield)
    if not math.isfinite(observed) or observed < 0:
        raise ValueError("option_price must be finite and non-negative")

    lower_bound, upper_bound = _price_bounds(
        kind, spot, strike, years, rate, dividend
    )
    bound_tolerance = max(float(tolerance), 1e-12)
    if observed < lower_bound - bound_tolerance or observed > upper_bound + bound_tolerance:
        raise ValueError(
            "option_price %.12g is outside no-arbitrage bounds [%.12g, %.12g]"
            % (observed, lower_bound, upper_bound)
        )
    if observed <= lower_bound + bound_tolerance:
        return 0.0
    if observed >= upper_bound - bound_tolerance:
        raise ValueError("option_price is at the upper bound; finite IV does not exist")

    low = 1e-9
    high = min(1.0, float(max_sigma))
    while (
        black_scholes_price(kind, spot, strike, years, rate, high, dividend)
        < observed
        and high < float(max_sigma)
    ):
        high = min(high * 2.0, float(max_sigma))
    high_price = black_scholes_price(kind, spot, strike, years, rate, high, dividend)
    if high_price < observed:
        raise ValueError("implied volatility exceeds max_sigma %.6g" % float(max_sigma))

    for _ in range(int(max_iterations)):
        mid = 0.5 * (low + high)
        model_price = black_scholes_price(
            kind, spot, strike, years, rate, mid, dividend
        )
        if abs(model_price - observed) <= float(tolerance):
            return mid
        if model_price < observed:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def option_greeks(
    option_type,
    underlying_price,
    strike_price,
    time_to_expiry,
    risk_free_rate,
    sigma,
    dividend_yield=0.0,
):
    """Return Delta, Gamma, Vega, Theta and Rho under BSM."""
    kind, spot, strike, years, sigma = _inputs(
        option_type, underlying_price, strike_price, time_to_expiry, sigma
    )
    rate = float(risk_free_rate)
    dividend = float(dividend_yield)
    d1, d2 = _d1_d2(spot, strike, years, rate, dividend, sigma)
    discounted_spot_factor = math.exp(-dividend * years)
    discounted_strike = strike * math.exp(-rate * years)
    pdf_d1 = _normal_pdf(d1)
    root_years = math.sqrt(years)

    gamma = discounted_spot_factor * pdf_d1 / (spot * sigma * root_years)
    vega = spot * discounted_spot_factor * pdf_d1 * root_years
    diffusion_theta = (
        -spot * discounted_spot_factor * pdf_d1 * sigma / (2.0 * root_years)
    )
    if kind == "C":
        delta = discounted_spot_factor * _normal_cdf(d1)
        theta = (
            diffusion_theta
            - rate * discounted_strike * _normal_cdf(d2)
            + dividend * spot * discounted_spot_factor * _normal_cdf(d1)
        )
        rho = discounted_strike * years * _normal_cdf(d2)
    else:
        delta = discounted_spot_factor * (_normal_cdf(d1) - 1.0)
        theta = (
            diffusion_theta
            + rate * discounted_strike * _normal_cdf(-d2)
            - dividend * spot * discounted_spot_factor * _normal_cdf(-d1)
        )
        rho = -discounted_strike * years * _normal_cdf(-d2)

    return {
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "vega_1pct": vega * 0.01,
        "theta_per_year": theta,
        "theta_per_day": theta / 365.0,
        "rho": rho,
        "rho_1pct": rho * 0.01,
    }


def calculate_option_analytics(
    option_type,
    underlying_price,
    strike_price,
    option_price,
    days_to_expiry,
    risk_free_rate=0.0,
    dividend_yield=0.0,
    year_days=365.0,
):
    """Calculate IV, standard Greeks and useful value/moneyness fields."""
    days = float(days_to_expiry)
    basis = float(year_days)
    if not math.isfinite(days) or days <= 0:
        raise ValueError("days_to_expiry must be finite and positive")
    if not math.isfinite(basis) or basis <= 0:
        raise ValueError("year_days must be finite and positive")
    years = days / basis
    kind = normalize_option_type(option_type)
    spot = float(underlying_price)
    strike = float(strike_price)
    observed = float(option_price)
    rate = float(risk_free_rate)
    dividend = float(dividend_yield)
    iv = implied_volatility(
        kind, spot, strike, observed, years, rate, dividend
    )
    # A price exactly on the lower bound has the limiting IV of zero.  Evaluate
    # Greeks at a tiny positive sigma so the result stays finite and explicit.
    greek_sigma = max(iv, 1e-9)
    result = option_greeks(
        kind, spot, strike, years, rate, greek_sigma, dividend
    )
    intrinsic = max(spot - strike, 0.0) if kind == "C" else max(strike - spot, 0.0)
    result.update({
        "option_type": kind,
        "underlying_price": spot,
        "strike_price": strike,
        "option_price": observed,
        "days_to_expiry": days,
        "time_to_expiry_years": years,
        "risk_free_rate": rate,
        "dividend_yield": dividend,
        "implied_volatility": iv,
        "theoretical_price": black_scholes_price(
            kind, spot, strike, years, rate, greek_sigma, dividend
        ),
        "intrinsic_value": intrinsic,
        "time_value": observed - intrinsic,
        "moneyness": spot / strike,
    })
    return result


__all__ = [
    "black_scholes_price",
    "calculate_option_analytics",
    "implied_volatility",
    "normalize_option_type",
    "option_greeks",
]
