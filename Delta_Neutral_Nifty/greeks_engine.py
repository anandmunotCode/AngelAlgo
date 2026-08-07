"""
Delta Neutral Nifty - Greeks Engine (Black-Scholes)
Calculates option Greeks (Delta, Gamma, Theta, Vega) and Implied Volatility.
Provides strike selection at target delta levels.
"""
import math
from scipy.stats import norm

from . import config


# ═══════════════════════════════════════════════════════════════
# BLACK-SCHOLES CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def _d1(S, K, T, r, sigma):
    """Calculate d1 parameter of Black-Scholes."""
    if T <= 0 or sigma <= 0:
        return float("inf") if S > K else float("-inf")
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def _d2(S, K, T, r, sigma):
    """Calculate d2 parameter of Black-Scholes."""
    return _d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def bs_call_price(S, K, T, r, sigma):
    """Black-Scholes European Call price."""
    if T <= 0:
        return max(S - K, 0.0)
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put_price(S, K, T, r, sigma):
    """Black-Scholes European Put price."""
    if T <= 0:
        return max(K - S, 0.0)
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_price(S, K, T, r, sigma, option_type="CE"):
    """Black-Scholes option price for either Call or Put."""
    if option_type == "CE":
        return bs_call_price(S, K, T, r, sigma)
    return bs_put_price(S, K, T, r, sigma)


# ═══════════════════════════════════════════════════════════════
# GREEKS
# ═══════════════════════════════════════════════════════════════

def delta(S, K, T, r, sigma, option_type="CE"):
    """
    Black-Scholes Delta.
    CE delta: +0.0 to +1.0 (positive)
    PE delta: -1.0 to -0.0 (negative)
    """
    if T <= 0:
        if option_type == "CE":
            return 1.0 if S > K else (0.5 if S == K else 0.0)
        else:
            return -1.0 if S < K else (-0.5 if S == K else 0.0)
    d1 = _d1(S, K, T, r, sigma)
    if option_type == "CE":
        return norm.cdf(d1)
    return norm.cdf(d1) - 1.0


