"""
Delta Neutral Nifty - Strategy Configuration
All parameters for the Iron Condor with Dynamic Adjustments.
Tuned to institutional-grade (Jane Street-inspired) standards.
"""
import os

# ═══════════════════════════════════════════════════════════════
# NIFTY CONTRACT SPECIFICATIONS
# ═══════════════════════════════════════════════════════════════
LOT_SIZE = 65                      # Shares per lot
NUM_LOTS = 1                       # Lots per entry
STRIKE_GAP = 50                    # Nifty options strike interval
CYCLE_START_DAY = 2                # Wednesday (0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri) - Official cycle start at 09:18 IST
WEEKLY_EXPIRY_DAY = 1              # Tuesday (Weekly expiry auto-squareoff at 15:15 IST)

# Nifty Spot Token (Angel One)
NIFTY_SPOT_TOKEN = "99926000"
NIFTY_SPOT_EXCHANGE = "NSE"
NIFTY_OPTIONS_EXCHANGE = "NFO"
NIFTY_SYMBOL = "NIFTY"

# ═══════════════════════════════════════════════════════════════
# DYNAMIC CAPITAL & RISK MANAGEMENT (INSTITUTIONAL GRADE)
# ═══════════════════════════════════════════════════════════════
# Total allocated capital per lot (Standard institutional sizing: Peak margin + drawdown buffer)
ALLOCATED_CAPITAL_PER_LOT = 200000.0   # Rs. 2,00,000 per lot base for mathematical % scaling
STRADDLE_CAPITAL_SL_PCT = 0.02         # Strict 2.0% stop loss on dynamic capital base
STRADDLE_PROFIT_DECAY_PCT = 0.70       # 70% combined straddle premium decay for profit booking
OTM_FULL_DECAY_PRICE = 1.0             # Exit when both short legs decay below Rs.1.00 (Full OTM capture)

# Dynamic Margin Estimation Fallbacks (used in paper mode or when API RMS is unavailable)
DEFAULT_MARGIN_PER_LOT_IC = 90000.0        # Exchange margin for hedged positions (Iron Condor / Iron Fly)
DEFAULT_MARGIN_PER_LOT_NAKED = 167000.0    # Exchange margin for unhedged / naked strangle
DEFAULT_MARGIN_PER_LOT_STRADDLE = 167000.0 # Exchange margin for naked straddle

# ═══════════════════════════════════════════════════════════════
# STRADDLE INFINITY ARMOR & COMPENSATOR ENGINE
# ═══════════════════════════════════════════════════════════════
STRADDLE_ARMOR_ENABLED = True          # Enable autonomous active counter-adjustments at Straddle
STRADDLE_ARMOR_WING_PTS = 100          # 100-point fixed wing offset (K + 100 CE, K - 100 PE)
STRADDLE_RATIO_TESTED_LOTS = 2         # 2 lots on tested wing (1x2 Ratio Backspread)
STRADDLE_RATIO_UNTESTED_LOTS = 1       # 1 lot on untested wing (protective armor)
STRADDLE_ARMOR_PROFIT_ROI_PCT = 0.02   # 2.0% ROI on capital base for taking full profit in Armor mode

# ═══════════════════════════════════════════════════════════════
# STRATEGY ENTRY & DAYTIME EXECUTION
# ═══════════════════════════════════════════════════════════════
ENTRY_DELTA = 0.15                     # Short strangle legs (sell at this delta / nearest)
DAYTIME_HEDGES_ENABLED = False         # False = Pure Short Strangle during day (100% Theta decay)
HEDGE_DELTA = 0.05                     # Fallback wing delta if daytime hedges are ever toggled on

# ═══════════════════════════════════════════════════════════════
# OVERNIGHT SHIELD (FAT-TAIL GAP PROTECTION - 100 PT WING)
# ═══════════════════════════════════════════════════════════════
# Buys dynamic 100-point OTM Call and Put hedges from active open short strikes:
# - Expiry Days: Bought at 15:29 IST (1 min before 15:30 close)
# - Non-Expiry Days: Bought at 15:44 IST (1 min before 15:45 close)
# - Next Morning 09:15 IST:
#   * If any short leg breached ITM: Hedges retained (Iron Condor armor active)
#     to cap max loss (~Rs.4,940); position held for 2 hours before clean exit.
#   * If both short legs remain OTM: Hedges unwound dot 09:15 IST; resumes pure strangle.
OVERNIGHT_SHIELD_ENABLED = True
OVERNIGHT_SHIELD_WING_PTS = 100         # 100-point gap from active open short strikes (Call+100, Put-100)
OVERNIGHT_SHIELD_QTY_MULTIPLIER = 1.0   # 1.0x quantity (equal to short legs, e.g. 65 shares per lot)

