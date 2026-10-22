"""
Delta Neutral Nifty - Strategy Runner
Main orchestrator: entry, monitoring, adjustment, and expiry handling.

Uses WebSocket V2 for real-time streaming of ALL option strikes.
Falls back to REST API if WebSocket is unavailable.

Usage:
    python -m Delta_Neutral_Nifty.strategy_runner             # Paper mode (default)
    python -m Delta_Neutral_Nifty.strategy_runner --live       # Live trading
"""
from datetime import datetime, timedelta
import sys
import time
import argparse

from . import config
from .utils import (
    setup_logger, now_ist, is_market_open, is_entry_window,
    get_current_expiry, is_expiry_day, time_to_expiry_years,
    get_atm_strike, print_banner, print_position_table, format_pnl, format_delta,
    get_market_close_time, get_shield_buy_time, IST,
)
from .angel_api import AngelOneAPI
from .greeks_engine import find_strike_at_delta, calculate_portfolio_greeks, bs_price
from .position_manager import PositionManager
from .adjustment_engine import AdjustmentEngine
from .websocket_feeder import WebSocketFeeder

logger = setup_logger("strategy")


class StrategyRunner:
    """Main orchestrator for the Delta-Neutral Iron Condor strategy."""

    def __init__(self, paper_mode=True):
        self.paper_mode = paper_mode
        self.api = AngelOneAPI()
        self.pm = PositionManager()
        self.expiry_date = None
        self.adj_engine = None
        self.last_chain_refresh = 0
        self.cached_chain_ltps = {"CE": {}, "PE": {}}

        # WebSocket V2 feeder for real-time ALL-strike streaming
        self.ws_feeder = None
        self.use_websocket = True  # Will fallback to REST if WS fails

    def start(self):
        """Main entry point."""
        mode_str = "PAPER TRADING (SIMULATION)" if self.paper_mode else "LIVE TRADING"
        print_banner(f"DELTA NEUTRAL NIFTY - {mode_str}")
        print(f"  Cycle: Starts Wednesday 09:18 IST -> Runs through Expiry")
        print(f"  Lot Size: {config.LOT_SIZE} x {config.NUM_LOTS} lot(s)")
        print(f"  Entry: 0.15 Delta Short Strangle + 0.05 Delta Long Hedges")
        print(f"  Adjustment Trigger: 50% Short Premium Surge (Losing Leg >= {config.LOSING_PREMIUM_SURGE_PCT*100:.0f}% from Baseline)")
        print(f"  Straddle Risk Controls: 2% Dynamic Capital SL | 70% Combined Theta Decay Target")
        print(f"  Data Source: WebSocket V2 (real-time streaming)")

        # Login
        self.api.login()
        self.api.fetch_instrument_master()

        # Determine expiry dynamically from Angel One master
        self.expiry_date = self.api.get_nearest_expiry_date()
        self.adj_engine = AdjustmentEngine(self.pm, self.api, self.expiry_date)

        logger.info(f"Current weekly expiry: {self.expiry_date}")

        # Check if we need to initialize a new position for current expiry
        stored_expiry = self.pm.position.get("expiry_date")
        if not stored_expiry or stored_expiry != str(self.expiry_date):
            logger.info(f"New weekly cycle detected (stored: '{stored_expiry}', live: '{self.expiry_date}'). Initializing fresh position.")
            self.pm.initialize_week(self.expiry_date)
        elif not self.pm.is_active:
            logger.info("Previous position inactive. Initializing fresh position.")
            self.pm.initialize_week(self.expiry_date)

        # Start WebSocket V2 feeder for real-time ALL-strike streaming
        self._start_websocket_feeder()

        # Main loop
        self._run_loop()

        # Cleanup
        if self.ws_feeder:
            self.ws_feeder.stop()

    def _start_websocket_feeder(self):
        """
        Initialize WebSocket V2 and subscribe to ALL option strikes.
        Used in BOTH paper and live modes — real market data is essential for accurate testing.
        Only difference: paper mode does not place real orders.
        """

        try:
            spot = self.api.get_spot_ltp()
            self.ws_feeder = WebSocketFeeder(self.api)

            success = self.ws_feeder.start(self.expiry_date, spot, range_pct=0.08)
            if success:
                self.use_websocket = True
                stats = self.ws_feeder.get_stats()
                logger.info(f"[WS V2] Real-time streaming ACTIVE: "
                             f"{stats['total_tokens']} tokens subscribed")
                print(f"  WebSocket V2: ACTIVE ({stats['total_tokens']} tokens streaming)")
            else:
                logger.warning("[WS V2] Failed to connect, falling back to REST API")
                self.use_websocket = False
                print(f"  WebSocket V2: FAILED (using REST fallback)")

        except Exception as e:
            logger.error(f"[WS V2] Init error: {e}")
            self.use_websocket = False
            print(f"  WebSocket V2: ERROR (using REST fallback)")

    def _get_live_chain_ltps(self, spot):
        """
        Get live option chain LTPs from the best available source.
        Priority: WebSocket V2 > REST API
        """
        if self.use_websocket and self.ws_feeder and self.ws_feeder.is_connected():
            # Real-time data from WebSocket — ALL strikes, zero rate limits
            chain = self.ws_feeder.get_chain_ltps()
            if chain["CE"] and chain["PE"]:
                return chain

            # WebSocket connected but no data yet, wait
            logger.debug("[WS] Connected but no ticks yet, waiting...")

        # Fallback: REST API (rate-limited, may miss strikes)
        try:
            return self.api.get_option_chain_ltps(self.expiry_date, spot)
        except Exception as e:
            logger.debug(f"REST chain fetch failed: {e}")
            return self.cached_chain_ltps

    def _run_loop(self):
        """Continuous monitoring loop during market hours."""
        while True:
            now = now_ist()

            if not is_market_open(now):
                # Check if post-market on expiry day
                if ((self.expiry_date and now.date() >= self.expiry_date) or is_expiry_day(now.date())) and now.hour >= 15:
                    if self.pm.is_active:
                        logger.info(f"Expiry day ({self.expiry_date}) market closed. Closing all positions.")
                        self._close_all_positions("EXPIRY_CLOSE")
                    break

                close_h, close_m = get_market_close_time(now.date())
                # Pre-market or post-market check
                if now.hour < config.MARKET_OPEN_HOUR or (now.hour == config.MARKET_OPEN_HOUR and now.minute < config.MARKET_OPEN_MINUTE):
                    logger.info("Pre-market. Waiting for 09:15 IST...")
                    time.sleep(10)
                    continue
                elif now.hour > close_h or (now.hour == close_h and now.minute >= close_m):
                    close_time_str = f"{close_h:02d}:{close_m:02d}"
                    logger.info(f"[AUTO-STOP] Session close at {close_time_str} IST. Exiting process cleanly.")
                    print_banner(f"SESSION CLOSED ({close_time_str} IST) - ENGINE EXITING CLEANLY")
                    break

            try:
                # Get spot price (real data in both paper and live modes)
                if self.use_websocket and self.ws_feeder and self.ws_feeder.is_connected():
                    spot = self.ws_feeder.get_spot()
                    if spot <= 0:
                        spot = self.api.get_spot_ltp()
                else:
                    spot = self.api.get_spot_ltp()

                T = time_to_expiry_years(self.expiry_date, now)

                # Update WebSocket feeder with latest T for Greeks calculation
                if self.ws_feeder:
                    self.ws_feeder.update_spot(spot)
                    self.ws_feeder.update_time_to_expiry(T)

                # Phase 1: Initial entry if no open short legs (Starts Wednesday 09:18 IST)
                if not self.pm.open_short_legs:
                    if now.date().weekday() == config.CYCLE_START_DAY:
                        if is_entry_window(now):
                            self._execute_initial_entry(spot, T)
                        else:
                            logger.debug(f"Waiting for 09:18 IST entry window (current: {now.strftime('%H:%M:%S')})...")
                    else:
                        logger.info(f"No open positions. Waiting for Wednesday 09:18 IST to initiate new weekly cycle. Today: {now.strftime('%A')}")
                        time.sleep(30)

                # Phase 2: Monitor and adjust
                if self.pm.open_short_legs:
                    self._monitor_and_adjust(spot, T, now)

            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
                # Re-auth on session errors
                if "jwt" in str(e).lower() or "session" in str(e).lower():
                    try:
                        self.api.login()
                    except Exception:
                        pass

            time.sleep(config.ADJUSTMENT_CHECK_SECONDS)

    def _execute_initial_entry(self, spot, T):
        """
        Execute the initial entry:
        - If DAYTIME_HEDGES_ENABLED: Buy 0.05-delta wings first, then sell 0.15-delta shorts (Iron Condor)
        - If DAYTIME_HEDGES_ENABLED is False (Default): Sell 0.15-delta CE + PE (Pure Strangle for 100% Theta)
        """
        mode_label = "IRON CONDOR" if getattr(config, "DAYTIME_HEDGES_ENABLED", False) else "PURE SHORT STRANGLE"
        print_banner(f"INITIAL ENTRY - {mode_label}")
        logger.info(f"Spot: {spot:.2f} | ATM: {get_atm_strike(spot)} | T={T:.6f}y")

        # Fetch full option chain (WebSocket or REST)
        chain_ltps = self._get_live_chain_ltps(spot)
        self.cached_chain_ltps = chain_ltps

        ce_count = len(chain_ltps.get("CE", {}))
        pe_count = len(chain_ltps.get("PE", {}))
        source = "WebSocket V2" if (self.use_websocket and self.ws_feeder and self.ws_feeder.is_connected()) else "REST API"
        logger.info(f"  Option chain: {ce_count} CE + {pe_count} PE strikes via {source}")

        if not chain_ltps["CE"] or not chain_ltps["PE"]:
            logger.error("Empty option chain! Cannot enter.")
            return

        r = config.RISK_FREE_RATE

        # ─── SELL 0.15 Delta CALL ─────────────────────────────────
        ce_strike, ce_delta, ce_iv, ce_prem = find_strike_at_delta(
            chain_ltps["CE"], spot, config.ENTRY_DELTA, "CE", T, r
        )
        # ─── SELL 0.15 Delta PUT ──────────────────────────────────
        pe_strike, pe_delta, pe_iv, pe_prem = find_strike_at_delta(
            chain_ltps["PE"], spot, config.ENTRY_DELTA, "PE", T, r
        )

        if not all([ce_strike, pe_strike]):
            logger.error("Could not find required short strangle strikes!")
            return

        logger.info(f"  SELL CE @ {ce_strike} (Delta={ce_delta:.4f}, IV={ce_iv*100:.1f}%, Prem={ce_prem:.2f})")
        logger.info(f"  SELL PE @ {pe_strike} (Delta={pe_delta:.4f}, IV={pe_iv*100:.1f}%, Prem={pe_prem:.2f})")

        legs_to_place = []

        if getattr(config, "DAYTIME_HEDGES_ENABLED", False):
            # ─── Optional Daytime Hedges ──────────────────────────────
            ce_h_strike, ce_h_delta, ce_h_iv, ce_h_prem = find_strike_at_delta(
                chain_ltps["CE"], spot, config.HEDGE_DELTA, "CE", T, r
            )
            pe_h_strike, pe_h_delta, pe_h_iv, pe_h_prem = find_strike_at_delta(
                chain_ltps["PE"], spot, config.HEDGE_DELTA, "PE", T, r
            )
            if not all([ce_h_strike, pe_h_strike]):
                logger.error("Could not find all required hedge strikes!")
                return
            logger.info(f"  BUY  CE @ {ce_h_strike} (Delta={ce_h_delta:.4f}, Prem={ce_h_prem:.2f}) [HEDGE FIRST]")
            logger.info(f"  BUY  PE @ {pe_h_strike} (Delta={pe_h_delta:.4f}, Prem={pe_h_prem:.2f}) [HEDGE FIRST]")

            net_credit = (ce_prem + pe_prem) - (ce_h_prem + pe_h_prem)
            logger.info(f"  Net Credit: {net_credit:.2f} pts = Rs.{net_credit * config.LOT_SIZE * config.NUM_LOTS:,.2f}")

            legs_to_place = [
                ("LONG_CALL", ce_h_strike, "CE", ce_h_delta, ce_h_iv, ce_h_prem, "BUY", True),
                ("LONG_PUT", pe_h_strike, "PE", pe_h_delta, pe_h_iv, pe_h_prem, "BUY", True),
                ("SHORT_CALL", ce_strike, "CE", ce_delta, ce_iv, ce_prem, "SELL", False),
                ("SHORT_PUT", pe_strike, "PE", pe_delta, pe_iv, pe_prem, "SELL", False),
            ]
        else:
            net_credit = ce_prem + pe_prem
            logger.info(f"  Net Credit: {net_credit:.2f} pts = Rs.{net_credit * config.LOT_SIZE * config.NUM_LOTS:,.2f} [100% PURE THETA]")
            legs_to_place = [
                ("SHORT_CALL", ce_strike, "CE", ce_delta, ce_iv, ce_prem, "SELL", False),
                ("SHORT_PUT", pe_strike, "PE", pe_delta, pe_iv, pe_prem, "SELL", False),
            ]

        for leg_args in legs_to_place:
            success = self._place_leg(*leg_args)
            if not success:
                logger.error(f"[ENTRY ABORTED] Order failed for {leg_args[0]} @ {leg_args[1]}. No further orders will be placed.")
                return

        print_banner(f"{mode_label} ENTERED SUCCESSFULLY")

    def _place_leg(self, leg_type, strike, opt_type, delta_val, iv, premium, txn, is_hedge, quantity=None, is_overnight_hedge=False):
        """Place order and add leg to position manager. Returns True on success, False on failure."""
        token_info = self.api.get_token_info(self.expiry_date, strike, opt_type) if self.api else None
        token = token_info["token"] if token_info else ""
        symbol = token_info["symbol"] if token_info else f"NIFTY_{int(strike)}_{opt_type}"
        qty = int(quantity) if (quantity is not None and quantity > 0) else (config.LOT_SIZE * config.NUM_LOTS)

        if not self.paper_mode and token_info and self.api:
            order_id = self.api.place_order(symbol, token, txn, qty)
            if order_id is None:
                logger.error(f"[ORDER FAILED] {leg_type} {txn} @ {strike} {opt_type} - Order not placed on broker!")
                return False
            logger.info(f"[ORDER CONFIRMED] {leg_type} {txn} @ {strike} {opt_type} (Qty: {qty}) -> OrderID: {order_id}")

        self.pm.add_leg(
            leg_type=leg_type, strike=strike, option_type=opt_type,
            delta_at_entry=delta_val, iv_at_entry=iv, entry_premium=premium,
            symbol_token=token, trading_symbol=symbol, is_hedge=is_hedge,
            quantity=qty, is_overnight_hedge=is_overnight_hedge,
        )
        return True

    def _close_all_positions(self, reason="EXPIRY_CLOSE"):
        """
        Square off all open legs on Angel One (if live) and update position manager.
        Order of exit:
        1. BUY back all SHORT legs first (closes market exposure & releases risk).
        2. SELL all LONG hedge legs second.
        """
        print_banner(f"CLOSING ALL POSITIONS: {reason}")
        logger.info(f"[CLOSE ALL] Executing square-off for {len(self.pm.open_legs)} open legs. Reason: {reason}")

        # 1. Close Short legs first (BUY back)
        for leg in list(self.pm.open_short_legs):
            if not self.paper_mode and self.api:
                symbol = leg.get("trading_symbol") or f"NIFTY_{int(leg['strike'])}_{leg['option_type']}"
                token = leg.get("symbol_token", "")
                qty = leg.get("quantity") or (config.LOT_SIZE * config.NUM_LOTS)
                logger.info(f"  [LIVE SQUAREOFF SHORT] BUY {qty}x {symbol} ({token})")
                order_id = self.api.place_order(symbol, token, "BUY", qty)
                if order_id is None:
                    logger.error(f"  [SQUAREOFF FAILED] Could not close short leg {symbol}! Manual intervention needed.")
            self.pm.close_leg(leg["id"], leg.get("current_premium", 0.0), reason)

        # 2. Close Hedge legs second (SELL)
        for leg in list(self.pm.open_hedge_legs):
            if not self.paper_mode and self.api:
                symbol = leg.get("trading_symbol") or f"NIFTY_{int(leg['strike'])}_{leg['option_type']}"
                token = leg.get("symbol_token", "")
                qty = leg.get("quantity") or (config.LOT_SIZE * config.NUM_LOTS)
                logger.info(f"  [LIVE SQUAREOFF HEDGE] SELL {qty}x {symbol} ({token})")
                order_id = self.api.place_order(symbol, token, "SELL", qty)
                if order_id is None:
                    logger.error(f"  [SQUAREOFF FAILED] Could not close hedge leg {symbol}! Manual intervention needed.")
            self.pm.close_leg(leg["id"], leg.get("current_premium", 0.0), reason)

        self.pm.position["status"] = "CLOSED"
        self.pm.save()

    def _deploy_overnight_shield(self, spot, T):
        """
        Deploy the Overnight Shield (15:29 Expiry / 15:44 Non-Expiry).
        Buys 100-point OTM Call and Put hedges from active open short strikes.
        """
        timing_label = "15:29 IST" if is_expiry_day(now_ist().date()) else "15:44 IST"
        print_banner(f"DEPLOYING OVERNIGHT SHIELD ({timing_label})")
        logger.info(f"[OVERNIGHT SHIELD] Calculating 100-pt gap hedges ({timing_label})...")

        shield_data = self.adj_engine.calculate_overnight_shield(spot, T, self.cached_chain_ltps)
        if not shield_data:
            logger.warning("[OVERNIGHT SHIELD] Could not calculate shield strikes. Skipping deployment.")
            return False

        ce_h = shield_data["CE"]
        pe_h = shield_data["PE"]

        logger.info(f"  Shield BUY Call Hedge: Strike {ce_h['strike']} (Δ={ce_h['delta']:.4f}, Prem={ce_h['premium']:.2f}, Qty={ce_h['quantity']})")
        logger.info(f"  Shield BUY Put  Hedge: Strike {pe_h['strike']} (Δ={pe_h['delta']:.4f}, Prem={pe_h['premium']:.2f}, Qty={pe_h['quantity']})")

        legs = [
            ("LONG_CALL", ce_h["strike"], "CE", ce_h["delta"], ce_h["iv"], ce_h["premium"], "BUY", True, ce_h["quantity"], True),
            ("LONG_PUT", pe_h["strike"], "PE", pe_h["delta"], pe_h["iv"], pe_h["premium"], "BUY", True, pe_h["quantity"], True),
        ]

        for leg_args in legs:
            success = self._place_leg(*leg_args)
            if not success:
                logger.error(f"[OVERNIGHT SHIELD] Failed to place order for {leg_args[0]} @ {leg_args[1]}")

        logger.info("[OVERNIGHT SHIELD] 100-pt gap structural protection active for overnight session!")
        return True

    def _unwind_overnight_shield(self):
        """
        Unwind the Overnight Shield at 09:15 IST.
        Sells all open overnight hedge legs, returning position to pure daytime strangle/straddle.
        """
        print_banner("UNWINDING OVERNIGHT SHIELD (09:15 IST)")
        open_hedges = list(self.pm.open_overnight_hedge_legs)
        logger.info(f"[OVERNIGHT UNWIND] Selling {len(open_hedges)} open overnight hedge legs...")

        for leg in open_hedges:
            if not self.paper_mode and self.api:
                symbol = leg.get("trading_symbol") or f"NIFTY_{int(leg['strike'])}_{leg['option_type']}"
                token = leg.get("symbol_token", "")
                qty = leg.get("quantity") or (config.LOT_SIZE * config.NUM_LOTS)
                logger.info(f"  [LIVE SQUAREOFF SHIELD] SELL {qty}x {symbol} ({token})")
                order_id = self.api.place_order(symbol, token, "SELL", qty)
                if order_id is None:
                    logger.error(f"  [SHIELD UNWIND FAILED] Could not close shield leg {symbol}! Manual check needed.")

            self.pm.close_leg(leg["id"], leg.get("current_premium", 0.0), reason="OVERNIGHT_SHIELD_UNWIND_09:15")

        logger.info("[OVERNIGHT UNWIND COMPLETE] Position returned to pure daytime harvest mode.")

    def _monitor_and_adjust(self, spot, T, now):
        """
        Refresh option chain, update Greeks, check adjustment triggers.

        WebSocket mode: Real-time data already streaming, just read latest state.
        REST mode: Poll every N seconds (rate-limited).
        """
        if self.use_websocket and self.ws_feeder and self.ws_feeder.is_connected():
            # WebSocket: Read real-time chain (no rate limits, no delays)
            chain_ltps = self.ws_feeder.get_chain_ltps()
            if chain_ltps["CE"] or chain_ltps["PE"]:
                self.cached_chain_ltps = chain_ltps
        else:
            # REST fallback: refresh every N seconds
            elapsed = time.time() - self.last_chain_refresh
            if elapsed >= config.OPTION_CHAIN_REFRESH_SECONDS:
                try:
                    self.cached_chain_ltps = self.api.get_option_chain_ltps(
                        self.expiry_date, spot
                    )
                    self.last_chain_refresh = time.time()
                except Exception as e:
                    logger.debug(f"Chain refresh failed: {e}")
                    return

        # Update live premiums for all open legs (same real data in paper & live)
        if self.use_websocket and self.ws_feeder and self.ws_feeder.is_connected():
            # WebSocket: Get live LTP for each open leg from streaming data
            for leg in self.pm.open_legs:
                strike = leg["strike"]
                opt_type = leg["option_type"]
                if opt_type in self.cached_chain_ltps and strike in self.cached_chain_ltps[opt_type]:
                    live_ltp = self.cached_chain_ltps[opt_type][strike]
                    if live_ltp > 0:
                        self.pm.update_leg_premium(leg["id"], live_ltp)
        else:
            # REST fallback for individual leg LTPs
            open_leg_query = [
                {"symbol": leg.get("trading_symbol", f"NIFTY_{int(leg['strike'])}_{leg['option_type']}"),
                 "token": leg.get("symbol_token", ""),
                 "strike": leg["strike"],
                 "type": leg["option_type"]}
                for leg in self.pm.open_legs
            ]
            if open_leg_query:
                try:
                    live_leg_ltps = self.api.get_multiple_option_ltps(open_leg_query)
                    for leg in self.pm.open_legs:
                        key = (leg["strike"], leg["option_type"])
                        if key in live_leg_ltps:
                            self.pm.update_leg_premium(leg["id"], live_leg_ltps[key])
                except Exception as e:
                    logger.debug(f"Direct leg LTP refresh error: {e}")

        # Calculate portfolio Greeks
        portfolio = calculate_portfolio_greeks(self.pm.open_legs, spot, T)
        net_delta = portfolio["net_delta"]

        # Fetch deployed margin (utilizes 10s TTL cache, 0.1 req/sec max)
        deployed_margin = self.api.get_deployed_margin(
            num_lots=config.NUM_LOTS,
            is_straddle=self.pm.is_straddle
        )

        # Persist live greeks, margin & mode for Node.js Web Dashboard streaming
        self.pm.update_live_greeks(portfolio, spot, deployed_margin=deployed_margin, is_paper=self.paper_mode)

        # Print status every refresh
        self._print_live_status(spot, T, portfolio)

        # ─── MORNING OVERNIGHT SHIELD EVALUATION & UNWIND (09:15 IST) ──
        if getattr(config, "OVERNIGHT_SHIELD_ENABLED", True):
            if now.hour == config.OVERNIGHT_SHIELD_SELL_HOUR and now.minute >= config.OVERNIGHT_SHIELD_SELL_MINUTE:
                if self.pm.open_overnight_hedge_legs:
                    today_str = str(now.date())
                    # 1. Check if gap breach hold is already active from earlier today
                    if self.pm.position.get("gap_breach_active"):
                        hold_until_str = self.pm.position.get("gap_breach_hold_until", "")
                        if hold_until_str:
                            try:
                                hold_until_dt = datetime.strptime(hold_until_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                                if now >= hold_until_dt:
                                    print_banner("GAP BREACH HOLD DURATION COMPLETED - CLOSING ALL POSITIONS")
                                    logger.info(f"[GAP BREACH AUTO-EXIT] {config.GAP_BREACH_HOLD_HOURS}h protection hold completed. Closing Iron Condor.")
                                    self._close_all_positions(reason="GAP_BREACH_TIMEOUT_EXIT")
                                    return
                            except Exception as e:
                                logger.error(f"Error parsing gap_breach_hold_until: {e}")

                    # 2. Check opening gap breach if not evaluated today
                    elif self.pm.position.get("last_gap_check_date") != today_str:
                        short_ce = self.pm.short_call_legs[-1] if self.pm.short_call_legs else None
                        short_pe = self.pm.short_put_legs[-1] if self.pm.short_put_legs else None

                        ce_breached = (spot >= short_ce["strike"]) if short_ce else False
                        pe_breached = (spot <= short_pe["strike"]) if short_pe else False

                        if ce_breached or pe_breached:
                            breached_info = f"CE {short_ce['strike']}" if ce_breached else f"PE {short_pe['strike']}"
                            print_banner(f"FAT-TAIL GAP BREACH DETECTED: {breached_info} ITM")
                            logger.warning(
                                f"[GAP BREACH] Spot {spot:.2f} breached short leg ({breached_info})! "
                                f"OVERNIGHT HEDGES RETAINED (Iron Condor armor active) to cap max loss at ~Rs.4,940. "
                                f"Position will be held for {config.GAP_BREACH_HOLD_HOURS} hours before closing."
                            )
                            hold_until = now + timedelta(hours=config.GAP_BREACH_HOLD_HOURS)
                            self.pm.position["gap_breach_active"] = True
                            self.pm.position["gap_breach_detected_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
                            self.pm.position["gap_breach_hold_until"] = hold_until.strftime("%Y-%m-%d %H:%M:%S")
                            self.pm.position["last_gap_check_date"] = today_str
                            self.pm.save()
                        else:
                            # Normal open! Both short legs remain safely OTM.
                            ce_str = f"CE {short_ce['strike']}" if short_ce else "N/A"
                            pe_str = f"PE {short_pe['strike']}" if short_pe else "N/A"
                            logger.info(
                                f"[NORMAL OPEN] Spot {spot:.2f} safely between {pe_str} and {ce_str}. "
                                f"Both short legs OTM. Unwinding overnight shield at 09:15 IST."
                            )
                            self._unwind_overnight_shield()
                            self.pm.position["last_shield_unwound_date"] = today_str
                            self.pm.position["last_gap_check_date"] = today_str
                            self.pm.position["gap_breach_active"] = False
                            self.pm.save()

        # ─── PRE-CLOSE OVERNIGHT SHIELD DEPLOYMENT (15:29 Expiry / 15:44 Non-Expiry) ──
        if getattr(config, "OVERNIGHT_SHIELD_ENABLED", True) and self.pm.open_short_legs:
            buy_hour, buy_minute = get_shield_buy_time(now.date())
            if now.hour == buy_hour and now.minute >= buy_minute:
                today_str = str(now.date())
                if self.pm.position.get("last_shield_bought_date") != today_str:
                    if not self.pm.open_overnight_hedge_legs:
                        self._deploy_overnight_shield(spot, T)
                        self.pm.position["last_shield_bought_date"] = today_str
                        self.pm.save()

        # ─── EXPIRY DAY AUTO-SQUAREOFF (15:15 IST) ────────────────
        if is_expiry_day(now.date()) or (self.expiry_date and now.date() >= self.expiry_date):
            if now.hour == 15 and now.minute >= 15:
                print_banner("EXPIRY DAY AUTO-SQUAREOFF (15:15 IST)")
                logger.info(f"Expiry day ({self.expiry_date}) market auto-squareoff reached at 15:15 IST. Closing all positions.")
                self._close_all_positions(reason="EXPIRY_AUTO_SQUAREOFF_15:15")
                return

        # ─── GAP BREACH DEFENSE MODE (NO STRANGLE ADJUSTMENTS) ─────
        if self.pm.position.get("gap_breach_active"):
            # Check 2% Capital Stop Loss while holding Iron Condor
            sl_hit, sl_reason = self.pm.check_straddle_stop_loss(spot, deployed_capital=deployed_margin)
            if sl_hit:
                print_banner("GAP BREACH CAPITAL STOP LOSS TRIGGERED")
                logger.warning(f"[GAP BREACH EXIT] {sl_reason}")
                self._close_all_positions(reason=sl_reason)
                return
            return  # Skip regular strangle adjustments while holding Iron Condor armor


        # ─── STRADDLE PHASE MONITORING (ZERO ADJUSTMENTS) ──────────
        if self.pm.is_straddle:
            # 2. Check Strict 2.0% Deployed Capital Stop Loss & Spot Circuit Breaker
            sl_hit, sl_reason = self.pm.check_straddle_stop_loss(spot, deployed_capital=deployed_margin)
            if sl_hit:
                print_banner("STRADDLE STOP LOSS TRIGGERED")
                logger.warning(f"[STRADDLE EXIT] {sl_reason}")
                self._close_all_positions(reason=sl_reason)
                return

            # 3. Check 70% Straddle Premium Decay Profit Target
            tp_hit, tp_reason = self.pm.check_straddle_profit_target()
            if tp_hit:
                print_banner("STRADDLE 70% THETA DECAY PROFIT TARGET REACHED")
                logger.info(f"[STRADDLE PROFIT EXIT] {tp_reason}")
                self._close_all_positions(reason=tp_reason)
                return

            # Zero adjustments during straddle mode
            return

        # ─── NON-STRADDLE / OTM PHASE MONITORING ───────────────────
        # 1. Check if both short legs have decayed below ₹1.00 (Full OTM Profit Capture)
        otm_hit, otm_reason = self.pm.check_otm_full_decay()
        if otm_hit:
            print_banner("OTM FULL DECAY PROFIT TARGET REACHED (< ₹1.00)")
            logger.info(f"[OTM PROFIT EXIT] {otm_reason}")
            self._close_all_positions(reason=otm_reason)
            return

        # 2. Evaluate Dynamic Adjustment Triggers (e.g. 50% Losing Leg Surge)
        action = self.adj_engine.evaluate(spot, T, self.cached_chain_ltps)
        if action:
            self.adj_engine.execute_adjustment(
                action, spot, T, self.cached_chain_ltps,
                paper_mode=self.paper_mode
            )

    def _print_live_status(self, spot, T, portfolio):
        """Print concise live dashboard."""
        net_d = portfolio["net_delta"]
        net_g = portfolio.get("net_gamma", 0)
        net_t = portfolio.get("net_theta", 0)

        pnl = self.pm.total_pnl
        adj_count = self.pm.position["adjustment_count"]
        straddle = "YES" if self.pm.is_straddle else "NO"

        # Data source indicator
        if self.use_websocket and self.ws_feeder and self.ws_feeder.is_connected():
            src = "WS"
            stats = self.ws_feeder.get_stats()
            ticks = stats.get("total_ticks_received", 0)
            src_info = f"WS:{ticks}ticks"
        else:
            src_info = "REST"

        status_line = (
            f"[{now_ist().strftime('%H:%M:%S')}] "
            f"Spot={spot:>9.2f} | "
            f"Delta={net_d:>+7.4f} | "
            f"Gamma={net_g:>+8.5f} | "
            f"Theta={net_t:>+7.2f} | "
            f"P&L={format_pnl(pnl):>12} | "
            f"Adj={adj_count} | "
            f"Straddle={straddle} | "
            f"{src_info}"
        )
        try:
            print(status_line)
        except UnicodeEncodeError:
            print(status_line.encode('ascii', 'ignore').decode('ascii'))


def main():
    parser = argparse.ArgumentParser(description="Delta Neutral Nifty Strategy")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: paper)")
    args = parser.parse_args()

    runner = StrategyRunner(paper_mode=not args.live)
    runner.start()


if __name__ == "__main__":
    main()
