"""
Delta Neutral Nifty - WebSocket V2 Live Option Chain Feeder
============================================================
Subscribes to ALL Nifty option strikes via Angel One SmartWebSocket V2.
Provides real-time LTP streaming for the entire option chain.
On every tick, Delta and all Greeks are recalculated using Black-Scholes.

Key Features:
  - Subscribe up to 1000 tokens in one WebSocket session (Angel One limit)
  - Zero rate-limit issues (WebSocket is unlimited streaming)
  - Real-time Greeks (Delta, Gamma, Theta, Vega) on every price tick
  - Thread-safe shared state for strategy_runner to read
"""
import threading
import time
import math
from collections import defaultdict

from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from . import config
from .utils import setup_logger, now_ist
from . import greeks_engine

logger = setup_logger("ws_feeder")

# Angel One exchange type codes
EXCHANGE_NSE_FO = 2   # NSE Futures & Options


class OptionTick:
    """Represents a single option contract's live state."""
    __slots__ = [
        "token", "symbol", "strike", "option_type", "expiry",
        "ltp", "iv", "delta", "gamma", "theta", "vega",
        "last_update_time",
    ]

    def __init__(self, token, symbol, strike, option_type, expiry):
        self.token = token
        self.symbol = symbol
        self.strike = float(strike)
        self.option_type = option_type  # "CE" or "PE"
        self.expiry = expiry
        self.ltp = 0.0
        self.iv = 0.0
        self.delta = 0.0
        self.gamma = 0.0
        self.theta = 0.0
        self.vega = 0.0
        self.last_update_time = 0.0

    def to_dict(self):
        return {
            "token": self.token,
            "symbol": self.symbol,
            "strike": self.strike,
            "option_type": self.option_type,
            "ltp": self.ltp,
            "iv": self.iv,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
        }


