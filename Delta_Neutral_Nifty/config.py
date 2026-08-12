"""
Delta Neutral Nifty - Strategy Configuration
All parameters for the Iron Condor with Dynamic Adjustments.
Tuned to institutional-grade (Jane Street-inspired) standards.
"""

# ═══════════════════════════════════════════════════════════════
# NIFTY CONTRACT SPECIFICATIONS
# ═══════════════════════════════════════════════════════════════
LOT_SIZE = 65                      # Shares per lot
NUM_LOTS = 1                       # Lots per entry
STRIKE_GAP = 50                    # Nifty options strike interval
CYCLE_START_DAY = 2                # Wednesday (Cycle entry at 09:18 IST)
WEEKLY_EXPIRY_DAY = 1              # Tuesday (Weekly expiry at 15:15 IST)

# Nifty Spot Token (Angel One)
NIFTY_SPOT_TOKEN = "99926000"
NIFTY_SPOT_EXCHANGE = "NSE"
NIFTY_OPTIONS_EXCHANGE = "NFO"
NIFTY_SYMBOL = "NIFTY"

# ═══════════════════════════════════════════════════════════════
# DYNAMIC CAPITAL & RISK MANAGEMENT (INSTITUTIONAL GRADE)
# ═══════════════════════════════════════════════════════════════
# Stop Loss is dynamically calculated on the ACTUAL Deployed Capital (Utilized Margin)
STRADDLE_CAPITAL_SL_PCT = 0.02         # Strict 2.0% of dynamic deployed capital (e.g. 2% on utilized margin)
STRADDLE_PROFIT_DECAY_PCT = 0.70       # 70% combined straddle premium decay for profit booking
OTM_FULL_DECAY_PRICE = 1.0             # Exit when both short legs decay below Rs.1.00 (Full OTM capture)

# Dynamic Margin Estimation Fallbacks (used in paper mode or when API RMS is unavailable)
DEFAULT_MARGIN_PER_LOT_IC = 65000.0    # Approximate exchange margin for 4-leg hedged Iron Condor
DEFAULT_MARGIN_PER_LOT_STRADDLE = 95000.0 # Approximate exchange margin for 4-leg Straddle

# ═══════════════════════════════════════════════════════════════
# STRATEGY ENTRY PARAMETERS
# ═══════════════════════════════════════════════════════════════
ENTRY_DELTA = 0.15                     # Short strangle legs (sell at this delta / nearest)
HEDGE_DELTA = 0.05                     # Protective wings (buy at this delta / nearest)

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
# - Option chain LTP refresh: every 5 seconds via REST
# - Greeks recalculation: on every LTP refresh
# - Adjustment decision: on every Greeks refresh
OPTION_CHAIN_REFRESH_SECONDS = 2   # Refresh option chain every 2s to strictly comply with Angel REST limits
ADJUSTMENT_CHECK_SECONDS = 1.0     # Check triggers & spot every 1.0s (strictly 1 req/sec max)
WEBSOCKET_RECONNECT_SECONDS = 3    # Auto-reconnect WebSocket if dropped
FALLBACK_POLL_SECONDS = 3          # REST polling if WebSocket fails

# ═══════════════════════════════════════════════════════════════
# MARKET HOURS (IST)
# ═══════════════════════════════════════════════════════════════
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 41

# Wait after market open for initial volatility to settle
ENTRY_DELAY_MINUTES = 3            # Enter at 09:18 (3 mins after open)

# Stop new entries this many minutes before close
CLOSE_BUFFER_MINUTES = 15          # No new entries after 15:15

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
