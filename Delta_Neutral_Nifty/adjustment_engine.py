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
from datetime import datetime
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

        # Step 4: Conditionally handle daytime hedge (only if DAYTIME_HEDGES_ENABLED is True)
        new_hedge_strike = 0.0
        new_hedge_delta = 0.0
        new_hedge_iv = 0.0
        new_hedge_premium = 0.0

        if getattr(config, "DAYTIME_HEDGES_ENABLED", False):
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

            # Place new hedge leg FIRST (Hedge-First for Margin Benefit)
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

        # Check straddle BEFORE placing new short leg
        would_be_straddle = abs(new_short_strike - losing_leg["strike"]) <= config.STRADDLE_PROXIMITY_PTS

        if would_be_straddle:
            logger.warning(
                f"[STRADDLE CHECK] New {profitable_side} strike {new_short_strike} = "
                f"Losing strike {losing_leg['strike']} → STRADDLE REACHED"
            )

        # Step 5: Place new short leg
        token_info = self.api.get_token_info(self.expiry_date, new_short_strike, profitable_side) if self.api else None
        short_token = token_info["token"] if token_info else ""
        short_symbol = token_info["symbol"] if token_info else f"NIFTY_{int(new_short_strike)}_{profitable_side}"

        if not paper_mode and token_info and self.api:
            short_order_id = self.api.place_order(
                short_symbol, short_token, "SELL",
                config.LOT_SIZE * config.NUM_LOTS
            )
            if short_order_id is None:
                logger.error(f"[ADJ ABORTED] Could not place new short SELL @ {new_short_strike}! Manual review needed.")
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

        # Step 6: Reset losing short leg's surge baseline to its CURRENT price
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
        if getattr(config, "DAYTIME_HEDGES_ENABLED", False):
            logger.info(
                f"  New HEDGE {profitable_side} @ {new_hedge_strike} (Δ={new_hedge_delta:.4f}, "
                f"Prem={new_hedge_premium:.2f})"
            )
        logger.info(f"  Adjustment P&L booked: {format_pnl(pnl_short + pnl_hedge)}")
        logger.info("=" * 65)

        return True

    def calculate_overnight_shield(self, spot, T, option_chain_ltps):
        """
        Calculate dynamic 100-point OTM hedge strikes and quantities.
        Rule:
          - Reads current open short Call and Put strikes (regardless of how many times adjusted).
          - Target CE Hedge Strike = Short Call Strike + 100 pts.
          - Target PE Hedge Strike = Short Put Strike - 100 pts.
          - Target Quantity = 1.0x (same as short legs = LOT_SIZE * NUM_LOTS).
        """
        short_calls = self.pm.short_call_legs
        short_puts = self.pm.short_put_legs
        if not short_calls or not short_puts:
            return None

        short_ce = short_calls[-1]
        short_pe = short_puts[-1]

        wing_pts = getattr(config, "OVERNIGHT_SHIELD_WING_PTS", 100)
        ce_h_strike = float(short_ce["strike"] + wing_pts)
        pe_h_strike = float(short_pe["strike"] - wing_pts)

        ce_chain = option_chain_ltps.get("CE", {})
        pe_chain = option_chain_ltps.get("PE", {})
        r = config.RISK_FREE_RATE

        # Call hedge premium & Greeks
        ce_h_prem = float(ce_chain.get(ce_h_strike, 0.0))
        if ce_h_prem > 0:
            ce_h_iv = greeks_engine.implied_volatility(ce_h_prem, spot, ce_h_strike, T, r, "CE")
            ce_h_delta = greeks_engine.all_greeks(spot, ce_h_strike, T, r, ce_h_iv, "CE")["delta"]
        else:
            ce_h_iv = float(short_ce.get("current_iv", 0.15))
            ce_h_prem = greeks_engine.bs_price(spot, ce_h_strike, T, r, ce_h_iv, "CE")
            ce_h_delta = greeks_engine.all_greeks(spot, ce_h_strike, T, r, ce_h_iv, "CE")["delta"]

        # Put hedge premium & Greeks
        pe_h_prem = float(pe_chain.get(pe_h_strike, 0.0))
        if pe_h_prem > 0:
            pe_h_iv = greeks_engine.implied_volatility(pe_h_prem, spot, pe_h_strike, T, r, "PE")
            pe_h_delta = greeks_engine.all_greeks(spot, pe_h_strike, T, r, pe_h_iv, "PE")["delta"]
        else:
            pe_h_iv = float(short_pe.get("current_iv", 0.15))
            pe_h_prem = greeks_engine.bs_price(spot, pe_h_strike, T, r, pe_h_iv, "PE")
            pe_h_delta = greeks_engine.all_greeks(spot, pe_h_strike, T, r, pe_h_iv, "PE")["delta"]

        multiplier = getattr(config, "OVERNIGHT_SHIELD_QTY_MULTIPLIER", 1.0)
        num_lots = max(1, int(round(config.NUM_LOTS * multiplier)))
        hedge_qty = num_lots * config.LOT_SIZE

        return {
            "CE": {
                "strike": ce_h_strike,
                "delta": ce_h_delta,
                "iv": ce_h_iv,
                "premium": ce_h_prem,
                "quantity": hedge_qty,
            },
            "PE": {
                "strike": pe_h_strike,
                "delta": pe_h_delta,
                "iv": pe_h_iv,
                "premium": pe_h_prem,
                "quantity": hedge_qty,
            },
            "num_lots": num_lots,
            "hedge_qty": hedge_qty,
        }

    # ═══════════════════════════════════════════════════════════════
    # STRADDLE COMPENSATOR & INFINITY ARMOR ENGINE
    # ═══════════════════════════════════════════════════════════════

    def determine_straddle_tested_side(self, spot, straddle_strike):
        """
        Determine which side was tested (threatened by market trend).
        - If PE was rolled up into CE -> tested_side = "CE" (Bullish trend).
        - If CE was rolled down into PE -> tested_side = "PE" (Bearish trend).
        Fallback: If spot >= straddle_strike -> "CE", else "PE".
        """
        adjustments = self.pm.position.get("adjustments", [])
        if adjustments:
            last_adj = adjustments[-1]
            reason = last_adj.get("reason", "")
            if "CE surged" in reason:
                return "CE"
            elif "PE surged" in reason:
                return "PE"
            closed_id = last_adj.get("closed_leg_id", "")
            for leg in self.pm.position.get("legs", []):
                if leg["id"] == closed_id:
                    if leg["option_type"] == "PE":
                        return "CE"
                    elif leg["option_type"] == "CE":
                        return "PE"

        return "CE" if spot >= straddle_strike else "PE"

    def check_and_deploy_straddle_compensator(self, spot, T, option_chain_ltps, paper_mode=True):
        """
        Autonomous Straddle Compensator & Infinity Armor Deployment.
        Triggered when both short legs converge to Straddle (Strike K).
        
        Covers 2 distinct operational scenarios:
        1. NAKED STRADDLE:
           - Immediate Armor Lock: Buy 2 lots of (K ± 100) on tested side (1x2 Ratio Backspread).
           - Buy 1 lot of (K ∓ 100) on untested side (Iron Fly wing + margin unlock).
           - Caps maximum tail loss forever, unlocks ~Rs.75,000 margin, and gives +1.00 net delta
             to ride the trend into massive profit if trend continues!
        
        2. HEDGED STRADDLE (Overnight hedges already present):
           - Case 2.A (ATM): Balance missing wing (1 lot) and add +1 lot on tested wing (1x2 Ratio).
           - Case 2.B (ITM Breached): Harvest decaying safe side (book profit) and roll fresh
             credit spread at live ATM strike to directly absorb the locked spread loss!
        """
        if not self.pm.is_straddle or not self.pm.is_active:
            return False

        if self.pm.position.get("straddle_armor_deployed", False):
            return False

        if not getattr(config, "STRADDLE_ARMOR_ENABLED", True):
            return False

        short_calls = self.pm.short_call_legs
        short_puts = self.pm.short_put_legs
        if not short_calls or not short_puts:
            return False

        short_ce = short_calls[-1]
        short_pe = short_puts[-1]
        straddle_strike = short_ce["strike"]

        tested_side = self.determine_straddle_tested_side(spot, straddle_strike)
        untested_side = "PE" if tested_side == "CE" else "CE"
        wing_pts = getattr(config, "STRADDLE_ARMOR_WING_PTS", 100)
        r = config.RISK_FREE_RATE

        logger.warning("=" * 65)
        logger.warning("[STRADDLE COMPENSATOR ACTIVATION]")
        logger.warning(f"  Straddle Strike: {straddle_strike} | Spot: {spot:.2f} | Tested Side: {tested_side}")

        # Check existing open hedges
        open_call_hedges = [h for h in self.pm.open_hedge_legs if h["option_type"] == "CE"]
        open_put_hedges = [h for h in self.pm.open_hedge_legs if h["option_type"] == "PE"]
        has_any_hedges = bool(open_call_hedges or open_put_hedges)

        # ─── SCENARIO 1: NAKED STRADDLE (Zero open hedges) ────────
        if not has_any_hedges:
            logger.info("  [SCENARIO 1: NAKED STRADDLE] Deploying 1x2 Ratio Armor Wings...")
            tested_wing_strike = (straddle_strike + wing_pts) if tested_side == "CE" else (straddle_strike - wing_pts)
            untested_wing_strike = (straddle_strike - wing_pts) if tested_side == "CE" else (straddle_strike + wing_pts)

            tested_lots = getattr(config, "STRADDLE_RATIO_TESTED_LOTS", 2) * config.NUM_LOTS
            tested_qty = tested_lots * config.LOT_SIZE

            untested_lots = getattr(config, "STRADDLE_RATIO_UNTESTED_LOTS", 1) * config.NUM_LOTS
            untested_qty = untested_lots * config.LOT_SIZE

            tested_chain = option_chain_ltps.get(tested_side, {})
            untested_chain = option_chain_ltps.get(untested_side, {})

            tested_prem = float(tested_chain.get(tested_wing_strike, 0.0))
            if tested_prem <= 0:
                tested_iv = float(short_ce.get("current_iv", 0.15))
                tested_prem = greeks_engine.bs_price(spot, tested_wing_strike, T, r, tested_iv, tested_side)
            tested_iv = greeks_engine.implied_volatility(tested_prem, spot, tested_wing_strike, T, r, tested_side)
            tested_delta = greeks_engine.all_greeks(spot, tested_wing_strike, T, r, tested_iv, tested_side)["delta"]

            untested_prem = float(untested_chain.get(untested_wing_strike, 0.0))
            if untested_prem <= 0:
                untested_iv = float(short_pe.get("current_iv", 0.15))
                untested_prem = greeks_engine.bs_price(spot, untested_wing_strike, T, r, untested_iv, untested_side)
            untested_iv = greeks_engine.implied_volatility(untested_prem, spot, untested_wing_strike, T, r, untested_side)
            untested_delta = greeks_engine.all_greeks(spot, untested_wing_strike, T, r, untested_iv, untested_side)["delta"]

            tested_tok_info = self.api.get_token_info(self.expiry_date, tested_wing_strike, tested_side) if self.api else None
            tested_tok = tested_tok_info["token"] if tested_tok_info else ""
            tested_sym = tested_tok_info["symbol"] if tested_tok_info else f"NIFTY_{int(tested_wing_strike)}_{tested_side}"

            if not paper_mode and tested_tok_info and self.api:
                logger.info(f"  [LIVE ORDER] BUY {tested_qty}x {tested_sym} ({tested_tok})")
                self.api.place_order(tested_sym, tested_tok, "BUY", tested_qty)

            self.pm.add_leg(
                leg_type=f"LONG_{tested_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
                strike=tested_wing_strike,
                option_type=tested_side,
                delta_at_entry=tested_delta,
                iv_at_entry=tested_iv,
                entry_premium=tested_prem,
                symbol_token=tested_tok,
                trading_symbol=tested_sym,
                quantity=tested_qty,
                is_hedge=True,
            )

            untested_tok_info = self.api.get_token_info(self.expiry_date, untested_wing_strike, untested_side) if self.api else None
            untested_tok = untested_tok_info["token"] if untested_tok_info else ""
            untested_sym = untested_tok_info["symbol"] if untested_tok_info else f"NIFTY_{int(untested_wing_strike)}_{untested_side}"

            if not paper_mode and untested_tok_info and self.api:
                logger.info(f"  [LIVE ORDER] BUY {untested_qty}x {untested_sym} ({untested_tok})")
                self.api.place_order(untested_sym, untested_tok, "BUY", untested_qty)

            self.pm.add_leg(
                leg_type=f"LONG_{untested_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
                strike=untested_wing_strike,
                option_type=untested_side,
                delta_at_entry=untested_delta,
                iv_at_entry=untested_iv,
                entry_premium=untested_prem,
                symbol_token=untested_tok,
                trading_symbol=untested_sym,
                quantity=untested_qty,
                is_hedge=True,
            )

            self.pm.position["straddle_armor_deployed"] = True
            self.pm.position["straddle_armor_type"] = "1x2_RATIO_BACKSPREAD"
            self.pm.position["straddle_tested_side"] = tested_side
            self.pm.position["straddle_tested_wing_strike"] = tested_wing_strike
            self.pm.position["straddle_untested_wing_strike"] = untested_wing_strike
            self.pm.save()

            logger.info(f"  [ARMOR LOCKED] Tested Wing {tested_wing_strike} ({tested_lots} lots) + Untested Wing {untested_wing_strike} (1 lot)")
            logger.info("=" * 65)
            return True

        # ─── SCENARIO 2: HEDGED STRADDLE (Hedges already exist) ───
        is_itm_breach = (tested_side == "CE" and spot >= (straddle_strike + 25)) or \
                         (tested_side == "PE" and spot <= (straddle_strike - 25))

        if not is_itm_breach:
            # ─── SCENARIO 2.A: ATM HEDGED STRADDLE ───────────────
            logger.info("  [SCENARIO 2.A: ATM HEDGED STRADDLE] Balancing wings & adding trend rider...")
            untested_hedges = open_put_hedges if untested_side == "PE" else open_call_hedges
            if not untested_hedges:
                untested_wing_strike = (straddle_strike - wing_pts) if untested_side == "PE" else (straddle_strike + wing_pts)
                untested_qty = config.LOT_SIZE * config.NUM_LOTS
                untested_chain = option_chain_ltps.get(untested_side, {})
                untested_prem = float(untested_chain.get(untested_wing_strike, 0.0))
                if untested_prem <= 0:
                    untested_iv = float(short_pe.get("current_iv", 0.15))
                    untested_prem = greeks_engine.bs_price(spot, untested_wing_strike, T, r, untested_iv, untested_side)
                untested_iv = greeks_engine.implied_volatility(untested_prem, spot, untested_wing_strike, T, r, untested_side)
                untested_delta = greeks_engine.all_greeks(spot, untested_wing_strike, T, r, untested_iv, untested_side)["delta"]

                untested_tok_info = self.api.get_token_info(self.expiry_date, untested_wing_strike, untested_side) if self.api else None
                untested_tok = untested_tok_info["token"] if untested_tok_info else ""
                untested_sym = untested_tok_info["symbol"] if untested_tok_info else f"NIFTY_{int(untested_wing_strike)}_{untested_side}"

                if not paper_mode and untested_tok_info and self.api:
                    self.api.place_order(untested_sym, untested_tok, "BUY", untested_qty)

                self.pm.add_leg(
                    leg_type=f"LONG_{untested_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
                    strike=untested_wing_strike,
                    option_type=untested_side,
                    delta_at_entry=untested_delta,
                    iv_at_entry=untested_iv,
                    entry_premium=untested_prem,
                    symbol_token=untested_tok,
                    trading_symbol=untested_sym,
                    quantity=untested_qty,
                    is_hedge=True,
                )

            tested_wing_strike = (straddle_strike + wing_pts) if tested_side == "CE" else (straddle_strike - wing_pts)
            extra_qty = config.LOT_SIZE * config.NUM_LOTS
            tested_chain = option_chain_ltps.get(tested_side, {})
            tested_prem = float(tested_chain.get(tested_wing_strike, 0.0))
            if tested_prem <= 0:
                tested_iv = float(short_ce.get("current_iv", 0.15))
                tested_prem = greeks_engine.bs_price(spot, tested_wing_strike, T, r, tested_iv, tested_side)
            tested_iv = greeks_engine.implied_volatility(tested_prem, spot, tested_wing_strike, T, r, tested_side)
            tested_delta = greeks_engine.all_greeks(spot, tested_wing_strike, T, r, tested_iv, tested_side)["delta"]

            tested_tok_info = self.api.get_token_info(self.expiry_date, tested_wing_strike, tested_side) if self.api else None
            tested_tok = tested_tok_info["token"] if tested_tok_info else ""
            tested_sym = tested_tok_info["symbol"] if tested_tok_info else f"NIFTY_{int(tested_wing_strike)}_{tested_side}"

            if not paper_mode and tested_tok_info and self.api:
                self.api.place_order(tested_sym, tested_tok, "BUY", extra_qty)

            self.pm.add_leg(
                leg_type=f"LONG_{tested_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
                strike=tested_wing_strike,
                option_type=tested_side,
                delta_at_entry=tested_delta,
                iv_at_entry=tested_iv,
                entry_premium=tested_prem,
                symbol_token=tested_tok,
                trading_symbol=tested_sym,
                quantity=extra_qty,
                is_hedge=True,
            )

            self.pm.position["straddle_armor_deployed"] = True
            self.pm.position["straddle_armor_type"] = "HEDGED_1x2_RATIO"
            self.pm.position["straddle_tested_side"] = tested_side
            self.pm.save()
            logger.info("  [SCENARIO 2.A COMPLETE] Converted to Hedged 1x2 Ratio Armor.")
            logger.info("=" * 65)
            return True

        else:
            # ─── SCENARIO 2.B: ITM BREACHED HEDGED STRADDLE ──────
            logger.info(f"  [SCENARIO 2.B: ITM BREACHED STRADDLE] Spot {spot:.2f} breached {straddle_strike} ({tested_side}). Harvesting safe side and shifting credit...")
            safe_short_leg = short_pe if tested_side == "CE" else short_ce
            safe_hedges = open_put_hedges if tested_side == "CE" else open_call_hedges

            if not paper_mode and self.api:
                sym = safe_short_leg.get("trading_symbol") or f"NIFTY_{int(safe_short_leg['strike'])}_{untested_side}"
                tok = safe_short_leg.get("symbol_token", "")
                q = safe_short_leg.get("quantity", config.LOT_SIZE * config.NUM_LOTS)
                self.api.place_order(sym, tok, "BUY", q)

            pnl_harvest = self.pm.close_leg(
                safe_short_leg["id"],
                safe_short_leg["current_premium"],
                f"STRADDLE_HARVEST_SAFE_{untested_side}"
            )
            logger.info(f"  [SAFE SIDE HARVESTED] Closed {safe_short_leg['strike']} {untested_side} @ {safe_short_leg['current_premium']:.2f}. PnL: {format_pnl(pnl_harvest)}")

            for h in safe_hedges:
                if not paper_mode and self.api:
                    h_sym = h.get("trading_symbol") or f"NIFTY_{int(h['strike'])}_{untested_side}"
                    h_tok = h.get("symbol_token", "")
                    h_q = h.get("quantity", config.LOT_SIZE * config.NUM_LOTS)
                    self.api.place_order(h_sym, h_tok, "SELL", h_q)
                self.pm.close_leg(h["id"], h["current_premium"], "STRADDLE_HARVEST_HEDGE")

            new_atm_strike = float(round(spot / config.STRIKE_GAP) * config.STRIKE_GAP)
            new_credit_side = untested_side
            new_credit_wing_strike = (new_atm_strike - wing_pts) if new_credit_side == "PE" else (new_atm_strike + wing_pts)

            new_chain = option_chain_ltps.get(new_credit_side, {})
            new_short_prem = float(new_chain.get(new_atm_strike, 0.0))
            if new_short_prem <= 0:
                new_iv = 0.15
                new_short_prem = greeks_engine.bs_price(spot, new_atm_strike, T, r, new_iv, new_credit_side)
            new_iv = greeks_engine.implied_volatility(new_short_prem, spot, new_atm_strike, T, r, new_credit_side)
            new_delta = greeks_engine.all_greeks(spot, new_atm_strike, T, r, new_iv, new_credit_side)["delta"]

            new_wing_prem = float(new_chain.get(new_credit_wing_strike, 0.0))
            if new_wing_prem <= 0:
                new_wing_prem = greeks_engine.bs_price(spot, new_credit_wing_strike, T, r, new_iv, new_credit_side)
            new_wing_iv = greeks_engine.implied_volatility(new_wing_prem, spot, new_credit_wing_strike, T, r, new_credit_side)
            new_wing_delta = greeks_engine.all_greeks(spot, new_credit_wing_strike, T, r, new_wing_iv, new_credit_side)["delta"]

            roll_qty = config.LOT_SIZE * config.NUM_LOTS

            tok_info = self.api.get_token_info(self.expiry_date, new_atm_strike, new_credit_side) if self.api else None
            tok = tok_info["token"] if tok_info else ""
            sym = tok_info["symbol"] if tok_info else f"NIFTY_{int(new_atm_strike)}_{new_credit_side}"

            if not paper_mode and tok_info and self.api:
                self.api.place_order(sym, tok, "SELL", roll_qty)

            self.pm.add_leg(
                leg_type=f"SHORT_{new_credit_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
                strike=new_atm_strike,
                option_type=new_credit_side,
                delta_at_entry=new_delta,
                iv_at_entry=new_iv,
                entry_premium=new_short_prem,
                symbol_token=tok,
                trading_symbol=sym,
                quantity=roll_qty,
                is_hedge=False,
            )

            wing_tok_info = self.api.get_token_info(self.expiry_date, new_credit_wing_strike, new_credit_side) if self.api else None
            wing_tok = wing_tok_info["token"] if wing_tok_info else ""
            wing_sym = wing_tok_info["symbol"] if wing_tok_info else f"NIFTY_{int(new_credit_wing_strike)}_{new_credit_side}"

            if not paper_mode and wing_tok_info and self.api:
                self.api.place_order(wing_sym, wing_tok, "BUY", roll_qty)

            self.pm.add_leg(
                leg_type=f"LONG_{new_credit_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
                strike=new_credit_wing_strike,
                option_type=new_credit_side,
                delta_at_entry=new_wing_delta,
                iv_at_entry=new_wing_iv,
                entry_premium=new_wing_prem,
                symbol_token=wing_tok,
                trading_symbol=wing_sym,
                quantity=roll_qty,
                is_hedge=True,
            )

            self.pm.position["straddle_armor_deployed"] = True
            self.pm.position["straddle_armor_type"] = "ITM_CREDIT_SHIFT"
            self.pm.position["straddle_tested_side"] = tested_side
            self.pm.save()

            logger.info(f"  [FRESH CREDIT DEPLOYED] Sold {new_atm_strike} {new_credit_side} @ {new_short_prem:.2f} + Bought {new_credit_wing_strike} @ {new_wing_prem:.2f}")
            logger.info(f"  [CREDIT OFFSET] Fresh net credit ~Rs.{(new_short_prem - new_wing_prem) * roll_qty:.2f} deployed to offset locked spread loss!")
            logger.info("=" * 65)
            return True

    def deploy_gap_breach_compensator(self, spot, T, option_chain_ltps, paper_mode=True):
        """
        Autonomous Gap Breach Compensator & Credit Shift (Scenario 2.B Architecture).
        Triggered when market opens with a fat-tail overnight gap breaching one of the short legs.
        Overnight hedge on the breached side caps maximum loss (locked defined-risk spread).
        
        Execution:
        1. HARVEST SAFE SIDE:
           - Close decaying safe-side short leg (booking max cash profit).
           - Close safe-side overnight hedge (salvaging remaining value).
        2. RETAIN BREACHED SIDE:
           - Keep breached short leg + overnight long hedge intact (defined-risk spread with locked loss).
        3. SHIFT FRESH CREDIT TENT (LIVE ATM):
           - Sell fresh ATM credit spread on the safe side (Sell ATM strike + Buy ATM wing).
           - Fresh net credit collected directly absorbs & offsets the locked spread loss!
        """
        if not self.pm.is_active:
            return False

        if self.pm.position.get("gap_breach_compensated", False):
            return False

        short_calls = self.pm.short_call_legs
        short_puts = self.pm.short_put_legs
        if not short_calls or not short_puts:
            return False

        short_ce = short_calls[-1]
        short_pe = short_puts[-1]

        ce_breached = spot >= short_ce["strike"]
        pe_breached = spot <= short_pe["strike"]

        if not (ce_breached or pe_breached):
            logger.info(f"[GAP BREACH COMPENSATOR] Spot {spot:.2f} is within strikes ({short_pe['strike']} PE - {short_ce['strike']} CE). No breach.")
            return False

        breached_side = "CE" if ce_breached else "PE"
        safe_side = "PE" if breached_side == "CE" else "CE"
        breached_leg = short_ce if breached_side == "CE" else short_pe
        safe_short_leg = short_pe if breached_side == "CE" else short_ce

        wing_pts = getattr(config, "OVERNIGHT_SHIELD_WING_PTS", 100)
        r = config.RISK_FREE_RATE

        logger.warning("=" * 65)
        logger.warning("[GAP BREACH COMPENSATOR ACTIVATION]")
        logger.warning(
            f"  Breached Side: {breached_side} (Strike {breached_leg['strike']} vs Spot {spot:.2f}) | "
            f"Safe Side: {safe_side} (Strike {safe_short_leg['strike']})"
        )

        # 1. Harvest Safe Side Short Leg
        safe_qty = safe_short_leg.get("quantity", config.LOT_SIZE * config.NUM_LOTS)
        if not paper_mode and self.api:
            sym = safe_short_leg.get("trading_symbol") or f"NIFTY_{int(safe_short_leg['strike'])}_{safe_side}"
            tok = safe_short_leg.get("symbol_token", "")
            self.api.place_order(sym, tok, "BUY", safe_qty)

        pnl_harvest = self.pm.close_leg(
            safe_short_leg["id"],
            safe_short_leg["current_premium"],
            f"GAP_BREACH_HARVEST_SAFE_{safe_side}"
        )
        logger.info(f"  [SAFE SIDE HARVESTED] Closed {safe_short_leg['strike']} {safe_side} @ {safe_short_leg['current_premium']:.2f}. Realized PnL: {format_pnl(pnl_harvest)}")

        # 2. Harvest Safe Side Overnight Hedges (if open)
        safe_hedges = [h for h in self.pm.open_hedge_legs if h["option_type"] == safe_side]
        for h in safe_hedges:
            h_qty = h.get("quantity", config.LOT_SIZE * config.NUM_LOTS)
            if not paper_mode and self.api:
                h_sym = h.get("trading_symbol") or f"NIFTY_{int(h['strike'])}_{safe_side}"
                h_tok = h.get("symbol_token", "")
                self.api.place_order(h_sym, h_tok, "SELL", h_qty)
            self.pm.close_leg(h["id"], h["current_premium"], f"GAP_BREACH_HARVEST_HEDGE_{safe_side}")
            logger.info(f"  [SAFE HEDGE HARVESTED] Closed {h['strike']} {safe_side} @ {h['current_premium']:.2f}")

        # 3. Deploy Fresh ATM Credit Spread on the Safe Side
        new_atm_strike = float(round(spot / config.STRIKE_GAP) * config.STRIKE_GAP)
        new_credit_side = safe_side
        new_credit_wing_strike = (new_atm_strike - wing_pts) if new_credit_side == "PE" else (new_atm_strike + wing_pts)

        new_chain = option_chain_ltps.get(new_credit_side, {}) if option_chain_ltps else {}
        new_short_prem = float(new_chain.get(new_atm_strike, 0.0))
        if new_short_prem <= 0:
            new_iv = 0.15
            new_short_prem = greeks_engine.bs_price(spot, new_atm_strike, T, r, new_iv, new_credit_side)
        new_iv = greeks_engine.implied_volatility(new_short_prem, spot, new_atm_strike, T, r, new_credit_side)
        new_delta = greeks_engine.all_greeks(spot, new_atm_strike, T, r, new_iv, new_credit_side)["delta"]

        new_wing_prem = float(new_chain.get(new_credit_wing_strike, 0.0))
        if new_wing_prem <= 0:
            new_wing_prem = greeks_engine.bs_price(spot, new_credit_wing_strike, T, r, new_iv, new_credit_side)
        new_wing_iv = greeks_engine.implied_volatility(new_wing_prem, spot, new_credit_wing_strike, T, r, new_credit_side)
        new_wing_delta = greeks_engine.all_greeks(spot, new_credit_wing_strike, T, r, new_wing_iv, new_credit_side)["delta"]

        roll_qty = config.LOT_SIZE * config.NUM_LOTS

        # Enter Short ATM Leg
        tok_info = self.api.get_token_info(self.expiry_date, new_atm_strike, new_credit_side) if self.api else None
        tok = tok_info["token"] if tok_info else ""
        sym = tok_info["symbol"] if tok_info else f"NIFTY_{int(new_atm_strike)}_{new_credit_side}"

        if not paper_mode and tok_info and self.api:
            self.api.place_order(sym, tok, "SELL", roll_qty)

        self.pm.add_leg(
            leg_type=f"SHORT_{new_credit_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
            strike=new_atm_strike,
            option_type=new_credit_side,
            delta_at_entry=new_delta,
            iv_at_entry=new_iv,
            entry_premium=new_short_prem,
            symbol_token=tok,
            trading_symbol=sym,
            quantity=roll_qty,
            is_hedge=False,
        )

        # Enter Long ATM Wing Hedge
        wing_tok_info = self.api.get_token_info(self.expiry_date, new_credit_wing_strike, new_credit_side) if self.api else None
        wing_tok = wing_tok_info["token"] if wing_tok_info else ""
        wing_sym = wing_tok_info["symbol"] if wing_tok_info else f"NIFTY_{int(new_credit_wing_strike)}_{new_credit_side}"

        if not paper_mode and wing_tok_info and self.api:
            self.api.place_order(wing_sym, wing_tok, "BUY", roll_qty)

        self.pm.add_leg(
            leg_type=f"LONG_{new_credit_side.replace('CE', 'CALL').replace('PE', 'PUT')}",
            strike=new_credit_wing_strike,
            option_type=new_credit_side,
            delta_at_entry=new_wing_delta,
            iv_at_entry=new_wing_iv,
            entry_premium=new_wing_prem,
            symbol_token=wing_tok,
            trading_symbol=wing_sym,
            quantity=roll_qty,
            is_hedge=True,
        )

        # Update position state
        self.pm.position["gap_breach_active"] = False
        self.pm.position["gap_breach_compensated"] = True
        self.pm.position["gap_breach_side"] = breached_side
        self.pm.position["gap_breach_compensation_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.pm.save()

        net_credit = (new_short_prem - new_wing_prem) * roll_qty
        logger.info(f"  [FRESH CREDIT DEPLOYED] Sold {new_atm_strike} {new_credit_side} @ {new_short_prem:.2f} + Bought {new_credit_wing_strike} @ {new_wing_prem:.2f}")
        logger.info(f"  [CREDIT OFFSET] Fresh net credit ~Rs.{net_credit:.2f} deployed to offset breached spread loss!")
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