def gamma(S, K, T, r, sigma):
    """Black-Scholes Gamma (same for Call and Put)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))


def theta(S, K, T, r, sigma, option_type="CE"):
    """Black-Scholes Theta (daily, in points per day)."""
    if T <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    d2 = _d2(S, K, T, r, sigma)
    common = -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
    if option_type == "CE":
        return (common - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365.0
    return (common + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365.0


def vega(S, K, T, r, sigma):
    """Black-Scholes Vega (per 1% IV change, same for Call and Put)."""
    if T <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    return S * norm.pdf(d1) * math.sqrt(T) / 100.0


def all_greeks(S, K, T, r, sigma, option_type="CE"):
    """Calculate all Greeks at once. Returns dict."""
    return {
        "delta": delta(S, K, T, r, sigma, option_type),
        "gamma": gamma(S, K, T, r, sigma),
        "theta": theta(S, K, T, r, sigma, option_type),
        "vega": vega(S, K, T, r, sigma),
    }


# ═══════════════════════════════════════════════════════════════
# IMPLIED VOLATILITY (Newton-Raphson)
# ═══════════════════════════════════════════════════════════════

def implied_volatility(market_price, S, K, T, r, option_type="CE",
                       max_iter=100, tol=0.001):
    """
    Calculate Implied Volatility using Newton-Raphson iteration.
    Returns IV as decimal (e.g., 0.15 = 15%).
    """
    if T <= 0 or market_price <= 0:
        return 0.0

    # Intrinsic value check
    intrinsic = max(S - K, 0) if option_type == "CE" else max(K - S, 0)
    if market_price < intrinsic:
        market_price = intrinsic + 0.01

    sigma = 0.20  # Initial guess: 20% IV

    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        v = vega(S, K, T, r, sigma) * 100  # Raw vega (not per-1%)

        if abs(v) < 1e-12:
            break

        diff = market_price - price
        if abs(diff) < tol:
            return sigma

        sigma += diff / v
        sigma = max(0.005, min(sigma, 5.0))  # Clamp between 0.5% and 500%

    return sigma


# ═══════════════════════════════════════════════════════════════
# STRIKE SELECTION AT TARGET DELTA
# ═══════════════════════════════════════════════════════════════

def find_strike_at_delta(strikes_with_premiums, spot, target_delta,
                         option_type, T, r=None):
    """
    Find the strike whose |delta| is mathematically closest to target_delta (e.g. 0.15).

    Args:
        strikes_with_premiums: dict {strike_price: market_premium}
        spot: current Nifty spot price
        target_delta: absolute delta target (e.g., 0.15 for short, 0.05 for hedge)
        option_type: "CE" or "PE"
        T: time to expiry in years
        r: risk-free rate (uses config default if None)

    Returns:
        (strike, abs_delta, iv, premium) or (None, None, None, None)
    """
    if r is None:
        r = config.RISK_FREE_RATE

    if T <= 0:
        T = 0.0001

    # 1. Estimate ATM IV from available near-ATM premiums
    atm_strike = round(spot / 50.0) * 50.0
    atm_ivs = []
    if strikes_with_premiums:
        for strk, prem in strikes_with_premiums.items():
            if abs(float(strk) - atm_strike) <= 400 and prem > 1.0:
                iv_est = implied_volatility(prem, spot, float(strk), T, r, option_type)
                if 0.05 <= iv_est <= 0.80:
                    atm_ivs.append(iv_est)

    avg_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else 0.12

    # 2. Build full strike range (Nifty 50-point step)
    min_strike = max(50.0, round((spot - 2500.0) / 50.0) * 50.0)
    max_strike = round((spot + 2500.0) / 50.0) * 50.0

    all_candidate_strikes = set()
    if strikes_with_premiums:
        for s in strikes_with_premiums.keys():
            all_candidate_strikes.add(float(s))

    s_curr = min_strike
    while s_curr <= max_strike:
        all_candidate_strikes.add(float(s_curr))
        s_curr += 50.0

    candidate_strikes = sorted(list(all_candidate_strikes))

    best_strike = None
    best_diff = float("inf")
    best_delta = None
    best_iv = None
    best_premium = None

    for strike in candidate_strikes:
        strike_f = float(strike)

        # For OTM targets (delta <= 0.45), filter out deep ITM strikes to avoid bad quote anomalies
        if target_delta <= 0.45:
            if option_type == "CE" and strike_f < (spot - 150):
                continue
            elif option_type == "PE" and strike_f > (spot + 150):
                continue

        prem = None
        if strikes_with_premiums:
            prem = strikes_with_premiums.get(strike_f)
            if prem is None:
                prem = strikes_with_premiums.get(int(strike_f))

        iv = None
        if prem is not None and prem > 0.10:
            calc_iv = implied_volatility(prem, spot, strike_f, T, r, option_type)
            if 0.01 <= calc_iv <= 1.5:
                iv = calc_iv

        if iv is None:
            iv = avg_iv
            if prem is None or prem <= 0:
                prem = bs_price(spot, strike_f, T, r, iv, option_type)

        d = abs(delta(spot, strike_f, T, r, iv, option_type))
        diff = abs(d - target_delta)

        if diff < best_diff:
            best_diff = diff
            best_strike = strike_f
            best_delta = d
            best_iv = iv
            best_premium = prem

    return best_strike, best_delta, best_iv, best_premium



def calculate_portfolio_greeks(legs, spot, T, r=None):
    """
    Calculate net portfolio Greeks from all open legs.

    Args:
        legs: list of leg dicts with keys: strike, option_type, current_premium,
              is_hedge, status, leg_type
        spot: current spot price
        T: time to expiry in years
        r: risk-free rate

    Returns:
        dict with net_delta, net_gamma, net_theta, net_vega and per-leg greeks
    """
    if r is None:
        r = config.RISK_FREE_RATE

    net = {"net_delta": 0.0, "net_gamma": 0.0, "net_theta": 0.0, "net_vega": 0.0}
    leg_greeks = []

    for leg in legs:
        if leg.get("status") != "OPEN":
            continue

        strike = float(leg["strike"])
        opt_type = leg["option_type"]
        premium = leg.get("current_premium", leg["entry_premium"])

        iv = implied_volatility(premium, spot, strike, T, r, opt_type)
        greeks = all_greeks(spot, strike, T, r, iv, opt_type)

        # Determine sign: SHORT legs have inverted Greeks
        is_short = leg["leg_type"].startswith("SHORT")
        multiplier = -1.0 if is_short else 1.0

        leg_info = {
            "leg_id": leg.get("id", ""),
            "leg_type": leg["leg_type"],
            "strike": strike,
            "option_type": opt_type,
            "iv": iv,
            "delta": greeks["delta"] * multiplier,
            "gamma": greeks["gamma"] * multiplier,
            "theta": greeks["theta"] * multiplier,
            "vega": greeks["vega"] * multiplier,
            "raw_delta": greeks["delta"],
        }
        leg_greeks.append(leg_info)

        net["net_delta"] += leg_info["delta"]
        net["net_gamma"] += leg_info["gamma"]
        net["net_theta"] += leg_info["theta"]
        net["net_vega"] += leg_info["vega"]

    net["leg_greeks"] = leg_greeks
    return net
