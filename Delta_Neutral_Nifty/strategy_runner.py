"""
Delta Neutral Nifty - Strategy Runner
Main orchestrator: entry, monitoring, adjustment, and expiry handling.

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

    def start(self):
        """Main entry point."""
        mode_str = "PAPER TRADING" if self.paper_mode else "⚡ LIVE TRADING ⚡"
        print_banner(f"DELTA NEUTRAL NIFTY - {mode_str}")
        print(f"  Lot Size: {config.LOT_SIZE} x {config.NUM_LOTS} lot(s)")
        print(f"  Entry Delta: +/-{config.ENTRY_DELTA} | Hedge Delta: +/-{config.HEDGE_DELTA}")
        print(f"  Monitoring: Every {config.OPTION_CHAIN_REFRESH_SECONDS}s")
        print(f"  Adjustment Triggers: NetDelta>{config.PORTFOLIO_DELTA_BREACH}, "
              f"PremCapture>{config.PREMIUM_CAPTURE_PCT*100:.0f}%, "
              f"LosingDelta>{config.LOSING_LEG_DELTA_THRESHOLD}, "
              f"Gamma>{config.GAMMA_DANGER_THRESHOLD}")

        # Login
        self.api.login()
        self.api.fetch_instrument_master()

        # Determine expiry dynamically from Angel One master
        self.expiry_date = self.api.get_nearest_expiry_date()
        self.adj_engine = AdjustmentEngine(self.pm, self.api, self.expiry_date)

        logger.info(f"Current weekly expiry: {self.expiry_date}")

        # Check if we need to initialize a new position
        if not self.pm.is_active:
            if self.pm.position.get("expiry_date") != str(self.expiry_date):
                self.pm.initialize_week(self.expiry_date)

        # Main loop
        self._run_loop()

    def _run_loop(self):
        """Continuous monitoring loop during market hours."""
        while True:
            now = now_ist()

            if not is_market_open(now):
                # Check if post-market on expiry day
                if is_expiry_day(now.date()) and now.hour >= 15:
                    if self.pm.is_active:
                        logger.info("Expiry day market closed. Closing all positions.")
                        self.pm.close_all("EXPIRY_CLOSE")
                    break

                # Pre-market or post-market check
                if now.hour < config.MARKET_OPEN_HOUR or (now.hour == config.MARKET_OPEN_HOUR and now.minute < config.MARKET_OPEN_MINUTE):
                    logger.info("Pre-market. Waiting for 09:15 IST...")
                    time.sleep(10)
                    continue
                elif now.hour > config.MARKET_CLOSE_HOUR or (now.hour == config.MARKET_CLOSE_HOUR and now.minute >= config.MARKET_CLOSE_MINUTE):
                    logger.info("[AUTO-STOP] Market closed at 15:30 IST. Exiting process to save GitHub minutes.")
                    print_banner("MARKET CLOSED (15:30 IST) - ENGINE EXITING CLEANLY")
                    break

            try:
                spot = self.api.get_spot_ltp()
                if self.paper_mode:
                    import random
                    spot += random.uniform(-0.30, 0.30)
                T = time_to_expiry_years(self.expiry_date, now)

                # Phase 1: Initial entry if no open short legs
                if not self.pm.open_short_legs:
                    self._execute_initial_entry(spot, T)

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
        SELL 0.15-delta CE + PE, BUY 0.05-delta CE + PE
        """
        print_banner("INITIAL IRON CONDOR ENTRY")
        logger.info(f"Spot: {spot:.2f} | ATM: {get_atm_strike(spot)} | T={T:.6f}y")

        # Fetch full option chain
        chain_ltps = self.api.get_option_chain_ltps(self.expiry_date, spot)
        self.cached_chain_ltps = chain_ltps

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

        logger.info(f"  SELL CE @ {ce_strike} (Δ={ce_delta:.4f}, IV={ce_iv*100:.1f}%, Prem={ce_prem:.2f})")
        logger.info(f"  SELL PE @ {pe_strike} (Δ={pe_delta:.4f}, IV={pe_iv*100:.1f}%, Prem={pe_prem:.2f})")
        logger.info(f"  BUY  CE @ {ce_h_strike} (Δ={ce_h_delta:.4f}, Prem={ce_h_prem:.2f}) [HEDGE]")
        logger.info(f"  BUY  PE @ {pe_h_strike} (Δ={pe_h_delta:.4f}, Prem={pe_h_prem:.2f}) [HEDGE]")

        net_credit = (ce_prem + pe_prem) - (ce_h_prem + pe_h_prem)
        logger.info(f"  Net Credit: {net_credit:.2f} pts = ₹{net_credit * config.LOT_SIZE:,.2f}")

        # Place orders (or simulate)
        self._place_leg("SHORT_CALL", ce_strike, "CE", ce_delta, ce_iv, ce_prem, "SELL", False)
        self._place_leg("SHORT_PUT", pe_strike, "PE", pe_delta, pe_iv, pe_prem, "SELL", False)
        self._place_leg("LONG_CALL", ce_h_strike, "CE", ce_h_delta, ce_h_iv, ce_h_prem, "BUY", True)
        self._place_leg("LONG_PUT", pe_h_strike, "PE", pe_h_delta, pe_h_iv, pe_h_prem, "BUY", True)

        print_banner("IRON CONDOR ENTERED SUCCESSFULLY")

    def _place_leg(self, leg_type, strike, opt_type, delta_val, iv, premium, txn, is_hedge):
        """Place order and add leg to position manager."""
        token_info = self.api.get_token_info(self.expiry_date, strike, opt_type)
        token = token_info["token"] if token_info else ""
        symbol = token_info["symbol"] if token_info else f"NIFTY_{int(strike)}_{opt_type}"

        if not self.paper_mode and token_info:
            self.api.place_order(symbol, token, txn, config.LOT_SIZE * config.NUM_LOTS)

        self.pm.add_leg(
            leg_type=leg_type, strike=strike, option_type=opt_type,
            delta_at_entry=delta_val, iv_at_entry=iv, entry_premium=premium,
            symbol_token=token, trading_symbol=symbol, is_hedge=is_hedge,
        )

    def _monitor_and_adjust(self, spot, T, now):
        """Refresh option chain, update Greeks, check adjustment triggers."""
        elapsed = time.time() - self.last_chain_refresh

        # Refresh option chain LTPs every N seconds
        if elapsed >= config.OPTION_CHAIN_REFRESH_SECONDS:
            try:
                self.cached_chain_ltps = self.api.get_option_chain_ltps(
                    self.expiry_date, spot
                )
                self.last_chain_refresh = time.time()
            except Exception as e:
                logger.debug(f"Chain refresh failed: {e}")
                return

        # Update live premiums for all open legs (Sub-second tick update)
        if self.paper_mode:
            for leg in self.pm.open_legs:
                iv = leg.get("iv_at_entry", 0.12)
                bs_prem = bs_price(spot, leg["strike"], T, config.RISK_FREE_RATE, iv, leg["option_type"])
                if bs_prem > 0:
                    self.pm.update_leg_premium(leg["id"], round(bs_prem, 2))
        else:
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

        # Persist live greeks for Node.js Web Dashboard streaming
        self.pm.update_live_greeks(portfolio, spot)

        # Print status every refresh
        self._print_live_status(spot, T, portfolio)

        # If straddle already reached: STOP adjustments and monitor Straddle Stop Loss
        if self.pm.is_straddle:
            sl_hit, sl_reason = self.pm.check_straddle_stop_loss(spot)
            if sl_hit:
                print_banner("🚨 STRADDLE STOP LOSS TRIGGERED 🚨")
                logger.warning(f"[STRADDLE EXIT] {sl_reason}")
                self.pm.close_all(reason=sl_reason)
            return

        # Evaluate adjustment
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
        straddle = "YES ⛔" if self.pm.is_straddle else "NO"

        status_line = (
            f"[{now_ist().strftime('%H:%M:%S')}] "
            f"Spot={spot:>9.2f} | "
            f"Delta={net_d:>+7.4f} | "
            f"Gamma={net_g:>+8.5f} | "
            f"Theta={net_t:>+7.2f} | "
            f"P&L={format_pnl(pnl):>12} | "
            f"Adj={adj_count} | "
            f"Straddle={straddle}"
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
