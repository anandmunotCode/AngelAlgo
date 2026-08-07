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


def is_market_open(dt=None):
    """Check if given datetime is within market hours (09:15 - 15:30 IST)."""
    if dt is None:
        dt = now_ist()
    market_open = dt.replace(
        hour=config.MARKET_OPEN_HOUR,
        minute=config.MARKET_OPEN_MINUTE,
        second=0, microsecond=0
    )
    market_close = dt.replace(
        hour=config.MARKET_CLOSE_HOUR,
        minute=config.MARKET_CLOSE_MINUTE,
        second=0, microsecond=0
    )
    return market_open <= dt <= market_close


def is_entry_window(dt=None):
    """Check if within safe entry window (after delay, before close buffer)."""
    if dt is None:
        dt = now_ist()
    entry_start = dt.replace(
        hour=config.MARKET_OPEN_HOUR,
        minute=config.MARKET_OPEN_MINUTE,
        second=0, microsecond=0
    ) + timedelta(minutes=config.ENTRY_DELAY_MINUTES)

    entry_end = dt.replace(
        hour=config.MARKET_CLOSE_HOUR,
        minute=config.MARKET_CLOSE_MINUTE,
        second=0, microsecond=0
    ) - timedelta(minutes=config.CLOSE_BUFFER_MINUTES)

    return entry_start <= dt <= entry_end


def get_next_weekly_expiry(from_date=None):
    """
    Calculate the next Nifty weekly expiry date (Thursday).
    If today IS Thursday and market is still open, today is the expiry.
    If today is after Thursday, next week's Thursday.
    """
    if from_date is None:
        from_date = now_ist().date()
    elif isinstance(from_date, datetime):
        from_date = from_date.date()

    weekday = from_date.weekday()  # Mon=0, Thu=3
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
    Mon-Thu -> this Thursday. Fri-Sun -> next Thursday.
    """
    if from_date is None:
        from_date = now_ist().date()
    elif isinstance(from_date, datetime):
        from_date = from_date.date()

    weekday = from_date.weekday()

    # If it's Friday(4), Saturday(5), or Sunday(6) -> next week's Thursday
    if weekday > config.WEEKLY_EXPIRY_DAY:
        return get_next_weekly_expiry(from_date)

    # Mon(0) to Thu(3) -> this Thursday
    days_to_thu = config.WEEKLY_EXPIRY_DAY - weekday
    return from_date + timedelta(days=days_to_thu)


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
    return f"₹{value:,.2f}"


def format_delta(value):
    """Format delta for display."""
    return f"{value:+.4f}"


def format_pnl(value):
    """Format P&L with color hint."""
    sign = "+" if value >= 0 else ""
    return f"{sign}₹{value:,.2f}"


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
