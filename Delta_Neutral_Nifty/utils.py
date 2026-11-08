"""
Delta Neutral Nifty - Utility Functions
Logging, timezone, expiry calculation, display helpers.
"""
import os
import logging
from datetime import datetime, timedelta, date, time as dt_time
from zoneinfo import ZoneInfo

from . import config

IST = ZoneInfo("Asia/Kolkata")


def setup_logger(name="delta_neutral", log_to_file=True):
    """Setup structured logger with console + optional file output."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler
    if log_to_file:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        today_str = now_ist().strftime("%Y-%m-%d")
        fh = logging.FileHandler(
            os.path.join(config.LOG_DIR, f"delta_neutral_{today_str}.log"),
            encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def now_ist():
    """Get current datetime in IST."""
    return datetime.now(IST)


def is_muhurat_trading_day(check_date=None):
    """Check if given date is an official Muhurat Trading day."""
    if check_date is None:
        check_date = now_ist().date()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()
    return check_date in getattr(config, "MUHURAT_TRADING_DATES", {})


def get_market_open_time(check_date=None):
    """
    Get (hour, minute) for market open based on NSE rules:
    - Muhurat trading: configured open time (e.g. 14:20 IST)
    - Normal trading day: 09:15 IST
    """
    if check_date is None:
        check_date = now_ist().date()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    if is_muhurat_trading_day(check_date):
        schedule = config.MUHURAT_TRADING_DATES[check_date]
        return (schedule.get("open_hour", 14), schedule.get("open_minute", 20))

    return (config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE)


def get_market_close_time(check_date=None):
    """
    Get (hour, minute) for market close based on NSE rules:
    - Muhurat trading: configured close time (e.g. 15:40 IST)
    - Expiry day: 15:30 IST
    - Non-expiry day: 15:40 IST (NSE Equity Derivatives Session Close)
    """
    if check_date is None:
        check_date = now_ist().date()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    if is_muhurat_trading_day(check_date):
        schedule = config.MUHURAT_TRADING_DATES[check_date]
        return (schedule.get("close_hour", 15), schedule.get("close_minute", 40))

    if is_expiry_day(check_date):
        return (config.MARKET_CLOSE_HOUR_EXPIRY, config.MARKET_CLOSE_MINUTE_EXPIRY)
    return (config.MARKET_CLOSE_HOUR_NON_EXPIRY, config.MARKET_CLOSE_MINUTE_NON_EXPIRY)


def get_shield_buy_time(check_date=None):
    """
    Get (hour, minute) for deploying overnight shield:
    - Muhurat trading: configured shield time (e.g. 15:39 IST)
    - Expiry day: 15:29 IST (1 min before 15:30 close)
    - Non-expiry day: 15:39 IST (exactly 1 min before 15:40 F&O close)
    """
    if check_date is None:
        check_date = now_ist().date()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()

    if is_muhurat_trading_day(check_date):
        schedule = config.MUHURAT_TRADING_DATES[check_date]
        return (schedule.get("shield_hour", 15), schedule.get("shield_minute", 39))

    if is_expiry_day(check_date):
        return (config.SHIELD_BUY_HOUR_EXPIRY, config.SHIELD_BUY_MINUTE_EXPIRY)
    return (config.SHIELD_BUY_HOUR_NON_EXPIRY, config.SHIELD_BUY_MINUTE_NON_EXPIRY)


def is_market_open(dt=None):
    """Check if given datetime is within market hours (handles Muhurat session and regular days)."""
    if dt is None:
        dt = now_ist()

    # Check Muhurat Trading day (operates on weekend/holidays)
    if is_muhurat_trading_day(dt.date()):
        open_h, open_m = get_market_open_time(dt.date())
        close_h, close_m = get_market_close_time(dt.date())
        market_open = dt.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        market_close = dt.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        return market_open <= dt <= market_close

    if dt.weekday() >= 5:
        return False

    open_h, open_m = get_market_open_time(dt.date())
    close_h, close_m = get_market_close_time(dt.date())
    market_open = dt.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    market_close = dt.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return market_open <= dt <= market_close


def is_entry_window(dt=None):
    """Check if within safe entry window (after delay, before close buffer)."""
    if dt is None:
        dt = now_ist()
    open_h, open_m = get_market_open_time(dt.date())
    close_h, close_m = get_market_close_time(dt.date())
    entry_start = dt.replace(
        hour=open_h,
        minute=open_m,
        second=0, microsecond=0
    ) + timedelta(minutes=config.ENTRY_DELAY_MINUTES)

    entry_end = dt.replace(
        hour=close_h,
        minute=close_m,
        second=0, microsecond=0
    ) - timedelta(minutes=config.CLOSE_BUFFER_MINUTES)

    return entry_start <= dt <= entry_end


def get_next_weekly_expiry(from_date=None):
    """
    Calculate the next Nifty weekly expiry date (Tuesday).
    If today IS Tuesday and market is still open, today is the expiry.
    If today is after Tuesday, next week's Tuesday.
    """
    if from_date is None:
        from_date = now_ist().date()
    elif isinstance(from_date, datetime):
        from_date = from_date.date()

    weekday = from_date.weekday()
    expiry_day = config.WEEKLY_EXPIRY_DAY

    if weekday <= expiry_day:
        days_ahead = expiry_day - weekday
    else:
        days_ahead = 7 - weekday + expiry_day

    expiry = from_date + timedelta(days=days_ahead)
    return expiry


def get_current_expiry(from_date=None):
    """
    Get the expiry date for the current trading week.
    Mon-Tue -> this Tuesday. Wed-Sun -> next Tuesday.
    """
    if from_date is None:
        from_date = now_ist().date()
    elif isinstance(from_date, datetime):
        from_date = from_date.date()

    weekday = from_date.weekday()

    # If it's Wednesday through Sunday -> next week's Tuesday
    if weekday > config.WEEKLY_EXPIRY_DAY:
        return get_next_weekly_expiry(from_date)

    # Monday or Tuesday -> this Tuesday
    days_to_expiry = config.WEEKLY_EXPIRY_DAY - weekday
    return from_date + timedelta(days=days_to_expiry)


def is_expiry_day(check_date=None):
    """Check if given date is a weekly expiry day."""
    if check_date is None:
        check_date = now_ist().date()
    elif isinstance(check_date, datetime):
        check_date = check_date.date()
    return check_date.weekday() == config.WEEKLY_EXPIRY_DAY


def time_to_expiry_years(expiry_date, current_dt=None):
    """
    Calculate time to expiry in years for Black-Scholes.
    Expiry is at 15:30 IST on expiry day.
    """
    if current_dt is None:
        current_dt = now_ist()

    if isinstance(expiry_date, date) and not isinstance(expiry_date, datetime):
        expiry_dt = datetime.combine(
            expiry_date, dt_time(15, 30), tzinfo=IST
        )
    else:
        expiry_dt = expiry_date

    diff_seconds = (expiry_dt - current_dt).total_seconds()
    if diff_seconds <= 0:
        return 0.0

    return diff_seconds / (365.25 * 24 * 3600)


def get_atm_strike(spot_price):
    """Round spot price to nearest Nifty strike (multiple of STRIKE_GAP)."""
    return int(round(spot_price / config.STRIKE_GAP) * config.STRIKE_GAP)


def format_premium(value):
    """Format premium for display."""
    return f"Rs.{value:,.2f}"


def format_delta(value):
    """Format delta for display."""
    return f"{value:+.4f}"


def format_pnl(value):
    """Format P&L with color hint."""
    sign = "+" if value >= 0 else ""
    return f"{sign}Rs.{value:,.2f}"


def print_banner(text, char="=", width=70):
    """Print formatted banner safely across all OS console encodings."""
    try:
        print(f"\n{char * width}")
        print(f"  {text}")
        print(f"{char * width}")
    except UnicodeEncodeError:
        safe_text = text.encode("ascii", "ignore").decode("ascii")
        print(f"\n{'=' * width}")
        print(f"  {safe_text}")
        print(f"{'=' * width}")


def print_position_table(legs):
    """Print positions in a formatted table."""
    if not legs:
        print("  No open positions.")
        return

    header = f"  {'Type':<14} {'Strike':>8} {'Entry':>8} {'Current':>8} {'P&L':>10} {'Delta':>8} {'Status':<8}"
    print(header)
    print("  " + "-" * 72)

    for leg in legs:
        pnl = 0.0
        if leg.get("status") == "OPEN":
            if leg.get("is_hedge"):
                pnl = (leg["current_premium"] - leg["entry_premium"]) * config.LOT_SIZE
            else:
                pnl = (leg["entry_premium"] - leg["current_premium"]) * config.LOT_SIZE

        print(
            f"  {leg['leg_type']:<14} "
            f"{leg['strike']:>8.0f} "
            f"{leg['entry_premium']:>8.2f} "
            f"{leg.get('current_premium', 0):>8.2f} "
            f"{format_pnl(pnl):>10} "
            f"{leg.get('current_delta', 0):>+8.4f} "
            f"{leg['status']:<8}"
        )