class WebSocketFeeder:
    """
    Real-time option chain feeder using Angel One WebSocket V2.

    Usage:
        feeder = WebSocketFeeder(api)
        feeder.start(expiry_date, spot_price)
        # ... later in strategy loop ...
        chain = feeder.get_full_chain()  # {strike: {"CE": OptionTick, "PE": OptionTick}}
        ce_ltps = feeder.get_ltps("CE")  # {strike: ltp}
    """

    def __init__(self, api):
        """
        Args:
            api: AngelOneAPI instance (must be logged in with feed_token available)
        """
        self.api = api
        self.sws = None
        self._ws_thread = None
        self._connected = False
        self._stop_event = threading.Event()

        # Thread-safe live data store
        self._lock = threading.RLock()
        self._chain = {}           # {strike: {"CE": OptionTick, "PE": OptionTick}}
        self._token_map = {}       # {token_str: OptionTick}
        self._spot_price = 0.0
        self._expiry_date = None
        self._T = 0.0              # Time to expiry in years

        # Stats
        self._tick_count = 0
        self._last_tick_time = 0.0
        self._greeks_calc_count = 0

    # ─── PUBLIC API ──────────────────────────────────────────────

    def start(self, expiry_date, spot_price, range_pct=0.08):
        """
        Start WebSocket and subscribe to all Nifty option tokens for given expiry.

        Args:
            expiry_date: date object for target expiry
            spot_price: current Nifty spot for determining strike range
            range_pct: how far from ATM to scan (0.08 = ±8% = ~80+ strikes CE+PE)
        """
        self._expiry_date = expiry_date
        self._spot_price = spot_price

        # Build token subscription list from instrument master
        chain_master = self.api.get_option_chain_for_expiry(expiry_date)
        if not chain_master:
            logger.error("No option chain found in instrument master!")
            return False

        lower_bound = spot_price * (1 - range_pct)
        upper_bound = spot_price * (1 + range_pct)

        nfo_tokens = []
        subscribed_count = 0

        for strike, opts in chain_master.items():
            strike_f = float(strike)
            if lower_bound <= strike_f <= upper_bound:
                for opt_type in ("CE", "PE"):
                    if opt_type in opts:
                        token = opts[opt_type]["token"]
                        symbol = opts[opt_type]["symbol"]

                        tick = OptionTick(token, symbol, strike_f, opt_type, str(expiry_date))

                        with self._lock:
                            if strike_f not in self._chain:
                                self._chain[strike_f] = {}
                            self._chain[strike_f][opt_type] = tick
                            self._token_map[str(token)] = tick

                        nfo_tokens.append(str(token))
                        subscribed_count += 1

        if not nfo_tokens:
            logger.error("No tokens to subscribe!")
            return False

        # Angel One limit: max 1000 tokens per WebSocket session
        if len(nfo_tokens) > 1000:
            logger.warning(f"Token count {len(nfo_tokens)} exceeds 1000 limit. Trimming to nearest strikes.")
            # Keep strikes closest to ATM
            sorted_strikes = sorted(self._chain.keys(), key=lambda s: abs(s - spot_price))
            trimmed_tokens = []
            trimmed_chain = {}
            for s in sorted_strikes:
                if len(trimmed_tokens) >= 998:  # Leave room for spot
                    break
                for ot in ("CE", "PE"):
                    if ot in self._chain[s]:
                        trimmed_tokens.append(str(self._chain[s][ot].token))
                trimmed_chain[s] = self._chain[s]

            with self._lock:
                self._chain = trimmed_chain
                self._token_map = {str(t.token): t for s in self._chain.values() for t in s.values()}
            nfo_tokens = trimmed_tokens

        logger.info(f"[WS FEEDER] Subscribing to {subscribed_count} option tokens "
                     f"({len(self._chain)} strikes x CE/PE) for expiry {expiry_date}")

        # Also subscribe to Nifty spot token for live spot
        spot_token = config.NIFTY_SPOT_TOKEN

        # Start WebSocket in background thread
        self._nfo_tokens = nfo_tokens
        self._spot_token = spot_token
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True, name="WS-Feeder")
        self._ws_thread.start()

        # Wait for connection
        for _ in range(50):  # 5 seconds max
            if self._connected:
                logger.info(f"[WS FEEDER] Connected and streaming {subscribed_count} tokens!")
                return True
            time.sleep(0.1)

        logger.warning("[WS FEEDER] WebSocket connection timed out, falling back to REST")
        return False

    def stop(self):
        """Stop WebSocket connection."""
        self._stop_event.set()
        if self.sws:
            try:
                self.sws.close_connection()
            except Exception:
                pass
        self._connected = False
        logger.info("[WS FEEDER] Stopped")

    def update_spot(self, spot_price):
        """Update spot price (called from strategy runner)."""
        with self._lock:
            self._spot_price = spot_price

    def update_time_to_expiry(self, T):
        """Update time to expiry in years."""
        with self._lock:
            self._T = T

    def is_connected(self):
        """Check if WebSocket is actively connected."""
        return self._connected

    def get_spot(self):
        """Get latest spot price."""
        with self._lock:
            return self._spot_price

    def get_full_chain(self):
        """
        Get the entire live option chain with Greeks.
        Returns: {strike: {"CE": OptionTick, "PE": OptionTick}}
        """
        with self._lock:
            return dict(self._chain)

    def get_ltps(self, option_type):
        """
        Get all LTPs for a given option type (for find_strike_at_delta compatibility).
        Returns: {strike: ltp}
        """
        result = {}
        with self._lock:
            for strike, opts in self._chain.items():
                if option_type in opts and opts[option_type].ltp > 0:
                    result[strike] = opts[option_type].ltp
        return result

    def get_chain_ltps(self):
        """
        Get LTPs in the format strategy_runner expects.
        Returns: {"CE": {strike: ltp}, "PE": {strike: ltp}}
        """
        return {"CE": self.get_ltps("CE"), "PE": self.get_ltps("PE")}

    def get_strike_greeks(self, strike, option_type):
        """Get live Greeks for a specific strike."""
        with self._lock:
            if strike in self._chain and option_type in self._chain[strike]:
                return self._chain[strike][option_type].to_dict()
        return None

    def get_all_deltas(self, option_type):
        """
        Get Delta for ALL strikes of a given option type.
        Returns: {strike: abs_delta} sorted by strike.
        """
        result = {}
        with self._lock:
            for strike in sorted(self._chain.keys()):
                if option_type in self._chain[strike]:
                    tick = self._chain[strike][option_type]
                    if tick.ltp > 0:
                        result[strike] = abs(tick.delta)
        return result

    def get_stats(self):
        """Get feeder statistics."""
        with self._lock:
            active_ticks = sum(
                1 for s in self._chain.values()
                for t in s.values() if t.ltp > 0
            )
        return {
            "connected": self._connected,
            "total_tokens": len(self._token_map),
            "active_ticks": active_ticks,
            "total_ticks_received": self._tick_count,
            "greeks_calculations": self._greeks_calc_count,
            "last_tick_time": self._last_tick_time,
        }

    # ─── WEBSOCKET INTERNALS ─────────────────────────────────────

    def _ws_loop(self):
        """WebSocket connection loop with auto-reconnect."""
        while not self._stop_event.is_set():
            try:
                self._connect_and_stream()
            except Exception as e:
                logger.error(f"[WS FEEDER] Connection error: {e}")

            if not self._stop_event.is_set():
                self._connected = False
                logger.info(f"[WS FEEDER] Reconnecting in {config.WEBSOCKET_RECONNECT_SECONDS}s...")
                time.sleep(config.WEBSOCKET_RECONNECT_SECONDS)

    def _connect_and_stream(self):
        """Establish WebSocket connection and subscribe to tokens."""
        api_key = self.api.credentials.get("ANGEL_API_KEY")
        client_code = self.api.client_code
        feed_token = self.api.feed_token
        auth_token = self.api.auth_token

        if not all([api_key, client_code, feed_token, auth_token]):
            logger.error("[WS FEEDER] Missing credentials for WebSocket!")
            return

        self.sws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)

        def on_open(wsapp):
            logger.info("[WS FEEDER] WebSocket connected, subscribing tokens...")
            self._connected = True

            # Subscribe NFO option tokens (LTP mode = 1)
            token_list = [
                {"exchangeType": EXCHANGE_NSE_FO, "tokens": self._nfo_tokens}
            ]

            # Also subscribe Nifty spot (NSE Cash = 1)
            token_list.append(
                {"exchangeType": 1, "tokens": [self._spot_token]}
            )

            self.sws.subscribe("option_chain_feed", mode=1, token_list=token_list)
            logger.info(f"[WS FEEDER] Subscribed to {len(self._nfo_tokens)} NFO tokens + Nifty spot")

        def on_data(wsapp, message):
            self._on_tick(message)

        def on_error(wsapp, error):
            logger.error(f"[WS FEEDER] Error: {error}")

        def on_close(wsapp):
            self._connected = False
            logger.info("[WS FEEDER] WebSocket disconnected")

        self.sws.on_open = on_open
        self.sws.on_data = on_data
        self.sws.on_error = on_error
        self.sws.on_close = on_close

        self.sws.connect()

    def _on_tick(self, message):
        """
        Process each incoming WebSocket tick.
        Recalculate IV and Greeks for the updated strike.
        """
        if not message or not isinstance(message, dict):
            return

        token = str(message.get("token", ""))
        ltp_raw = message.get("last_traded_price", 0)

        # Angel One WebSocket sends price * 100 for NFO
        # Check exchange_type to determine divisor
        exchange_type = message.get("exchange_type", 0)

        if exchange_type == 1:
            # NSE Cash (Nifty Spot) — price / 100
            spot = ltp_raw / 100.0 if ltp_raw > 100000 else float(ltp_raw)
            with self._lock:
                if spot > 0:
                    self._spot_price = spot
            return

        # NFO option tick
        ltp = ltp_raw / 100.0 if ltp_raw > 10000 else float(ltp_raw)

        if ltp <= 0:
            return

        with self._lock:
            tick = self._token_map.get(token)
            if not tick:
                return

            tick.ltp = ltp
            tick.last_update_time = time.time()
            self._tick_count += 1
            self._last_tick_time = time.time()

            # Recalculate Greeks using Black-Scholes
            spot = self._spot_price
            T = self._T

        if spot > 0 and T > 0:
            self._recalculate_greeks(tick, spot, T)

    def _recalculate_greeks(self, tick, spot, T):
        """
        Recalculate IV and all Greeks for a single strike on every price tick.
        Uses Newton-Raphson IV + Black-Scholes Greeks.
        """
        r = config.RISK_FREE_RATE

        try:
            # Step 1: Market LTP → Implied Volatility (Newton-Raphson)
            iv = greeks_engine.implied_volatility(
                tick.ltp, spot, tick.strike, T, r, tick.option_type
            )

            if iv <= 0.001:
                return

            # Step 2: IV → All Greeks (Black-Scholes)
            d = greeks_engine.delta(spot, tick.strike, T, r, iv, tick.option_type)
            g = greeks_engine.gamma(spot, tick.strike, T, r, iv)
            th = greeks_engine.theta(spot, tick.strike, T, r, iv, tick.option_type)
            v = greeks_engine.vega(spot, tick.strike, T, r, iv)

            with self._lock:
                tick.iv = iv
                tick.delta = d
                tick.gamma = g
                tick.theta = th
                tick.vega = v
                self._greeks_calc_count += 1

        except Exception as e:
            logger.debug(f"Greeks calc error for {tick.symbol}: {e}")
