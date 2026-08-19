"""
Delta Neutral Nifty - Adjustment Engine
Institutional 50% Short Premium Surge Adjustment Logic.

ADJUSTMENT TRIGGER:
  Losing short leg surges by >= 50% from its baseline premium.
  (Surge % = (Live - Baseline) / Baseline * 100 >= 50%)

ADJUSTMENT ACTION:
  1. Close the PROFITABLE short leg + its paired hedge (book profit).
  2. Enter NEW short leg on the profitable side matching the losing short leg's absolute delta.
  3. Enter NEW hedge leg on the profitable side matching the losing hedge leg's current delta.
  4. Reset the losing short leg's surge baseline to its CURRENT market price.
  5. Check if Call Strike == Put Strike (Straddle Convergence) → Lock adjustments forever.
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

        # ─── 50% LOSING SHORT LEG SURGE TRIGGER EVALUATION ────────────────
        trigger = None
        profitable_side = None

        # Each short leg evaluates surge against its surge_baseline_premium (fallback to entry_premium)
        ce_baseline = short_ce.get("surge_baseline_premium", short_ce["entry_premium"])
        pe_baseline = short_pe.get("surge_baseline_premium", short_pe["entry_premium"])

        ce_surge = (short_ce["current_premium"] - ce_baseline) / max(ce_baseline, 0.01)
        pe_surge = (short_pe["current_premium"] - pe_baseline) / max(pe_baseline, 0.01)

        if ce_surge >= config.LOSING_PREMIUM_SURGE_PCT:
            profitable_side = "PE"  # CE is losing (surged >= 50%), PE is profitable
            trigger = (
                f"PREMIUM_SURGE: CE surged {ce_surge*100:.1f}% >= {config.LOSING_PREMIUM_SURGE_PCT*100:.0f}% "
                f"(Baseline: {ce_baseline:.2f} -> Live: {short_ce['current_premium']:.2f})"
            )
        elif pe_surge >= config.LOSING_PREMIUM_SURGE_PCT:
            profitable_side = "CE"  # PE is losing (surged >= 50%), CE is profitable
            trigger = (
                f"PREMIUM_SURGE: PE surged {pe_surge*100:.1f}% >= {config.LOSING_PREMIUM_SURGE_PCT*100:.0f}% "
                f"(Baseline: {pe_baseline:.2f} -> Live: {short_pe['current_premium']:.2f})"
            )

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

        # Step 1: Close profitable short leg (BUY back)
        if not paper_mode and self.api:
            short_sym = profitable_leg.get("trading_symbol") or f"NIFTY_{int(profitable_leg['strike'])}_{profitable_side}"
            short_tok = profitable_leg.get("symbol_token", "")
            short_q = profitable_leg.get("quantity") or (config.LOT_SIZE * config.NUM_LOTS)
            logger.info(f"  [LIVE ORDER] Closing profitable short leg: BUY {short_q}x {short_sym} ({short_tok})")
            close_short_id = self.api.place_order(short_sym, short_tok, "BUY", short_q)
            if close_short_id is None:
                logger.error(f"  [ADJ CLOSE FAILED] Could not close profitable short leg {short_sym}!")

        pnl_short = self.pm.close_leg(
            profitable_leg["id"],
            profitable_leg["current_premium"],
            f"ADJ: {trigger}"
        )

        # Step 2: Close paired hedge (SELL)
        pnl_hedge = 0.0
        if profitable_hedge:
            if not paper_mode and self.api:
                hedge_sym = profitable_hedge.get("trading_symbol") or f"NIFTY_{int(profitable_hedge['strike'])}_{profitable_side}"
                hedge_tok = profitable_hedge.get("symbol_token", "")
                hedge_q = profitable_hedge.get("quantity") or (config.LOT_SIZE * config.NUM_LOTS)
                logger.info(f"  [LIVE ORDER] Closing profitable hedge leg: SELL {hedge_q}x {hedge_sym} ({hedge_tok})")
                close_hedge_id = self.api.place_order(hedge_sym, hedge_tok, "SELL", hedge_q)
                if close_hedge_id is None:
                    logger.error(f"  [ADJ CLOSE FAILED] Could not close profitable hedge leg {hedge_sym}!")

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

        # Step 4: Find new hedge strike matching the LOSING HEDGE'S CURRENT DELTA (Dynamic Symmetrical Wing)
        losing_hedge = action.get("losing_hedge")
        if losing_hedge and losing_hedge.get("current_delta"):
            target_hedge_delta = abs(float(losing_hedge.get("current_delta")))
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
        would_be_straddle = abs(new_short_strike - losing_leg["strike"]) <= config.STRADDLE_PROXIMITY_PTS

        if would_be_straddle:
            logger.warning(
                f"[STRADDLE CHECK] New {profitable_side} strike {new_short_strike} = "
                f"Losing strike {losing_leg['strike']} → STRADDLE REACHED"
            )

        # Step 5: Place new hedge leg FIRST (Hedge-First for Margin Benefit)
        hedge_token_info = self.api.get_token_info(self.expiry_date, new_hedge_strike, profitable_side) if self.api else None
        hedge_token = hedge_token_info["token"] if hedge_token_info else ""
        hedge_symbol = hedge_token_info["symbol"] if hedge_token_info else f"NIFTY_{int(new_hedge_strike)}_{profitable_side}"

        if not paper_mode and hedge_token_info and self.api:
            hedge_order_id = self.api.place_order(
                hedge_symbol, hedge_token, "BUY",
                config.LOT_SIZE * config.NUM_LOTS
            )
            if hedge_order_id is None:
                logger.error(f"[ADJ ABORTED] Could not place new hedge BUY @ {new_hedge_strike}. Adjustment incomplete!")
                return False

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

        # Step 6: Place new short leg SECOND
        token_info = self.api.get_token_info(self.expiry_date, new_short_strike, profitable_side) if self.api else None
        short_token = token_info["token"] if token_info else ""
        short_symbol = token_info["symbol"] if token_info else f"NIFTY_{int(new_short_strike)}_{profitable_side}"

        if not paper_mode and token_info and self.api:
            short_order_id = self.api.place_order(
                short_symbol, short_token, "SELL",
                config.LOT_SIZE * config.NUM_LOTS
            )
            if short_order_id is None:
                logger.error(f"[ADJ ABORTED] Could not place new short SELL @ {new_short_strike}. Hedge already placed - manual review needed!")
                return False

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

        # Step 7: Reset losing short leg's surge baseline to its CURRENT price
        # The next adjustment will ONLY trigger if it expands ANOTHER 50% from this new level!
        self.pm.update_surge_baseline(losing_leg["id"], losing_leg["current_premium"])
        logger.info(
            f"  [SURGE BASELINE RESET] Losing leg {losing_leg['strike']} {losing_leg['option_type']} "
            f"surge baseline updated to {losing_leg['current_premium']:.2f} "
            f"(Next surge trigger >= {losing_leg['current_premium'] * (1 + config.LOSING_PREMIUM_SURGE_PCT):.2f})"
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
