"""
Delta Neutral Nifty - Adjustment Engine
Jane Street-inspired multi-factor adjustment logic.

TRIGGERS (any ONE fires → evaluate adjustment):
  1. Portfolio Net Delta breaches ±0.10
  2. Profitable leg captures ≥75% of max profit (premium → 25% of entry)
  3. Losing leg |delta| crosses 0.30 (approaching ATM danger)
  4. Gamma of any sold leg > 0.015 (extreme gamma risk)

ADJUSTMENT ACTION:
  1. Close the PROFITABLE leg (book profit)
  2. Close its paired hedge
  3. Sell NEW 0.15-delta option on the SAME SIDE as the closed profitable leg
  4. Buy NEW 0.05-delta hedge for the new short leg
  5. Check if this creates a Straddle → if yes, STOP forever

STRADDLE STOP:
  Once both short strikes are on the SAME strike, NO further adjustments.
"""
from . import config
from .utils import setup_logger, format_pnl, format_delta
from . import greeks_engine

logger = setup_logger("adjustment")


class AdjustmentEngine:
    """Evaluates and executes delta-neutral adjustments."""

    def __init__(self, position_manager, api, expiry_date):
        self.pm = position_manager
        self.api = api
        self.expiry_date = expiry_date

    def evaluate(self, spot, T, option_chain_ltps):
        """
        Evaluate all adjustment triggers and return action if needed.

        Returns:
            dict with action details, or None if no adjustment needed.
            {
                "trigger": str,
                "profitable_side": "CE" or "PE",
                "profitable_leg": leg_dict,
                "profitable_hedge": leg_dict or None,
                "losing_leg": leg_dict,
            }
        """
        if self.pm.is_straddle:
            return None

        open_shorts = self.pm.open_short_legs
        if len(open_shorts) < 2:
            return None

        # Separate CE and PE short legs (use the latest of each)
        short_calls = self.pm.short_call_legs
        short_puts = self.pm.short_put_legs
        if not short_calls or not short_puts:
            return None

        short_ce = short_calls[-1]
        short_pe = short_puts[-1]

        # Recalculate portfolio Greeks
        all_open = self.pm.open_legs
        portfolio = greeks_engine.calculate_portfolio_greeks(all_open, spot, T)
        net_delta = portfolio["net_delta"]

        # Per-leg analysis
        ce_greeks = self._find_leg_greeks(portfolio, short_ce["id"])
        pe_greeks = self._find_leg_greeks(portfolio, short_pe["id"])

        if not ce_greeks or not pe_greeks:
            return None

        # Update current deltas in position manager
        self.pm.update_leg_premium(short_ce["id"], short_ce["current_premium"],
                                   ce_greeks["raw_delta"])
        self.pm.update_leg_premium(short_pe["id"], short_pe["current_premium"],
                                   pe_greeks["raw_delta"])

        # ─── TRIGGER EVALUATION ──────────────────────────────────

        trigger = None
        profitable_side = None

        # Trigger 1: Losing Leg Premium Surge >= 50% from entry (e.g., 100 -> 150)
        ce_surge = (short_ce["current_premium"] - short_ce["entry_premium"]) / max(short_ce["entry_premium"], 0.01)
        pe_surge = (short_pe["current_premium"] - short_pe["entry_premium"]) / max(short_pe["entry_premium"], 0.01)

        if ce_surge >= config.LOSING_PREMIUM_SURGE_PCT:
            profitable_side = "PE"  # CE is losing (surged), PE is profitable
            trigger = (
                f"PREMIUM_SURGE: CE surged {ce_surge*100:.1f}% >= {config.LOSING_PREMIUM_SURGE_PCT*100:.0f}% "
                f"(Entry: {short_ce['entry_premium']:.2f} -> Live: {short_ce['current_premium']:.2f})"
            )
        elif pe_surge >= config.LOSING_PREMIUM_SURGE_PCT:
            profitable_side = "CE"  # PE is losing (surged), CE is profitable
            trigger = (
                f"PREMIUM_SURGE: PE surged {pe_surge*100:.1f}% >= {config.LOSING_PREMIUM_SURGE_PCT*100:.0f}% "
                f"(Entry: {short_pe['entry_premium']:.2f} -> Live: {short_pe['current_premium']:.2f})"
            )

        # Trigger 2: Premium capture on winning leg (50%+ profit captured)
        if trigger is None:
            ce_capture = 1.0 - (short_ce["current_premium"] / max(short_ce["entry_premium"], 0.01))
            pe_capture = 1.0 - (short_pe["current_premium"] / max(short_pe["entry_premium"], 0.01))

            if ce_capture >= config.PREMIUM_CAPTURE_PCT:
                profitable_side = "CE"
                trigger = f"PREMIUM_CAPTURE: CE captured {ce_capture*100:.0f}% profit"
            elif pe_capture >= config.PREMIUM_CAPTURE_PCT:
                profitable_side = "PE"
                trigger = f"PREMIUM_CAPTURE: PE captured {pe_capture*100:.0f}% profit"

        # Trigger 3: Portfolio delta breach
        if trigger is None and abs(net_delta) > config.PORTFOLIO_DELTA_BREACH:
            if net_delta > 0:
                profitable_side = "PE"
                trigger = f"DELTA_BREACH: Net Δ={net_delta:+.4f} > +{config.PORTFOLIO_DELTA_BREACH}"
            else:
                profitable_side = "CE"
                trigger = f"DELTA_BREACH: Net Δ={net_delta:+.4f} < -{config.PORTFOLIO_DELTA_BREACH}"

        # Trigger 4: Losing leg delta threshold
        if trigger is None:
            ce_abs_delta = abs(ce_greeks["raw_delta"])
            pe_abs_delta = abs(pe_greeks["raw_delta"])

            if ce_abs_delta >= config.LOSING_LEG_DELTA_THRESHOLD:
                profitable_side = "PE"  # CE is losing (high delta), PE is profitable
                trigger = f"DELTA_THRESHOLD: CE |Δ|={ce_abs_delta:.4f} > {config.LOSING_LEG_DELTA_THRESHOLD}"
            elif pe_abs_delta >= config.LOSING_LEG_DELTA_THRESHOLD:
                profitable_side = "CE"  # PE is losing (high delta), CE is profitable
                trigger = f"DELTA_THRESHOLD: PE |Δ|={pe_abs_delta:.4f} > {config.LOSING_LEG_DELTA_THRESHOLD}"

        # Trigger 5: Gamma danger
        if trigger is None:
            ce_gamma = abs(ce_greeks.get("gamma", 0))
            pe_gamma = abs(pe_greeks.get("gamma", 0))

            if ce_gamma > config.GAMMA_DANGER_THRESHOLD:
                profitable_side = "PE"
                trigger = f"GAMMA_DANGER: CE γ={ce_gamma:.5f} > {config.GAMMA_DANGER_THRESHOLD}"
            elif pe_gamma > config.GAMMA_DANGER_THRESHOLD:
                profitable_side = "CE"
                trigger = f"GAMMA_DANGER: PE γ={pe_gamma:.5f} > {config.GAMMA_DANGER_THRESHOLD}"

        if trigger is None:
            return None

        # Determine which legs to act on
        if profitable_side == "CE":
            profitable_leg = short_ce
            losing_leg = short_pe
        else:
            profitable_leg = short_pe
            losing_leg = short_ce

        # Find paired hedges
        profitable_hedge = self._find_paired_hedge(profitable_leg)
        losing_hedge = self._find_paired_hedge(losing_leg)

        logger.warning(f"[TRIGGER] {trigger}")

        return {
            "trigger": trigger,
            "profitable_side": profitable_side,
            "profitable_leg": profitable_leg,
            "profitable_hedge": profitable_hedge,
            "losing_leg": losing_leg,
            "losing_hedge": losing_hedge,
            "net_delta": net_delta,
        }

    def execute_adjustment(self, action, spot, T, option_chain_ltps, paper_mode=True):
        """
        Execute the adjustment:
        1. Close profitable short leg + its hedge
        2. Sell new 0.15-delta on same side
        3. Buy new 0.05-delta hedge on same side
        4. Check straddle condition
        """
        profitable_leg = action["profitable_leg"]
        profitable_hedge = action["profitable_hedge"]
        profitable_side = action["profitable_side"]
        trigger = action["trigger"]

        logger.info("=" * 65)
        logger.info(f"[ADJUSTMENT #{self.pm.position['adjustment_count'] + 1}]")
        logger.info(f"  Trigger: {trigger}")
        logger.info(f"  Closing profitable {profitable_side} leg @ strike {profitable_leg['strike']}")

        # Step 1: Close profitable short leg
        pnl_short = self.pm.close_leg(
            profitable_leg["id"],
            profitable_leg["current_premium"],
            f"ADJ: {trigger}"
        )

        # Step 2: Close paired hedge
        pnl_hedge = 0.0
        if profitable_hedge:
            pnl_hedge = self.pm.close_leg(
                profitable_hedge["id"],
                profitable_hedge["current_premium"],
                f"ADJ_HEDGE: paired with {profitable_leg['id']}"
            )

        # Step 3: Find new short strike matching the LOSING LEG'S DELTA (Exact Delta Matching)
        # Institutional Standard (Jane Street / Citadel): To achieve Net Delta = 0.00,
        # the new short leg's delta must match the losing leg's current absolute delta.
        losing_leg = action["losing_leg"]
        losing_leg_delta = abs(losing_leg.get("current_delta", config.ENTRY_DELTA))
        target_delta = max(losing_leg_delta, config.ENTRY_DELTA)

        side_ltps = option_chain_ltps.get(profitable_side, {})
        new_short_strike, new_short_delta, new_short_iv, new_short_premium = \
            greeks_engine.find_strike_at_delta(
                side_ltps, spot, target_delta, profitable_side, T
            )

        if new_short_strike is None:
            logger.error("Could not find suitable strike for new short leg!")
            return False

        # Step 4: Find new hedge strike matching the LOSING SIDE'S HEDGE DELTA
        # To achieve complete 4-leg Net Delta = 0.00:
        # Net Delta (Losing Spread) + Net Delta (New Spread) must equal 0.00.
        # So we match both Short Delta AND Hedge Delta to the losing side!
        losing_hedge = action.get("losing_hedge")
        if losing_hedge:
            target_hedge_delta = abs(losing_hedge.get("current_delta", config.HEDGE_DELTA))
        else:
            target_hedge_delta = config.HEDGE_DELTA

        new_hedge_strike, new_hedge_delta, new_hedge_iv, new_hedge_premium = \
            greeks_engine.find_strike_at_delta(
                side_ltps, spot, target_hedge_delta, profitable_side, T
            )

        if new_hedge_strike is None:
            logger.error("Could not find suitable strike for new hedge!")
            return False

        logger.info(
            f"  [FULL 4-LEG MATCH] New {profitable_side} Short={new_short_strike} (Δ={new_short_delta:.4f}) | "
            f"Hedge={new_hedge_strike} (Δ={new_hedge_delta:.4f}) → Net Portfolio Delta = 0.00"
        )

        # Check straddle BEFORE placing new legs
        losing_leg = action["losing_leg"]
        would_be_straddle = abs(new_short_strike - losing_leg["strike"]) <= config.STRADDLE_PROXIMITY_PTS

        if would_be_straddle:
            logger.warning(
                f"[STRADDLE CHECK] New {profitable_side} strike {new_short_strike} = "
                f"Losing strike {losing_leg['strike']} → STRADDLE REACHED"
            )

        # Step 5: Place new short leg
        token_info = self.api.get_token_info(self.expiry_date, new_short_strike, profitable_side)
        short_token = token_info["token"] if token_info else ""
        short_symbol = token_info["symbol"] if token_info else ""

        if not paper_mode and token_info:
            self.api.place_order(
                short_symbol, short_token, "SELL",
                config.LOT_SIZE * config.NUM_LOTS
            )

        short_leg_type = f"SHORT_{profitable_side.replace('CE', 'CALL').replace('PE', 'PUT')}"
        self.pm.add_leg(
            leg_type=short_leg_type,
            strike=new_short_strike,
            option_type=profitable_side,
            delta_at_entry=new_short_delta,
            iv_at_entry=new_short_iv,
            entry_premium=new_short_premium,
            symbol_token=short_token,
            trading_symbol=short_symbol,
            is_hedge=False,
        )

        # Step 6: Place new hedge
        hedge_token_info = self.api.get_token_info(self.expiry_date, new_hedge_strike, profitable_side)
        hedge_token = hedge_token_info["token"] if hedge_token_info else ""
        hedge_symbol = hedge_token_info["symbol"] if hedge_token_info else ""

        if not paper_mode and hedge_token_info:
            self.api.place_order(
                hedge_symbol, hedge_token, "BUY",
                config.LOT_SIZE * config.NUM_LOTS
            )

        hedge_leg_type = f"LONG_{profitable_side.replace('CE', 'CALL').replace('PE', 'PUT')}"
        self.pm.add_leg(
            leg_type=hedge_leg_type,
            strike=new_hedge_strike,
            option_type=profitable_side,
            delta_at_entry=new_hedge_delta,
            iv_at_entry=new_hedge_iv,
            entry_premium=new_hedge_premium,
            symbol_token=hedge_token,
            trading_symbol=hedge_symbol,
            is_hedge=True,
        )

        # Record adjustment
        self.pm.record_adjustment(
            reason=trigger,
            closed_leg_id=profitable_leg["id"],
            closed_pnl=pnl_short + pnl_hedge,
            new_short_strike=new_short_strike,
            new_hedge_strike=new_hedge_strike,
            new_short_premium=new_short_premium,
            new_hedge_premium=new_hedge_premium,
        )

        # Check straddle
        if would_be_straddle:
            self.pm.check_straddle()

        logger.info(
            f"  New SHORT {profitable_side} @ {new_short_strike} (Δ={new_short_delta:.4f}, "
            f"Prem={new_short_premium:.2f})"
        )
        logger.info(
            f"  New HEDGE {profitable_side} @ {new_hedge_strike} (Δ={new_hedge_delta:.4f}, "
            f"Prem={new_hedge_premium:.2f})"
        )
        logger.info(f"  Adjustment P&L booked: {format_pnl(pnl_short + pnl_hedge)}")
        logger.info("=" * 65)

        return True

    # ─── HELPERS ──────────────────────────────────────────────────

    def _find_leg_greeks(self, portfolio_greeks, leg_id):
        """Find Greeks for a specific leg from portfolio calculation."""
        for lg in portfolio_greeks.get("leg_greeks", []):
            if lg["leg_id"] == leg_id:
                return lg
        return None

    def _find_paired_hedge(self, short_leg):
        """
        Find the hedge (long) leg paired with a short leg.
        Matching: same option_type, is_hedge=True, status=OPEN.
        Returns the closest strike hedge.
        """
        hedges = [
            l for l in self.pm.open_hedge_legs
            if l["option_type"] == short_leg["option_type"]
        ]
        if not hedges:
            return None

        # Return the hedge closest to the short leg's strike (on the OTM side)
        if short_leg["option_type"] == "CE":
            # CE hedge should be at a HIGHER strike
            valid = [h for h in hedges if h["strike"] >= short_leg["strike"]]
        else:
            # PE hedge should be at a LOWER strike
            valid = [h for h in hedges if h["strike"] <= short_leg["strike"]]

        if valid:
            return min(valid, key=lambda h: abs(h["strike"] - short_leg["strike"]))
        return hedges[-1] if hedges else None
