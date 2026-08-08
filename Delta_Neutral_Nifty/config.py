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
WEEKLY_EXPIRY_DAY = 3              # Thursday (Mon=0, Thu=3)

# Nifty Spot Token (Angel One)
NIFTY_SPOT_TOKEN = "99926000"
NIFTY_SPOT_EXCHANGE = "NSE"
NIFTY_OPTIONS_EXCHANGE = "NFO"
NIFTY_SYMBOL = "NIFTY"

# ═══════════════════════════════════════════════════════════════
# CAPITAL & RISK MANAGEMENT (INSTITUTIONAL GRADE)
# ═══════════════════════════════════════════════════════════════
CAPITAL_PER_LOT = 100000               # Total deployed capital per lot in INR (₹1,00,000)
STRADDLE_CAPITAL_SL_PCT = 0.02         # Strict 2.0% capital Stop Loss (₹2,000 per lot)
STRADDLE_PROFIT_DECAY_PCT = 0.70       # 70% combined straddle premium decay for profit booking
OTM_FULL_DECAY_PRICE = 1.0             # Exit when both short legs decay below ₹1.00 (Full OTM capture)

# ═══════════════════════════════════════════════════════════════
# STRATEGY ENTRY PARAMETERS
# ═══════════════════════════════════════════════════════════════
ENTRY_DELTA = 0.15                     # Short strangle legs (sell at this delta / nearest)
HEDGE_DELTA = 0.05                     # Protective wings (buy at this delta / nearest)

# ═══════════════════════════════════════════════════════════════
# DYNAMIC ADJUSTMENT TRIGGERS (ACTIVE IN NON-STRADDLE PHASE)
# ═══════════════════════════════════════════════════════════════
# Primary Trigger: Losing leg premium surges by >= 50% from entry (e.g. 100 -> 150)
LOSING_PREMIUM_SURGE_PCT = 0.50        # 50% expansion on losing leg
PREMIUM_CAPTURE_PCT = 0.50             # Profitable leg captured 50%+ profit
PORTFOLIO_DELTA_BREACH = 0.10          # |Net Delta| > 0.10 rebalance
LOSING_LEG_DELTA_THRESHOLD = 0.30      # Losing leg |delta| > 0.30
GAMMA_DANGER_THRESHOLD = 0.015         # High gamma risk trigger

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
MARKET_CLOSE_MINUTE = 30

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
PAPER_TRADES_FILE = "paper_trades_log.csv"
INSTRUMENT_MASTER_CACHE = "instrument_master.json"
LOG_DIR = "logs"

# ═══════════════════════════════════════════════════════════════
# INSTRUMENT MASTER URL (Angel One)
# ═══════════════════════════════════════════════════════════════
INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
