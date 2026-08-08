"""
Delta Neutral Nifty - Position Manager
Tracks all legs, adjustment history, P&L, and persistence.
"""
import os
import json
import csv
import uuid
from datetime import datetime

from . import config
from .utils import setup_logger, now_ist, format_pnl

logger = setup_logger("positions")


class PositionManager:
    """Manages all open and closed legs for the weekly Iron Condor."""

    def __init__(self, position_file=None, trade_log_file=None):
        self.position_file = position_file or config.POSITION_FILE
        self.trade_log_file = trade_log_file or config.TRADE_LOG_FILE
        self.position = self._empty_position()
        self._load()

    def _empty_position(self):
        return {
            "expiry_date": "",
            "initial_entry_time": "",
            "is_straddle_reached": False,
            "adjustment_count": 0,
            "total_premium_collected": 0.0,
            "total_premium_paid": 0.0,
            "status": "PENDING",
            "legs": [],
            "adjustments": [],
        }

    # ─── PERSISTENCE ──────────────────────────────────────────────

    def _load(self):
        """Load position from disk."""
        if os.path.exists(self.position_file):
            try:
                with open(self.position_file, "r", encoding="utf-8") as f:
                    self.position = json.load(f)
                logger.info(f"Loaded position: {self.position['status']} | "
                            f"{len(self.open_legs)} open legs")
            except (json.JSONDecodeError, KeyError):
                logger.warning("Corrupt position file, starting fresh")
                self.position = self._empty_position()

    def save(self):
        """Save position to disk."""
        with open(self.position_file, "w", encoding="utf-8") as f:
            json.dump(self.position, f, indent=2, default=str)

    # ─── LEG MANAGEMENT ──────────────────────────────────────────

    def add_leg(self, leg_type, strike, option_type, delta_at_entry, iv_at_entry,
                entry_premium, symbol_token, trading_symbol, is_hedge=False):
        """
        Add a new leg to the position.
        leg_type: SHORT_CALL, SHORT_PUT, LONG_CALL, LONG_PUT
        """
        leg = {
            "id": str(uuid.uuid4())[:8],
            "leg_type": leg_type,
            "strike": float(strike),
            "option_type": option_type,
            "delta_at_entry": float(delta_at_entry),
            "iv_at_entry": float(iv_at_entry),
            "entry_premium": float(entry_premium),
            "current_premium": float(entry_premium),
            "current_delta": float(delta_at_entry),
            "entry_time": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol_token": symbol_token,
            "trading_symbol": trading_symbol,
            "status": "OPEN",
            "is_hedge": is_hedge,
            "exit_premium": 0.0,
            "exit_time": "",
            "pnl": 0.0,
        }
        self.position["legs"].append(leg)

        if is_hedge:
            self.position["total_premium_paid"] += entry_premium
        else:
            self.position["total_premium_collected"] += entry_premium

        logger.info(
            f"[+LEG] {leg_type} {option_type} @ {strike} | "
            f"Δ={delta_at_entry:+.4f} | Premium={entry_premium:.2f} | "
            f"{'HEDGE' if is_hedge else 'SHORT'}"
        )
        self.save()
        return leg["id"]

    def close_leg(self, leg_id, exit_premium, reason=""):
        """Close a specific leg and calculate P&L."""
        for leg in self.position["legs"]:
            if leg["id"] == leg_id and leg["status"] == "OPEN":
                leg["status"] = "CLOSED"
                leg["exit_premium"] = float(exit_premium)
                leg["exit_time"] = now_ist().strftime("%Y-%m-%d %H:%M:%S")

                # P&L calculation
                if leg["is_hedge"]:
                    # Long leg: profit = exit - entry
                    leg["pnl"] = (exit_premium - leg["entry_premium"]) * config.LOT_SIZE * config.NUM_LOTS
                else:
                    # Short leg: profit = entry - exit
                    leg["pnl"] = (leg["entry_premium"] - exit_premium) * config.LOT_SIZE * config.NUM_LOTS

                logger.info(
                    f"[-LEG] CLOSED {leg['leg_type']} {leg['option_type']} @ {leg['strike']} | "
                    f"Entry={leg['entry_premium']:.2f} -> Exit={exit_premium:.2f} | "
                    f"P&L={format_pnl(leg['pnl'])} | Reason: {reason}"
                )
                self._log_trade(leg, reason)
                self.save()
                return leg["pnl"]

        logger.warning(f"Leg {leg_id} not found or already closed")
        return 0.0

    def update_leg_premium(self, leg_id, current_premium, current_delta=None):
        """Update live premium and delta for a leg."""
        for leg in self.position["legs"]:
            if leg["id"] == leg_id and leg["status"] == "OPEN":
                leg["current_premium"] = float(current_premium)
                if current_delta is not None:
                    leg["current_delta"] = float(current_delta)
                return

    def update_live_greeks(self, portfolio, spot):
        """Update position state with live portfolio greeks for dashboard streaming."""
        self.position["spot_price"] = float(spot)
        self.position["net_delta"] = float(portfolio.get("net_delta", 0.0))
        self.position["net_gamma"] = float(portfolio.get("net_gamma", 0.0))
        self.position["net_theta"] = float(portfolio.get("net_theta", 0.0))
        self.position["net_vega"] = float(portfolio.get("net_vega", 0.0))
        # Update individual leg deltas
        for leg_info in portfolio.get("leg_greeks", []):
            leg_id = leg_info.get("leg_id")
            if leg_id:
                for leg in self.position["legs"]:
                    if leg["id"] == leg_id:
                        leg["current_delta"] = abs(float(leg_info.get("raw_delta", 0)))
                        break
        self.save()

    # ─── POSITION QUERIES ─────────────────────────────────────────

    @property
    def open_legs(self):
        """Get all open legs."""
        return [l for l in self.position["legs"] if l["status"] == "OPEN"]

    @property
    def open_short_legs(self):
        """Get open short (sold) legs only."""
        return [l for l in self.open_legs if not l["is_hedge"]]

    @property
    def open_hedge_legs(self):
        """Get open hedge (bought) legs only."""
        return [l for l in self.open_legs if l["is_hedge"]]

    @property
    def short_call_legs(self):
        return [l for l in self.open_short_legs if l["option_type"] == "CE"]

    @property
    def short_put_legs(self):
        return [l for l in self.open_short_legs if l["option_type"] == "PE"]

    @property
    def is_active(self):
        return self.position["status"] in ("ACTIVE", "STRADDLE")

    @property
    def is_straddle(self):
        return self.position["is_straddle_reached"]

    def get_leg_by_id(self, leg_id):
        for leg in self.position["legs"]:
            if leg["id"] == leg_id:
                return leg
        return None

    # ─── STRADDLE DETECTION ───────────────────────────────────────

    def check_straddle(self):
        """
        Check if short legs have converged to a straddle (same strike).
        Returns True if straddle condition is met.
        """
        short_calls = self.short_call_legs
        short_puts = self.short_put_legs

        if not short_calls or not short_puts:
            return False

        call_strike = short_calls[-1]["strike"]  # Latest short call
        put_strike = short_puts[-1]["strike"]     # Latest short put

        proximity = abs(call_strike - put_strike)
        if proximity <= config.STRADDLE_PROXIMITY_PTS:
            self.position["is_straddle_reached"] = True
            self.position["status"] = "STRADDLE"
            # Record combined straddle entry premium for 70% decay target calculation
            straddle_entry_prem = short_calls[-1]["current_premium"] + short_puts[-1]["current_premium"]
            self.position["straddle_entry_combined_premium"] = straddle_entry_prem
            logger.warning(
                f"[STRADDLE REACHED] Call={call_strike} | Put={put_strike} | "
                f"Combined Premium={straddle_entry_prem:.2f} | Gap={proximity} pts → NO FURTHER ADJUSTMENTS"
            )
            self.save()
            return True
        return False

    def check_straddle_stop_loss(self, spot):
        """
        Check Stop Loss rules ONLY when position is in STRADDLE phase:
        Rule 1: Portfolio Net P&L <= - (2% of Deployed Capital) [e.g. ₹2,000 on ₹1 Lakh]
        Rule 2: Spot distance from Straddle strike >= 1.25% (Emergency Circuit Breaker)

        Returns (True, reason_str) if SL is triggered, else (False, "")
        """
        if not self.is_straddle or not self.is_active:
            return False, ""

        short_calls = self.short_call_legs
        if not short_calls:
            return False, ""

        straddle_strike = short_calls[-1]["strike"]

        # Rule 1: Strict 2% Deployed Capital Hard Stop Loss
        max_allowed_loss_inr = config.CAPITAL_PER_LOT * config.NUM_LOTS * config.STRADDLE_CAPITAL_SL_PCT
        current_total_pnl = self.total_pnl

        if current_total_pnl <= -max_allowed_loss_inr:
            reason = (
                f"STRADDLE SL [2% CAPITAL LOSS LIMIT]: Total P&L {format_pnl(current_total_pnl)} "
                f"breached 2% Capital Limit ({format_pnl(-max_allowed_loss_inr)})"
            )
            logger.warning(f"🚨 {reason}")
            return True, reason

        # Rule 2: Spot Distance Stop Loss (±1.25% from Straddle Strike)
        spot_distance_pct = abs(spot - straddle_strike) / straddle_strike
        if spot_distance_pct >= config.STRADDLE_SPOT_SL_PCT:
            reason = (
                f"STRADDLE SL [SPOT MOVE BREACH]: Spot {spot:.2f} moved {spot_distance_pct*100:.2f}% "
                f"from Straddle Strike {straddle_strike} (Limit: {config.STRADDLE_SPOT_SL_PCT*100:.2f}%)"
            )
            logger.warning(f"🚨 {reason}")
            return True, reason

        return False, ""

    def check_straddle_profit_target(self):
        """
        Check Profit Target ONLY when position is in STRADDLE phase:
        Rule: Exit all legs when combined straddle short premium decays by >= 70%
        
        Returns (True, reason_str) if profit target reached, else (False, "")
        """
        if not self.is_straddle or not self.is_active:
            return False, ""

        short_calls = self.short_call_legs
        short_puts = self.short_put_legs
        if not short_calls or not short_puts:
            return False, ""

        entry_combined = self.position.get("straddle_entry_combined_premium", 0.0)
        if entry_combined <= 0:
            entry_combined = short_calls[-1]["entry_premium"] + short_puts[-1]["entry_premium"]

        live_combined = short_calls[-1]["current_premium"] + short_puts[-1]["current_premium"]
        decay_pct = (entry_combined - live_combined) / max(entry_combined, 0.01)

        if decay_pct >= config.STRADDLE_PROFIT_DECAY_PCT:
            reason = (
                f"STRADDLE PROFIT [70% THETA DECAY]: Straddle premium decayed {decay_pct*100:.1f}% "
                f"(From {entry_combined:.2f} -> Live: {live_combined:.2f}) >= {config.STRADDLE_PROFIT_DECAY_PCT*100:.0f}%"
            )
            logger.info(f"🎯 {reason}")
            return True, reason

        return False, ""

    def check_otm_full_decay(self):
        """
        Check Full Profit Decay when position never reached Straddle (OTM phase):
        Rule: If both short CE and short PE LTPs drop below ₹1.00, full profit is collected.
        
        Returns (True, reason_str) if both short legs < ₹1.00, else (False, "")
        """
        if self.is_straddle or not self.is_active:
            return False, ""

        short_calls = self.short_call_legs
        short_puts = self.short_put_legs
        if not short_calls or not short_puts:
            return False, ""

        ce_ltp = short_calls[-1]["current_premium"]
        pe_ltp = short_puts[-1]["current_premium"]

        if ce_ltp <= config.OTM_FULL_DECAY_PRICE and pe_ltp <= config.OTM_FULL_DECAY_PRICE:
            reason = (
                f"OTM FULL DECAY [PREMIUM < Rs.{config.OTM_FULL_DECAY_PRICE:.2f}]: "
                f"Both CE ({ce_ltp:.2f}) and PE ({pe_ltp:.2f}) decayed to zero. 100% Profit Captured!"
            )
            logger.info(f"[PROFIT] {reason}")
            return True, reason

        return False, ""

    # ─── INITIALIZATION ───────────────────────────────────────────

    def initialize_week(self, expiry_date):
        """Start a fresh position for the new weekly expiry."""
        self.position = self._empty_position()
        self.position["expiry_date"] = str(expiry_date)
        self.position["initial_entry_time"] = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        self.position["status"] = "ACTIVE"
        self.save()
        logger.info(f"[NEW WEEK] Position initialized for expiry: {expiry_date}")

    def record_adjustment(self, reason, closed_leg_id, closed_pnl,
                          new_short_strike, new_hedge_strike,
                          new_short_premium, new_hedge_premium):
        """Record an adjustment event."""
        adj = {
            "time": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "closed_leg_id": closed_leg_id,
            "pnl_booked": float(closed_pnl),
            "new_short_strike": float(new_short_strike),
            "new_hedge_strike": float(new_hedge_strike),
            "new_short_premium": float(new_short_premium),
            "new_hedge_premium": float(new_hedge_premium),
        }
        self.position["adjustments"].append(adj)
        self.position["adjustment_count"] += 1
        self.save()

    def close_all(self, reason="EXPIRY"):
        """Close all remaining open legs."""
        for leg in self.open_legs:
            self.close_leg(leg["id"], leg["current_premium"], reason)
        self.position["status"] = "CLOSED"
        self.save()

    # ─── P&L ──────────────────────────────────────────────────────

    @property
    def total_realized_pnl(self):
        """Total P&L from all closed legs."""
        return sum(l["pnl"] for l in self.position["legs"] if l["status"] == "CLOSED")

    @property
    def total_unrealized_pnl(self):
        """Total unrealized P&L from open legs."""
        pnl = 0.0
        for leg in self.open_legs:
            if leg["is_hedge"]:
                pnl += (leg["current_premium"] - leg["entry_premium"]) * config.LOT_SIZE * config.NUM_LOTS
            else:
                pnl += (leg["entry_premium"] - leg["current_premium"]) * config.LOT_SIZE * config.NUM_LOTS
        return pnl

    @property
    def total_pnl(self):
        return self.total_realized_pnl + self.total_unrealized_pnl

    # ─── TRADE LOG ────────────────────────────────────────────────

    def _log_trade(self, leg, reason):
        """Append closed trade to CSV log."""
        file_exists = os.path.exists(self.trade_log_file)
        with open(self.trade_log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "expiry", "entry_time", "exit_time", "leg_type", "option_type",
                    "strike", "entry_premium", "exit_premium", "pnl_pts", "pnl_inr",
                    "is_hedge", "reason"
                ])
            writer.writerow([
                self.position["expiry_date"],
                leg["entry_time"],
                leg["exit_time"],
                leg["leg_type"],
                leg["option_type"],
                leg["strike"],
                leg["entry_premium"],
                leg["exit_premium"],
                round(leg["entry_premium"] - leg["exit_premium"], 2) if not leg["is_hedge"]
                    else round(leg["exit_premium"] - leg["entry_premium"], 2),
                round(leg["pnl"], 2),
                leg["is_hedge"],
                reason,
            ])