# SEBI CAS / Exchange Closing Timings for Overnight Shield:
SHIELD_BUY_HOUR_EXPIRY = 15
SHIELD_BUY_MINUTE_EXPIRY = 29          # 15:29 IST on weekly/monthly expiry days
SHIELD_BUY_HOUR_NON_EXPIRY = 15
SHIELD_BUY_MINUTE_NON_EXPIRY = 39      # 15:39 IST (exactly 1 min before 15:40 F&O close)

# Morning Evaluation & Unwind:
OVERNIGHT_SHIELD_SELL_HOUR = 9
OVERNIGHT_SHIELD_SELL_MINUTE = 15      # Dot 09:15 IST morning opening check
GAP_BREACH_COMPENSATOR_ENABLED = True  # If short leg breached ITM, deploy active Scenario 2.B Credit Shift & Harvest

# ═══════════════════════════════════════════════════════════════
# DYNAMIC ADJUSTMENT TRIGGERS (ACTIVE IN NON-STRADDLE PHASE)
# ═══════════════════════════════════════════════════════════════
# Single Institutional Trigger: Losing short leg premium surges by >= 50% from baseline
LOSING_PREMIUM_SURGE_PCT = 0.50        # 50% expansion on losing short leg from baseline

# Straddle Convergence Setting
STRADDLE_PROXIMITY_PTS = 0             # 0 = exact same strike required (Call Strike == Put Strike)
STRADDLE_SPOT_SL_PCT = 0.0125          # Exit if Nifty moves >= 1.25% from Straddle Strike

# ═══════════════════════════════════════════════════════════════
# MONITORING FREQUENCY (INSTITUTIONAL GRADE)
# ═══════════════════════════════════════════════════════════════
# Jane Street operates tick-by-tick. With retail API constraints:
# - WebSocket for spot price: real-time (sub-second)
# - Option chain LTP refresh: every 2 seconds via REST
# - Greeks recalculation: on every LTP refresh
# - Adjustment decision: on every Greeks refresh
OPTION_CHAIN_REFRESH_SECONDS = 2   # Refresh option chain every 2s to strictly comply with Angel REST limits
ADJUSTMENT_CHECK_SECONDS = 1.0     # Check triggers & spot every 1.0s (strictly 1 req/sec max)
WEBSOCKET_RECONNECT_SECONDS = 3    # Auto-reconnect WebSocket if dropped
FALLBACK_POLL_SECONDS = 3          # REST polling if WebSocket fails

# ═══════════════════════════════════════════════════════════════
# MARKET HOURS (IST) - NSE F&O EXTENDED CAS TIMINGS (15:40 CLOSE)
# ═══════════════════════════════════════════════════════════════
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15

# Dynamic Session Close (Expiry vs Non-Expiry):
MARKET_CLOSE_HOUR_EXPIRY = 15
MARKET_CLOSE_MINUTE_EXPIRY = 30        # 15:30 IST on expiry day
MARKET_CLOSE_HOUR_NON_EXPIRY = 15
MARKET_CLOSE_MINUTE_NON_EXPIRY = 40    # 15:40 IST on non-expiry day (NSE Equity Derivatives Close)

# Fallback environment overrides
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", 15))
MARKET_CLOSE_MINUTE = int(os.getenv("MARKET_CLOSE_MINUTE", 40))

# Wait after market open for initial volatility to settle
ENTRY_DELAY_MINUTES = 3            # Enter at 09:18 (3 mins after open)

# Stop new entries this many minutes before close
CLOSE_BUFFER_MINUTES = 15          # No new entries after 15:15 / 15:30

# ═══════════════════════════════════════════════════════════════
# BLACK-SCHOLES MODEL PARAMETERS
# ═══════════════════════════════════════════════════════════════
RISK_FREE_RATE = 0.065             # India 10Y bond / RBI repo rate (~6.5%)
DIVIDEND_YIELD = 0.012             # Nifty approximate dividend yield (~1.2%)

# ═══════════════════════════════════════════════════════════════
# FILE PATHS
# ═══════════════════════════════════════════════════════════════
POSITION_FILE = "positions.json"
TRADE_LOG_FILE = "trade_log.csv"
INSTRUMENT_MASTER_CACHE = "instrument_master.json"
LOG_DIR = "logs"

# ═══════════════════════════════════════════════════════════════
# INSTRUMENT MASTER URL (Angel One)
# ═══════════════════════════════════════════════════════════════
INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
