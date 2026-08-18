"""
Delta Neutral Nifty - Strategy Runner
Main orchestrator: entry, monitoring, adjustment, and expiry handling.

Uses WebSocket V2 for real-time streaming of ALL option strikes.
Falls back to REST API if WebSocket is unavailable.

Usage:
    python -m Delta_Neutral_Nifty.strategy_runner             # Paper mode (default)
    python -m Delta_Neutral_Nifty.strategy_runner --live       # Live trading
"""
import sys
import time
import argparse

from . import config
from .utils import (
    setup_logger, now_ist, is_market_open, is_entry_window,
    get_current_expiry, is_expiry_day, time_to_expiry_years,
    get_atm_strike, print_banner, print_position_table, format_pnl, format_delta,
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

                # Pre-market or post-market check
                if now.hour < config.MARKET_OPEN_HOUR or (now.hour == config.MARKET_OPEN_HOUR and now.minute < config.MARKET_OPEN_MINUTE):
                    logger.info("Pre-market. Waiting for 09:15 IST...")
                    time.sleep(10)
                    continue
                elif now.hour > config.MARKET_CLOSE_HOUR or (now.hour == config.MARKET_CLOSE_HOUR and now.minute >= config.MARKET_CLOSE_MINUTE):
                    close_time_str = f"{config.MARKET_CLOSE_HOUR:02d}:{config.MARKET_CLOSE_MINUTE:02d}"
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
        Execute the initial Iron Condor:
        1. BUY 0.05-delta CE + PE (Hedges first for RMS margin reduction)
        2. SELL 0.15-delta CE + PE (Short strangle legs)

        Uses WebSocket V2 real-time LTPs for ALL strikes simultaneously.
        """
        print_banner("INITIAL IRON CONDOR ENTRY")
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
        # ─── BUY 0.05 Delta CALL (hedge) ─────────────────────────
        ce_h_strike, ce_h_delta, ce_h_iv, ce_h_prem = find_strike_at_delta(
            chain_ltps["CE"], spot, config.HEDGE_DELTA, "CE", T, r
        )
        # ─── BUY 0.05 Delta PUT (hedge) ──────────────────────────
        pe_h_strike, pe_h_delta, pe_h_iv, pe_h_prem = find_strike_at_delta(
            chain_ltps["PE"], spot, config.HEDGE_DELTA, "PE", T, r
        )

        if not all([ce_strike, pe_strike, ce_h_strike, pe_h_strike]):
            logger.error("Could not find all required strikes!")
            return

        logger.info(f"  BUY  CE @ {ce_h_strike} (Delta={ce_h_delta:.4f}, Prem={ce_h_prem:.2f}) [HEDGE FIRST]")
        logger.info(f"  BUY  PE @ {pe_h_strike} (Delta={pe_h_delta:.4f}, Prem={pe_h_prem:.2f}) [HEDGE FIRST]")
        logger.info(f"  SELL CE @ {ce_strike} (Delta={ce_delta:.4f}, IV={ce_iv*100:.1f}%, Prem={ce_prem:.2f})")
        logger.info(f"  SELL PE @ {pe_strike} (Delta={pe_delta:.4f}, IV={pe_iv*100:.1f}%, Prem={pe_prem:.2f})")

        net_credit = (ce_prem + pe_prem) - (ce_h_prem + pe_h_prem)
        logger.info(f"  Net Credit: {net_credit:.2f} pts = Rs.{net_credit * config.LOT_SIZE:,.2f}")

        # Place orders (Hedge first for broker margin benefit, then short legs)
        self._place_leg("LONG_CALL", ce_h_strike, "CE", ce_h_delta, ce_h_iv, ce_h_prem, "BUY", True)
        self._place_leg("LONG_PUT", pe_h_strike, "PE", pe_h_delta, pe_h_iv, pe_h_prem, "BUY", True)
        self._place_leg("SHORT_CALL", ce_strike, "CE", ce_delta, ce_iv, ce_prem, "SELL", False)
        self._place_leg("SHORT_PUT", pe_strike, "PE", pe_delta, pe_iv, pe_prem, "SELL", False)

        print_banner("IRON CONDOR ENTERED SUCCESSFULLY")

    def _place_leg(self, leg_type, strike, opt_type, delta_val, iv, premium, txn, is_hedge):
        """Place order and add leg to position manager."""
        token_info = self.api.get_token_info(self.expiry_date, strike, opt_type) if self.api else None
        token = token_info["token"] if token_info else ""
        symbol = token_info["symbol"] if token_info else f"NIFTY_{int(strike)}_{opt_type}"

        if not self.paper_mode and token_info and self.api:
            self.api.place_order(symbol, token, txn, config.LOT_SIZE * config.NUM_LOTS)

        self.pm.add_leg(
            leg_type=leg_type, strike=strike, option_type=opt_type,
            delta_at_entry=delta_val, iv_at_entry=iv, entry_premium=premium,
            symbol_token=token, trading_symbol=symbol, is_hedge=is_hedge,
        )

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
                self.api.place_order(symbol, token, "BUY", qty)
            self.pm.close_leg(leg["id"], leg.get("current_premium", 0.0), reason)

        # 2. Close Hedge legs second (SELL)
        for leg in list(self.pm.open_hedge_legs):
            if not self.paper_mode and self.api:
                symbol = leg.get("trading_symbol") or f"NIFTY_{int(leg['strike'])}_{leg['option_type']}"
                token = leg.get("symbol_token", "")
                qty = leg.get("quantity") or (config.LOT_SIZE * config.NUM_LOTS)
                logger.info(f"  [LIVE SQUAREOFF HEDGE] SELL {qty}x {symbol} ({token})")
                self.api.place_order(symbol, token, "SELL", qty)
            self.pm.close_leg(leg["id"], leg.get("current_premium", 0.0), reason)

        self.pm.position["status"] = "CLOSED"
        self.pm.save()

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

        # ─── EXPIRY DAY AUTO-SQUAREOFF (15:15 IST) ────────────────
        if is_expiry_day(now.date()) or (self.expiry_date and now.date() >= self.expiry_date):
            if now.hour == 15 and now.minute >= 15:
                print_banner("EXPIRY DAY AUTO-SQUAREOFF (15:15 IST)")
                logger.info(f"Expiry day ({self.expiry_date}) market auto-squareoff reached at 15:15 IST. Closing all positions.")
                self._close_all_positions(reason="EXPIRY_AUTO_SQUAREOFF_15:15")
                return

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
